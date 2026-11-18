#!/usr/bin/env python3
"""
1번 에이전트(시장조사·아이템 발굴) 결과를 공용 DB(trade_hub)에 저장한다.

  python discover_store.py import-reports [reports]   # 예전 reports/*_discover/raw.json을 DB로 가져오기
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from trade_hub import db

ROOT = Path(__file__).resolve().parent


def save_discover(conn, run_id, candidates, origin="discover"):
    """후보 점수표 → products(candidate) + product_scores. {품목명: 품목 번호}를 돌려준다."""
    ids = {}
    with conn:
        for c in candidates:
            pid = db.upsert_product(
                conn, c["name"], run_id, origin=origin, category=c.get("category"),
                search_keyword_en=c.get("search_keyword_en"), keywords_ko_json=db.dumps(c.get("keywords_ko")),
                reason=c.get("reason"), regulations=c.get("regulations"), risk=c.get("risk"),
                est_retail_krw=c.get("est_retail_krw"), ref_url=c.get("ref_url"))   # 사진은 저장 안 함 (확정 사진은 사람·공급사 몫)
            s = c.get("scores", {})
            conn.execute(
                "INSERT INTO product_scores (product_id, run_id, demand, margin, competition, regulation, logistics,"
                " total, score_notes, est_fob_usd, naver_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, run_id, s.get("demand"), s.get("margin"), s.get("competition"), s.get("regulation"),
                 s.get("logistics"), c.get("_total"), c.get("score_notes"), c.get("est_fob_usd"),
                 db.dumps(c.get("_naver"))))
            ids[c["name"]] = pid
    return ids


def import_reports(conn, reports_dir):
    """reports/*/raw.json 중 1번 에이전트 결과만 DB로 가져온다. 이미 가져온 폴더는 건너뛴다."""
    done = 0
    for raw in sorted(Path(reports_dir).glob("*/raw.json")):
        data = json.loads(raw.read_text(encoding="utf-8"))
        if "candidates" not in data:
            print(f"  · 건너뜀 (1번 에이전트 결과 아님): {raw.parent.name}")
            continue
        folder = str(raw.parent.resolve())
        if conn.execute("SELECT 1 FROM agent_runs WHERE report_dir = ?", (folder,)).fetchone():
            print(f"  · 건너뜀 (이미 있음): {raw.parent.name}")
            continue
        try:
            started = datetime.strptime(raw.parent.name[:15], "%Y-%m-%d_%H%M").strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            started = db.now()
        with conn:
            run_id = conn.execute(
                "INSERT INTO agent_runs (agent, status, args_json, report_dir, started_at, finished_at)"
                " VALUES ('discover', 'succeeded', ?, ?, ?, ?)",
                (db.dumps({"imported_from": raw.parent.name, "hint": data.get("hint")}), folder, started, started)
            ).lastrowid
        ids = save_discover(conn, run_id, data["candidates"])
        print(f"  ✓ {raw.parent.name}: 후보 {len(ids)}개")
        done += 1
    return done


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] != "import-reports":
        sys.exit(__doc__)
    conn = db.connect()
    print(f"DB: {db.default_path()}")
    n = import_reports(conn, sys.argv[2] if len(sys.argv) > 2 else ROOT / "reports")
    print(f"가져온 실행: {n}건")
