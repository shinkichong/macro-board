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
import io
import json
import os
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

def get(url: str, **kw) -> requests.Response:
    """짧은 지수 백오프를 붙인 GET.

    stream=True 로 받는다 — 사내망 TLS 검사 프록시가 큰 응답을 한 번에
    내려받는 요청에서 연결을 끊는 경우가 있어, 스트리밍으로 우회한다.
    """
    last = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=True, **kw)
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

def fred(series_id: str, start: str = START) -> list[list]:
    """FRED. API 키 없이 fredgraph.csv 로 받는다."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    rows = list(csv.reader(io.StringIO(get(url).text)))
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1] in (".", "", "NA"):
            continue          # FRED 는 휴일을 "." 로 표시한다
        out.append([r[0], float(r[1])])
    if not out:
        raise RuntimeError(f"FRED {series_id}: 값이 비어 있음")
    return out


def yahoo(symbol: str) -> list[list]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range=25y&interval=1d")
    j = get(url).json()["chart"]["result"][0]
    ts = j["timestamp"]
    closes = j["indicators"]["quote"][0]["close"]
    out = [[datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), round(c, 4)]
           for t, c in zip(ts, closes) if c is not None]
    if not out:
        raise RuntimeError(f"Yahoo {symbol}: 값이 비어 있음")
    return out


def stooq(symbol: str) -> list[list]:
    url = f"https://stooq.com/q/d/l/?{urlencode({'s': symbol, 'i': 'd'})}"
    rows = list(csv.reader(io.StringIO(get(url).text)))
    if not rows or rows[0][0].lower() != "date":
        raise RuntimeError(f"Stooq {symbol}: 응답 형식이 예상과 다름")
    return [[r[0], float(r[4])] for r in rows[1:] if len(r) >= 5 and r[4]]


def index_series(cfg: dict) -> list[list]:
    """Yahoo 우선, 실패하면 Stooq."""
    try:
        return yahoo(cfg["yahoo"])
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


def ism_pmi() -> tuple[list[list], dict]:
    for sid in S.ISM_CANDIDATES:
        try:
            raw = dbnomics(sid)
        except Exception:
            continue
        clean, cut_at = _drop_implausible_tail(raw)
        if not clean:
            continue
        if cut_at:
            return clean, {"stale": True,
                           "note": f"DBnomics ISM 미러가 {cut_at} 이후 비정상 값을 내놓기 "
                                   "시작해 그 이전 마지막 정상값에서 멈춰 있습니다."}
        return clean, {}
    raise RuntimeError("ISM PMI 수집 실패. DBnomics 후보: "
                       + ", ".join(dbnomics_search("ISM manufacturing PMI")))


def fear_greed() -> list[list]:
    """CNN 내부 엔드포인트. 브라우저처럼 보이는 헤더가 없으면 거부당한다."""
    url = S.CNN_FG_URL.format(start="2020-09-19")
    r = get(url, headers={"User-Agent": UA,
                          "Referer": "https://edition.cnn.com/markets/fear-and-greed",
                          "Origin": "https://edition.cnn.com"})
    hist = r.json()["fear_and_greed_historical"]["data"]
    return [[datetime.utcfromtimestamp(p["x"] / 1000).strftime("%Y-%m-%d"),
             round(float(p["y"]), 2)] for p in hist]


def vkospi(prev_data: list[list] | None = None) -> list[list]:
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
    missing = _business_days_since(max(have) if have else None)

    if key and missing:
        got = 0
        for day in missing[-40:]:                       # 한 번에 최대 40일치만
            v = _vkospi_openapi(day, key)
            if v is not None:
                have[day] = v
                got += 1
            time.sleep(0.25)
        print(f"    오픈API 로 {got}일치 추가", flush=True)
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
    return dedupe([[d, v] for d, v in have.items()])


def _business_days_since(last: str | None) -> list[str]:
    """마지막 수집일 다음 영업일부터 오늘까지. (휴장일은 응답이 비어 자연히 걸러진다)"""
    start = date.fromisoformat(last) + timedelta(days=1) if last else date.today() - timedelta(days=7)
    out, d = [], start
    while d <= date.today():
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _vkospi_openapi(day: str, key: str) -> float | None:
    """KRX 공식 오픈API — 파생상품지수 일별시세에서 VKOSPI 행을 찾는다."""
    url = S.KRX_OPENAPI_BASE + S.KRX_OPENAPI_PATH
    try:
        r = session.get(url, params={"basDd": day.replace("-", "")},
                        headers={"AUTH_KEY": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        rows = r.json().get("OutBlock_1") or []
    except Exception:
        return None
    for row in rows:
        name = row.get("IDX_NM") or row.get("IDX_NAME") or ""
        if any(frag in name for frag in S.VKOSPI_NAME_MATCH):
            raw = str(row.get("CLSPRC_IDX") or row.get("CLSPRC") or "").replace(",", "")
            if raw and raw not in ("-", ""):
                return float(raw)
    return None


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

def build_jobs(prev: dict) -> dict:
    jobs = {}

    for key, cfg in S.FRED.items():
        jobs[key] = dict(
            fn=(lambda c=cfg: fred(c["id"])),
            name=cfg["name"], unit=cfg["unit"], decimals=cfg["decimals"],
            threshold=cfg["threshold"], below_is=cfg["below_is"],
            freq="daily", source="FRED",
            source_url=f"https://fred.stlouisfed.org/series/{cfg['id']}")

    for key, cfg in S.INDICES.items():
        jobs[key] = dict(
            fn=(lambda c=cfg: index_series(c)),
            name=cfg["name"], unit=cfg["unit"], decimals=cfg["decimals"],
            threshold=None, below_is=None,
            freq="daily", source="Yahoo Finance / Stooq", source_url="")

    jobs["fear_greed"] = dict(
        fn=fear_greed, name="Fear & Greed", unit="", decimals=0,
        threshold=50, below_is="bad", freq="daily", source="CNN",
        source_url="https://edition.cnn.com/markets/fear-and-greed",
        bands=[[0, 25, "극단적 공포"], [25, 45, "공포"], [45, 55, "중립"],
               [55, 75, "탐욕"], [75, 100, "극단적 탐욕"]])

    jobs["oecd_cli"] = dict(
        fn=oecd_cli, name="OECD 경기선행지수 (미국)", unit="", decimals=2,
        threshold=100, below_is="bad", freq="monthly", source="OECD",
        source_url="https://data-explorer.oecd.org/")

    jobs["ism_pmi"] = dict(
        fn=ism_pmi, name="ISM 제조업지수", unit="", decimals=1,
        threshold=50, below_is="bad", freq="monthly", source="ISM (DBnomics 경유)",
        source_url="https://db.nomics.world/ISM/pmi",
        ref_url=S.ISM_REF_URL, ref_label="값 대조")

    jobs["global_m2_yoy"] = dict(
        fn=global_m2, name="글로벌 M2 증감율 (YoY, 중국 제외)", unit="%", decimals=2,
        threshold=0, below_is="bad", freq="monthly",
        source="FRED · ECB · BOJ 합성",
        source_url="", note="미국·유로존·일본 M2 를 달러로 환산해 합산한 뒤 전년동월비. "
                            "중국은 소스 단절로 제외 (scripts/sources.py 참고)")

    jobs["vkospi"] = dict(
        fn=(lambda: vkospi(prev.get("vkospi", {}).get("data"))),
        name="VKOSPI", unit="", decimals=2,
        threshold=20, below_is="good", freq="daily", source="KRX",
        source_url="http://data.krx.co.kr/",
        ref_url=S.VKOSPI_REF_URL, ref_label="값 대조")

    return jobs


def run(only: set[str] | None, check_only: bool) -> int:
    prev = {}
    if OUT.exists():
        prev = json.loads(OUT.read_text(encoding="utf-8")).get("series", {})

    jobs = build_jobs(prev)
    series, failures = {}, []

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
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    try:
        return run(only, a.check)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
