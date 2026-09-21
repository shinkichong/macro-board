#!/usr/bin/env python3
"""
데모용 가짜 데이터 생성기.

실제 수집(fetch_data.py)이 한 번도 돌지 않았을 때 화면을 확인하기 위한 것입니다.
여기서 나온 숫자는 전부 난수이며 실제 시장 데이터가 아닙니다.
"""
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import sources as S

random.seed(7)
OUT = Path(__file__).resolve().parent.parent / "data" / "macro.json"
TODAY = date(2026, 9, 15)


def daily(years, start_val, drift, vol, floor=None, cap=None, mean_rev=None):
    days, v, out = int(years * 365), start_val, []
    d0 = TODAY - timedelta(days=days)
    for i in range(days):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        shock = random.gauss(0, vol)
        if mean_rev is not None:
            shock += (mean_rev - v) * 0.012
        v = v * (1 + drift) + shock
        if floor is not None:
            v = max(floor, v)
        if cap is not None:
            v = min(cap, v)
        out.append([d.isoformat(), round(v, 4)])
    return out


def monthly(years, start_val, vol, mean_rev, floor=None, cap=None):
    v, out = start_val, []
    y, m = TODAY.year - int(years), TODAY.month
    for _ in range(int(years * 12)):
        v += random.gauss(0, vol) + (mean_rev - v) * 0.08
        if floor is not None:
            v = max(floor, v)
        if cap is not None:
            v = min(cap, v)
        out.append([f"{y:04d}-{m:02d}-01", round(v, 3)])
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out[:-1]          # 월별 지표는 최근 1개월 발표 지연이 정상


SPEC = {
    "spx":           daily(12, 2000, 0.00035, 14, floor=600),
    "ndx":           daily(12, 4600, 0.00045, 45, floor=1200),
    "fear_greed":    daily(5, 52, 0, 4.5, floor=3, cap=97, mean_rev=52),
    "vix":           daily(12, 17, 0, 0.9, floor=9, cap=80, mean_rev=17.5),
    "ust10y":        daily(12, 2.4, 0, 0.035, floor=0.3, cap=6, mean_rev=3.9),
    "hy_yield":      daily(12, 3.8, 0, 0.05, floor=1.5, cap=12, mean_rev=4.2),
    "yc_10y2y":      daily(12, 1.6, 0, 0.025, floor=-1.5, cap=3, mean_rev=0.35),
    "us_cpi_yoy":    monthly(18, 2.8, 0.3, 1.0, floor=0, cap=9),
    "real_policy_rate": monthly(18, 0.3, 0.15, 0.5, floor=-3, cap=4),
    "global_m2_yoy": monthly(18, 6.0, 1.6, 5.5, floor=-9, cap=22),
    "oecd_cli":      monthly(18, 100, 0.35, 100, floor=95, cap=104),
    "ism_pmi":       monthly(18, 52, 1.5, 51, floor=33, cap=62),
    "kospi":         daily(12, 1900, 0.00032, 17, floor=700),
    "kosdaq":        daily(12, 560, 0.00030, 7, floor=280),
    "vkospi":        daily(12, 16, 0, 0.8, floor=8, cap=70, mean_rev=17),
    "kr_exports_yoy": monthly(18, 5.0, 1.8, 8.0, floor=-25, cap=40),
}

META = {
    "spx":           dict(name="S&P 500", unit="", decimals=0, threshold=None, below_is=None, freq="daily", source="Yahoo Finance",
                          card_url="https://stock.naver.com/worldstock/index/.INX/price"),
    "ndx":           dict(name="나스닥 종합", unit="", decimals=0, threshold=None, below_is=None, freq="daily", source="Yahoo Finance",
                          card_url="https://stock.naver.com/worldstock/index/.IXIC/price"),
    "fear_greed":    dict(name="Fear & Greed", unit="", decimals=0, threshold=50, below_is="bad", freq="daily", source="CNN",
                          bands=[[0, 25, "극단적 공포"], [25, 45, "공포"], [45, 55, "중립"], [55, 75, "탐욕"], [75, 100, "극단적 탐욕"]]),
    "vix":           dict(name="VIX 지수", unit="", decimals=2, threshold=20, below_is="good", freq="daily", source="FRED"),
    "ust10y":        dict(name="미국 10년물 국채금리", unit="%", decimals=2, threshold=None, below_is=None, freq="daily", source="FRED"),
    "hy_yield":      dict(name="미국 하이일드 스프레드", unit="%p", decimals=2, threshold=None, below_is=None, freq="daily", source="FRED"),
    "yc_10y2y":      dict(name="장단기 금리차 (10Y-2Y)", unit="%p", decimals=2, threshold=0, below_is="bad", freq="daily", source="FRED"),
    "us_cpi_yoy":    dict(name="미국 CPI (YoY)", unit="%", decimals=2, threshold=2, below_is="good", freq="monthly", source="FRED"),
    "real_policy_rate": dict(name="실질 정책금리 (정책금리 - 헤드라인 PCE)", unit="%p", decimals=2, threshold=0, below_is="bad", freq="monthly",
                          source="FRED (Fed Funds 목표범위 + PCEPI 합성)"),
    "global_m2_yoy": dict(name="글로벌 M2 증감율 (YoY)", unit="%", decimals=2, threshold=0, below_is="bad", freq="monthly",
                          source="FRED · ECB · PBOC · BOJ 합성", note="미국·유로존·중국·일본 M2 를 달러로 환산해 합산한 뒤 전년동월비"),
    "oecd_cli":      dict(name="OECD 경기선행지수 (미국)", unit="", decimals=2, threshold=100, below_is="bad", freq="monthly", source="OECD"),
    "ism_pmi":       dict(name="ISM 제조업지수", unit="", decimals=1, threshold=50, below_is="bad", freq="monthly", source="ISM (DBnomics 경유)",
                          card_url="https://kr.investing.com/economic-calendar/ism-manufacturing-pmi-173"),
    "kospi":         dict(name="코스피", unit="", decimals=2, threshold=None, below_is=None, freq="daily", source="Yahoo Finance / KRX",
                          card_url="https://stock.naver.com/domestic/index/KOSPI/price"),
    "kosdaq":        dict(name="코스닥", unit="", decimals=2, threshold=None, below_is=None, freq="daily", source="Yahoo Finance / KRX",
                          card_url="https://stock.naver.com/domestic/index/KOSDAQ/price"),
    "vkospi":        dict(name="VKOSPI", unit="", decimals=2, threshold=20, below_is="good", freq="daily", source="KRX",
                          ref_url="https://kr.investing.com/indices/kospi-volatility", ref_label="값 대조"),
    "kr_exports_yoy": dict(name="한국 수출증가율 (YoY)", unit="%", decimals=2, threshold=0, below_is="bad", freq="monthly",
                          source="FRED (OECD MEI 경유)"),
}

series = {}
for k, data in SPEC.items():
    series[k] = {**META[k], "data": data, "last_date": data[-1][0],
                 "last_value": data[-1][1], "stale": False}

# 글로벌 M2 는 고정환율 토글용 보조 계열을 함께 갖는다
series["global_m2_yoy"]["fixed_fx"] = [
    [d, round(v + random.gauss(0.8, 0.5), 2)] for d, v in series["global_m2_yoy"]["data"]
]

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(
    dict(generated_at="2026-09-15T21:10:00Z", demo=True, layout=S.LAYOUT, series=series),
    ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"{OUT} — 데모 데이터 {sum(len(v['data']) for v in series.values()):,}건")
