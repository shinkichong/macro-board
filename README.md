# 매크로 보드

미국·국내 시장의 매크로 지표 13종을 한 화면에 모아 보는 정적 대시보드입니다.
GitHub Actions 가 하루 두 번 데이터를 수집해 JSON 으로 커밋하고, GitHub Pages 가 그 JSON 을 그립니다.
서버도, API 키도, 비용도 필요 없습니다.

## 왜 이런 구조인가

브라우저에서 FRED·Stooq·CNN 을 직접 `fetch` 하면 전부 CORS 로 막힙니다.
그래서 수집은 서버 쪽(Actions 러너)에서 하고, 프론트엔드는 같은 도메인의 정적 JSON 만 읽습니다.
매크로 지표는 대부분 일별·월별이라 실시간 스트리밍이 필요 없어서 이 방식이 잘 맞습니다.

부수적으로, 데이터가 커밋 히스토리에 쌓이므로 원본 소스가 사라져도 과거치는 남습니다.

## 시작하기

```bash
git clone <저장소 주소> && cd <저장소>
pip install -r requirements.txt

python scripts/fetch_data.py --check   # 어떤 소스가 살아있는지 먼저 점검
python scripts/fetch_data.py           # data/macro.json 생성
python scripts/build_page.py           # index.html 갱신

python -m http.server 8000             # http://localhost:8000
```

`index.html` 을 `file://` 로 바로 열면 `fetch` 가 막혀 내장 예비 데이터가 뜹니다.
실데이터를 보려면 위처럼 간단한 HTTP 서버를 띄우세요.

### GitHub Pages 에 올리기

1. 저장소 **Settings ▸ Pages ▸ Source** 를 `GitHub Actions` 로 설정
2. **Settings ▸ Actions ▸ General ▸ Workflow permissions** 를 `Read and write` 로 설정
3. (선택) **Settings ▸ Secrets and variables ▸ Actions** 에 `KRX_API_KEY` 추가 — VKOSPI 용
4. **Actions** 탭에서 `매크로 데이터 갱신` 을 한 번 수동 실행

이후 미국 시장은 화~토 KST 약 06:17 + 07:23(재시도), 국내 시장은 월~금 KST 약 16:13 + 17:41(재시도)
에 자동으로 돕니다 (분 단위를 정시에서 벗어나게 잡은 이유는 아래 "깨질 수 있는 곳" 참고).

### 화면에서 바로 수동 갱신하기

대시보드의 각 그룹 제목 옆 **"↻ 업데이트"** 버튼을 누르면 GitHub Actions 를 그 자리에서
바로 실행시킬 수 있습니다. GitHub Pages 는 서버가 없는 정적 사이트라, 이 버튼은 브라우저에서
GitHub REST API(`workflow_dispatch`)를 직접 호출합니다 — 그러려면 권한 있는 토큰이 필요합니다.

1. [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens/new) 에서
   **Fine-grained personal access token** 발급
   - Repository access: **Only select repositories** → 이 저장소만 선택
   - Permissions: **Actions** → `Read and write`
2. 버튼을 처음 누르면 토큰 입력창이 뜹니다. 붙여넣으면 이 브라우저의 `localStorage` 에만 저장되고
   (서버로 전송되지 않음) 이후로는 클릭 한 번으로 바로 실행됩니다.
3. 토큰이 만료되었거나 권한이 부족하면 자동으로 다시 물어봅니다.

토큰은 Actions 실행 권한을 갖고 있으니, **본인 브라우저에만** 입력하고 공용 PC 에서는 쓰지 마세요.

## 파일 구조

```
scripts/sources.py        지표 레지스트리 — 차트 추가·수정은 여기만 고치면 됩니다
scripts/fetch_data.py     수집기
scripts/build_page.py     src/index.template.html + 데이터 → index.html
scripts/make_demo_data.py 화면 확인용 난수 데이터 생성기
src/index.template.html   대시보드 원본
index.html                빌드 결과물 (이 파일이 배포됩니다)
data/macro.json           수집된 시계열
```

## 데이터 소스

| 지표 | 소스 | 비고 |
|---|---|---|
| S&P 500 · 나스닥 | Yahoo Finance → Stooq | Yahoo 실패 시 Stooq 로 자동 전환 |
| Fear & Greed | CNN `production.dataviz.cnn.io` | 비공식. 2020-08 이후만 |
| VIX | FRED `VIXCLS` | |
| 미국 10년물 | FRED `DGS10` | |
| 하이일드 스프레드 | FRED `BAMLH0A0HYM2` | 금리(Effective Yield)를 보려면 `BAMLH0A0HYM2EY` |
| 장단기 금리차 | FRED `T10Y2Y` | 이미 계산된 계열 |
| 미국 CPI (YoY) | FRED `CPIAUCNS` | 계절조정 전(NSA) 지수로 직접 전년동월비 계산 |
| 실질 정책금리 | FRED `DFEDTARU`/`DFEDTARL` + `PCEPI` 합성 | 정책금리(목표범위 중간값) - 헤드라인 PCE(YoY). 기준선 0 |
| 글로벌 M2 증감율 | FRED + ECB + PBOC + BOJ 합성 | 아래 설명 참고 |
| OECD 경기선행지수 | OECD SDMX | FRED 미러는 갱신 중단됨 |
| ISM 제조업지수 | DBnomics `ISM/pmi/pm` | 2016년 FRED 에서 삭제됨 |
| 코스피 · 코스닥 | Yahoo Finance → Stooq | |
| VKOSPI | KRX 공식 오픈API + 정보데이터시스템 | 인증키 선택. 아래 설명 참고 |
| 한국 수출증가율 (YoY) | FRED `XTEXVA01KRM659S` (OECD MEI 경유) | 아래 "깨질 수 있는 곳" 참고 — 최근 구간 값이 의심스러움 |

FRED 는 API 키 없이 `fredgraph.csv` 로 받습니다. DBnomics·OECD 도 키가 필요 없습니다.
즉 **어떤 계정 등록도 없이** 전부 돕니다.

## 화면 읽는 법

각 카드 왼쪽 세로 막대가 그 지표의 현재 상태입니다.

- **기준선이 있는 지표** — 기준선 대비 위치로 판정합니다.
  VIX 20, VKOSPI 20, ISM 50, Fear & Greed 50, 금리차 0, OECD CLI 100, 글로벌 M2 0%.
  차트에서 기준선 위/아래가 서로 다른 색으로 칠해지므로, 보드 전체가 한눈에 읽힙니다.
- **기준선이 없는 주가지수** — 200일 이동평균 대비로 판정하고, 그 이동평균을 점선으로 함께 그립니다.

상단 신호 줄의 "N / 13 양호" 는 이 판정을 합산한 값입니다. 클릭하면 해당 카드로 이동합니다.

## 글로벌 M2 에 대하여

단일 시리즈가 없어서 직접 합성합니다.
미국·유로존·중국·일본 M2 를 각각 받아 → 그 시점 환율로 달러 환산 → 합산 → 전년동월비.
이 네 나라면 글로벌 M2 의 70~80% 가 커버됩니다.

카드의 토글로 두 방식을 비교할 수 있습니다.

- **달러 환산** — 시중에 도는 "글로벌 유동성" 차트는 대부분 이 방식입니다.
  달러가 강해지면 실제 통화량이 늘어도 지수가 꺾이는데, 위험자산 가격과의 상관관계는
  오히려 이쪽이 높아서 일부러 그렇게 씁니다.
- **고정환율** — 최신 환율로 전 구간을 고정해 환율 효과를 뺀 순수 통화량 증가분입니다.

두 선이 벌어지는 구간 자체가 정보입니다. 그 구간의 움직임은 통화량이 아니라 달러가 만든 것입니다.

이 차트는 **항상 1~2개월 비어 보이는 게 정상**입니다. 중국 M2 는 익월 중순,
ECB 는 약 한 달 지연 발표입니다.

## 깨질 수 있는 곳

무료 소스를 엮은 구조라 언젠가는 하나씩 어긋납니다. 한 소스가 죽어도 나머지는 갱신되고,
실패한 지표는 직전 값을 유지한 채 카드에 "갱신 실패" 배지가 붙습니다.
`python scripts/fetch_data.py --check` 로 어디가 막혔는지 바로 볼 수 있습니다.

**글로벌 M2 구성 시리즈** — 각국 M2 의 DBnomics 시리즈 ID 와 단위(억 원 / 백만 유로 등)는
제공처 사정으로 바뀝니다. `--check` 가 후보 데이터셋을 출력해주니,
확인한 ID 를 `scripts/sources.py` 의 `M2_COMPONENTS[...]["series_ids"]` 에 고정하고
`scale` 도 함께 맞춰주세요. 값이 수십 배 어긋나 보이면 대개 `scale` 문제입니다.

**하이일드 스프레드 (`BAMLH0A0HYM2`)** — ICE Data Indices 소유의 라이선스 계열이라
FRED 무료 CSV 는 `cosd` 를 아무리 과거로 줘도 최근 ~3년치만 내려줍니다(그 이상은 ICE 승인 필요).
그래서 `rate_hy_combo()` 는 새로 받은 값에 이전 실행에서 저장해둔 값을 병합합니다 —
한 번 확보한 날짜는 FRED 창에서 밀려나도 우리 쪽 기록에 남아 시간이 지날수록 구간이 넓어집니다
(다시 좁아지지는 않습니다). 다만 이 방식을 도입한 시점(2026-09-18, 2023-09-18~) 이전으로는
거슬러 올라갈 수 없습니다.

**한국 수출증가율 (`XTEXVA01KRM659S`)** — OECD MEI 를 FRED 가 미러링하는 계열입니다.
2026-01 부터 값이 32% → 70%(2026-06)까지 치솟는데, 2025년엔 저성장 기저였던 걸 감안해도
2021년 코로나 기저효과 최고치(44%)를 넘어서는 수준이라 실제 경기로 설명이 안 됩니다.
ISM PMI 미러가 깨졌던 것과 같은 부류의 문제로 의심됩니다. 안전장치(freeze)는 아직 없지만,
하이일드 스프레드와 같은 이유로 `kr_exports_yoy()` 도 이전 값과 병합해 저장합니다 —
이 계열이 나중에 최근 구간만 주는 식으로 막혀도 그동안 확보한 과거치는 남습니다.
지금 보이는 최근 구간 값 자체는 참고만 하시고, 관세청 수출입 통계나 한국은행 ECOS 로
교차 확인하세요.

**코스피/코스닥 최신 날짜가 하루씩 왔다갔다** — 버그가 아닙니다. Yahoo Finance 가 휴장일
"당일" 봉을 잠깐 `close: null` 이 아닌 임시값으로 내려줬다가 몇 시간 뒤 `null` 로 정정하는
경우가 있습니다. `yahoo()` 가 `close is not None` 인 값만 쓰므로, 정정되기 전에 수집하면
최신 날짜가 하루 앞서 보였다가 다음 갱신에서 실제 마지막 거래일로 되돌아옵니다. 코드를
고칠 필요는 없고, 그 사이 값을 본 거라면 참고만 하세요.

**Fear & Greed 오실레이터(KOSPI)가 며칠씩 뒤처져 보임** — 두 가지가 겹친 결과입니다.
① (수정됨) `kospi_fear_greed_osc()` 가 이번 실행에서 막 갱신된 값 대신 실행 시작 시점의
`prev`(직전 실행 결과)만 봐서, 같은 실행 안에서 VKOSPI·국채선물·옵션거래량이 새로 갱신돼도
그걸 못 쓰고 매번 한 실행씩 늦게 반영되고 있었습니다 — 이번에 `fetch_data.py` 를 고쳐
이번 실행에서 갱신된 값을 우선 쓰도록 했습니다.
② (여전히 남음) 이 오실레이터는 KOSPI(Yahoo)·VKOSPI·국채선물 2종·옵션거래량 2종, 총 6개
계열의 **공통 날짜**로만 계산됩니다. Yahoo 의 `^KS11` 이 특정 날짜를 통째로 비우거나(위
항목 참고) KRX 공식 API 가 그날 아직 값을 안 준 경우, 그 하루는 여섯 계열이 겹치는 날이
하루도 없을 수 있어 오실레이터가 그 며칠 동안은 못 나아갑니다. 계속 며칠 이상 멈춰
있다면 `python scripts/fetch_data.py --group "국내 시장"` 로 직접 확인해보세요.

**GitHub Actions 스케줄 자체가 통째로 스킵됨** — 위 지연과 별개로, 예정된 4번 중 일부가
아예 실행되지 않고 넘어가는 경우를 실제로 확인했습니다(2026-09-21: 4번 중 2번만 실행).
GitHub 공식 문서에 "부하가 심하면 예약 실행이 아예 건너뛰어질 수 있고 재실행되지 않는다"고
명시돼 있어, 이건 무료 플랜의 구조적 한계로 보입니다. 화면 위 "↻ 업데이트" 버튼으로
언제든 수동 보완할 수 있습니다.

**OECD CLI** — SDMX 키 구조가 개편되면 후보 URL 이 전부 실패할 수 있습니다.
[OECD Data Explorer](https://data-explorer.oecd.org/) 에서 원하는 계열을 고른 뒤
Download ▸ "Copy API link" 로 받은 URL 을 `OECD_CLI_CANDIDATES` 맨 앞에 넣으세요.

**VKOSPI** — 두 경로를 함께 씁니다. 과거치는 KRX 정보데이터시스템에서 기간 조회로 한 번에
적재하고, 이후 갱신은 공식 오픈API 로 빠진 날짜만 채웁니다.

공식 오픈API 를 쓰려면 [openapi.krx.co.kr](https://openapi.krx.co.kr) 에서 무료 인증키를
발급받고, **"서비스 이용" 에서 [파생상품지수 일별시세] 를 개별 신청해 승인까지 받아야 합니다.**
인증키만으로는 호출되지 않습니다. 승인 후 환경변수 `KRX_API_KEY` 에 넣으세요
(Actions 라면 저장소 Secrets). 제공 기간은 2010년 이후이고, 한 번에 하루치만 주기 때문에
과거 적재가 아니라 증분 갱신 전용입니다.

인증키가 없어도 정보데이터시스템만으로 돌아갑니다. 다만 비공식 내부 엔드포인트라
`bld` 값이 바뀌면 깨집니다. 그때는 data.krx.co.kr 에서 변동성지수 시세 화면을 연 뒤
F12 ▸ Network 에서 `getJsonData.cmd` 요청의 payload 를 복사해
`VKOSPI_PAYLOAD_CANDIDATES` 에 넣으세요.

GitHub Actions 러너는 해외 IP 라 정보데이터시스템 쪽이 느리거나 막힐 수 있습니다.
공식 오픈API 를 붙여두면 이 문제에서 자유롭습니다.

**Investing.com 은 쓰지 않습니다.** 값은 맞지만 Cloudflare 가 데이터센터 IP 를 차단하고,
약관이 자동 추출을 금지하며, 애초에 그쪽도 KRX 를 받아 보여주는 중간 배포자입니다.
대신 눈으로 값을 대조할 수 있도록 VKOSPI 카드 하단에 참고 링크로 걸어뒀습니다.

**Fear & Greed** — 브라우저처럼 보이는 헤더가 없으면 CNN 이 거부합니다.
수집기가 `User-Agent` 와 `Referer` 를 붙이지만, CNN 이 구조를 바꾸면 깨집니다.

## 지표 추가하기

`scripts/sources.py` 의 `FRED` 딕셔너리에 한 줄 넣으면 FRED 계열은 바로 붙습니다.

```python
"dxy": dict(id="DTWEXBGS", name="달러지수", unit="", decimals=2,
            threshold=None, below_is=None),
```

그리고 같은 파일 맨 아래 `LAYOUT` 의 원하는 그룹에 키를 추가하세요.
다른 소스는 `fetch_data.py` 에 fetcher 함수를 하나 쓰고 `build_jobs()` 에 등록하면 됩니다.

## 라이선스와 데이터 권리

코드는 자유롭게 쓰셔도 됩니다. 데이터는 각 제공처 소유입니다.

- **ISM** — 데이터 소유권이 ISM 에 있고 재배포 제한이 있습니다. 2016년 FRED 에서 삭제된 이유도
  이것입니다. 공개 사이트로 운영하신다면 출처를 표기하거나, S&P Global US Manufacturing PMI
  또는 완전 공개인 Philly Fed·Chicago Fed 제조업지수로 대체하는 편이 안전합니다.
- **CNN Fear & Greed** — 공식 API 가 아니며 CNN 이용약관은 개인적 이용 범위까지만 허용합니다.
- **KRX** — 스크래핑 데이터는 참고용입니다. 상업적 용도라면 KRX 약관을 확인하세요.
- **OECD** — 인용 시 출처 표기가 요구됩니다.

개인용으로 쓰신다면 위 사항은 대체로 문제되지 않습니다.
공개·상업적으로 쓰실 계획이면 ISM 과 CNN 두 개를 먼저 정리하세요.
