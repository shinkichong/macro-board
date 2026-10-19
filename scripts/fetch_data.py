#!/usr/bin/env python3
"""
매크로 대시보드 데이터 수집기.

    python scripts/fetch_data.py            # 전체 수집 → data/macro.json
    python scripts/fetch_data.py --check    # 각 소스가 살아있는지만 점검 (파일 안 씀)
    python scripts/fetch_data.py --only vix,spx

설계 원칙
  · 한 소스가 죽어도 나머지는 갱신된다. 실패한 지표는 직전 JSON 값을 그대로 물려받고
    stale=true 로 표시된다. 화면에는 "N일 전 데이터" 배지가 뜬다.
  · 모든 시계열은 [["YYYY-MM-DD", 값], ...] 오름차순으로 통일한다.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import ssl
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).parent))
import sources as S  # noqa: E402

# Windows 콘솔(cp949)에서도 ✓/✗ 같은 기호를 그대로 출력할 수 있도록.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "macro.json"

START = "2005-01-01"          # 일별 계열 시작
START_MONTHLY = "1995-01-01"  # 월별 계열 시작
TIMEOUT = 45
# data.krx.co.kr 의 비공식 내부 엔드포인트(정보데이터시스템)는 응답을 거부할 때도
# 있지만, 클라우드 IP 에서는 아예 응답 없이 오래 물고 있는 경우가 많다.
# 과거치 백필이 연도별 × 후보 2개로 여러 번 호출되므로 짧게 끊는다.
KRX_MDC_TIMEOUT = 8

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 사내망 방화벽이 "브라우저처럼 보이는" User-Agent 로 나가는 요청을 일부 목적지에서
# 끊어버리는 것이 관측되어(FRED 등), 세션 기본값은 브라우저를 흉내내지 않는
# 값으로 둔다. CNN 처럼 진짜 브라우저 헤더가 필요한 곳만 개별 요청에서 UA 를 얹는다.
DEFAULT_UA = "Mozilla/5.0"


class _LegacyTLSAdapter(HTTPAdapter):
    """일부 사내망 TLS 검사 프록시는 레거시(비보안) 재협상을 사용한다.
    OpenSSL 3.x 는 기본적으로 이를 거부해 연결이 끊기므로 명시적으로 허용한다."""

    def _ctx(self):
        ctx = ssl.create_default_context()
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        return ctx

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ctx()
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        kw["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*a, **kw)


session = requests.Session()
session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"})
session.mount("https://", _LegacyTLSAdapter())


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════

def get(url: str, *, timeout: int = TIMEOUT, retries: int = 3, **kw) -> requests.Response:
    """짧은 지수 백오프를 붙인 GET.

    stream=True 로 받는다 — 사내망 TLS 검사 프록시가 큰 응답을 한 번에
    내려받는 요청에서 연결을 끊는 경우가 있어, 스트리밍으로 우회한다.
    """
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout, stream=True, **kw)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} — {url[:110]}")
        except requests.RequestException as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def monthly_avg(pairs: list[list]) -> dict[str, float]:
    """일별 계열을 월평균으로 접는다 (환율 환산용)."""
    buckets: dict[str, list[float]] = {}
    for d, v in pairs:
        buckets.setdefault(d[:7], []).append(v)
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def yoy(pairs: list[list]) -> list[list]:
    """전년동월대비 증감율(%). 월별 계열 전용."""
    lookup = {d[:7]: v for d, v in pairs}
    out = []
    for d, v in pairs:
        y, m = int(d[:4]), int(d[5:7])
        prev = lookup.get(f"{y - 1:04d}-{m:02d}")
        if prev:
            out.append([d, round((v / prev - 1) * 100, 2)])
    return out


# ══════════════════════════════════════════════════════════════
# 소스별 fetcher
# ══════════════════════════════════════════════════════════════

def fred(series_id: str, start: str = START,
         prev_data: list[list] | None = None) -> list[list]:
    """FRED. API 키 없이 fredgraph.csv 로 받는다.

    GitHub Actions 같은 클라우드 IP 대역에서는 FRED 가 응답 없이 45초씩
    물고 있다가 실패하는 경우를 봐서(연결 자체는 되니 재시도해도 잘 안 풀린다),
    기본(45초)보다는 짧게 주되 재시도 성공률을 위해 30초 정도는 기다린다.

    prev_data 를 주면 마지막 저장일 근처부터만 요청하는 증분 갱신으로 동작한다 —
    매번 전체 이력(수천 건)을 다시 받지 않아 요청이 가볍고 빠르다.

    주의: cosd 를 그 계열의 실제 최신 관측치 시점 이후로 주면 FRED 는 cosd 를
    통째로 무시하고 전체 이력(예: VIX 는 1990년부터)을 돌려준다 — 실측 확인함.
    그래서 "마지막 저장일 + 1일" 이 아니라 "마지막 저장일 - 7일" 을 요청
    시작점으로 써서, 이미 최신이라 해도 항상 cosd 가 실제 최신 관측치보다
    확실히 과거이도록 여유를 둔다.
    """
    have = {d: v for d, v in (prev_data or [])}
    fetch_start = start
    if have:
        anchor = (date.fromisoformat(max(have)) - timedelta(days=7)).isoformat()
        fetch_start = max(start, anchor)

    global _FRED_DOWN
    if not _FRED_DOWN:
        try:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={fetch_start}"
            rows = list(csv.reader(io.StringIO(get(url, timeout=30, retries=2).text)))
            for r in rows[1:]:
                if len(r) < 2 or r[1] in (".", "", "NA"):
                    continue          # FRED 는 휴일을 "." 로 표시한다
                have[r[0]] = float(r[1])
            if not have:
                raise RuntimeError(f"FRED {series_id}: 값이 비어 있음")
            return dedupe([[d, v] for d, v in have.items()])
        except requests.RequestException as e:
            # 타임아웃·연결 실패면 이번 실행의 나머지 FRED 호출도 같은 식으로 1분씩
            # 물고 있다가 실패하므로, 이후로는 FRED 를 건너뛰고 곧장 대체 소스로 간다.
            _FRED_DOWN = True
            print(f"    FRED 응답 없음({type(e).__name__}) → 이번 실행은 대체 소스 사용", flush=True)

    rows = _fred_fallback(series_id, fetch_start)
    have.update({d: v for d, v in rows})
    if not have:
        raise RuntimeError(f"FRED {series_id}: 대체 소스에도 값이 없음")
    return dedupe([[d, v] for d, v in have.items()])


_FRED_DOWN = False


def _fred_fallback(series_id: str, start: str) -> list[list]:
    """FRED 대체 소스 (sources.FRED_FALLBACKS). 없으면 FRED 공식 API(키 필요)."""
    cfg = S.FRED_FALLBACKS.get(series_id)
    if cfg:
        print(f"    {series_id} → {cfg['kind']} 대체 소스", flush=True)
        kind = cfg["kind"]
        if kind == "bls":
            return _bls_series(cfg["series"], start)
        if kind == "dbnomics":
            return [[d, v] for d, v in dbnomics(cfg["series"]) if d >= start]
        if kind == "fed_ddp":
            return _fed_ddp(cfg["rel"], cfg["package"], cfg["column"], start)
        if kind == "nyfed":
            return _nyfed_target(cfg["side"], start)
        if kind == "treasury":
            return _treasury_spread(cfg["long"], cfg["short"], start)
        raise RuntimeError(f"알 수 없는 FRED 대체 소스 종류: {kind}")

    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError(f"FRED {series_id}: 응답 없음, 무료 대체 소스 없음 "
                           "(FRED_API_KEY 를 설정하면 FRED 공식 API 로 재시도합니다)")
    print(f"    {series_id} → FRED 공식 API", flush=True)
    j = get("https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": key, "file_type": "json",
                    "observation_start": start}).json()
    return [[o["date"], float(o["value"])] for o in j.get("observations", [])
            if o.get("value") not in (".", "", None)]


def _bls_series(series_id: str, start: str) -> list[list]:
    """BLS 공개 API v1 (키 없이 요청당 최대 10년)."""
    out = []
    y0, y1 = int(start[:4]), date.today().year
    for a in range(y0, y1 + 1, 10):
        j = session.post("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                         json={"seriesid": [series_id], "startyear": str(a),
                               "endyear": str(min(a + 9, y1))}, timeout=TIMEOUT).json()
        if j.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS {series_id}: {j.get('message')}")
        for x in j["Results"]["series"][0]["data"]:
            if x["period"].startswith("M") and x["period"] != "M13" and x["value"] not in ("-", ""):
                out.append([f"{x['year']}-{x['period'][1:]}-01", float(x["value"])])
    return [[d, v] for d, v in sorted(out) if d >= start]


def _fed_ddp(rel: str, package: str, column: str, start: str) -> list[list]:
    """연준 Data Download Program CSV. 6번째 줄이 열 ID("M2.M" 등), 그 뒤가 관측치."""
    url = (f"https://www.federalreserve.gov/datadownload/Output.aspx?rel={rel}&series={package}"
           "&lastobs=&from=&to=&filetype=csv&label=include&layout=seriescolumn")
    rows = list(csv.reader(io.StringIO(get(url, timeout=60).text)))
    ids = rows[5] if len(rows) > 5 else []
    if column not in ids:
        raise RuntimeError(f"연준 DDP {rel}: 열 {column} 이 없음 (패키지 구성 변경 가능성)")
    i = ids.index(column)
    out = []
    for r in rows[6:]:
        try:
            out.append([normalize_period(r[0]), float(r[i])])
        except (ValueError, IndexError):
            continue          # "ND"(자료 없음) 등
    return [[d, v] for d, v in out if d >= start]


def _nyfed_target(side: str, start: str) -> list[list]:
    """뉴욕연준 EFFR API — 영업일마다 FOMC 목표범위 하단/상단이 함께 온다.
    범위 목표제는 2008-12-16 부터라 그 전 날짜는 값이 없다(FRED DFEDTARU/L 과 같음)."""
    url = ("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
           f"?startDate={max(start, '2008-12-16')}&endDate={date.today().isoformat()}")
    rows = get(url, timeout=60).json()["refRates"]
    return sorted([x["effectiveDate"], float(x[side])] for x in rows if x.get(side) is not None)


def _treasury_spread(long: str, short: str, start: str) -> list[list]:
    """미 재무부 일별 명목 수익률곡선 CSV(연도별)에서 long - short 를 계산한다."""
    out = []
    for y in range(int(start[:4]), date.today().year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={y}&page&_format=csv")
        for r in csv.DictReader(io.StringIO(get(url).text)):
            try:
                m, d, yy = r["Date"].split("/")
                out.append([f"{yy}-{m}-{d}", round(float(r[long]) - float(r[short]), 2)])
            except (KeyError, ValueError):
                continue
    return [[d, v] for d, v in sorted(out) if d >= start]


def yahoo(symbol: str, prev_data: list[list] | None = None) -> list[list]:
    """Yahoo v8 chart. prev_data 가 있으면 최근 3개월치만 받아 병합하는
    증분 갱신으로 동작한다 — 매번 25년치를 다시 받지 않는다. 3개월을 쓰는
    이유는 파이프라인이 몇 주 안 돌아도 공백 없이 이어붙일 여유를 두기 위함."""
    have = {d: v for d, v in (prev_data or [])}
    range_ = "3mo" if have else "25y"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval=1d")
    j = get(url).json()["chart"]["result"][0]
    ts = j["timestamp"]
    closes = list(j["indicators"]["quote"][0]["close"])
    # Yahoo 는 장 마감 뒤 몇 시간 동안 마지막 일봉 close 를 None 으로 둘 때가 있다
    # (HYG·IEF 에서 자주 관측 — Fear&Greed 오실레이터가 하루 늦게 남는 원인).
    # 정규장이 끝난 게 확실하면(regularMarketTime ≥ 정규장 종료) 그 캔들의 close 를
    # meta.regularMarketPrice(=종가)로 채운다. 장중이면 건드리지 않는다.
    meta = j.get("meta", {})
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    if (ts and closes[-1] is None and meta.get("regularMarketPrice") is not None
            and regular.get("start") and regular.get("end")
            and regular["start"] <= ts[-1] < regular["end"] <= meta.get("regularMarketTime", 0)):
        closes[-1] = meta["regularMarketPrice"]
    # 장중에 돌면 오늘 캔들의 close 는 종가가 아니라 현재가다. 이걸 저장하면
    # 병합 방식에 따라 장중 값이 종가처럼 굳어버리므로(2026-09-30 코스피가
    # 09:36 장중값 6,951 로 굳은 사례), 정규장 종료 + 30분 전에는 오늘 캔들을 버린다.
    intraday_from = None
    if regular.get("start") and regular.get("end") and time.time() < regular["end"] + 1800:
        intraday_from = regular["start"]
    for t, c in zip(ts, closes):
        if c is None:
            continue
        if intraday_from is not None and t >= intraday_from:
            continue
        have[datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")] = round(c, 4)
    if not have:
        raise RuntimeError(f"Yahoo {symbol}: 값이 비어 있음")
    return dedupe([[d, v] for d, v in have.items()])


def stooq(symbol: str) -> list[list]:
    url = f"https://stooq.com/q/d/l/?{urlencode({'s': symbol, 'i': 'd'})}"
    rows = list(csv.reader(io.StringIO(get(url).text)))
    if not rows or rows[0][0].lower() != "date":
        raise RuntimeError(f"Stooq {symbol}: 응답 형식이 예상과 다름")
    return [[r[0], float(r[4])] for r in rows[1:] if len(r) >= 5 and r[4]]


def index_series(cfg: dict, prev_data: list[list] | None = None) -> list[list]:
    """Yahoo 우선, 실패하면 Stooq."""
    try:
        return yahoo(cfg["yahoo"], prev_data=prev_data)
    except Exception as e:
        print(f"    Yahoo 실패({e}) → Stooq 로 재시도", flush=True)
        return stooq(cfg["stooq"])


def dbnomics(series_id: str) -> list[list]:
    url = ("https://api.db.nomics.world/v22/series"
           f"?observations=1&series_ids={series_id}")
    docs = get(url).json().get("series", {}).get("docs", [])
    if not docs:
        raise RuntimeError(f"DBnomics {series_id}: 시리즈 없음")
    d = docs[0]
    out = []
    for p, v in zip(d["period"], d["value"]):
        if v is None:
            continue
        out.append([normalize_period(p), float(v)])
    if not out:
        raise RuntimeError(f"DBnomics {series_id}: 관측치 없음")
    return out


def dbnomics_search(query: str, limit: int = 8) -> list[str]:
    """--check 모드에서 대체 시리즈 후보를 제안하기 위한 검색."""
    try:
        url = f"https://api.db.nomics.world/v22/search?q={requests.utils.quote(query)}&limit={limit}"
        docs = get(url).json().get("results", {}).get("docs", [])
        return [f"{d.get('provider_code')}/{d.get('dataset_code')}" for d in docs]
    except Exception:
        return []


def normalize_period(p: str) -> str:
    """'2024-03' / '2024Q1' / '2024-03-15' 를 YYYY-MM-DD 로 통일."""
    p = str(p)
    if len(p) == 7 and "Q" not in p:
        return p + "-01"
    if "Q" in p:
        y, q = p.split("Q")
        return f"{int(y):04d}-{(int(q) - 1) * 3 + 1:02d}-01"
    if len(p) == 4:
        return p + "-01-01"
    return p


def oecd_cli() -> list[list]:
    start = START_MONTHLY[:7]
    errors = []
    for tpl in S.OECD_CLI_CANDIDATES:
        url = tpl.format(start=start)
        try:
            text = get(url).text
            rows = list(csv.DictReader(io.StringIO(text)))
            out = []
            for r in rows:
                period = r.get("TIME_PERIOD") or r.get("TIME_PERIOD:Time period")
                val = r.get("OBS_VALUE") or r.get("OBS_VALUE:Observation value")
                if period and val not in (None, "", "NaN"):
                    out.append([normalize_period(period), float(val)])
            if out:
                return sorted(out)
        except Exception as e:
            errors.append(f"{e}")
    raise RuntimeError("OECD CLI 후보 URL 전부 실패. "
                       "data-explorer.oecd.org 에서 'Copy API link' 로 받은 URL 을 "
                       f"sources.OECD_CLI_CANDIDATES 맨 앞에 넣어주세요. ({errors[:1]})")


def oecd_monagg(area: str, measure: str, unit: str, adjustment: str = "N") -> list[list]:
    """OECD SDMX 통화량 지표(Monetary aggregates). DBnomics 의 국가별 M2/M3
    미러(BOJ, IMF/IFS 등)가 끊겼을 때 국가통화 단위 원계열 대체용으로 쓴다."""
    key = ".".join([area, "M", measure, unit, "", adjustment, "", "", ""])
    url = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_MONAGG,4.0/"
           f"{key}?startPeriod={START_MONTHLY[:7]}&format=csvfilewithlabels")
    rows = list(csv.DictReader(io.StringIO(get(url).text)))
    out = []
    for r in rows:
        period = r.get("TIME_PERIOD")
        val = r.get("OBS_VALUE")
        if period and val not in (None, "", "NaN"):
            out.append([normalize_period(period), float(val)])
    if not out:
        raise RuntimeError(f"OECD MONAGG {area}/{measure}/{unit}: 값이 비어 있음")
    return sorted(out)


def _drop_implausible_tail(data: list[list], floor: float = 25.0):
    """ISM 제조업 PMI 는 확산지수라 역사적으로 25 아래로 떨어진 적이 없다
    (2008년 금융위기 32대, 2020년 코로나 41대가 최저). DBnomics 미러가
    2025-09 부터 이 범위를 벗어난 값을 내놓기 시작했는데, 이는 수집 파이프라인
    쪽 오류로 보고 그 지점부터는 잘라내 마지막 정상값에서 멈춰 둔다."""
    for i, (d, v) in enumerate(data):
        if v < floor:
            return data[:i], d
    return data, None


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _ism_prnewswire() -> dict[str, float]:
    """PR Newswire 의 ISM 보도자료 목록 제목에서 월별 제조업 PMI 를 뽑는다.
    제목 형식이 2025-08 무렵 "... Manufacturing ISM® Report On Business®" 에서
    "... ISM® Manufacturing PMI® Report" 로 바뀌어 둘 다 받는다."""
    text = html.unescape(get(S.ISM_PRN_URL).text)
    pat = re.compile(r"Manufacturing PMI\S* at (\d{2}(?:\.\d)?)%;\s*([A-Z][a-z]+) (\d{4}) "
                     r"(?:ISM\S* Manufacturing PMI|Manufacturing ISM)")
    out = {}
    for val, month, year in pat.findall(text):
        if month in _MONTHS:
            out[f"{year}-{_MONTHS[month]:02d}-01"] = float(val)
    if not out:
        raise RuntimeError("PR Newswire ISM: 제목에서 PMI 를 찾지 못함 (페이지 형식 변경 가능성)")
    return out


def ism_pmi(prev_data: list[list] | None = None) -> tuple[list[list], dict]:
    """DBnomics 미러(과거치) + PR Newswire 보도자료(최근치)를 합친다.
    보도자료 제목은 최초 발표치라 ISM 의 연례 계절조정 개정이 반영되지 않는다
    (2024년분이 DBnomics 개정치와 0.1~0.4 차이). 그래서 같은 달이 겹치면
    DBnomics 정상값을 우선하고, 보도자료는 DBnomics 가 비었거나 깨진 달만 채운다."""
    have = {d: v for d, v in (prev_data or [])}
    cut_at, clean = None, []
    for sid in S.ISM_CANDIDATES:
        try:
            clean, cut_at = _drop_implausible_tail(dbnomics(sid))
        except Exception:
            continue
        if clean:
            break
    try:
        for d, v in _ism_prnewswire().items():
            have.setdefault(d, v)   # 이미 가진 달(DBnomics 개정치 등)은 덮지 않는다
    except Exception as e:
        have.update(dict(clean))
        print(f"    PR Newswire 실패({e}) — DBnomics/직전 값만 사용", flush=True)
        if not have:
            raise RuntimeError("ISM PMI 수집 실패. DBnomics 후보: "
                               + ", ".join(dbnomics_search("ISM manufacturing PMI")))
        # 보도자료를 못 받았는데 DBnomics 가 깨진 구간에 걸려 있으면 멈춰 있다고 알린다
        if cut_at and max(have) < cut_at:
            return dedupe([[d, v] for d, v in have.items()]), {
                "stale": True,
                "note": f"DBnomics ISM 미러가 {cut_at} 이후 비정상 값을 내놓기 "
                        "시작해 그 이전 마지막 정상값에서 멈춰 있습니다."}
    have.update(dict(clean))
    return dedupe([[d, v] for d, v in have.items()]), {}


def fear_greed() -> list[list]:
    """CNN 내부 엔드포인트. 브라우저처럼 보이는 헤더가 없으면 거부당한다."""
    url = S.CNN_FG_URL.format(start="2020-09-19")
    r = get(url, headers={"User-Agent": UA,
                          "Referer": "https://edition.cnn.com/markets/fear-and-greed",
                          "Origin": "https://edition.cnn.com"})
    hist = r.json()["fear_and_greed_historical"]["data"]
    return [[datetime.utcfromtimestamp(p["x"] / 1000).strftime("%Y-%m-%d"),
             round(float(p["y"]), 2)] for p in hist]


# ══════════════════════════════════════════════════════════════
# 커스텀 Fear & Greed 오실레이터 (S&P500 / NASDAQ)
# src/미국 피어앤그리드 오실레이터_yahoo.txt 의 계산을 순수 파이썬으로 이식.
# pandas/scikit-learn 없이 rolling·EWM·RSI·min-max 를 직접 구현한다.
# ══════════════════════════════════════════════════════════════

def _rolling_mean(vals: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= window:
            s -= vals[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


def _rsi10(vals: list[float], window: int = 10) -> list[float | None]:
    n = len(vals)
    gain = [0.0] * n
    loss = [0.0] * n
    for i in range(1, n):
        d = vals[i] - vals[i - 1]
        gain[i] = d if d > 0 else 0.0
        loss[i] = -d if d < 0 else 0.0
    g, l = _rolling_mean(gain, window), _rolling_mean(loss, window)
    out: list[float | None] = [None] * n
    for i in range(n):
        if g[i] is None:
            continue
        if l[i] == 0:
            out[i] = 100.0 if g[i] > 0 else 50.0
        else:
            out[i] = 100 - 100 / (1 + g[i] / l[i])
    return out


def _ewm(vals: list[float | None], span: int) -> list[float | None]:
    """pandas .ewm(span=span, adjust=False).mean() 과 동일한 재귀식."""
    alpha = 2 / (span + 1)
    out: list[float | None] = [None] * len(vals)
    prev = None
    for i, v in enumerate(vals):
        if v is None:
            continue
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def _minmax(vals: list[float | None]) -> list[float | None]:
    xs = [v for v in vals if v is not None]
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1.0
    return [None if v is None else (v - lo) / span for v in vals]


_FG_CACHE: dict = {}


def _compute_fear_greed_pair() -> dict:
    """spx/ndx 각각의 (오실레이터, 동반 가격) 시리즈를 계산해 캐시한다.
    두 카드가 같은 보조 입력(VIX·금리·HYG/IEF)을 공유하므로 한 번만 계산한다."""
    if _FG_CACHE:
        return _FG_CACHE

    raw = {
        "spx": index_series(S.INDICES["spx"]),
        "ndx": index_series(S.INDICES["ndx"]),
        "vix": index_series(S.FG_VIX),
        "dgs10": index_series(S.FG_DGS10),
        "dgs5": index_series(S.FG_DGS5),
        "hyg": index_series(S.FG_HYG),
        "ief": index_series(S.FG_IEF),
    }
    maps = {k: {d: v for d, v in vs if d >= S.FG_CALC_START} for k, vs in raw.items()}
    common = set(maps["spx"])
    for m in maps.values():
        common &= set(m)
    dates = sorted(common)
    if len(dates) < 130:
        raise RuntimeError(f"Fear&Greed 오실레이터: 공통 거래일이 {len(dates)}개뿐 — 계산 불가")
    a = {k: [m[d] for d in dates] for k, m in maps.items()}

    risk_appetite = [h / i for h, i in zip(a["hyg"], a["ief"])]
    bond_spread = [t10 - t5 for t10, t5 in zip(a["dgs10"], a["dgs5"])]
    vix_n = _minmax(a["vix"])
    risk_n = _minmax(risk_appetite)
    bond_n = _minmax(bond_spread)

    def one(price: list[float]):
        momentum = [None if m is None else (p - m) / m * 100
                    for p, m in zip(price, _rolling_mean(price, 125))]
        rsi_n = _minmax(_rsi10(price))
        mom_n = _minmax(momentum)
        fgi: list[float | None] = []
        for mo, ri, vi, bo, rs in zip(mom_n, risk_n, vix_n, bond_n, rsi_n):
            if None in (mo, ri, vi, bo, rs):
                fgi.append(None)
            else:
                fgi.append(mo * 0.2 + ri * 0.2 + (1 - vi) * 0.2 + bo * 0.2 + rs * 0.2)
        macd = [None if (m is None or l is None) else m - l
                for m, l in zip(_ewm(fgi, 12), _ewm(fgi, 26))]
        hist = [None if (m is None or s is None) else m - s
                for m, s in zip(macd, _ewm(macd, 9))]
        osc = [[d, round(v, 4)] for d, v in zip(dates, hist) if v is not None]
        keep = {d for d, _ in osc}
        pr = [[d, round(p, 2)] for d, p in zip(dates, price) if d in keep]
        return osc, pr

    _FG_CACHE["spx"] = one(a["spx"])
    _FG_CACHE["ndx"] = one(a["ndx"])
    return _FG_CACHE


def fear_greed_osc_spx() -> tuple[list[list], dict]:
    osc, price = _compute_fear_greed_pair()["spx"]
    return osc, {"price_data": price}


def fear_greed_osc_ndx() -> tuple[list[list], dict]:
    osc, price = _compute_fear_greed_pair()["ndx"]
    return osc, {"price_data": price}


def rate_hy_combo(prev_rate: list[list] | None = None,
                   prev_spread: list[list] | None = None) -> tuple[list[list], dict]:
    """미국 10년물 국채금리와 하이일드 스프레드를 한 차트에 겹쳐 보기 위해
    정렬한다.

    10년물은 단독 카드와 같은 Yahoo `^TNX` 를 쓴다(FRED DGS10 은 갱신 지연
    이슈로 단독 카드도 이미 Yahoo 로 옮김 — 두 카드가 같은 소스를 쓰도록 통일).

    하이일드 스프레드(BAMLH0A0HYM2)는 Yahoo 에 대응 티커가 없어 그대로 FRED 를
    쓴다. 이건 ICE Data Indices 라이선스 계열이라 FRED 무료 CSV 는 cosd 를
    아무리 과거로 줘도 최근 ~3년치만 돌려준다(승인 없이는 그 이상 불가). 그래서
    새로 받은 값과 이전에 저장해둔 값을 합쳐서 쓴다 — 한 번 확보한 날짜는 FRED
    창에서 밀려나도 우리 쪽 기록에 남아, 시간이 지날수록 보이는 구간이
    넓어진다(다시 좁아지지는 않는다).

    FRED 는 하이일드 스프레드를 10년물(Yahoo)보다 하루 이상 늦게 발표하는
    경우가 흔하다. 예전에는 두 시리즈의 "공통 날짜"만 남겼는데, 그러면
    단독 10년물 카드보다 이 콤보 카드가 하루 전 값을 보여주는 것처럼 보인다.
    그래서 날짜 기준은 10년물(Yahoo) 쪽으로 두고, 스프레드는 그 날짜까지
    나온 가장 최근 값을 그대로 이어 쓴다(forward-fill) — 10년물은 항상
    최신치가 보이고, 스프레드만 며칠 늦게 갱신되는 걸 감수하는 방식.
    """
    ust10y = {d: v for d, v in index_series(S.FG_DGS10, prev_data=prev_rate)}
    hy = {d: v for d, v in fred(S.FRED["hy_yield"]["id"], prev_data=prev_spread)}
    if len(ust10y) < 30 or not hy:
        raise RuntimeError("국채금리·하이일드 스프레드: 데이터가 부족해 계산 불가")

    dates = sorted(ust10y)
    hy_dates = sorted(hy)
    rate, spread = [], []
    i, last_hy = 0, None
    for d in dates:
        while i < len(hy_dates) and hy_dates[i] <= d:
            last_hy = hy[hy_dates[i]]
            i += 1
        if last_hy is None:
            continue  # 하이일드 이력 시작 전 날짜는 건너뜀
        rate.append([d, ust10y[d]])
        spread.append([d, last_hy])

    if len(rate) < 30:
        raise RuntimeError(f"국채금리·하이일드 스프레드: 계산 가능한 날짜가 {len(rate)}개뿐 — 계산 불가")
    return rate, {"price_data": spread}


def real_policy_rate() -> list[list]:
    """실질 정책금리 = 정책금리(Fed Funds 목표범위 중간값) - 헤드라인 PCE(YoY).

    정책금리는 FOMC 가 바꿀 때만 계단식으로 움직이는 일별 계열이고, PCE 는
    1~2개월 늦게 발표되는 월별 계열이라 둘의 발표 시점이 다르다. 아직 발표되지
    않은 최근 달은 마지막으로 발표된 PCE 값을 그대로 이어 쓴다 — "지금 정책금리
    대비 가장 최근에 나온 물가"를 보는 실무 관행과 같다.
    """
    upper = {d: v for d, v in fred("DFEDTARU", START_MONTHLY)}
    lower = {d: v for d, v in fred("DFEDTARL", START_MONTHLY)}
    monthly_rate = {}
    for d in sorted(set(upper) & set(lower)):
        monthly_rate[d[:7]] = (upper[d] + lower[d]) / 2  # 그 달 마지막 값이 남는다(오름차순)

    pce_by_month = {d[:7]: v for d, v in yoy(fred("PCEPI", START_MONTHLY))}
    pce_months = sorted(pce_by_month)

    out = []
    idx, pce_val = 0, None
    for m in sorted(monthly_rate):
        while idx < len(pce_months) and pce_months[idx] <= m:
            pce_val = pce_by_month[pce_months[idx]]
            idx += 1
        if pce_val is None:
            continue
        out.append([f"{m}-01", round(monthly_rate[m] - pce_val, 3)])
    if len(out) < 12:
        raise RuntimeError(f"실질 정책금리: 계산 가능한 달이 {len(out)}개뿐 — 계산 불가")
    return out


def _ecos_series(stat_code: str, item_code: str, start: str) -> list[list]:
    """한국은행 ECOS StatisticSearch — 월별(M) 원계열/지수를 그대로 반환한다.
    무료 인증키가 필요하다 (https://ecos.bok.or.kr, 즉시 자동 발급).

    GitHub Actions 같은 클라우드 IP 대역에서는 연결 자체가 45초씩 물고 있다가
    실패하는 것이 관측돼(KRX 정보데이터시스템과 같은 부류의 문제로 보인다),
    FRED 와 마찬가지로 타임아웃/재시도를 짧게 줘서 빨리 다음(FRED 폴백)으로
    넘어가게 한다.
    """
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        raise RuntimeError("ECOS_API_KEY 가 필요합니다.")
    end = date.today().strftime("%Y%m")
    start_ym = start.replace("-", "")[:6]
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1000/"
           f"{stat_code}/M/{start_ym}/{end}/{item_code}")
    body = get(url, timeout=10, retries=2).json()
    if "RESULT" in body:
        raise RuntimeError(f"ECOS {stat_code}: {body['RESULT'].get('MESSAGE')}")
    rows = body.get("StatisticSearch", {}).get("row") or []
    out = []
    for row in rows:
        t, v = row.get("TIME"), row.get("DATA_VALUE")
        if not t or v in (None, ""):
            continue
        out.append([f"{t[:4]}-{t[4:6]}-01", float(v)])
    if not out:
        raise RuntimeError(f"ECOS {stat_code}: 값이 비어 있음")
    return out


def kr_exports_yoy(prev_data: list[list] | None = None) -> list[list]:
    """한국 수출증가율(YoY) — 관세청 통관 수출액(달러) 기준.

    확정치(ECOS 901Y118)로 전년동월비를 계산하고, 확정치가 아직 없는 최근 달은
    산업통상부가 매월 1일 발표하는 수출입동향 속보치로 채운다. 확정치가 나오면
    그 달은 확정치로 바뀐다(속보치와 보통 1%p 이내 차이).

    예전에는 한국은행 수출금액지수(403Y001)를 썼는데, 한 달 반 늦게 나오고
    통관 기준보다 1~7%p 높게 나와 뉴스의 수출 증가율과 맞지 않았다.
    ECOS 가 실패하면 직전 값을 유지하고 속보치만 덧붙인다.
    """
    have = {d: v for d, v in (prev_data or [])}
    last_final = None
    try:
        final = yoy(_ecos_series(*S.KR_EXPORTS_ECOS, START_MONTHLY))
        have = {d: v for d, v in final}      # 확정치로 전면 교체(이전 기준 값 제거)
        last_final = max(have)
    except Exception as e:
        print(f"    ECOS 관세청 수출액 실패({e}) — 직전 값 유지", flush=True)

    try:
        flash = _motie_exports(after=last_final or (max(have) if have else "2000-01-01"))
        for d, v in flash.items():
            print(f"    산업부 속보치 {d[:7]} = {v:+.1f}%", flush=True)
            have[d] = v
    except Exception as e:
        print(f"    산업부 수출입동향 실패({e}) — 확정치만 사용", flush=True)

    if not have:
        raise RuntimeError("한국 수출증가율: 수집된 값이 없습니다.")
    return [[d, have[d]] for d in sorted(have)]


_MOTIE_TITLE = re.compile(r"(\d{4})년\s*(?:연간\s*및\s*)?(\d{1,2})월(?:\s*및\s*(?:상반기|연간))?\s*수출입\s*동향$")


def _motie_exports(after: str, max_months: int = 3) -> dict[str, float]:
    """산업통상부 보도자료 게시판에서 after 이후 달의 "O월 수출입 동향"을 찾아
    전년동월비 수출 증가율을 읽는다. 게시판에 다른 글이 많아 제목 검색("수출입")을
    쓰고, ICT 수출입 동향 등은 제목 형식으로 걸러낸다. 서버가 연속 요청을 끊는
    것이 관측돼 요청 사이에 간격을 둔다."""
    board = S.MOTIE_BOARD_URL
    page = get(f"{board}?searchCondition=1&searchKeyword=%EC%88%98%EC%B6%9C%EC%9E%85&pageIndex=1",
               headers={"User-Agent": UA}, timeout=20).text
    posts = {}
    for art_id, title in re.findall(
            r'href="/kor/article/ATCL3f49a5a8c/(\d+)/view[^"]*"[^>]*>\s*<i>\s*([^<]*?)\s*</i>', page):
        m = _MOTIE_TITLE.search(html.unescape(title).strip())
        if m:
            d = f"{int(m[1]):04d}-{int(m[2]):02d}-01"
            if d > after:
                posts.setdefault(d, art_id)

    out = {}
    for d in sorted(posts)[-max_months:]:
        time.sleep(1.5)
        v = _motie_export_growth(posts[d], int(d[:4]), int(d[5:7]))
        if v is None:
            print(f"    산업부 {d[:7]} 수출입동향에서 수출 증가율을 찾지 못함 (문구 변경 가능성)", flush=True)
        else:
            out[d] = v
    return out


def _motie_export_growth(art_id: str, year: int, month: int) -> float | None:
    """보도자료 본문에서 먼저 찾고, 본문이 비어 있으면 첨부 PDF 첫 2쪽에서 찾는다."""
    view = get(f"{S.MOTIE_BOARD_URL}/{art_id}/view", headers={"User-Agent": UA}, timeout=20).text
    v = _parse_export_growth(re.sub(r"<[^>]+>", " ", html.unescape(view)), year, month)
    if v is not None:
        return v
    pdf = re.search(r'href="(/attach/down/[^"]+)"[^>]*title="[^"]*\.pdf', view)
    if not pdf:
        return None
    from pypdf import PdfReader     # PDF 를 열 때만 필요 — 없으면 이 달은 확정치를 기다린다
    time.sleep(1.5)
    raw = get("https://www.motir.go.kr" + pdf[1], headers={"User-Agent": UA}, timeout=60).content
    text = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages[:2])
    return _parse_export_growth(text, year, month)


def _parse_export_growth(text: str, year: int, month: int) -> float | None:
    """"‘26.9월 수출은 전년 동월 대비 83.5% 증가" 또는 "9월 수출 1,209.4억 달러(+83.5%)".
    감소는 "감소" 또는 "△" 로 표기된다. "1~9월 누적 수출"은 잡지 않는다."""
    t = re.sub(r"\s+", " ", text)
    m = re.search(rf"{year % 100:02d}\s*\.\s*{month}\s*월\s*수출은\s*전년\s*동월\s*대비\s*"
                  r"([\d.]+)\s*%\s*(증가|감소)", t)
    if m:
        return float(m[1]) * (1 if m[2] == "증가" else -1)
    m = re.search(rf"(?<![\d~～]){month}\s*월\s*수출\s*[\d,]+(?:\.\d+)?\s*억\s*달러\s*"
                  r"\(\s*([+△▲-]?)\s*([\d.]+)\s*%\s*\)", t)
    if m:
        return float(m[2]) * (-1 if m[1] in ("△", "-") else 1)
    return None


def vkospi(prev_data: list[list] | None = None, trading_days: set[str] | None = None) -> list[list]:
    """
    과거치는 KRX 정보데이터시스템에서 기간 조회로 한 번에 적재하고,
    이후 갱신은 공식 오픈API 로 빠진 날짜만 하루씩 채웁니다.

    오픈API 는 하루치씩만 주기 때문에, 이미 쌓인 데이터가 있으면
    마지막 날짜 이후만 호출합니다. 평상시 하루 1~2회 호출로 끝납니다.
    """
    have = {d: v for d, v in (prev_data or [])}

    if not have:
        print("    과거치 없음 → 정보데이터시스템에서 전 구간 적재", flush=True)
        for y in range(2009, date.today().year + 1):   # VKOSPI 산출 시작 2009-04
            for d, v in _vkospi_mdc(f"{y}0101", f"{y}1231"):
                have[d] = v
            time.sleep(0.4)                             # KRX 에 부담 주지 않기

    key = os.environ.get("KRX_API_KEY")
    missing = _business_days_since(max(have) if have else None, trading_days)

    if key and missing:
        got, errs = 0, []
        for day in missing[-40:]:                       # 한 번에 최대 40일치만
            v, err = _vkospi_openapi(day, key)
            if v is not None:
                have[day] = v
                got += 1
            elif err:
                errs.append((day, err))
            time.sleep(0.25)
        print(f"    오픈API 로 {got}일치 추가", flush=True)
        _print_krx_failures(errs)
    elif missing and not key:
        # 인증키가 없으면 최근 구간만 정보데이터시스템으로 메운다.
        for d, v in _vkospi_mdc(missing[0].replace("-", ""),
                                date.today().strftime("%Y%m%d")):
            have[d] = v

    if not have:
        raise RuntimeError(
            "VKOSPI 수집 실패. KRX_API_KEY 를 설정하거나, "
            "data.krx.co.kr 에서 getJsonData.cmd 의 bld 를 확인해 "
            "sources.VKOSPI_PAYLOAD_CANDIDATES 에 반영해주세요.")
    _check_kr_staleness(have, trading_days, "VKOSPI")
    return dedupe([[d, v] for d, v in have.items()])


def _business_days_since(last: str | None, trading_days: set[str] | None = None) -> list[str]:
    """마지막 수집일 다음 영업일부터 오늘까지.

    trading_days 를 주면(코스피 실제 거래일 목록) 주말뿐 아니라 추석·설 같은
    공휴일도 걸러낸다 — 주지 않으면 주말만 거르는 예전 방식(월~금 전부
    거래일로 가정)으로 동작한다.
    """
    start = date.fromisoformat(last) + timedelta(days=1) if last else date.today() - timedelta(days=7)
    out, d = [], start
    while d <= date.today():
        iso = d.isoformat()
        if trading_days is not None:
            if iso in trading_days:
                out.append(iso)
        elif d.weekday() < 5:
            out.append(iso)
        d += timedelta(days=1)
    return out


def _kr_trading_days(series: dict, prev: dict) -> set[str]:
    """이번 실행에서 갱신된(실패했으면 직전) KOSPI 실제 거래일 목록.

    KOSPI 는 Yahoo/Stooq 실거래 데이터라 주말은 물론 추석·설 같은 공휴일에도
    캔들이 없다 — 이 날짜 목록을 VKOSPI/국채선물/옵션 등 KRX 전용 오픈API
    수집의 "실제 개장일" 기준으로 재사용해서, 휴장일을 영업일로 착각해
    '새 데이터 0건'을 오탐하지 않게 한다.
    """
    data = series.get("kospi", {}).get("data") or prev.get("kospi", {}).get("data") or []
    return {d for d, _ in data}


def _check_kr_staleness(have: dict, trading_days: set[str], label: str, buffer_days: int = 1) -> None:
    """실제 개장일(trading_days)인데도 buffer_days 를 넘겨서까지 못 가져온
    날짜가 여러 날 쌓이면 예외를 던진다.

    KRX 오픈API 가 조용히 빈 값만 반환해도(키 만료, 승인 취소 등) 이전
    함수들은 "이미 저장된 값이 있으니 성공"으로 넘어가 stale 배지 없이
    며칠씩 멈춰 있는 문제가 있었다 — 실제 개장일 기준 공백이 하루를 넘게
    쌓이면 여기서 걸러 stale 배지가 뜨게 한다.
    """
    if not trading_days:
        return
    cutoff = (date.today() - timedelta(days=buffer_days)).isoformat()
    last = max(have) if have else ""
    stuck = sorted(d for d in trading_days if last < d <= cutoff)
    if len(stuck) > 1:
        raise RuntimeError(f"{label}: 실제 개장일 {stuck[0]} 이후 {len(stuck)}일치를 "
                           "KRX 오픈API에서 못 가져왔습니다 (KRX_API_KEY 만료/승인 문제 가능성)")


def _print_krx_failures(errs: list[tuple[str, str]]) -> None:
    """KRX 오픈API 호출 실패 사유를 CI 로그에 남긴다 (원인 불명 상태로 조용히
    삼키지 않기 위함). 같은 사유가 여러 날 반복되면 한 줄로 묶는다."""
    if not errs:
        return
    by_reason: dict[str, list[str]] = {}
    for day, err in errs:
        by_reason.setdefault(err, []).append(day)
    for reason, days in by_reason.items():
        print(f"    ✗ KRX 오픈API 실패({reason}): {days[0]}"
              + (f" 외 {len(days) - 1}일" if len(days) > 1 else ""), flush=True)


def _krx_openapi_get(url: str, day: str, key: str) -> tuple[list, str | None]:
    """KRX 공식 오픈API 공통 호출. (행 목록, 실패 사유) 를 반환한다.

    실패 사유를 상위(각 지표 수집 함수)로 올려보내, 실제 개장일인데도
    계속 못 가져오는 경우 원인(HTTP 상태 코드/응답 파싱 실패 등)이 CI 로그에
    남도록 한다 — 예전엔 여기서 그냥 None 을 반환해 삼켜버려서, 며칠씩
    조용히 실패해도 원인을 알 방법이 없었다.
    """
    try:
        r = session.get(url, params={"basDd": day.replace("-", "")},
                        headers={"AUTH_KEY": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        return r.json().get("OutBlock_1") or [], None
    except Exception as e:
        return [], str(e)


def _vkospi_openapi(day: str, key: str) -> tuple[float | None, str | None]:
    """KRX 공식 오픈API — 파생상품지수 일별시세에서 VKOSPI 행을 찾는다."""
    rows, err = _krx_openapi_get(S.KRX_OPENAPI_BASE + S.KRX_OPENAPI_PATH, day, key)
    if err:
        return None, err
    for row in rows:
        name = row.get("IDX_NM") or row.get("IDX_NAME") or ""
        if any(frag in name for frag in S.VKOSPI_NAME_MATCH):
            raw = str(row.get("CLSPRC_IDX") or row.get("CLSPRC") or "").replace(",", "")
            if raw and raw not in ("-", ""):
                return float(raw), None
    return None, ("행은 왔지만 VKOSPI 이름 매칭 실패" if rows else "빈 응답(OutBlock_1 없음)")


def _krx_idx_value(day: str, key: str, path: str, idx_name: str) -> tuple[float | None, str | None]:
    """KRX 공식 오픈API 지수 시세정보(파생상품지수/유가증권지수 공용)에서
    IDX_NM 이 정확히 일치하는 행의 종가.
    (VKOSPI 는 이름이 조금씩 바뀌어 부분일치를 쓰지만, 그 외 지수류는
    이름이 안정적이라 오탐 방지를 위해 정확히 일치하는 것만 고른다.)"""
    rows, err = _krx_openapi_get(S.KRX_OPENAPI_BASE + path, day, key)
    if err:
        return None, err
    for row in rows:
        if row.get("IDX_NM") == idx_name:
            raw = str(row.get("CLSPRC_IDX") or "").replace(",", "")
            if raw and raw not in ("-", ""):
                return float(raw), None
    return None, (f"행은 왔지만 '{idx_name}' 이름 매칭 실패" if rows else "빈 응답(OutBlock_1 없음)")


def drvprod_index_series(prev_data: list[list] | None, idx_name: str, label: str,
                          trading_days: set[str] | None = None) -> list[list]:
    """파생상품지수 일별시세에서 임의의 지수를 하루씩 증분 수집한다 (VKOSPI 와 동일한 패턴).
    과거치를 한 번에 적재할 무료 경로가 없어 처음 수집한 날부터 하루씩 쌓인다."""
    have = {d: v for d, v in (prev_data or [])}
    key = os.environ.get("KRX_API_KEY")
    if not key:
        if have:
            return dedupe([[d, v] for d, v in have.items()])
        raise RuntimeError(f"{label} 수집 실패: KRX_API_KEY 가 필요합니다.")

    missing = _business_days_since(max(have) if have else None, trading_days)
    got, errs = 0, []
    for day in missing[-40:]:
        v, err = _krx_idx_value(day, key, S.KRX_OPENAPI_PATH, idx_name)
        if v is not None:
            have[day] = v
            got += 1
        elif err:
            errs.append((day, err))
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)
    _print_krx_failures(errs)

    if not have:
        raise RuntimeError(f"{label} 수집 실패: 유효한 거래일 데이터가 없습니다.")
    _check_kr_staleness(have, trading_days, label)
    return dedupe([[d, v] for d, v in have.items()])


def kospi_index_series(prev_data: list[list] | None) -> list[list]:
    """코스피 지수 종가. KRX 공식 오픈API(유가증권지수 시세정보)를 우선 쓰고,
    그 API 승인 전이거나 그날 값이 아직 없으면 Yahoo/Stooq 로 빈 날짜만 채운다.

    Fear&Greed 오실레이터(KOSPI)의 다른 네 재료(VKOSPI·국채선물·옵션거래량)가
    전부 KRX 데이터라, 코스피 값도 KRX 로 맞추면 Yahoo 쪽의 일시적 결측/오류
    (README "깨질 수 있는 곳" 참고)가 오실레이터의 날짜 교집합을 막는 일이 줄어든다.
    """
    have = {d: v for d, v in (prev_data or [])}
    key = os.environ.get("KRX_API_KEY")
    # KRX 는 당일 값을 다음 날 아침에야 주므로, 그 사이 Yahoo 값이 먼저
    # 들어와 있다. 최근 10일은 KRX 에 매번 다시 물어 KRX 값으로 덮어쓰고
    # (Yahoo 코스피는 KRX 종가와 어긋나는 일이 잦다), KRX 가 아직 없으면 Yahoo 최신값으로 갱신한다.
    recent = _business_days_since((date.today() - timedelta(days=10)).isoformat())
    krx_days: set[str] = set()
    if key:
        missing = _business_days_since(max(have) if have else None)
        got = 0
        for day in sorted(set(missing[-40:]) | set(recent)):
            v, _err = _krx_idx_value(day, key, S.KRX_OPENAPI_PATH_INDEX, S.KOSPI_IDX_NAME)
            if v is not None:
                have[day] = v
                krx_days.add(day)
                got += 1
            time.sleep(0.25)
        print(f"    KRX 오픈API 로 {got}일치 확인", flush=True)

    try:
        yahoo_prev = [[d, v] for d, v in have.items()]
        for d, v in index_series(S.INDICES["kospi"], prev_data=yahoo_prev):
            if d in recent and d not in krx_days:
                have[d] = v        # KRX 가 아직 안 준 최근 날짜는 Yahoo 최신값으로 갱신
            else:
                have.setdefault(d, v)  # KRX 값이 이미 있는 날짜는 덮어쓰지 않는다
    except Exception as e:
        if not have:
            raise
        print(f"    Yahoo/Stooq 보완 실패({e}) — KRX 값만 사용", flush=True)

    if not have:
        raise RuntimeError("코스피: 수집된 값이 없습니다.")
    return dedupe([[d, v] for d, v in have.items()])


def _vkospi_mdc(start: str, end: str) -> list[list]:
    """KRX 정보데이터시스템 내부 엔드포인트 — 기간 조회가 되어 과거치 적재에 쓴다."""
    last_err = None
    for bld, extra in S.VKOSPI_PAYLOAD_CANDIDATES:
        payload = {"bld": bld, "locale": "ko_KR", "strtDd": start, "endDd": end,
                   "share": "1", "money": "1", "csvxls_isNo": "false", **extra}
        try:
            r = session.post(S.KRX_JSON_URL, data=payload, timeout=KRX_MDC_TIMEOUT,
                             headers={"Referer": S.KRX_REFERER,
                                      "X-Requested-With": "XMLHttpRequest"})
            rows = r.json().get("output") or r.json().get("OutBlock_1") or []
            parsed = []
            for row in rows:
                d = (row.get("TRD_DD") or "").replace("/", "-").strip()
                v = (row.get("CLSPRC_IDX") or row.get("TDD_CLSPRC") or "").replace(",", "")
                if d and v and v not in ("-", ""):
                    parsed.append([d, float(v)])
            if parsed:
                return parsed
        except Exception as e:
            last_err = e
    if last_err:
        print(f"    정보데이터시스템 {start}~{end} 실패: {last_err}", flush=True)
    return []


def _kospi200_opt_volumes(day: str, key: str) -> tuple[tuple[int, int] | None, str | None]:
    """코스피200 옵션(미니/위클리 제외) 콜·풋 당일 총 거래량 합계.
    KRX 오픈API '옵션 일별매매정보 (주식옵션外)' — 종목별로 나오는 걸 PROD_NM 으로 필터해 더한다."""
    rows, err = _krx_openapi_get(S.KRX_OPENAPI_BASE + S.KRX_OPT_PATH, day, key)
    if err:
        return None, err
    call_vol = sum(int(row.get("ACC_TRDVOL") or 0) for row in rows
                   if row.get("PROD_NM") == S.KRX_OPT_PROD_NAME and row.get("RGHT_TP_NM") == "CALL")
    put_vol = sum(int(row.get("ACC_TRDVOL") or 0) for row in rows
                  if row.get("PROD_NM") == S.KRX_OPT_PROD_NAME and row.get("RGHT_TP_NM") == "PUT")
    if call_vol == 0 and put_vol == 0:
        # 휴장일 등 — 거래된 게 없으면 그 날은 건너뛴다 (에러는 아님)
        return None, None
    return (call_vol, put_vol), None


def kospi200_option_pcr(prev_data: list[list] | None = None,
                        trading_days: set[str] | None = None) -> list[list]:
    """코스피200 옵션 풋/콜 거래량 비율(PUT/CALL 총거래량).

    오픈API 가 하루치씩만 주기 때문에 VKOSPI 와 같은 방식으로, 이미 쌓인
    데이터가 있으면 마지막 날짜 다음 영업일부터 하루씩만 채운다. 이 지표는
    과거치를 한 번에 적재할 무료 경로가 없어 첫 실행부터 하루씩 쌓인다.
    """
    have = {d: v for d, v in (prev_data or [])}
    key = os.environ.get("KRX_API_KEY")
    if not key:
        if have:
            return dedupe([[d, v] for d, v in have.items()])
        raise RuntimeError("코스피200 옵션 풋/콜 비율 수집 실패: KRX_API_KEY 가 필요합니다 "
                           "('옵션 일별매매정보 (주식옵션外)' API 승인 필요).")

    missing = _business_days_since(max(have) if have else None, trading_days)
    got, errs = 0, []
    for day in missing[-40:]:
        vols, err = _kospi200_opt_volumes(day, key)
        if vols and vols[0] > 0:
            have[day] = round(vols[1] / vols[0], 4)
            got += 1
        elif err:
            errs.append((day, err))
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)
    _print_krx_failures(errs)

    if not have:
        raise RuntimeError("코스피200 옵션 풋/콜 비율 수집 실패: 유효한 거래일 데이터가 없습니다.")
    _check_kr_staleness(have, trading_days, "코스피200 옵션 풋/콜 비율")
    return dedupe([[d, v] for d, v in have.items()])


def kospi200_option_volume(prev_data: list[list] | None, side: str,
                          trading_days: set[str] | None = None) -> list[list]:
    """코스피200 옵션 콜 또는 풋의 당일 총 거래량을 하루씩 증분 수집한다.
    kospi200_pcr 과 같은 API 를 쓰지만 원거래량 자체를 저장해둬야
    KOSPI Fear&Greed 오실레이터의 5일 이동평균 계산에 쓸 수 있다."""
    have = {d: v for d, v in (prev_data or [])}
    key = os.environ.get("KRX_API_KEY")
    if not key:
        if have:
            return dedupe([[d, v] for d, v in have.items()])
        raise RuntimeError(f"코스피200 옵션 {side} 거래량 수집 실패: KRX_API_KEY 가 필요합니다.")

    idx = 0 if side == "call" else 1
    missing = _business_days_since(max(have) if have else None, trading_days)
    got, errs = 0, []
    for day in missing[-40:]:
        vols, err = _kospi200_opt_volumes(day, key)
        if vols is not None:
            have[day] = vols[idx]
            got += 1
        elif err:
            errs.append((day, err))
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)
    _print_krx_failures(errs)

    if not have:
        raise RuntimeError(f"코스피200 옵션 {side} 거래량 수집 실패: 유효한 거래일 데이터가 없습니다.")
    _check_kr_staleness(have, trading_days, f"코스피200 옵션 {side} 거래량")
    return dedupe([[d, v] for d, v in have.items()])


def kospi_fear_greed_osc(series: dict, prev: dict) -> tuple[list[list], dict]:
    """KOSPI Fear & Greed 오실레이터.

    VKOSPI·국채선물지수·옵션거래량은 하루치씩만 커버되는 공식 API 로 채워지므로,
    매번 새로 전체 이력을 받아오는 대신 이미 누적된 각 시리즈의 이력을 그대로
    읽어와 계산한다 (KOSPI 자체는 Yahoo/Stooq 로 전체 이력이 항상 있지만,
    나머지 4개가 짧으면 그만큼만 계산된다).

    `run()` 의 job 실행 순서상 이 job은 항상 그 5개 시리즈보다 나중에 도니,
    이번 실행에서 막 갱신된 `series` 값을 우선 쓰고 없으면(= 이번 실행 대상이
    아니었던 경우) `prev` 로 떨어진다. `series` 만 보면 이번 실행에서 막
    갱신됐는데도 그 갱신분을 못 쓰고 항상 한 번 늦게(지난 실행 기준으로)
    계산되는 문제가 있었다.
    """
    def source(key: str) -> list[list]:
        return series.get(key, prev.get(key, {})).get("data", [])

    kospi = dict(source("kospi"))
    vkospi = dict(source("vkospi"))
    b5 = dict(source("bond5y_futures"))
    b10 = dict(source("bond10y_futures"))
    call = dict(source("kospi200_call_vol"))
    put = dict(source("kospi200_put_vol"))

    dates = sorted(set(kospi) & set(vkospi) & set(b5) & set(b10) & set(call) & set(put))
    if len(dates) < 30:
        raise RuntimeError(f"KOSPI Fear&Greed 오실레이터: 공통 거래일이 {len(dates)}개뿐이라 "
                           "계산할 수 없습니다 (VKOSPI/국채선물/옵션거래량 이력이 더 쌓여야 함).")

    kospi_v = [kospi[d] for d in dates]
    vkospi_v = [vkospi[d] for d in dates]
    spread_v = [b10[d] - b5[d] for d in dates]
    call_v = [call[d] for d in dates]
    put_v = [put[d] for d in dates]

    call_ma5 = _rolling_mean(call_v, 5)
    put_ma5 = _rolling_mean(put_v, 5)
    pcr = [None if (c is None or not c) else p / c for c, p in zip(call_ma5, put_ma5)]

    momentum = [None if m is None else (k - m) / m * 100
                for k, m in zip(kospi_v, _rolling_mean(kospi_v, 125))]
    rsi = _rsi10(kospi_v)

    mom_n, pcr_n = _minmax(momentum), _minmax(pcr)
    vix_n, spread_n, rsi_n = _minmax(vkospi_v), _minmax(spread_v), _minmax(rsi)

    fgi: list[float | None] = []
    for mo, pc, vi, sp, rs in zip(mom_n, pcr_n, vix_n, spread_n, rsi_n):
        if None in (mo, pc, vi, sp, rs):
            fgi.append(None)
        else:
            fgi.append(mo * 0.2 + (1 - pc) * 0.2 + (1 - vi) * 0.2 + sp * 0.2 + rs * 0.2)

    macd = [None if (m is None or l is None) else m - l
            for m, l in zip(_ewm(fgi, 12), _ewm(fgi, 26))]
    hist = [None if (m is None or s is None) else m - s
            for m, s in zip(macd, _ewm(macd, 9))]

    osc = [[d, round(v, 5)] for d, v in zip(dates, hist) if v is not None]
    keep = {d for d, _ in osc}
    price = [[d, round(p, 2)] for d, p in zip(dates, kospi_v) if d in keep]
    if not osc:
        raise RuntimeError("KOSPI Fear&Greed 오실레이터: 계산 결과가 비어 있습니다.")
    return osc, {"price_data": price}


_KOFIA_CACHE: dict[tuple[str, str], list[dict]] = {}


def _kofia_credit_rows(start: str, end: str) -> list[dict]:
    """금융투자협회 FreeSIS 신용공여 잔고추이 (기간 조회, YYYYMMDD).
    코스피·코스닥 두 카드가 같은 응답을 쓰므로 한 실행 안에서는 캐시한다."""
    if (start, end) not in _KOFIA_CACHE:
        body = {"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                             "tmpV45": start, "tmpV46": end, "OBJ_NM": S.KOFIA_CREDIT_OBJ}}
        r = session.post(S.KOFIA_URL, json=body, timeout=TIMEOUT,
                         headers={"Referer": S.KOFIA_REF_URL})
        r.raise_for_status()
        _KOFIA_CACHE[(start, end)] = r.json().get("ds1") or []
    return _KOFIA_CACHE[(start, end)]


def kr_credit_balance(market: str, prev_data: list[list] | None = None,
                      trading_days: set[str] | None = None) -> list[list]:
    """신용거래융자 잔고(조원). 처음엔 1년치를 한 번에 적재하고, 이후에는
    마지막 날짜 며칠 전부터 오늘까지만 조회해 이어 붙인다 — 하루치씩 쌓이고,
    며칠 수집이 빠져도 다음 실행에서 메워진다. 이미 저장된 과거치는 FreeSIS
    조회 구간 밖으로 밀려나도 그대로 남는다."""
    have = {d: v for d, v in (prev_data or [])}
    today = date.today()
    backfill_from = today - timedelta(days=S.KOFIA_CREDIT_BACKFILL_DAYS)
    if not have:
        start = backfill_from
        print(f"    과거치 없음 → {start} 부터 적재", flush=True)
    elif date.fromisoformat(min(have)) > backfill_from + timedelta(days=10):
        # 보관 구간을 늘렸으면 앞쪽 빈 구간까지 한 번에 다시 받는다
        start = backfill_from
        print(f"    보관 시작일 {min(have)} → {start} 부터 보충", flush=True)
    else:
        # 협회가 최근 며칠 값을 정정하는 경우를 대비해 마지막 날짜 7일 전부터 다시 받는다
        start = date.fromisoformat(max(have)) - timedelta(days=7)

    col = S.KOFIA_CREDIT_COLS[market]
    got = 0
    for row in _kofia_credit_rows(start.strftime("%Y%m%d"), today.strftime("%Y%m%d")):
        raw, v = str(row.get("TMPV1", "")), row.get(col)
        if len(raw) != 8 or v in (None, ""):
            continue
        d = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        have[d] = round(float(v) / 1_000_000, 4)   # 백만원 → 조원
        got += 1
    print(f"    FreeSIS 에서 {got}일치 확인", flush=True)

    if not have:
        raise RuntimeError("신용잔고: 금융투자협회 FreeSIS 응답이 비었습니다.")
    # 협회 발표가 영업일 1~2일 늦으므로 여유를 넉넉히 둔다
    _check_kr_staleness(have, trading_days, f"신용잔고({market})", buffer_days=4)
    return dedupe([[d, v] for d, v in have.items()])


CREDIT_PEAK_WINDOW = 250   # 낙폭 기준 고점을 찾는 구간 (약 1년 = 250 영업일)
# 차트 기준선 — 낙폭이 이 아래면 카드가 경고색이 된다. 코스닥은 변동이 커서 더 깊게 둔다.
CREDIT_FLUSH_LINE = {"kospi": -20, "kosdaq": -30}
# 지수 낙폭 카드 기준선 — 국내는 신용잔고와 같게, 미국은 통상 약세장 기준(-20%).
DRAWDOWN_LINE = {**CREDIT_FLUSH_LINE, "spx": -20, "ndx": -20}
CREDIT_SPEED_DAYS = 5      # 낙폭 속도: 최근 N 영업일 동안 낙폭이 몇 %p 커졌나


def _rolling_drawdown(rows: list[list], label: str) -> dict:
    """직전 1년(CREDIT_PEAK_WINDOW 영업일) 최고치 대비 낙폭(%) 계열과 요약값.
    고점이면 0%, 아래로 갈수록 고점에서 많이 빠졌다는 뜻이다. 첫 1년은
    기준 구간이 모자라 계산하지 않는다."""
    if len(rows) < CREDIT_PEAK_WINDOW + 30:
        raise RuntimeError(f"{label}: 데이터가 부족해 낙폭을 계산할 수 없습니다")
    vals = [v for _, v in rows]
    dd, peaks = [], []
    for i in range(CREDIT_PEAK_WINDOW - 1, len(rows)):
        window = vals[i - CREDIT_PEAK_WINDOW + 1:i + 1]
        peak = max(window)
        dd.append([rows[i][0], round((vals[i] / peak - 1) * 100, 2)])
        peaks.append((peak, rows[i - CREDIT_PEAK_WINDOW + 1 + window.index(peak)][0]))
    last_year = (date.fromisoformat(dd[-1][0]) - timedelta(days=365)).isoformat()
    peak, peak_date = peaks[-1]
    return dict(
        dd=dd, last=vals[-1], peak=peak, peak_date=peak_date[5:].replace("-", "/"),
        tags=[f"1년 MDD {min(v for d, v in dd if d >= last_year):.1f}%",
              f"{CREDIT_SPEED_DAYS}일 {dd[-1][1] - dd[-1 - CREDIT_SPEED_DAYS][1]:+.1f}%p"])


def credit_index_combo(series: dict, prev: dict, credit_key: str, index_key: str,
                       label: str) -> tuple[list[list], dict]:
    """신용융자 잔고의 낙폭(%)과 지수를 한 차트에 겹쳐 그린다.

    협회 발표가 영업일 1~2일 늦으므로 낙폭은 신용잔고 날짜까지만 두고,
    지수는 최신까지 그대로 붙인다. 듀얼 차트가 두 계열 날짜의 합집합을
    x 축으로 쓰므로 길이가 달라도 된다. 낙폭 계산에 1년이 먼저 필요해서
    신용잔고는 3년치를 쌓아둔다.
    """
    credit = series.get(credit_key, {}).get("data") or prev.get(credit_key, {}).get("data") or []
    index = {d: v for d, v in (series.get(index_key, {}).get("data")
                               or prev.get(index_key, {}).get("data") or [])}
    if not index:
        raise RuntimeError(f"{label}: 지수 데이터가 없습니다")
    r = _rolling_drawdown(credit, label)
    right = [[d, v] for d, v in sorted(index.items()) if d >= r["dd"][0][0]]
    return r["dd"], {
        "price_data": right, "price_last_date": right[-1][0], "summary_tag": r["tags"],
        "note": f"잔고 {r['last']:.2f}조 · 1년 고점 {r['peak']:.2f}조({r['peak_date']}) 대비 낙폭",
    }


def index_drawdown(series: dict, prev: dict, index_key: str, label: str) -> tuple[list[list], dict]:
    """지수의 직전 1년 고점 대비 낙폭(%). 신용잔고 낙폭 카드와 같은 계산이라
    두 카드를 나란히 놓고 지수와 신용 중 어느 쪽이 더 털렸는지 비교할 수 있다."""
    rows = series.get(index_key, {}).get("data") or prev.get(index_key, {}).get("data") or []
    r = _rolling_drawdown(rows, label)
    return r["dd"], {
        "summary_tag": r["tags"],
        "note": f"지수 {r['last']:,.0f} · 1년 고점 {r['peak']:,.0f}({r['peak_date']}) 대비 낙폭",
    }


RSI_PERIOD = 14   # 증권사 HTS 기본값과 같은 14일 Wilder RSI


def index_rsi(series: dict, prev: dict, index_key: str, label: str) -> tuple[list[list], dict]:
    """지수 종가의 14일 RSI(Wilder 평활)와 지수를 한 차트에 겹쳐 그린다.
    70 이상 과매수, 30 이하 과매도로 본다. (F&G 오실레이터의 _rsi10 은
    원본 스크립트를 따른 단순평균 방식이라 HTS 값과 달라 따로 계산한다.)"""
    rows = series.get(index_key, {}).get("data") or prev.get(index_key, {}).get("data") or []
    if len(rows) < RSI_PERIOD * 10:
        raise RuntimeError(f"{label}: 데이터가 부족해 RSI를 계산할 수 없습니다")
    avg_g = avg_l = 0.0
    out = []
    for i in range(1, len(rows)):
        d = rows[i][1] - rows[i - 1][1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= RSI_PERIOD:
            avg_g += g / RSI_PERIOD
            avg_l += l / RSI_PERIOD
            if i < RSI_PERIOD:
                continue
        else:
            avg_g = (avg_g * (RSI_PERIOD - 1) + g) / RSI_PERIOD
            avg_l = (avg_l * (RSI_PERIOD - 1) + l) / RSI_PERIOD
        rsi = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        out.append([rows[i][0], round(rsi, 2)])
    out = out[-500:]   # 화면은 1년만 쓰므로 약 2년치만 남겨 JSON 크기를 줄인다
    v = out[-1][1]
    word = "과매수" if v >= 70 else "과매도" if v <= 30 else "중립"
    return out, {"price_data": [[d, x] for d, x in rows if d >= out[0][0]], "summary_tag": [word]}


def _wilder_rsi(vals: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder 평활 RSI. 앞쪽 period 개는 None."""
    out: list[float | None] = [None] * len(vals)
    avg_g = avg_l = 0.0
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= period:
            avg_g += g / period
            avg_l += l / period
            if i < period:
                continue
        else:
            avg_g = (avg_g * (period - 1) + g) / period
            avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def _weekly_rsi_asof(rows: list[list], period: int = RSI_PERIOD) -> list[float | None]:
    """날짜별 '그날 기준' 주봉 RSI. 지난 주들은 주 마지막 종가, 이번 주는 그날
    종가를 임시 주봉으로 쓴다 — 엑셀 STOCKHISTORY 주봉(진행 중인 주 포함)과 같다."""
    weeks: list[float] = []            # 끝난 주들의 종가
    avg_g = avg_l = 0.0                # 끝난 주까지의 Wilder 누적(첫 period 개는 합/period)
    out: list[float | None] = []
    cur_wk = None
    for i, (d, c) in enumerate(rows):
        wk = date.fromisoformat(d).isocalendar()[:2]
        if cur_wk is not None and wk != cur_wk:
            weeks.append(rows[i - 1][1])          # 지난 주 확정
            n = len(weeks) - 1                    # 확정된 주간 변화 개수
            if n >= 1:
                dlt = weeks[-1] - weeks[-2]
                g, l = max(dlt, 0.0), max(-dlt, 0.0)
                if n <= period:
                    avg_g += g / period
                    avg_l += l / period
                else:
                    avg_g = (avg_g * (period - 1) + g) / period
                    avg_l = (avg_l * (period - 1) + l) / period
        cur_wk = wk
        n = len(weeks)                 # 이번 주까지 넣으면 변화가 n 개
        if n < period:
            out.append(None)
            continue
        dlt = c - weeks[-1]
        g, l = max(dlt, 0.0), max(-dlt, 0.0)
        if n == period:                # 첫 RSI: 앞 period-1 개 + 이번 주의 단순평균
            ag, al = avg_g + g / period, avg_l + l / period
        else:
            ag = (avg_g * (period - 1) + g) / period
            al = (avg_l * (period - 1) + l) / period
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


_CONSTITUENTS: dict[str, list[str]] = {}


def _constituents(universe: str) -> list[str]:
    """S&P 500(위키백과 표) / Nasdaq-100(nasdaq.com) 현재 구성종목을 Yahoo 표기로."""
    if universe in _CONSTITUENTS:
        return _CONSTITUENTS[universe]
    if universe == "spx":
        # 위키백과는 공용 세션(레거시 TLS 어댑터)으로 접속하면 403 을 준다 — 기본 requests 로 받는다
        r = requests.get(S.SP500_LIST_URL, headers={"User-Agent": "Mozilla/5.0 (macro-board)"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        t = r.text
        tb = re.search(r'<table[^>]*id="constituents".*?</table>', t, re.S).group(0)
        syms = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if tds:
                syms.append(html.unescape(re.sub(r"<[^>]+>", "", tds[0])).strip())
    elif universe == "ndx":
        j = get(S.NDX_LIST_URL, headers={"User-Agent": UA, "Accept": "application/json"}).json()
        syms = [r["symbol"] for r in j["data"]["data"]["rows"]]
    else:   # kospi / kosdaq: 네이버 시가총액 순위에서 ETF 를 뺀 상위 KR_TOP_N 종목
        suffix = ".KS" if universe == "kospi" else ".KQ"
        codes, page = [], 1
        while len(codes) < S.KR_TOP_N and page <= 5:
            j = get(S.NAVER_MARKET_VALUE_URL.format(market=universe.upper()),
                    params={"page": page, "pageSize": 100}).json()
            codes += [r["itemCode"] for r in j.get("stocks", []) if r.get("stockEndType") == "stock"]
            page += 1
        _CONSTITUENTS[universe] = sorted({c + suffix for c in codes[:S.KR_TOP_N]})
        if len(_CONSTITUENTS[universe]) < 90:
            raise RuntimeError(f"{universe} 종목 목록이 비정상적으로 짧습니다")
        return _CONSTITUENTS[universe]
    syms = sorted({x.replace(".", "-") for x in syms if x})
    if len(syms) < 90:
        raise RuntimeError(f"{universe} 구성종목 목록이 비정상적으로 짧습니다 ({len(syms)}개)")
    _CONSTITUENTS[universe] = syms
    return syms


_SPARK_CACHE: dict[str, list[list]] = {}


def _spark_closes(symbols: list[str]) -> dict[str, list[list]]:
    """Yahoo spark 로 여러 종목의 2년 일별 종가를 20개씩 묶어 받는다.
    장중에 돌면 오늘 값(현재가)은 버린다 — yahoo() 와 같은 이유."""
    need = [x for x in symbols if x not in _SPARK_CACHE]
    for i in range(0, len(need), 20):
        chunk = need[i:i + 20]
        try:
            r = get("https://query1.finance.yahoo.com/v7/finance/spark",
                    params={"symbols": ",".join(chunk), "range": "2y", "interval": "1d"})
            results = r.json().get("spark", {}).get("result") or []
        except Exception as e:
            print(f"    spark 실패({chunk[0]}…): {e}", flush=True)
            results = []
        for res in results:
            resp = (res.get("response") or [{}])[0]
            ts = resp.get("timestamp") or []
            closes = ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            regular = ((resp.get("meta") or {}).get("currentTradingPeriod") or {}).get("regular") or {}
            cut = (regular.get("start")
                   if regular.get("end") and time.time() < regular["end"] + 1800 else None)
            _SPARK_CACHE[res["symbol"]] = [
                [datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), c]
                for t, c in zip(ts, closes) if c is not None and (cut is None or t < cut)]
        time.sleep(0.3)
    return {x: _SPARK_CACHE[x] for x in symbols if x in _SPARK_CACHE}


def market_breadth(universe: str, prev_entry: dict | None) -> tuple[list[list], dict]:
    """구성종목 중 200일선·50일선 위에 있는 비율(0~1)을 날짜별로 계산한다.
    data = 200일선 위 비율, pct50 = 50일선 위 비율. 2년치 종가로 최근 약 1년을
    계산하고 이전 실행에서 쌓인 값과 합친다. 현재 구성종목 기준이라 과거치에는
    생존편향이 조금 있다."""
    try:
        members = _constituents(universe)
    except Exception as e:
        # 목록 출처가 클라우드 IP 를 막는 경우 등 — 직전에 받아둔 목록을 그대로 쓴다
        members = (prev_entry or {}).get("constituents") or []
        print(f"    구성종목 목록 실패({e}) → 직전 목록 {len(members)}개 사용", flush=True)
        if not members:
            raise
    closes = _spark_closes(members)
    if len(closes) < 80:
        raise RuntimeError(f"시장 폭({universe}): 종가를 받은 종목이 {len(closes)}개뿐")
    above: dict[int, dict[str, list[int]]] = {50: {}, 200: {}}   # 날짜 → [위, 전체]
    for rows in closes.values():
        vals = [v for _, v in rows]
        for win in (50, 200):
            if len(vals) < win:
                continue
            run = sum(vals[:win])
            for i in range(win - 1, len(vals)):
                if i >= win:
                    run += vals[i] - vals[i - win]
                cnt = above[win].setdefault(rows[i][0], [0, 0])
                cnt[0] += vals[i] > run / win
                cnt[1] += 1

    def pct(win):   # 종목 절반 이상이 계산된 날짜만 (휴장일·데이터 공백 방지)
        return {d: round(a / n, 4) for d, (a, n) in above[win].items() if n >= len(closes) * 0.5}

    have200 = {d: v for d, v in ((prev_entry or {}).get("data") or [])}
    have50 = {d: v for d, v in ((prev_entry or {}).get("pct50") or [])}
    have200.update(pct(200))
    have50.update(pct(50))
    days = sorted(set(have200) & set(have50))
    if not days:
        raise RuntimeError(f"시장 폭({universe}): 계산된 날짜가 없습니다")
    print(f"    {len(closes)}종목 · 최신 {days[-1]} 200일선 위 {have200[days[-1]]:.1%}"
          f" / 50일선 위 {have50[days[-1]]:.1%}", flush=True)
    return [[d, have200[d]] for d in days], {"pct50": [[d, have50[d]] for d in days],
                                             "constituents": members}


def _step(table: list[tuple], v: float) -> float:
    """엑셀 LOOKUP 과 같이 v 이상인 가장 큰 하한의 원점수."""
    out = table[0][1]
    for lo, sc in table:
        if v >= lo:
            out = sc
    return out


def _ffill(rows: list[list], days: list[str]) -> dict[str, float]:
    """rows 를 days 각 날짜 기준 가장 최근 값으로 맞춘다(그 전 값이 없으면 생략)."""
    src = sorted(rows)
    out, j, last = {}, 0, None
    for d in days:
        while j < len(src) and src[j][0] <= d:
            last = src[j][1]
            j += 1
        if last is not None:
            out[d] = last
    return out


def _pct_rank_series(rows: list[list], window: int = 250, min_obs: int = 60) -> list[list]:
    """각 날짜 값이 직전 window 개 안에서 몇 번째인지 0~100 으로 (0 = 가장 낮음)."""
    vals = [v for _, v in rows]
    out = []
    for i, (d, v) in enumerate(rows):
        w = vals[max(0, i - window + 1):i + 1]
        if len(w) >= min_obs:
            out.append([d, 100 * sum(x < v for x in w) / (len(w) - 1 or 1)])
    return out


DIP_MARKETS = {
    # key: (가격 계열, 시장 폭, 변동성 계열·구간, 심리 계열(가공), 신용 재료, MDD 구간 키)
    "spy":    dict(price="spy", breadth="breadth_spx", vol="vix", vol_table="DIP_VIX", vol_name="VIX",
                   sentiment="fear_greed", credit="hy"),
    "qqq":    dict(price="qqq", breadth="breadth_ndx", vol="vix", vol_table="DIP_VIX", vol_name="VIX",
                   sentiment="fear_greed", credit="hy"),
    "kospi":  dict(price="kospi", breadth="breadth_kospi", vol="vkospi", vol_table="DIP_VKOSPI",
                   vol_name="VKOSPI", sentiment="kospi_fg_osc", credit="kospi_credit"),
    "kosdaq": dict(price="kosdaq", breadth="breadth_kosdaq", vol="vkospi", vol_table="DIP_VKOSPI",
                   vol_name="VKOSPI", sentiment="kospi_fg_osc", credit="kosdaq_credit"),
}


def _dip_explain(market: str, cfg: dict, det: dict, vol_table: list, credit_table: list,
                 senti_name: str, stages: list, rsi_w_table: list, b50_table: list,
                 gate: list | None) -> tuple[list[dict], dict]:
    """Buy-the-Dip 점수의 근거를 화면 팝업용으로 정리한다.
    breakdown: 현재 지표별 (현재값, 원점수 0~100, 최대 반영점수, 반영점수)
    rules: 지표별 구간표, 매수 단계표."""
    W = S.DIP_WEIGHTS
    vol_name = cfg["vol_name"]
    credit_is_hy = cfg["credit"] == "hy"
    rs, raw = det["raw_sub"], det["raw"]

    def row(label, value, raw_score, weight, sub=None):
        return dict(label=label, value=value, raw=round(raw_score, 1), weight=weight,
                    points=round(raw_score * weight / 100, 2), sub=sub)

    breakdown = [
        row("MDD (1년 고점 대비)", f"{det['mdd']:.1%}", raw["mdd"], W["mdd"]),
        row(vol_name, f"{det['vol']:.1f}", raw["vix"], W["vix"]),
        row("RSI(14) 일 60% + 주 40%", f"일 {det['rsi_d']:.1f} / 주 {det['rsi_w']:.1f}", raw["rsi"], W["rsi"],
            sub=f"일 {rs['rsi_d']:.0f}점 · 주 {rs['rsi_w']:.0f}점"),
        row("Fear & Greed" if senti_name == "F&G" else "코스피 F&G 오실레이터 1년 순위",
            f"{det['senti']:.0f}", raw["fg"], W["fg"]),
        row("시장 폭 200일 60% + 50일 40%", f"200일 {det['b200']:.1%} / 50일 {det['b50']:.1%}",
            raw["breadth"], W["breadth"], sub=f"200일 {rs['b200']:.0f}점 · 50일 {rs['b50']:.0f}점"),
        row("HY OAS" if credit_is_hy else "신용융자 잔고 낙폭",
            f"{det['credit'] * 100:.2f}%" if credit_is_hy else f"-{det['credit']:.1%}", raw["hy"], W["hy"]),
    ]
    rev_labels = [("vix_off_peak", f"{vol_name}가 최근 {S.DIP_VIX_PEAK_DAYS}거래일 고점 대비 15% 이상 하락"),
                  ("rsi_recross", "일봉 RSI가 30 이하에서 30 위로 재돌파"),
                  ("above_10dma", "종가가 10일 이동평균 이상"),
                  ("breadth_up", f"50일선 위 종목 비율이 {S.DIP_BREADTH_IMPROVE}거래일 전보다 높음")]
    for key, label in rev_labels:
        met = bool(det["rev"][key])
        breakdown.append(dict(label=label, value="충족" if met else "미충족", raw=100 if met else 0,
                              weight=S.DIP_REVERSAL_PTS, points=S.DIP_REVERSAL_PTS if met else 0,
                              reversal=True))

    def bands(table, fmt, higher_is_more=True):
        """[(하한, 원점수)] → [[구간 글자, 원점수]]"""
        out = []
        for k, (lo, sc) in enumerate(table):
            hi = table[k + 1][0] if k + 1 < len(table) else None
            txt = f"{fmt(lo)} 이상" if hi is None else f"{fmt(lo)} ~ {fmt(hi)}"
            out.append([txt, sc])
        return out

    pct0 = lambda v: f"{v:.0%}"
    num = lambda v: f"{v:g}"
    tables = [
        dict(label="MDD (고점 대비 하락폭)", weight=W["mdd"], rows=bands(S.DIP_MDD[market], pct0)),
        dict(label=vol_name, weight=W["vix"], rows=bands(vol_table, num)),
        dict(label="일봉 RSI(14) — RSI 항목의 60%", weight=round(W["rsi"] * .6, 1), rows=bands(S.DIP_RSI_D, num)),
        dict(label="주봉 RSI(14) — RSI 항목의 40%", weight=round(W["rsi"] * .4, 1), rows=bands(rsi_w_table, num)),
        dict(label=breakdown[3]["label"], weight=W["fg"], rows=bands(S.DIP_FG, num)),
        dict(label="시장 폭 200일선 위 비율 — 시장 폭 항목의 60%", weight=round(W["breadth"] * .6, 1),
             rows=bands(S.DIP_BREADTH, pct0)),
        dict(label="시장 폭 50일선 위 비율 — 시장 폭 항목의 40%", weight=round(W["breadth"] * .4, 1),
             rows=bands(b50_table, pct0)),
        dict(label=breakdown[5]["label"], weight=W["hy"],
             rows=bands(credit_table, (lambda v: f"{v * 100:g}%") if credit_is_hy else pct0)),
    ]
    rules = dict(
        tables=tables,
        stages=[[lo, name, pct] for lo, name, pct in stages],
        panic_max=sum(W.values()), reversal_max=S.DIP_REVERSAL_PTS * 4,
        gate=[[need, cap] for need, cap in gate] if gate else None,
        vix_floor=list(S.DIP_VIX_FLOOR) if market in ("spy", "qqq") else None,
        mdd_now=abs(det["mdd"]),
        note=("각 지표의 원점수(0~100)에 반영 비율을 곱해 더한 패닉 점수(최대 80)와 "
              "반전 신호 4개(각 5점, 최대 20)를 합산합니다. 구간은 '하한 이상'인 가장 높은 구간을 적용합니다."),
    )
    return breakdown, rules


def dip_score(series: dict, prev: dict, market: str) -> tuple[list[list], dict]:
    """Buy-the-Dip 패닉 점수(0~100). data/buy_the_dip_panic_score_v1.1.xlsx 와 같은 틀
    (패닉 80 + 반전 20)로 날짜마다 계산해 최근 1년 추이를 남긴다. 엑셀에서 손으로 넣던
    반전 조건 두 개(변동성 고점 대비 15% 하락, 시장 폭 5거래일 개선)도 자동 판정한다.
    국내판은 sources.DIP_MARKETS 대로 VKOSPI·코스피 F&G 순위·신용잔고 낙폭을 쓴다."""
    cfg = DIP_MARKETS[market]

    def src(k):
        return series.get(k) or prev.get(k) or {}
    px = src(cfg["price"]).get("data") or []
    br = src(cfg["breadth"])
    if len(px) < 300 or not br.get("data"):
        raise RuntimeError(f"Buy-the-Dip({market}): 재료 데이터가 부족합니다")
    px = px[-800:]
    days = [d for d, _ in px]
    vals = [v for _, v in px]
    rsi_d = _wilder_rsi(vals)
    rsi_w = _weekly_rsi_asof(px)
    vol = _ffill(src(cfg["vol"]).get("data") or [], days)
    if cfg["sentiment"] == "fear_greed":
        senti_rows, senti_name = src("fear_greed").get("data") or [], "F&G"
    else:
        senti_rows, senti_name = _pct_rank_series(src(cfg["sentiment"]).get("data") or []), "F&G순위"
    senti = _ffill(senti_rows, days)
    if cfg["credit"] == "hy":
        credit = {d: v / 100 for d, v in _ffill(src("hy_yield").get("data") or [], days).items()}
        credit_table, credit_fmt = S.DIP_HY, lambda v: f"HY {v * 100:.2f}%"
    else:
        cdd = _rolling_drawdown(src(cfg["credit"]).get("data") or [], cfg["credit"])["dd"]
        credit = {d: abs(v) / 100 for d, v in _ffill(cdd, days).items()}
        credit_table, credit_fmt = S.DIP_CREDIT_DD, lambda v: f"신용잔고 -{v:.0%}"
    vol_table = getattr(S, cfg["vol_table"])
    us = market in ("spy", "qqq")
    rsi_w_table = S.DIP_RSI_W_US if us else S.DIP_RSI_W
    b50_table = S.DIP_BREADTH_50_US if us else S.DIP_BREADTH
    b200 = _ffill(br["data"], days)
    b50 = _ffill(br.get("pct50") or [], days)
    br_days = {d for d, _ in br["data"]}
    vol_list = [vol.get(d) for d in days]

    out, rev_hist, detail = [], [], None
    start = (date.fromisoformat(days[-1]) - timedelta(days=400)).isoformat()
    for i, d in enumerate(days):
        if (d < start or d not in br_days or d not in vol or d not in senti or d not in credit
                or i < 10 or rsi_d[i - 1] is None or rsi_w[i] is None):
            continue
        lo_day = (date.fromisoformat(d) - timedelta(days=365)).isoformat()
        peak = max(v for dd, v in zip(days[:i + 1], vals[:i + 1]) if dd >= lo_day)
        mdd = vals[i] / peak - 1
        sma10 = sum(vals[i - 9:i + 1]) / 10
        vol_peak = max(v for v in vol_list[max(0, i - S.DIP_VIX_PEAK_DAYS + 1):i + 1] if v is not None)
        j5 = i - S.DIP_BREADTH_IMPROVE
        raw = dict(
            mdd=_step(S.DIP_MDD[market], abs(mdd)),
            vix=_step(vol_table, vol[d]),
            rsi=0.6 * _step(S.DIP_RSI_D, rsi_d[i]) + 0.4 * _step(rsi_w_table, rsi_w[i]),
            fg=_step(S.DIP_FG, senti[d]),
            breadth=0.6 * _step(S.DIP_BREADTH, b200[d]) + 0.4 * _step(b50_table, b50[d]),
            hy=_step(credit_table, credit[d]),
        )
        panic = sum(raw[k] * S.DIP_WEIGHTS[k] / 100 for k in raw)
        rev = dict(
            vix_off_peak=vol[d] <= vol_peak * 0.85,
            rsi_recross=rsi_d[i - 1] <= 30 < rsi_d[i],
            above_10dma=vals[i] >= sma10,
            breadth_up=j5 >= 0 and days[j5] in b50 and b50[d] > b50[days[j5]],
        )
        total = round(panic + S.DIP_REVERSAL_PTS * sum(rev.values()))
        out.append([d, total])
        rev_hist.append([d, sum(rev.values())])
        detail = dict(mdd=mdd, vol=vol[d], rsi_d=rsi_d[i], rsi_w=rsi_w[i], senti=senti[d],
                      b200=b200[d], b50=b50[d], credit=credit[d], panic=panic, rev=rev, raw=raw,
                      raw_sub=dict(rsi_d=_step(S.DIP_RSI_D, rsi_d[i]), rsi_w=_step(rsi_w_table, rsi_w[i]),
                                   b200=_step(S.DIP_BREADTH, b200[d]), b50=_step(b50_table, b50[d])))
    if not out:
        raise RuntimeError(f"Buy-the-Dip({market}): 계산 가능한 날짜가 없습니다")

    total = out[-1][1]
    stages = S.DIP_STAGES[market]
    _, stage, pct = [x for x in stages if total >= x[0]][-1]
    gate = S.DIP_MDD_GATE.get(market)
    gated = False
    if gate:
        allowed = [cap for need, cap in gate if abs(detail["mdd"]) >= need][-1]
        if pct > allowed:
            _, stage, pct = [x for x in stages if total >= x[0] and x[2] <= allowed][-1]
            gated = True
    vix_floor = False
    if market in ("spy", "qqq"):
        vix_min, floor_pct = S.DIP_VIX_FLOOR
        if detail["vol"] >= vix_min and pct < floor_pct:
            _, stage, pct = next(x for x in stages if x[2] >= floor_pct)
            vix_floor = True
    buy_from = next(lo for lo, _, p in stages if p > 0)
    core_from = next(lo for lo, name, _ in stages if name == "본격 분할매수")
    rv = detail["rev"]
    marks = " ".join(f"{name}{'✓' if rv[key] else '·'}" for key, name in
                     (("vix_off_peak", f"{cfg['vol_name']}↓"), ("rsi_recross", "RSI30↑"),
                      ("above_10dma", "10일선"), ("breadth_up", "폭↑")))
    note = (f"패닉 {detail['panic']:.1f}/80 · 반전 {S.DIP_REVERSAL_PTS * sum(rv.values())}/20 ({marks}) — "
            f"MDD {detail['mdd']:.1%} · {cfg['vol_name']} {detail['vol']:.1f}"
            f" · RSI 일 {detail['rsi_d']:.0f}/주 {detail['rsi_w']:.0f}"
            f" · {senti_name} {detail['senti']:.0f} · 폭 200일 {detail['b200']:.0%}/50일 {detail['b50']:.0%}"
            f" · {credit_fmt(detail['credit'])}")
    breakdown, rules = _dip_explain(market, cfg, detail, vol_table, credit_table, senti_name, stages,
                                    rsi_w_table, b50_table, gate)
    return out, {
        "price_data": [[d, v] for d, v in px if d >= out[0][0]],
        "reversal_data": rev_hist,   # 날짜별 충족된 반전 신호 개수(0~4)
        "dip_breakdown": breakdown,  # 화면 팝업: 현재 지표별 점수
        "dip_rules": rules,          # 화면 팝업: 구간표·가중치·매수 단계
        "summary_tag": [stage, f"누적투입 {pct:.0%}"]
                       + (["MDD 조건으로 제한"] if gated else [])
                       + ([f"VIX {S.DIP_VIX_FLOOR[0]} 이상 최소 {S.DIP_VIX_FLOOR[1]:.0%}"] if vix_floor else []),
        "summary_state": "good" if pct > 0 else "neutral",
        "osc_ref": [buy_from, core_from],   # 1차 매수 · 본격 분할매수 시작점
        "note": note,
    }


def dedupe(rows: list[list]) -> list[list]:
    """날짜 오름차순 정렬 + 중복 날짜 제거."""
    seen, out = set(), []
    for d, v in sorted(rows):
        if d not in seen:
            seen.add(d)
            out.append([d, v])
    return out


def global_m2() -> tuple[list[list], dict]:
    """
    각국 M2 → USD 환산 → 합산 → YoY.
    달러 환산본과 고정환율본을 함께 반환한다 (화면에서 토글).
    """
    levels_usd: dict[str, dict[str, float]] = {}   # 국가 → {YYYY-MM: USD}
    levels_fixed: dict[str, dict[str, float]] = {}
    resolved: dict[str, str] = {}

    for code, cfg in S.M2_COMPONENTS.items():
        # 1) 원계열 확보
        if cfg.get("fred"):
            raw = fred(cfg["fred"], START_MONTHLY)
            resolved[code] = f"FRED/{cfg['fred']}"
        elif cfg.get("oecd_monagg"):
            m = cfg["oecd_monagg"]
            raw = oecd_monagg(**m)
            resolved[code] = f"OECD/MONAGG/{m['area']}.{m['measure']}.{m['unit']}"
        else:
            raw, sid = None, None
            for cand in cfg["series_ids"]:
                try:
                    raw = dbnomics(cand)
                    sid = cand
                    break
                except Exception:
                    continue
            if raw is None:
                hint = ", ".join(dbnomics_search(cfg["search"])) or "검색 결과 없음"
                raise RuntimeError(
                    f"{cfg['label']} M2 시리즈를 찾지 못했습니다. "
                    f"DBnomics 데이터셋 후보: {hint} — "
                    f"sources.M2_COMPONENTS['{code}']['series_ids'] 에 정확한 ID 를 넣어주세요.")
            resolved[code] = f"DBnomics/{sid}"

        monthly = {d[:7]: v * cfg["scale"] for d, v in raw}

        # 2) USD 환산
        if cfg["fx"] is None:
            levels_usd[code] = dict(monthly)
            levels_fixed[code] = dict(monthly)
            continue

        fx_monthly = monthly_avg(fred(cfg["fx"], START_MONTHLY))
        if not fx_monthly:
            raise RuntimeError(f"{cfg['label']}: 환율 {cfg['fx']} 수집 실패")
        base_key = max(fx_monthly)                 # 고정환율 = 최신 환율
        base_rate = fx_monthly[base_key]

        conv_var, conv_fix = {}, {}
        for m, v in monthly.items():
            rate = fx_monthly.get(m)
            if rate is None:
                continue
            if cfg["fx_mode"] == "multiply":
                conv_var[m], conv_fix[m] = v * rate, v * base_rate
            else:
                conv_var[m], conv_fix[m] = v / rate, v / base_rate
        levels_usd[code], levels_fixed[code] = conv_var, conv_fix

    def combine(levels):
        months = set.intersection(*(set(v) for v in levels.values()))
        return [[m + "-01", sum(levels[c][m] for c in levels)] for m in sorted(months)]

    meta = dict(components=resolved,
                fixed_fx=yoy(combine(levels_fixed)))
    return yoy(combine(levels_usd)), meta


# ══════════════════════════════════════════════════════════════
# 오케스트레이션
# ══════════════════════════════════════════════════════════════

def build_jobs(prev: dict, series: dict) -> dict:
    jobs = {}

    for key, cfg in S.FRED.items():
        if key in ("vix", "ust10y"):
            continue  # 아래에서 Yahoo 로 별도 등록 — FRED VIXCLS/DGS10 원본이
            # 2026-09-18 이후로 갱신이 지연되고 있는 게 확인됨 (README 참고)
        jobs[key] = dict(
            fn=(lambda c=cfg, k=key: fred(c["id"], prev_data=prev.get(k, {}).get("data"))),
            name=cfg["name"], unit=cfg["unit"], decimals=cfg["decimals"],
            threshold=cfg["threshold"], below_is=cfg["below_is"],
            freq="daily", source="FRED",
            source_url=f"https://fred.stlouisfed.org/series/{cfg['id']}")

    jobs["vix"] = dict(
        fn=(lambda: index_series(S.FG_VIX, prev_data=prev.get("vix", {}).get("data"))),
        name=S.FRED["vix"]["name"], unit=S.FRED["vix"]["unit"],
        decimals=S.FRED["vix"]["decimals"], threshold=S.FRED["vix"]["threshold"],
        below_is=S.FRED["vix"]["below_is"], freq="daily",
        source="Yahoo Finance (^VIX)",
        source_url="https://finance.yahoo.com/quote/%5EVIX/")

    jobs["ust10y"] = dict(
        fn=(lambda: index_series(S.FG_DGS10, prev_data=prev.get("ust10y", {}).get("data"))),
        name=S.FRED["ust10y"]["name"], unit=S.FRED["ust10y"]["unit"],
        decimals=S.FRED["ust10y"]["decimals"], threshold=S.FRED["ust10y"]["threshold"],
        below_is=S.FRED["ust10y"]["below_is"], freq="daily",
        source="Yahoo Finance (^TNX)",
        source_url="https://finance.yahoo.com/quote/%5ETNX/")

    for key, cfg in S.INDICES.items():
        if key == "kospi":
            continue  # 아래에서 KRX 공식 API 우선 + Yahoo/Stooq 보완으로 따로 등록
        jobs[key] = dict(
            fn=(lambda c=cfg, k=key: index_series(c, prev_data=prev.get(k, {}).get("data"))),
            name=cfg["name"], unit=cfg["unit"], decimals=cfg["decimals"],
            threshold=None, below_is=None,
            freq="daily", source="Yahoo Finance / Stooq", source_url="",
            card_url=cfg.get("card_url", ""))

    jobs["kospi"] = dict(
        fn=(lambda: kospi_index_series(prev.get("kospi", {}).get("data"))),
        name=S.INDICES["kospi"]["name"], unit=S.INDICES["kospi"]["unit"],
        decimals=S.INDICES["kospi"]["decimals"],
        threshold=None, below_is=None,
        freq="daily", source="KRX 공식 오픈API + Yahoo Finance / Stooq 보완", source_url="",
        card_url=S.INDICES["kospi"].get("card_url", ""))

    jobs["fear_greed"] = dict(
        fn=fear_greed, name="Fear & Greed", unit="", decimals=0,
        threshold=50, below_is="bad", freq="daily", source="CNN",
        source_url="https://edition.cnn.com/markets/fear-and-greed",
        bands=[[0, 25, "극단적 공포"], [25, 45, "공포"], [45, 55, "중립"],
               [55, 75, "탐욕"], [75, 100, "극단적 탐욕"]])

    jobs["spx_fg_osc"] = dict(
        fn=fear_greed_osc_spx, name="Fear & Greed 오실레이터 (S&P500)", unit="", decimals=3,
        threshold=0, below_is="bad", freq="daily",
        source="Yahoo Finance · FRED (커스텀 계산)", source_url="",
        kind="dual", price_label="S&P500", price_unit="", price_decimals=0,
        osc_legend="Fear & Greed Oscillator (S&P500)", price_legend="S&P500 Index",
        fixed_period_months=6)
    jobs["ndx_fg_osc"] = dict(
        fn=fear_greed_osc_ndx, name="Fear & Greed 오실레이터 (NASDAQ)", unit="", decimals=3,
        threshold=0, below_is="bad", freq="daily",
        source="Yahoo Finance · FRED (커스텀 계산)", source_url="",
        kind="dual", price_label="NASDAQ", price_unit="", price_decimals=0,
        osc_legend="Fear & Greed Oscillator (NASDAQ)", price_legend="NASDAQ Index",
        fixed_period_months=6)

    jobs["rate_hy_combo"] = dict(
        fn=(lambda: rate_hy_combo(prev.get("rate_hy_combo", {}).get("data"),
                                   prev.get("rate_hy_combo", {}).get("price_data"))),
        name="미국 10년물 국채금리 · 하이일드 스프레드", unit="%", decimals=2,
        threshold=None, below_is=None, freq="daily", source="Yahoo Finance · FRED",
        source_url="https://finance.yahoo.com/quote/%5ETNX/",
        kind="dual", price_label="하이일드 스프레드", price_unit="%p", price_decimals=2,
        osc_legend="미국 10년물 국채금리 (%)", price_legend="하이일드 스프레드 (%p)",
        card_url="https://fred.stlouisfed.org/series/BAMLH0A0HYM2")

    jobs["oecd_cli"] = dict(
        fn=oecd_cli, name="OECD 경기선행지수 (미국)", unit="", decimals=2,
        threshold=100, below_is="bad", freq="monthly", source="OECD",
        source_url="https://data-explorer.oecd.org/")

    jobs["ism_pmi"] = dict(
        fn=(lambda: ism_pmi(prev.get("ism_pmi", {}).get("data"))),
        name="ISM 제조업지수", unit="", decimals=1,
        threshold=50, below_is="bad", freq="monthly", source="ISM (DBnomics · PR Newswire 보도자료)",
        source_url="https://db.nomics.world/ISM/pmi",
        ref_url=S.ISM_REF_URL, ref_label="값 대조", card_url=S.ISM_REF_URL)

    jobs["global_m2_yoy"] = dict(
        fn=global_m2, name="글로벌 M2 증감율 (YoY, 중국 제외)", unit="%", decimals=2,
        threshold=0, below_is="bad", freq="monthly",
        source="FRED · ECB · BOJ 합성",
        source_url="", note="미국·유로존·일본 M2 를 달러로 환산해 합산한 뒤 전년동월비. "
                            "중국은 소스 단절로 제외 (scripts/sources.py 참고)")

    jobs["us_cpi_yoy"] = dict(
        fn=(lambda: yoy(fred("CPIAUCNS", START_MONTHLY))),
        name="미국 CPI (YoY)", unit="%", decimals=2,
        threshold=2, below_is="good", freq="monthly", source="FRED",
        source_url="https://fred.stlouisfed.org/series/CPIAUCNS",
        note="연준 물가안정 목표(2%) 기준. 계절조정 전(NSA) 지수로 계산한 전년동월비")

    jobs["real_policy_rate"] = dict(
        fn=real_policy_rate, name="실질 정책금리 (정책금리 - 헤드라인 PCE)", unit="%p", decimals=2,
        threshold=0, below_is="bad", freq="monthly",
        source="FRED (Fed Funds 목표범위 + PCEPI 합성)",
        source_url="https://fred.stlouisfed.org/series/DFEDTARU",
        note="정책금리는 Fed Funds 목표범위 중간값, 물가는 헤드라인 PCE 전년동월비. "
             "PCE 발표가 늦어 아직 안 나온 최근 달은 마지막 발표치를 그대로 사용")

    jobs["kr_exports_yoy"] = dict(
        fn=(lambda: kr_exports_yoy(prev.get("kr_exports_yoy", {}).get("data"))),
        name="한국 수출증가율 (YoY)", unit="%", decimals=2,
        threshold=0, below_is="bad", freq="monthly",
        source="관세청 통관 수출액 (ECOS 901Y118) + 산업통상부 수출입동향 속보",
        source_url="https://www.motir.go.kr/kor/article/ATCL3f49a5a8c")

    jobs["vkospi"] = dict(
        fn=(lambda: vkospi(prev.get("vkospi", {}).get("data"),
                           trading_days=_kr_trading_days(series, prev))),
        name="VKOSPI", unit="", decimals=2,
        threshold=20, below_is="good", freq="daily", source="KRX",
        source_url="http://data.krx.co.kr/",
        ref_url=S.VKOSPI_REF_URL, ref_label="값 대조")

    jobs["kospi200_pcr"] = dict(
        fn=(lambda: kospi200_option_pcr(prev.get("kospi200_pcr", {}).get("data"),
                                        trading_days=_kr_trading_days(series, prev))),
        name="코스피200 옵션 풋/콜 비율", unit="", decimals=3,
        threshold=1, below_is="good", freq="daily", source="KRX",
        source_url="", fixed_period_months=6,
        note="코스피200 옵션(미니·위클리 제외) 콜·풋 당일 총 거래량 비율(PUT/CALL). "
             "1보다 높으면 풋 거래가 더 많다는 뜻으로 통상 공포 신호로 해석됩니다.")

    jobs["bond5y_futures"] = dict(
        fn=(lambda: drvprod_index_series(prev.get("bond5y_futures", {}).get("data"),
                                         "5년 국채선물 추종 지수", "5년 국채선물 추종 지수",
                                         trading_days=_kr_trading_days(series, prev))),
        name="5년 국채선물 추종 지수", unit="", decimals=2,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")

    jobs["bond10y_futures"] = dict(
        fn=(lambda: drvprod_index_series(prev.get("bond10y_futures", {}).get("data"),
                                         "10년국채선물지수", "10년국채선물지수",
                                         trading_days=_kr_trading_days(series, prev))),
        name="10년 국채선물지수", unit="", decimals=2,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")

    jobs["kospi_credit"] = dict(
        fn=(lambda: kr_credit_balance("kospi", prev.get("kospi_credit", {}).get("data"),
                                      trading_days=_kr_trading_days(series, prev))),
        name="코스피 신용융자 잔고", unit="조원", decimals=2,
        threshold=None, below_is=None, freq="daily",
        source="금융투자협회 FreeSIS (신용공여 잔고추이)", source_url=S.KOFIA_REF_URL,
        note="증권사가 개인 투자자에게 빌려준 주식 매수 자금(신용거래융자) 잔고. "
             "협회 발표가 영업일 기준 1~2일 늦습니다.")
    jobs["kosdaq_credit"] = dict(
        fn=(lambda: kr_credit_balance("kosdaq", prev.get("kosdaq_credit", {}).get("data"),
                                      trading_days=_kr_trading_days(series, prev))),
        name="코스닥 신용융자 잔고", unit="조원", decimals=2,
        threshold=None, below_is=None, freq="daily",
        source="금융투자협회 FreeSIS (신용공여 잔고추이)", source_url=S.KOFIA_REF_URL,
        note="증권사가 개인 투자자에게 빌려준 주식 매수 자금(신용거래융자) 잔고. "
             "협회 발표가 영업일 기준 1~2일 늦습니다.")

    for mkt, idx_name in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        jobs[f"{mkt}_credit_combo"] = dict(
            fn=(lambda m=mkt, n=idx_name: credit_index_combo(
                series, prev, f"{m}_credit", m, f"{n} 신용잔고·지수")),
            name=f"{idx_name} 신용잔고 낙폭 · {idx_name}지수", unit="%", decimals=1,
            delta_unit="%p",
            threshold=None, below_is=None, freq="daily",
            source="금융투자협회 FreeSIS · KRX / Yahoo Finance", source_url=S.KOFIA_REF_URL,
            kind="dual", price_label=f"{idx_name}지수", price_unit="", price_decimals=0,
            osc_legend="신용잔고 낙폭 (%)", price_legend=f"{idx_name}지수",
            osc_color="var(--ink)", osc_width=2.4, fixed_period_months=12, legend_bottom=True,
            osc_ref=CREDIT_FLUSH_LINE[mkt],
            note="")   # 현재 잔고·고점은 credit_index_combo 가 채운다

    for mkt, idx_name, src in (("spx", "S&P 500", "Yahoo Finance / Stooq"),
                               ("ndx", "나스닥 종합", "Yahoo Finance / Stooq"),
                               ("kospi", "코스피지수", "KRX / Yahoo Finance"),
                               ("kosdaq", "코스닥지수", "KRX / Yahoo Finance")):
        jobs[f"{mkt}_drawdown"] = dict(
            fn=(lambda m=mkt, n=idx_name: index_drawdown(series, prev, m, f"{n} 낙폭")),
            name=f"{idx_name} 낙폭 (1년 고점 대비)", unit="%", decimals=1, delta_unit="%p",
            threshold=None, below_is=None, osc_ref=DRAWDOWN_LINE[mkt],
            freq="daily", source=src, source_url="",
            # 듀얼 차트 틀(월 눈금·격자·기준선)만 빌려 쓰고 지수 선은 그리지 않는다
            kind="dual", osc_legend="지수 낙폭 (%)",
            osc_color="var(--ink)", osc_width=2.4, legend_bottom=True,
            fixed_period_months=12, note="")   # 현재 지수·고점은 index_drawdown 이 채운다

    for mkt, idx_name, src in (("ndx", "나스닥", "Yahoo Finance / Stooq"),
                               ("kospi", "코스피", "KRX / Yahoo Finance")):
        jobs[f"{mkt}_rsi"] = dict(
            fn=(lambda m=mkt, n=idx_name: index_rsi(series, prev, m, f"{n} RSI")),
            name=f"{idx_name} RSI (14일) · {idx_name}지수", unit="", decimals=1, delta_unit="p",
            threshold=None, below_is=None, osc_ref=[30, 70], osc_range=[0, 100],
            freq="daily", source=f"{src} (커스텀 계산)", source_url="",
            kind="dual", price_label=f"{idx_name}지수", price_unit="", price_decimals=0,
            osc_legend="RSI (14)", price_legend=f"{idx_name}지수",
            osc_color="var(--ink)", osc_width=1.8, fixed_period_months=12,
            note="70 이상 과매수, 30 이하 과매도")

    # Buy-the-Dip 점수 재료 (LAYOUT 에는 없는 내부 전용 시리즈)
    for key, sym in (("spy", "SPY"), ("qqq", "QQQ")):
        jobs[key] = dict(
            # 점수 계산에는 1년 고점·주봉 RSI 용 약 3년치면 충분해서 그만큼만 남긴다
            fn=(lambda k=key, y=sym: index_series(dict(yahoo=y, stooq=f"{y.lower()}.us"),
                                                  prev_data=prev.get(k, {}).get("data"))[-800:]),
            name=f"{sym} 종가 (내부용)", unit="", decimals=2,
            threshold=None, below_is=None, freq="daily", source="Yahoo Finance", source_url="")
    for key, uni, label in (("breadth_spx", "spx", "S&P 500"), ("breadth_ndx", "ndx", "Nasdaq-100"),
                            ("breadth_kospi", "kospi", "코스피 시총 상위 200"),
                            ("breadth_kosdaq", "kosdaq", "코스닥 시총 상위 200")):
        jobs[key] = dict(
            fn=(lambda k=key, u=uni: market_breadth(u, prev.get(k))),
            name=f"{label} 200일선 위 종목 비율 (내부용)", unit="", decimals=3,
            threshold=None, below_is=None, freq="daily",
            source="Yahoo Finance (구성종목 계산)", source_url="")

    # LAYOUT 에는 없는 내부 전용 시리즈 — kospi_fg_osc 계산에만 쓰인다.
    jobs["kospi200_call_vol"] = dict(
        fn=(lambda: kospi200_option_volume(prev.get("kospi200_call_vol", {}).get("data"), "call",
                                           trading_days=_kr_trading_days(series, prev))),
        name="코스피200 옵션 콜 거래량 (내부용)", unit="", decimals=0,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")
    jobs["kospi200_put_vol"] = dict(
        fn=(lambda: kospi200_option_volume(prev.get("kospi200_put_vol", {}).get("data"), "put",
                                           trading_days=_kr_trading_days(series, prev))),
        name="코스피200 옵션 풋 거래량 (내부용)", unit="", decimals=0,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")

    jobs["kospi_fg_osc"] = dict(
        fn=(lambda: kospi_fear_greed_osc(series, prev)),
        name="Fear & Greed 오실레이터 (KOSPI)", unit="", decimals=3,
        threshold=0, below_is="bad", freq="daily",
        source="KRX (커스텀 계산)", source_url="",
        kind="dual", price_label="KOSPI", price_unit="", price_decimals=0,
        osc_legend="Fear & Greed Oscillator (KOSPI)", price_legend="KOSPI Index",
        fixed_period_months=6)

    # Buy-the-Dip 점수 — 재료(지수·VKOSPI·F&G·신용잔고·시장 폭)가 모두 갱신된 뒤에 계산
    for key, mkt, label, src_txt in (
            ("dip_spy", "spy", "S&P 500 (SPY)", "Yahoo · CBOE · CNN · FRED (커스텀 계산)"),
            ("dip_qqq", "qqq", "Nasdaq-100 (QQQ)", "Yahoo · CBOE · CNN · FRED (커스텀 계산)"),
            ("dip_kospi", "kospi", "코스피", "KRX · 금융투자협회 · 네이버 · Yahoo (커스텀 계산)"),
            ("dip_kosdaq", "kosdaq", "코스닥", "KRX · 금융투자협회 · 네이버 · Yahoo (커스텀 계산)")):
        price_label = mkt.upper() if mkt in ("spy", "qqq") else label
        jobs[key] = dict(
            fn=(lambda m=mkt: dip_score(series, prev, m)),
            name=f"Buy-the-Dip 점수 · {label}", unit="점", decimals=0, delta_unit="점",
            threshold=None, below_is=None,
            osc_range=[0, 100], freq="daily", source=src_txt, source_url="",
            kind="dual", price_label=price_label, price_unit="", price_decimals=0,
            osc_legend="점수", price_legend=price_label,
            osc_color="var(--ink)", osc_width=2.0, fixed_period_months=12,
            note="")   # 세부 지표는 dip_score 가 채운다

    return jobs


def run(only: set[str] | None, check_only: bool) -> int:
    prev = {}
    if OUT.exists():
        prev = json.loads(OUT.read_text(encoding="utf-8")).get("series", {})

    series: dict = {}
    jobs = build_jobs(prev, series)
    failures = []

    for key, job in jobs.items():
        if only and key not in only:
            if key in prev:
                series[key] = prev[key]
            continue

        print(f"[{key}] {job['name']} …", flush=True)
        try:
            result = job["fn"]()
            extra = {}
            if isinstance(result, tuple):
                result, extra = result

            entry = {k: v for k, v in job.items() if k != "fn"}
            entry.update(extra)
            entry["data"] = result
            entry["last_date"] = result[-1][0]
            entry["last_value"] = result[-1][1]
            entry.setdefault("stale", False)
            series[key] = entry
            print(f"    ✓ {len(result):,}건, 최신 {result[-1][0]} = {result[-1][1]}", flush=True)

        except Exception as e:
            failures.append((key, str(e)))
            print(f"    ✗ {e}", flush=True)
            if key in prev:
                series[key] = {**prev[key], "stale": True, "error": str(e)}
                print("      → 직전 데이터 유지 (stale)", flush=True)

    print()
    print(f"성공 {len(jobs) - len(failures)} / {len(jobs)}")
    for k, e in failures:
        print(f"  실패 · {k}: {e[:160]}")

    if check_only:
        print("\n--check 모드라 파일을 쓰지 않았습니다.")
        return 1 if failures else 0

    if not series:
        print("수집된 지표가 하나도 없어 기존 파일을 보존합니다.")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(
        generated_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        layout=S.LAYOUT,
        series=series)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    size = OUT.stat().st_size / 1024
    print(f"\n{OUT} 저장 완료 ({size:,.0f} KB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="소스 점검만 하고 파일은 쓰지 않음")
    ap.add_argument("--only", help="쉼표로 구분한 지표 키만 갱신")
    ap.add_argument("--group", help="sources.LAYOUT 의 그룹 이름만 갱신 (예: '미국 시장', '국내 시장')")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None

    if a.group:
        grp = next((g for g in S.LAYOUT if g["group"] == a.group), None)
        if not grp:
            names = ", ".join(g["group"] for g in S.LAYOUT)
            print(f"알 수 없는 그룹: '{a.group}' (사용 가능: {names})")
            return 1
        group_keys = set(grp["keys"]) | S.INTERNAL_GROUP_EXTRAS.get(a.group, set())
        only = (only & group_keys) if only else group_keys

    try:
        return run(only, a.check)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
