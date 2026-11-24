"""--reprice: 국내 판매가 다시 조사·변형 나누기 — 임시 DB·가짜 제출값 (실제 호출 없음)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import reprice
from trade_hub import db as trade_db

TODAY = "2026-11-24"


def variant(name, price, basis="낱개", resale=False, unit=None):
    return {"name": name, "search_keyword_en": None, "basis": basis, "est_retail_krw": price,
            "unit_price_krw": unit if unit is not None else price, "price_range_krw": f"{price}원 근처", "resale": resale,
            "sources": [{"site": "쿠팡", "url": "https://example.com/a", "price_krw": price, "unit": basis}]}


class RepriceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trade_db.connect(Path(self.tmp.name) / "t.db")
        with self.conn:
            self.clicker = trade_db.upsert_product(self.conn, "키캡 키링 클리커", stage="sourcing", origin="discover",
                                                   est_retail_krw=4000, notes="사장님 메모: 샘플 먼저",
                                                   source_country="China", category="디지털")
            self.conn.execute("INSERT INTO product_scores (product_id, total, score_notes) VALUES (?, 70, '원래 근거')",
                              (self.clicker,))
            self.steamer = trade_db.upsert_product(self.conn, "실리콘 찜기", origin="discover", est_retail_krw=9000)
            self.dropped = trade_db.upsert_product(self.conn, "중단 품목", stage="dropped")
        self.run = SimpleNamespace(id=None, report_dir=None)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def row(self, pid):
        return reprice.targets(self.conn, [pid])[[r["id"] for r in reprice.targets(self.conn, [pid])].index(pid)]

    def product(self, pid):
        return self.conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def test_schema_accepts_submission(self):
        ok = {"split": True, "reason": "-", "variants": [variant("a", 1000), variant("b", 5000, resale=True)]}
        self.assertEqual(reprice.sa.validate(ok, reprice.REPRICE_TOOL["input_schema"]), [])
        bad = {**ok, "split": "yes"}
        self.assertTrue(reprice.sa.validate(bad, reprice.REPRICE_TOOL["input_schema"]))

    def test_targets(self):
        self.assertEqual([r["id"] for r in reprice.targets(self.conn)], [self.clicker])          # 진행 중만
        self.assertEqual([r["id"] for r in reprice.targets(self.conn, [self.steamer, self.dropped])],
                         [self.clicker, self.steamer, self.dropped])                              # 지정 번호는 더함

    def test_single_updates_price_and_tag_keeps_memo(self):
        v = variant("실리콘 찜기", 12900, basis="묶음 2개", unit=6450)
        reprice.apply(self.conn, self.row(self.steamer), {"split": False, "variants": [v], "reason": "-"}, None, TODAY,
                      log=lambda *a: None)
        p = self.product(self.steamer)
        self.assertEqual(p["est_retail_krw"], 12900)
        self.assertTrue(p["notes"].startswith("[1번 시세 2026-11-24] 묶음 2개 기준 12,900원, 개당 6,450원"))
        again = reprice.with_tag(p["notes"] + "\n내 메모", "[1번 시세 2026-12-01] 새 줄")
        self.assertEqual(again, "[1번 시세 2026-12-01] 새 줄\n내 메모")                         # 태그 줄만 바뀜

    def test_split_makes_candidates_and_leaves_original(self):
        data = {"split": True, "reason": "2구와 LED형 가격이 3배 차이",
                "variants": [variant("키캡 키링 클리커 2구", 3500), variant("키캡 키링 클리커 LED형", 11000, resale=False)]}
        res = reprice.apply(self.conn, self.row(self.clicker), data, None, TODAY, log=lambda *a: None)
        made = [m["id"] for m in res["made"]]
        self.assertEqual(len(made), 2)
        orig = self.product(self.clicker)
        self.assertEqual((orig["stage"], orig["est_retail_krw"]), ("sourcing", 4000))           # 단계·판매가 그대로
        self.assertIn("나눔 → #", orig["notes"])
        self.assertTrue(orig["notes"].endswith("사장님 메모: 샘플 먼저"))
        for pid, price in zip(made, (3500, 11000)):
            p = self.product(pid)
            self.assertEqual((p["stage"], p["est_retail_krw"], p["source_country"]), ("candidate", price, "China"))
            self.assertIn(f"#{self.clicker}에서 나눈 변형", p["notes"])
            score = self.conn.execute("SELECT total, score_notes FROM product_scores WHERE product_id = ?", (pid,)).fetchone()
            self.assertEqual(score["total"], 70)
            self.assertTrue(score["score_notes"].startswith(f"[#{self.clicker}에서 나눈 변형"))
        ev = self.conn.execute("SELECT from_stage, to_stage, note FROM product_events WHERE product_id = ? ORDER BY id DESC",
                               (self.clicker,)).fetchone()
        self.assertEqual((ev["from_stage"], ev["to_stage"]), ("sourcing", "sourcing"))

    def test_split_links_existing_product(self):
        data = {"split": True, "reason": "-", "variants": [{**variant("찜기형", 9000), "existing_id": self.steamer},
                                                         variant("새 변형", 20000)]}
        res = reprice.apply(self.conn, self.row(self.clicker), data, None, TODAY, log=lambda *a: None)
        self.assertEqual([m["existing"] for m in res["made"]], [True, False])
        self.assertEqual(res["made"][0]["id"], self.steamer)
        self.assertEqual(self.product(self.steamer)["est_retail_krw"], 9000)                       # 기존 품목은 안 건드림
        self.assertIsNone(self.product(self.steamer)["notes"])
        self.assertIn("(기존 품목)", self.product(self.clicker)["notes"])
        self.assertIn(f"#{self.steamer} 실리콘 찜기", reprice.existing_block(reprice.others(self.conn, self.clicker)))

    def test_only_ids(self):
        self.assertEqual([r["id"] for r in reprice.targets(self.conn, [self.steamer], with_active=False)], [self.steamer])

    def test_split_name_clash_and_resale_tag(self):
        data = {"split": True, "reason": "-", "variants": [variant("실리콘 찜기", 5000), variant("한정판 박스", 90000, resale=True)]}
        res = reprice.apply(self.conn, self.row(self.clicker), data, None, TODAY, log=lambda *a: None)
        names = [m["name"] for m in res["made"]]
        self.assertEqual(names[0], f"실리콘 찜기 (#{self.clicker}에서 나눔)")                     # 기존 품목을 덮어쓰지 않음
        self.assertEqual(self.product(self.steamer)["est_retail_krw"], 9000)
        self.assertIn("리셀 시세", self.product(res["made"][1]["id"])["notes"])

    def test_run_writes_report_and_survives_failure(self):
        calls = iter([RuntimeError("검색 실패"),
                      {"split": False, "reason": "-", "variants": [variant("실리콘 찜기", 9900)]}])

        def fake(*a, **k):
            x = next(calls)
            if isinstance(x, Exception):
                raise x
            return x
        with mock.patch.object(reprice.sa, "run_agent", side_effect=fake):
            res = reprice.run(None, self.conn, self.run, [self.steamer], out=self.tmp.name, log=lambda *a: None)
        self.assertEqual(len(res), 1)
        self.assertEqual(self.product(self.steamer)["est_retail_krw"], 9900)
        raw = json.loads((Path(self.run.report_dir) / "raw.json").read_text(encoding="utf-8"))
        self.assertIn("error", raw[0])
        with mock.patch.object(reprice.sa, "run_agent", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                reprice.run(None, self.conn, self.run, [self.steamer], out=self.tmp.name, log=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
