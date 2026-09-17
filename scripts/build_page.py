#!/usr/bin/env python3
"""
src/index.template.html + data/macro.json  →  index.html

index.html 은 실행 시 data/macro.json 을 먼저 읽습니다.
여기서 끼워넣는 데이터는 그 요청이 실패했을 때(수집 전이거나 file:// 로 연 경우)를
위한 예비본이라, 용량을 줄이려고 오래된 구간은 솎아냅니다.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "index.template.html"
SRC = ROOT / "data" / "macro.json"
OUT = ROOT / "index.html"

KEEP_FULL_DAYS = 365   # 최근 1년은 전부, 그 이전은 5영업일 간격


def thin(payload: dict) -> dict:
    latest = max((s.get("last_date", "") for s in payload["series"].values()), default="")
    if not latest:
        return payload
    cut = (date.fromisoformat(latest) - timedelta(days=KEEP_FULL_DAYS)).isoformat()
    for s in payload["series"].values():
        if s.get("freq") != "daily":
            continue
        dec = max(s.get("decimals", 2), 1)
        for field in ("data", "fixed_fx"):
            if not s.get(field):
                continue
            old = [p for p in s[field] if p[0] < cut][::5]
            new = [p for p in s[field] if p[0] >= cut]
            s[field] = [[d, round(v, dec)] for d, v in old + new]
    return payload


def main() -> int:
    if not SRC.exists():
        print("data/macro.json 이 없습니다. "
              "먼저 scripts/fetch_data.py 또는 scripts/make_demo_data.py 를 실행하세요.")
        return 1

    payload = thin(json.loads(SRC.read_text(encoding="utf-8")))
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # </script> 가 데이터에 섞여 script 블록을 깨뜨리는 것만 막으면 된다.
    blob = blob.replace("</", "<\\/")

    html = TPL.read_text(encoding="utf-8")
    if "__EMBEDDED_DATA__" not in html:
        print("템플릿에 __EMBEDDED_DATA__ 자리표시자가 없습니다.")
        return 1

    OUT.write_text(html.replace("__EMBEDDED_DATA__", blob), encoding="utf-8")
    print(f"{OUT} 생성 ({OUT.stat().st_size / 1024:,.0f} KB, "
          f"예비 데이터 {sum(len(s['data']) for s in payload['series'].values()):,}점)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
