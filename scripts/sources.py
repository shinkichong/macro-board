"""
지표 레지스트리.

차트를 추가/수정하려면 이 파일만 고치면 됩니다.
fetch_data.py 는 여기 정의된 대로 수집하고, index.html 은 결과 JSON 을 그대로 그립니다.

threshold  : 차트에 기준선을 긋고 그 아래/위를 음영 처리할 값 (None 이면 기준선 없음)
below_is   : 기준선 아래 구간의 의미. "good" | "bad" | None
unit       : 값 뒤에 붙일 단위
decimals   : 표시 소수점 자리
"""

# ─────────────────────────────────────────────────────────────
# FRED: API 키 없이 fredgraph.csv 로 받습니다.
# ─────────────────────────────────────────────────────────────
FRED = {
    "vix":        dict(id="VIXCLS",           name="VIX 지수",            unit="",   decimals=2, threshold=20,  below_is="good"),
    "ust10y":     dict(id="DGS10",            name="미국 10년물 국채금리", unit="%",  decimals=2, threshold=None, below_is=None),
    "hy_yield":   dict(id="BAMLH0A0HYM2EY",   name="미국 하이일드 금리",   unit="%",  decimals=2, threshold=None, below_is=None),
    "yc_10y2y":   dict(id="T10Y2Y",           name="장단기 금리차 (10Y-2Y)", unit="%p", decimals=2, threshold=0,  below_is="bad"),
}

# ─────────────────────────────────────────────────────────────
# 주가지수: Yahoo v8 chart → 실패 시 Stooq CSV
# ─────────────────────────────────────────────────────────────
INDICES = {
    "spx":    dict(yahoo="^GSPC", stooq="^spx",  name="S&P 500",  unit="",   decimals=0),
    "ndx":    dict(yahoo="^IXIC", stooq="^ndq",  name="나스닥 종합", unit="",   decimals=0),
    "kospi":  dict(yahoo="^KS11", stooq="^kospi",  name="코스피",  unit="",   decimals=2),
    "kosdaq": dict(yahoo="^KQ11", stooq="^kosdaq", name="코스닥",  unit="",   decimals=2),
}

# ─────────────────────────────────────────────────────────────
# OECD 경기선행지수 (CLI)
#
# OECD 는 2024년에 SDMX 구조를 개편했고 FRED 미러(G7LOLITONOSTSAM 등)는
# 업데이트가 멈췄습니다. 아래 후보 URL 을 순서대로 시도해서 먼저 응답하는 걸 씁니다.
#
# 키가 안 맞으면 OECD Data Explorer 에서 원하는 계열을 고른 뒤
#   Download ▸ "Copy API link" 를 눌러 나온 URL 을 맨 앞에 추가하세요.
#   https://data-explorer.oecd.org/  (Composite leading indicators)
# ─────────────────────────────────────────────────────────────
OECD_CLI_CANDIDATES = [
    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1"
    "/USA.M.LI...AA...H?startPeriod={start}&dimensionAtObservation=AllDimensions&format=csvfilewithlabels",

    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1"
    "/USA.M.LI...AA...?startPeriod={start}&format=csvfilewithlabels",

    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.0"
    "/USA.M.LI...AA...H?startPeriod={start}&format=csvfilewithlabels",
]

# ─────────────────────────────────────────────────────────────
# ISM 제조업 PMI
#
# 2016년 6월 FRED 에서 ISM 22개 시리즈가 전부 삭제됐습니다(라이선스).
# 현재 무료 경로는 DBnomics 미러가 사실상 유일합니다.
# 데이터 소유권은 ISM 에 있으니 공개 배포 시 출처를 표기하세요.
# ─────────────────────────────────────────────────────────────
ISM_CANDIDATES = ["ISM/pmi/pm", "ISM/pmi/PM"]

# ─────────────────────────────────────────────────────────────
# 글로벌 M2
#
# 각국 M2 를 자국통화로 받아 → 그 시점 환율로 USD 환산 → 합산 → YoY.
# FRED 의 해외 M2 (MYAGM2*) 는 전부 중단됐으므로 DBnomics 를 경유합니다.
#
# series_ids 는 후보 목록입니다. 첫 번째로 데이터가 나오는 걸 씁니다.
# 전부 실패하면 DBnomics 검색으로 대체 후보를 찾아 로그에 출력하니,
#   python scripts/fetch_data.py --check
# 로 확인한 뒤 여기에 고정해 주세요.
#
# scale : 원계열을 "자국통화 1단위" 로 맞추기 위한 배수
#         (예: 백만 유로 단위로 오는 계열이면 1e6)
# ─────────────────────────────────────────────────────────────
M2_COMPONENTS = {
    "US": dict(
        label="미국",
        fred="M2SL",              # 십억 달러, 계절조정
        scale=1e9,
        fx=None,                  # 이미 달러
        search="united states M2 money stock monthly",
    ),
    "EA": dict(
        label="유로존",
        series_ids=[
            "ECB/BSI/M.U2.Y.V.M20.X.1.U2.2300.Z01.E",
            "ECB/BSI/M.U2.Y.V.M30.X.1.U2.2300.Z01.E",   # M3 (M2 없을 때 대체)
        ],
        scale=1e6,                # 백만 유로
        fx="DEXUSEU",             # USD per EUR  → 곱하기
        fx_mode="multiply",
        search="euro area M2 monetary aggregate outstanding",
    ),
    # 중국(CN) 은 뺐습니다. PBOC/IMF 의 DBnomics 미러가 모두 끊겼고,
    # 유일하게 남은 NBS 소스는 최근 13개월치만 제공해 장기 YoY 계산에 못 씁니다.
    # (2026-09 확인) 더 나은 소스를 찾으면 위 EA/JP 항목과 같은 형태로 다시 추가하세요.
    # DBnomics 의 BOJ/MD02 (2024-06 이후 미갱신) 와 IMF/IFS (2025-02 에서 멈춤) 가
    # 모두 죽어서, OECD 의 새 SDMX API(sdmx.oecd.org, Monetary aggregates)로 옮겼습니다.
    # 일본은 M2 를 따로 발표하지 않아 M3(광의통화)로 대체합니다 — 위 EA 와 같은 처리.
    "JP": dict(
        label="일본",
        oecd_monagg=dict(area="JPN", measure="MABM", unit="XDC", adjustment="N"),
        scale=1e6,                # 백만 엔 (UNIT_MULT=6)
        fx="DEXJPUS",             # JPY per USD  → 나누기
        fx_mode="divide",
        search="japan M3 broad money OECD monetary aggregates",
    ),
}

# ─────────────────────────────────────────────────────────────
# CNN Fear & Greed  (비공식 엔드포인트, 2020-08 이후만 제공)
# ─────────────────────────────────────────────────────────────
CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"

# ─────────────────────────────────────────────────────────────
# VKOSPI
#
# 두 경로를 함께 씁니다.
#
#  1) KRX 공식 오픈API — 인증키가 있으면 우선 사용. 하루치씩만 주므로
#     최초 적재가 아니라 매일 증분 갱신에 씁니다.
#       · https://openapi.krx.co.kr 에서 무료 인증키 발급
#       · 발급 후 "서비스 이용" 에서 [파생상품지수 일별시세] 개별 신청 → 담당자 승인 필요
#       · 승인되면 환경변수 KRX_API_KEY 에 넣어두세요 (Actions 는 Secrets)
#       · 제공 기간은 2010년 이후
#
#  2) KRX 정보데이터시스템 — 인증키 없이 기간 조회가 되므로 과거치 적재용.
#     비공식 내부 엔드포인트라 bld 값이 바뀌면 깨집니다. 그때는
#     data.krx.co.kr 에서 변동성지수 시세 화면을 연 뒤 F12 ▸ Network 에서
#     getJsonData.cmd 요청의 payload 를 복사해 아래에 넣으세요.
#
# Investing.com 에도 값이 있지만 수집 소스로는 쓰지 않습니다.
# Cloudflare 가 데이터센터 IP 를 막고, 약관이 자동 추출을 금지하며,
# 애초에 그쪽도 KRX 를 받아 보여주는 중간 배포자입니다.
# 대신 눈으로 대조할 참고 링크로만 카드에 걸어둡니다.
# ─────────────────────────────────────────────────────────────
KRX_OPENAPI_BASE = "https://data-dbg.krx.co.kr/svc/apis"
KRX_OPENAPI_PATH = "/idx/drvprod_dd_trd"      # 파생상품지수 일별시세
VKOSPI_NAME_MATCH = ("변동성지수", "VKOSPI")   # 응답 행에서 VKOSPI 를 골라내는 이름 조각

KRX_JSON_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_REFERER = "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"

VKOSPI_PAYLOAD_CANDIDATES = [
    # (bld, 추가 파라미터)
    ("dbms/MDC/STAT/standard/MDCSTAT00601", {"indIdx": "5", "indIdx2": "300",
                                             "tboxindIdx_finder_equidx0_2": "코스피 200 변동성지수"}),
    ("dbms/MDC/STAT/standard/MDCSTAT00301", {"indIdx": "5", "indIdx2": "300"}),
]

VKOSPI_REF_URL = "https://kr.investing.com/indices/kospi-volatility"

# DBnomics의 ISM 미러가 깨졌을 때 눈으로 최신값을 대조할 참고 링크.
# investing.com 자체를 수집 소스로 쓰지는 않습니다 — 자동 추출 금지 약관,
# Cloudflare 의 데이터센터 IP 차단, 그리고 어차피 ISM 을 받아 보여주는
# 재배포자일 뿐이라는 점 때문입니다 (VKOSPI 와 동일한 이유).
ISM_REF_URL = "https://kr.investing.com/economic-calendar/ism-manufacturing-pmi-173"

# ─────────────────────────────────────────────────────────────
# 화면 구성 — index.html 이 이 순서/그룹대로 카드를 그립니다.
# ─────────────────────────────────────────────────────────────
LAYOUT = [
    dict(group="미국 시장", keys=[
        "spx", "ndx", "fear_greed", "spx_fg_osc", "ndx_fg_osc", "vix",
        "ust10y", "hy_yield", "yc_10y2y",
        "global_m2_yoy", "oecd_cli", "ism_pmi",
    ]),
    dict(group="국내 시장", keys=["kospi", "kosdaq", "vkospi"]),
]

# ─────────────────────────────────────────────────────────────
# 커스텀 Fear & Greed 오실레이터 (S&P500 / NASDAQ)
#
# src/미국 피어앤그리드 오실레이터_yahoo.txt 의 계산을 이식한 것.
# 모멘텀·RSI·VIX·금리스프레드(10Y-5Y)·리스크선호(HYG/IEF)를 정규화해
# 합성한 뒤 MACD 방식으로 오실레이터화한다. HYG/IEF 는 이 계산에만 쓰는
# 보조 입력이라 따로 카드로 만들지 않는다.
# ─────────────────────────────────────────────────────────────
FG_HYG = dict(yahoo="HYG", stooq="hyg.us")
FG_IEF = dict(yahoo="IEF", stooq="ief.us")
FG_CALC_START = "2024-01-01"   # 원본 스크립트와 동일 — 정규화 기준 구간
