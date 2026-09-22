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

def fred(series_id: str, start: str = START) -> list[list]:
    """FRED. API 키 없이 fredgraph.csv 로 받는다.

    GitHub Actions 같은 클라우드 IP 대역에서는 FRED 가 응답 없이 45초씩
    물고 있다가 실패하는 경우를 봐서(연결 자체는 되니 재시도해도 잘 안 풀린다),
    타임아웃과 재시도 횟수를 짧게 줘서 실패할 때 빨리 넘어가게 한다.
    """
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    rows = list(csv.reader(io.StringIO(get(url, timeout=15, retries=2).text)))
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
        "vix": fred("VIXCLS", S.FG_CALC_START),
        "dgs10": fred("DGS10", S.FG_CALC_START),
        "dgs5": fred("DGS5", S.FG_CALC_START),
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
    두 FRED 시리즈를 공통 날짜로 정렬한다.

    BAMLH0A0HYM2 는 ICE Data Indices 라이선스 계열이라 FRED 무료 CSV 는
    cosd 를 아무리 과거로 줘도 최근 ~3년치만 돌려준다(승인 없이는 그 이상 불가).
    그래서 새로 받은 값과 이전에 저장해둔 값을 합쳐서 쓴다 — 한 번 확보한
    날짜는 FRED 창에서 밀려나도 우리 쪽 기록에 남아, 시간이 지날수록
    보이는 구간이 넓어진다(다시 좁아지지는 않는다).
    """
    ust10y = {d: v for d, v in fred(S.FRED["ust10y"]["id"])}
    hy = {d: v for d, v in fred(S.FRED["hy_yield"]["id"])}
    for d, v in (prev_rate or []):
        ust10y.setdefault(d, v)
    for d, v in (prev_spread or []):
        hy.setdefault(d, v)
    dates = sorted(set(ust10y) & set(hy))
    if len(dates) < 30:
        raise RuntimeError(f"국채금리·하이일드 스프레드: 공통 거래일이 {len(dates)}개뿐 — 계산 불가")
    rate = [[d, ust10y[d]] for d in dates]
    spread = [[d, hy[d]] for d in dates]
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
    무료 인증키가 필요하다 (https://ecos.bok.or.kr, 즉시 자동 발급)."""
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        raise RuntimeError("ECOS_API_KEY 가 필요합니다.")
    end = date.today().strftime("%Y%m")
    start_ym = start.replace("-", "")[:6]
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1000/"
           f"{stat_code}/M/{start_ym}/{end}/{item_code}")
    body = get(url).json()
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
    """한국 수출증가율(YoY).

    한국은행 ECOS 수출금액지수(403Y001, 총지수 *AA)로 직접 전년동월비를
    계산한다 — 관세청 통관 실적 기반 원자료라 갱신이 빠르고, OECD MEI 를
    FRED 가 미러링하던 이전 소스(XTEXVA01KRM659S)보다 최신이다.

    ECOS_API_KEY 가 없거나 ECOS 호출이 실패하면 이전 FRED 방식으로 떨어진다.
    다만 그 미러는 2026-06 이후로 원본 자체가 갱신을 멈춘 상태다(README 참고).
    이전 값과 병합해 과거치가 사라지지 않게 한다.
    """
    try:
        idx = _ecos_series("403Y001", "*AA", START_MONTHLY)
        return yoy(idx)
    except Exception as e:
        print(f"    ECOS 실패({e}) → FRED(OECD 미러) 로 대체", flush=True)

    fresh = {d: v for d, v in fred("XTEXVA01KRM659S", START_MONTHLY)}
    for d, v in (prev_data or []):
        fresh.setdefault(d, v)
    return [[d, fresh[d]] for d in sorted(fresh)]


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


def _krx_idx_value(day: str, key: str, path: str, idx_name: str) -> float | None:
    """KRX 공식 오픈API 지수 시세정보(파생상품지수/유가증권지수 공용)에서
    IDX_NM 이 정확히 일치하는 행의 종가.
    (VKOSPI 는 이름이 조금씩 바뀌어 부분일치를 쓰지만, 그 외 지수류는
    이름이 안정적이라 오탐 방지를 위해 정확히 일치하는 것만 고른다.)"""
    url = S.KRX_OPENAPI_BASE + path
    try:
        r = session.get(url, params={"basDd": day.replace("-", "")},
                        headers={"AUTH_KEY": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        rows = r.json().get("OutBlock_1") or []
    except Exception:
        return None
    for row in rows:
        if row.get("IDX_NM") == idx_name:
            raw = str(row.get("CLSPRC_IDX") or "").replace(",", "")
            if raw and raw not in ("-", ""):
                return float(raw)
    return None


def drvprod_index_series(prev_data: list[list] | None, idx_name: str, label: str) -> list[list]:
    """파생상품지수 일별시세에서 임의의 지수를 하루씩 증분 수집한다 (VKOSPI 와 동일한 패턴).
    과거치를 한 번에 적재할 무료 경로가 없어 처음 수집한 날부터 하루씩 쌓인다."""
    have = {d: v for d, v in (prev_data or [])}
    key = os.environ.get("KRX_API_KEY")
    if not key:
        if have:
            return dedupe([[d, v] for d, v in have.items()])
        raise RuntimeError(f"{label} 수집 실패: KRX_API_KEY 가 필요합니다.")

    missing = _business_days_since(max(have) if have else None)
    got = 0
    for day in missing[-40:]:
        v = _krx_idx_value(day, key, S.KRX_OPENAPI_PATH, idx_name)
        if v is not None:
            have[day] = v
            got += 1
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)

    if not have:
        raise RuntimeError(f"{label} 수집 실패: 유효한 거래일 데이터가 없습니다.")
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
    if key:
        missing = _business_days_since(max(have) if have else None)
        got = 0
        for day in missing[-40:]:
            v = _krx_idx_value(day, key, S.KRX_OPENAPI_PATH_INDEX, S.KOSPI_IDX_NAME)
            if v is not None:
                have[day] = v
                got += 1
            time.sleep(0.25)
        print(f"    KRX 오픈API 로 {got}일치 추가", flush=True)

    try:
        for d, v in index_series(S.INDICES["kospi"]):
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


def _kospi200_opt_volumes(day: str, key: str) -> tuple[int, int] | None:
    """코스피200 옵션(미니/위클리 제외) 콜·풋 당일 총 거래량 합계.
    KRX 오픈API '옵션 일별매매정보 (주식옵션外)' — 종목별로 나오는 걸 PROD_NM 으로 필터해 더한다."""
    url = S.KRX_OPENAPI_BASE + S.KRX_OPT_PATH
    try:
        r = session.get(url, params={"basDd": day.replace("-", "")},
                        headers={"AUTH_KEY": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        rows = r.json().get("OutBlock_1") or []
    except Exception:
        return None
    call_vol = sum(int(row.get("ACC_TRDVOL") or 0) for row in rows
                   if row.get("PROD_NM") == S.KRX_OPT_PROD_NAME and row.get("RGHT_TP_NM") == "CALL")
    put_vol = sum(int(row.get("ACC_TRDVOL") or 0) for row in rows
                  if row.get("PROD_NM") == S.KRX_OPT_PROD_NAME and row.get("RGHT_TP_NM") == "PUT")
    if call_vol == 0 and put_vol == 0:
        return None      # 휴장일 등 — 거래된 게 없으면 그 날은 건너뛴다
    return call_vol, put_vol


def kospi200_option_pcr(prev_data: list[list] | None = None) -> list[list]:
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

    missing = _business_days_since(max(have) if have else None)
    got = 0
    for day in missing[-40:]:
        vols = _kospi200_opt_volumes(day, key)
        if vols and vols[0] > 0:
            have[day] = round(vols[1] / vols[0], 4)
            got += 1
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)

    if not have:
        raise RuntimeError("코스피200 옵션 풋/콜 비율 수집 실패: 유효한 거래일 데이터가 없습니다.")
    return dedupe([[d, v] for d, v in have.items()])


def kospi200_option_volume(prev_data: list[list] | None, side: str) -> list[list]:
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
    missing = _business_days_since(max(have) if have else None)
    got = 0
    for day in missing[-40:]:
        vols = _kospi200_opt_volumes(day, key)
        if vols is not None:
            have[day] = vols[idx]
            got += 1
        time.sleep(0.25)
    print(f"    오픈API 로 {got}일치 추가", flush=True)

    if not have:
        raise RuntimeError(f"코스피200 옵션 {side} 거래량 수집 실패: 유효한 거래일 데이터가 없습니다.")
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
        jobs[key] = dict(
            fn=(lambda c=cfg: fred(c["id"])),
            name=cfg["name"], unit=cfg["unit"], decimals=cfg["decimals"],
            threshold=cfg["threshold"], below_is=cfg["below_is"],
            freq="daily", source="FRED",
            source_url=f"https://fred.stlouisfed.org/series/{cfg['id']}")

    for key, cfg in S.INDICES.items():
        if key == "kospi":
            continue  # 아래에서 KRX 공식 API 우선 + Yahoo/Stooq 보완으로 따로 등록
        jobs[key] = dict(
            fn=(lambda c=cfg: index_series(c)),
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
        threshold=None, below_is=None, freq="daily", source="FRED",
        source_url=f"https://fred.stlouisfed.org/series/{S.FRED['ust10y']['id']}",
        kind="dual", price_label="하이일드 스프레드", price_unit="%p", price_decimals=2,
        osc_legend="미국 10년물 국채금리 (%)", price_legend="하이일드 스프레드 (%p)")

    jobs["oecd_cli"] = dict(
        fn=oecd_cli, name="OECD 경기선행지수 (미국)", unit="", decimals=2,
        threshold=100, below_is="bad", freq="monthly", source="OECD",
        source_url="https://data-explorer.oecd.org/")

    jobs["ism_pmi"] = dict(
        fn=ism_pmi, name="ISM 제조업지수", unit="", decimals=1,
        threshold=50, below_is="bad", freq="monthly", source="ISM (DBnomics 경유)",
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
        source="한국은행 ECOS (수출금액지수 403Y001)",
        source_url="https://ecos.bok.or.kr/#/SearchStat")

    jobs["vkospi"] = dict(
        fn=(lambda: vkospi(prev.get("vkospi", {}).get("data"))),
        name="VKOSPI", unit="", decimals=2,
        threshold=20, below_is="good", freq="daily", source="KRX",
        source_url="http://data.krx.co.kr/",
        ref_url=S.VKOSPI_REF_URL, ref_label="값 대조")

    jobs["kospi200_pcr"] = dict(
        fn=(lambda: kospi200_option_pcr(prev.get("kospi200_pcr", {}).get("data"))),
        name="코스피200 옵션 풋/콜 비율", unit="", decimals=3,
        threshold=1, below_is="good", freq="daily", source="KRX",
        source_url="", fixed_period_months=6,
        note="코스피200 옵션(미니·위클리 제외) 콜·풋 당일 총 거래량 비율(PUT/CALL). "
             "1보다 높으면 풋 거래가 더 많다는 뜻으로 통상 공포 신호로 해석됩니다.")

    jobs["bond5y_futures"] = dict(
        fn=(lambda: drvprod_index_series(prev.get("bond5y_futures", {}).get("data"),
                                         "5년 국채선물 추종 지수", "5년 국채선물 추종 지수")),
        name="5년 국채선물 추종 지수", unit="", decimals=2,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")

    jobs["bond10y_futures"] = dict(
        fn=(lambda: drvprod_index_series(prev.get("bond10y_futures", {}).get("data"),
                                         "10년국채선물지수", "10년국채선물지수")),
        name="10년 국채선물지수", unit="", decimals=2,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")

    # LAYOUT 에는 없는 내부 전용 시리즈 — kospi_fg_osc 계산에만 쓰인다.
    jobs["kospi200_call_vol"] = dict(
        fn=(lambda: kospi200_option_volume(prev.get("kospi200_call_vol", {}).get("data"), "call")),
        name="코스피200 옵션 콜 거래량 (내부용)", unit="", decimals=0,
        threshold=None, below_is=None, freq="daily", source="KRX", source_url="")
    jobs["kospi200_put_vol"] = dict(
        fn=(lambda: kospi200_option_volume(prev.get("kospi200_put_vol", {}).get("data"), "put")),
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
