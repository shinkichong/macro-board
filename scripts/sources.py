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
# FRED 가 응답하지 않을 때(GitHub Actions IP 에서 타임아웃이 잦다) 쓸 대체 소스.
# 모두 FRED 가 받아 오는 원 출처라 값이 같다 (2026-09-30 대조 확인).
#   kind: bls        → BLS 공개 API (키 없음, 요청당 10년)
#         dbnomics   → DBnomics 시리즈 ID
#         fed_ddp    → 연준 Data Download Program CSV (rel, 패키지 해시, 열 ID)
#         nyfed      → 뉴욕연준 EFFR API 의 목표범위 상단(to)/하단(from)
#         treasury   → 미 재무부 일별 수익률곡선 (long - short)
# 여기 없는 시리즈(하이일드 스프레드 등 ICE BofA 계열)는 무료 대체 소스가 없어,
# 환경변수 FRED_API_KEY 가 있으면 FRED 공식 API(api.stlouisfed.org)로만 재시도한다.
# DEXUSEU·DEXJPUS 대체값은 일별이 아니라 H.10 월평균이다 — 월평균으로만 쓰이므로 결과는 같다.
FRED_FALLBACKS = {
    "CPIAUCNS": dict(kind="bls", series="CUUR0000SA0"),
    "PCEPI":    dict(kind="dbnomics", series="BEA/NIPA-T20804/DPCERG-M"),
    "M2SL":     dict(kind="fed_ddp", rel="H6", package="798e2796917702a5f8423426ba7e6b42", column="M2.M"),
    "DEXUSEU":  dict(kind="fed_ddp", rel="H10", package="c5d6e0edf324b2fb28d73bcacafaaa02", column="RXI$US_N.M.EU"),
    "DEXJPUS":  dict(kind="fed_ddp", rel="H10", package="c5d6e0edf324b2fb28d73bcacafaaa02", column="RXI_N.M.JA"),
    "DFEDTARU": dict(kind="nyfed", side="targetRateTo"),
    "DFEDTARL": dict(kind="nyfed", side="targetRateFrom"),
    "T10Y2Y":   dict(kind="treasury", long="10 Yr", short="2 Yr"),
}

# 한국 수출증가율: 관세청 통관 수출액(달러) 기준.
#  · 확정치 — ECOS 901Y118(수출입 총괄, 관세청) 수출금액 T002. 다음 달 중순~말에야 들어온다.
#  · 속보치 — 산업통상부가 매월 1일 발표하는 "OOOO년 O월 수출입 동향" 보도자료.
#    본문이 비어 있고 PDF/HWP 첨부만 있는 달이 많아, 본문 → PDF 순으로 숫자를 찾는다.
#    뉴스에 나오는 "O월 수출 OO% 증가"가 이 값이다.
KR_EXPORTS_ECOS = ("901Y118", "T002")
MOTIE_BOARD_URL = "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c"

FRED = {
    "vix":        dict(id="VIXCLS",           name="VIX 지수",            unit="",   decimals=2, threshold=20,  below_is="good"),
    "ust10y":     dict(id="DGS10",            name="미국 10년물 국채금리", unit="%",  decimals=2, threshold=None, below_is=None),
    "hy_yield":   dict(id="BAMLH0A0HYM2",     name="미국 하이일드 스프레드", unit="%p", decimals=2, threshold=None, below_is=None),
    "yc_10y2y":   dict(id="T10Y2Y",           name="장단기 금리차 (10Y-2Y)", unit="%p", decimals=2, threshold=0,  below_is="bad"),
}

# ─────────────────────────────────────────────────────────────
# 주가지수: Yahoo v8 chart → 실패 시 Stooq CSV
# ─────────────────────────────────────────────────────────────
INDICES = {
    "spx":    dict(yahoo="^GSPC", stooq="^spx",  name="S&P 500",  unit="",   decimals=0,
                   card_url="https://stock.naver.com/worldstock/index/.INX/price"),
    "ndx":    dict(yahoo="^IXIC", stooq="^ndq",  name="나스닥 종합", unit="",   decimals=0,
                   card_url="https://stock.naver.com/worldstock/index/.IXIC/price"),
    "kospi":  dict(yahoo="^KS11", stooq="^kospi",  name="코스피",  unit="",   decimals=2,
                   card_url="https://stock.naver.com/domestic/index/KOSPI/price"),
    "kosdaq": dict(yahoo="^KQ11", stooq="^kosdaq", name="코스닥",  unit="",   decimals=2,
                   card_url="https://stock.naver.com/domestic/index/KOSDAQ/price"),
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
# DBnomics ISM 미러는 2025-09 부터 비정상 값(10~11대)을 내다가 2026-01 이후 갱신이
# 끊겼다. ISM 은 매월 보도자료를 PR Newswire 로 배포하고, 그 제목에 헤드라인 PMI 가
# 들어 있다 ("Manufacturing PMI® at 54.6%; August 2026 ISM® Manufacturing PMI® Report").
# 목록 한 페이지(100건)에 약 2년치가 들어 있어 이걸로 최근 값을 채운다.
ISM_PRN_URL = "https://www.prnewswire.com/news/institute-for-supply-management/?page=1&pagesize=100"

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

# 코스피 지수 자체(카드 + Fear&Greed 오실레이터의 재료)를 KRX 공식 데이터로 우선
# 채우기 위한 엔드포인트. "유가증권지수 시세정보" — 위 파생상품지수와는 별도 상품이라
# openapi.krx.co.kr 에서 별도로 신청·승인받아야 한다(승인 전에는 자동으로 Yahoo/Stooq
# 로만 채워지니 카드가 멈추지는 않는다).
KRX_OPENAPI_PATH_INDEX = "/idx/kospi_dd_trd"  # 유가증권지수 시세정보
KOSPI_IDX_NAME = "코스피"                     # 응답 행에서 코스피 지수를 정확히 골라내는 이름

# "옵션 일별매매정보 (주식옵션外)" — 종목(행사가·만기월)별 일별 거래량을 준다.
# PROD_NM 이 "코스피200 옵션"인 것만 콜/풋으로 나눠 더하면 그날 콜·풋 총 거래량이 된다
# (미니코스피200/위클리 옵션은 별도 상품이라 제외). KRX_API_KEY 승인 시 이 API도
# 별도로 신청해야 한다. 승인 신청명: "옵션 일별매매정보 (주식옵션外)".
KRX_OPT_PATH = "/drv/opt_bydd_trd"
KRX_OPT_PROD_NAME = "코스피200 옵션"

KRX_JSON_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_REFERER = "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"

VKOSPI_PAYLOAD_CANDIDATES = [
    # (bld, 추가 파라미터)
    ("dbms/MDC/STAT/standard/MDCSTAT00601", {"indIdx": "5", "indIdx2": "300",
                                             "tboxindIdx_finder_equidx0_2": "코스피 200 변동성지수"}),
    ("dbms/MDC/STAT/standard/MDCSTAT00301", {"indIdx": "5", "indIdx2": "300"}),
]

# ─────────────────────────────────────────────────────────────
# 신용거래융자 잔고 (코스피 / 코스닥): 금융투자협회 FreeSIS ▸ 신용공여 잔고추이.
# 인증키 없이 기간 조회가 되는 화면 내부 JSON 엔드포인트(공식 공개 API 아님).
# 응답 단위는 백만원, 영업일 기준 1~2일 늦게 올라온다.
#   TMPV1 일자 · TMPV2 융자 합계 · TMPV3 유가증권 · TMPV4 코스닥
#   TMPV5~7 대주(합계/유가/코스닥) · TMPV8 청약자금대출 · TMPV9 예탁증권담보융자
# ─────────────────────────────────────────────────────────────
KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
KOFIA_CREDIT_OBJ = "STATSCU0100000070BO"
KOFIA_CREDIT_COLS = {"kospi": "TMPV3", "kosdaq": "TMPV4"}
KOFIA_CREDIT_BACKFILL_DAYS = 365 * 3   # 채워둘 과거 구간 (모자라면 다음 실행에서 앞쪽을 보충)
KOFIA_REF_URL = "https://freesis.kofia.or.kr/"

VKOSPI_REF_URL = "https://kr.investing.com/indices/kospi-volatility"

# DBnomics의 ISM 미러가 깨졌을 때 눈으로 최신값을 대조할 참고 링크.
# investing.com 자체를 수집 소스로 쓰지는 않습니다 — 자동 추출 금지 약관,
# Cloudflare 의 데이터센터 IP 차단, 그리고 어차피 ISM 을 받아 보여주는
# 재배포자일 뿐이라는 점 때문입니다 (VKOSPI 와 동일한 이유).
ISM_REF_URL = "https://kr.investing.com/economic-calendar/ism-manufacturing-pmi-173"

# ─────────────────────────────────────────────────────────────
# Buy-the-Dip 패닉 점수 (data/buy_the_dip_panic_score_v1.1.xlsx 와 같은 규칙)
#
# 패닉 80점 + 반전 20점. 각 표는 (하한, 원점수) — 엑셀 LOOKUP 과 같이
# "값 이상인 가장 큰 하한"의 원점수를 쓴다. 원점수(0~100) × 가중치/100 을 합산.
# SPY·QQQ 를 S&P 500·Nasdaq-100 대용으로 쓴다(엑셀과 동일).
# ─────────────────────────────────────────────────────────────
DIP_MDD = {   # |고점 대비 하락률|
    "spy": [(0, 0), (.05, 15), (.10, 30), (.15, 50), (.20, 65), (.25, 80), (.30, 90), (.35, 100)],
    # QQQ 는 20년 백테스트(2008~2026)에서 같은 하락장에 SPY 보다 3~8점 낮게 나와 한 단계 얕게 조정
    "qqq": [(0, 0), (.06, 15), (.10, 30), (.15, 50), (.22, 65), (.30, 80), (.38, 90), (.48, 100)],
}
DIP_VIX = [(0, 0), (15, 10), (20, 25), (25, 40), (30, 55), (35, 70), (40, 85), (50, 95), (65, 100)]
DIP_RSI_D = [(0, 100), (20, 100), (25, 80), (30, 60), (35, 40), (40, 20), (50, 0)]
DIP_RSI_W = [(0, 100), (30, 100), (35, 80), (40, 60), (45, 40), (50, 20), (55, 0)]
# 미국판 전용: 하락 초입에 너무 빨리 100점에 닿던 두 지표를 엄격하게 (20년 백테스트 근거).
# 국내판은 기존 표(DIP_RSI_W, DIP_BREADTH)를 그대로 쓴다.
DIP_RSI_W_US = [(0, 100), (30, 80), (35, 60), (40, 40), (45, 20), (50, 0)]
DIP_BREADTH_50_US = [(0, 100), (.05, 90), (.10, 75), (.15, 55), (.20, 40), (.30, 25), (.40, 10), (.50, 0)]
# MDD 조건(미국·국내): 점수가 높아도 실제 하락폭이 따라오지 않으면 누적투입률을 이 이상 올리지 않는다.
# (필요 MDD, 허용 최대 누적투입률). 2008년 7월처럼 긴 하락장 첫 국면에서 70~100%를 다 쓰던 문제 대응.
DIP_MDD_GATE = {
    "spy": [(0, .30), (.15, .50), (.20, .70), (.30, 1.0)],
    "qqq": [(0, .30), (.15, .50), (.22, .70), (.35, 1.0)],
    "kospi": [(0, .30), (.20, .50), (.28, .70), (.40, 1.0)],   # 국내는 하락폭이 커서 기준을 깊게
    "kosdaq": [(0, .30), (.22, .50), (.32, .70), (.45, 1.0)],
}
# 미국판 VIX 최소 투입: 점수와 상관없이 누적투입률을 최소 이만큼 (위에서부터 먼저 맞는 규칙).
#  · VIX 35 이상 또는 50일 평균의 2배 이상 → 20% (2008~2026 첫 신호 8번 모두 1년 뒤 플러스)
#  · VIX 가 50일 평균의 1.6배 이상 → 10% 탐색 매수 (급등 초입. 수익 우위는 평균 수준이지만 기회를 늘림)
# 백테스트(기존 'VIX 35→20%' 대비): 매수한 하락장 SPY 18→23·QQQ 21→26번, 2년 손익 +9·+15%p, 최악 평가손 동일.
DIP_VIX_FLOOR = [
    dict(level=35, ratio=2.0, pct=.20, label="VIX 35 이상 또는 50일 평균의 2배 이상"),
    dict(level=None, ratio=1.6, pct=.10, label="VIX가 50일 평균의 1.6배 이상"),
]
DIP_FG = [(0, 100), (10, 80), (20, 60), (30, 40), (45, 20), (55, 10), (75, 0)]
DIP_BREADTH = [(0, 100), (.1, 90), (.2, 80), (.3, 60), (.4, 40), (.5, 25), (.6, 15), (.7, 0)]
DIP_HY = [(0, 0), (.03, 15), (.04, 30), (.05, 45), (.06, 65), (.08, 85), (.10, 100)]
DIP_WEIGHTS = dict(mdd=30, vix=12, rsi=10, fg=6, breadth=13, hy=9)   # 합 80
DIP_REVERSAL_PTS = 5                                                 # 4개 × 5 = 20
# 매수 단계: 시장별 (하한 점수, 매수 단계, 누적투입률)
_KR_STAGE_NAMES = [("대기", 0), ("현금 유지·점검", 0), ("1차 소액 매수", .05), ("본격 분할매수", .15),
                   ("매수 비중 확대", .30), ("강한 매수", .50), ("대규모 분할매수", .75),
                   ("현금 대부분 투입", .90), ("남은 자금 마무리", 1.0)]
DIP_STAGES = {
    # 미국: 1년에 한 번꼴인 일반 조정(30~40점)에서 10~30%, 약세장(50~60점)에서 절반 이상,
    # 위기(70점 이상)에서 나머지를 투입. 엑셀 v1.1 Thresholds 표와 같다.
    "us": [(0, "대기", 0), (20, "현금 유지·점검", 0), (30, "1차 매수", .10), (35, "2차 매수", .20),
           (40, "본격 분할매수", .30), (50, "매수 비중 확대", .50), (60, "강한 매수", .70),
           (70, "대규모 분할매수", .85), (80, "남은 자금 마무리", 1.0)],
    # 코스닥: 아래 DIP_KOSDAQ_V2 점수(지표 보정판)와 짝을 이루는 35점 시작 절충안.
    # 2008~2026 백테스트: 매수 신호 구간 17→16번(거의 유지), 2년 손익 +75%p → +89%p,
    # 투입 대비 수익률 22% → 27%. 금융위기형 평가손은 -44%p 로 비슷.
    "kosdaq": [(0, "대기", 0), (25, "현금 유지·점검", 0), (35, "1차 매수", .10), (40, "2차 매수", .20),
               (45, "본격 분할매수", .30), (55, "매수 비중 확대", .50), (65, "강한 매수", .70),
               (75, "대규모 분할매수", .85), (85, "남은 자금 마무리", 1.0)],
}
# 코스피: 미국식 단계 + 국내용 MDD 조건. 2008~2026 백테스트에서 투입 대비 수익률은 그대로(37~38%)
# 두고 투입을 두 배로 늘려 2년 손익이 +92%p → +175%p (대신 금융위기형 평가손 -22% → -35%).
DIP_STAGES["spy"] = DIP_STAGES["qqq"] = DIP_STAGES["kospi"] = DIP_STAGES["us"]
# 엑셀이 정하지 않은 반전 조건의 세부 기준
DIP_VIX_PEAK_DAYS = 20      # "VIX 고점" = 최근 20 거래일 최고치
DIP_BREADTH_IMPROVE = 5     # "Breadth 5거래일 개선" = 50일선 위 비율이 5거래일 전보다 높음
# 국내판 (코스피·코스닥). 미국판과 같은 틀·가중치에 재료만 바꾼다.
#   VIX → VKOSPI (올해 평소 수준이 높아 구간을 위로 올림)
#   Fear & Greed → 코스피 F&G 오실레이터의 최근 1년 순위(0 = 1년 중 가장 공포)
#   HY OAS → 신용융자 잔고 낙폭(1년 고점 대비) — 반대매매로 레버리지가 털렸는지
#   시장 폭 → 시가총액 상위 200종목(네이버) 중 50·200일선 위 비율
DIP_MDD["kospi"] = [(0, 0), (.07, 15), (.12, 30), (.18, 50), (.25, 65), (.32, 80), (.40, 90), (.50, 100)]
DIP_MDD["kosdaq"] = [(0, 0), (.10, 15), (.16, 30), (.22, 50), (.30, 65), (.38, 80), (.46, 90), (.55, 100)]
DIP_VKOSPI = [(0, 0), (20, 10), (25, 25), (30, 40), (35, 55), (40, 70), (50, 85), (65, 95), (80, 100)]
DIP_CREDIT_DD = [(0, 0), (.05, 15), (.10, 30), (.15, 45), (.20, 65), (.30, 85), (.40, 100)]
# ── 코스닥 전용 지표 보정판 (2008~2026 백테스트 근거) ─────────────────────
#  · 주봉 RSI·시장 폭이 일반 하락에서도 70~100 으로 포화 → 엄격한 표
#  · VKOSPI 는 절대값 대신 "직전 1년 중간값의 몇 배" (2026년처럼 평소 수준이 높은 해 보정)
#  · 원/달러(1년 평균 대비 상승률)·회사채 AA- 스프레드(국고 3년 대비) 추가 — 외국인 이탈·신용경색형 위기
DIP_KOSDAQ_V2 = dict(
    weights=dict(mdd=30, vix=12, rsi=7, fg=4, breadth=11, hy=9, krw=4, spr=3),   # 합 80
    vkospi_rel=[(0, 0), (1.10, 10), (1.25, 25), (1.40, 40), (1.60, 55), (1.80, 70), (2.10, 85), (2.50, 95), (3.0, 100)],
    breadth_200=[(0, 100), (.10, 85), (.20, 65), (.30, 45), (.40, 30), (.50, 15), (.60, 0)],
    krw=[(0, 0), (.03, 15), (.05, 30), (.08, 50), (.12, 70), (.18, 85), (.25, 100)],
    spread=[(0, 0), (.70, 15), (.90, 30), (1.10, 50), (1.40, 70), (2.0, 85), (2.8, 100)],
)
KR_TOP_N = 200
NAVER_MARKET_VALUE_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}"
SP500_LIST_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_LIST_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

# ─────────────────────────────────────────────────────────────
# 화면 구성 — index.html 이 이 순서/그룹대로 카드를 그립니다.
# ─────────────────────────────────────────────────────────────
LAYOUT = [
    dict(group="미국 시장", keys=[
        "spx", "ndx", "fear_greed", "spx_fg_osc", "ndx_fg_osc",
        "spx_drawdown", "ndx_drawdown", "ndx_rsi", "dip_spy", "dip_qqq", "vix",
        "ust10y", "rate_hy_combo", "yc_10y2y",
        "us_cpi_yoy", "real_policy_rate", "global_m2_yoy", "oecd_cli", "ism_pmi",
    ]),
    dict(group="국내 시장", keys=["kospi", "kosdaq", "kospi_fg_osc", "dip_kospi", "dip_kosdaq", "vkospi",
                                  "kospi_drawdown", "kosdaq_drawdown", "kospi_rsi",
                                  "kospi_credit_combo", "kosdaq_credit_combo", "kospi200_pcr", "kr_exports_yoy"]),
]

# LAYOUT 에는 없지만(카드로 안 뜸) 그 그룹을 --group 으로 갱신할 때 같이 갱신해야 하는
# 내부 전용 시리즈. 코스피200 옵션 콜/풋 원거래량과 국채선물지수 2종은 카드로
# 노출하지 않지만 kospi_fg_osc 계산에 쓰여서 계속 수집은 해야 한다.
# 신용융자 잔고 2종도 단독 카드 대신 지수와 겹친 콤보 카드로만 보여주지만,
# 원본은 날짜 손실 없이 따로 쌓아둔다.
INTERNAL_GROUP_EXTRAS = {
    # Buy-the-Dip 점수 재료: SPY·QQQ 종가, 구성종목 시장 폭(50·200일선 위 비율)
    "미국 시장": {"spy", "qqq", "breadth_spx", "breadth_ndx"},
    "국내 시장": {"kospi200_call_vol", "kospi200_put_vol",
                "bond5y_futures", "bond10y_futures",
                "kospi_credit", "kosdaq_credit",
                "breadth_kospi", "breadth_kosdaq", "usdkrw", "kr_credit_spread"},
}

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
# VIX·10년물·5년물은 원래 FRED(VIXCLS/DGS10/DGS5)에서 받았으나, GitHub Actions
# 클라우드 IP 에서 FRED 가 며칠씩 응답을 안 주는 문제가 있어 Yahoo 로 옮겼다.
# ^VIX 는 FRED VIXCLS 와 값이 사실상 동일(둘 다 CBOE 원천), ^TNX/^FVX 는
# CBOE 지수라 FRED 의 재무부 공식치와 소수점 둘째자리 정도 차이가 날 수 있지만
# 정규화해서 합성하는 이 계산 목적상 무시할 수준이다.
# stooq 쪽 심볼(^vix/10usy.b/5usy.b)은 Stooq 가 현재 봇 차단(JS 챌린지)
# 중이라 실제로 검증하지 못했다 — 나중에 Stooq 가 다시 열리면 확인할 것.
FG_VIX = dict(yahoo="^VIX", stooq="^vix")
FG_DGS10 = dict(yahoo="^TNX", stooq="10usy.b")
FG_DGS5 = dict(yahoo="^FVX", stooq="5usy.b")
FG_CALC_START = "2024-01-01"   # 원본 스크립트와 동일 — 정규화 기준 구간
