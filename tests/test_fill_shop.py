"""--fill-shop: 기존 품목에 쇼핑인사이트 채우기 — 임시 DB·가짜 모델·가짜 네이버 응답 (실제 호출 없음)."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import naver_shopping as ns
import supplier_agent as sa
from trade_hub import db as trade_db

from test_naver_shopping import shop_response


class FakeClient:
    def __init__(self, items):
        self.items, self.calls = items, 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"items": self.items})])


class FillShopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = trade_db.connect(Path(self.tmp.name) / "t.db")
        sa._NAVER_DISK[:] = [ns.NaverCache(Path(self.tmp.name) / "c.json", log=lambda *a: None)]
        self.old_trend = {"keyword": "실리콘 찜기", "keywords": ["실리콘 찜기"], "volume_idx": 3.5, "growth_3m": 10, "yoy": 5}
        self.ids = {}
        for name, naver in (("실리콘 찜기", self.old_trend), ("옛 품목", None), ("애매한 것", {"keyword": "x", "keywords": ["x"]})):
            pid = trade_db.upsert_product(self.conn, name, keywords_ko_json=json.dumps([name], ensure_ascii=False))
            self.conn.execute("INSERT INTO product_scores (product_id, total, naver_json) VALUES (?, 50, ?)",
                              (pid, json.dumps(naver, ensure_ascii=False) if naver else None))
            self.ids[name] = pid
        self.conn.commit()

    def tearDown(self):
        sa._NAVER_DISK.clear()
        self.conn.close()
        self.tmp.cleanup()

    def naver(self, path, keys, body):
        if path == ns.SHOPPING_PATH:
            return shop_response(body, {"실리콘 찜기": [5] * 9 + [9, 9, 9], "옛 품목": [10] * 12})
        months = sa._month_window()[0]
        return {"results": [{"title": g["groupName"], "data": [{"period": m.isoformat(), "ratio": 10} for m in months]}
                            for g in body["keywordGroups"]]}

    def naver_json(self, name):
        return json.loads(self.conn.execute("SELECT naver_json FROM product_scores WHERE product_id = ?",
                                            (self.ids[name],)).fetchone()[0] or "null")

    def run_fill(self, client, naver=None):
        with mock.patch.object(sa, "_naver_call", side_effect=naver or self.naver):
            return sa.fill_shop_insight((client, "m", None, {}, "x"), self.conn, ("id", "s"), log=lambda *a: None)

    def test_fill_and_skip_next_time(self):
        client = FakeClient([{"id": self.ids["실리콘 찜기"], "shop_category": "생활/건강"},
                             {"id": self.ids["옛 품목"], "shop_category": "생활/건강"},
                             {"id": self.ids["애매한 것"], "shop_category": None}])
        self.assertEqual(self.run_fill(client), 3)
        d = self.naver_json("실리콘 찜기")
        self.assertEqual({k: d[k] for k in self.old_trend}, self.old_trend)           # 기존 점수 근거는 그대로
        self.assertEqual((d["shop_category"], d["shopping_insight"]["trend"]), ("생활/건강", "오름"))
        old = self.naver_json("옛 품목")
        self.assertIn("volume_idx", old)                                              # 비어 있던 줄은 검색어 트렌드도
        self.assertEqual(old["shopping_insight"]["trend"], "보합")
        vague = self.naver_json("애매한 것")
        self.assertIsNone(vague["shop_category"])
        self.assertNotIn("shopping_insight", vague)
        self.assertEqual(self.run_fill(client), 0)                                    # 다 정했으면 다시 안 함
        self.assertEqual(client.calls, 1)

    def test_shopping_failure_is_retried_later(self):
        def fail(path, keys, body):
            if path == ns.SHOPPING_PATH:
                raise sa.NaverError("HTTP 500")
            return self.naver(path, keys, body)
        client = FakeClient([{"id": self.ids["실리콘 찜기"], "shop_category": "생활/건강"}])
        self.run_fill(client, fail)
        self.assertEqual(self.naver_json("실리콘 찜기"), self.old_trend)              # 실패하면 기록 안 함 → 다음에 다시


if __name__ == "__main__":
    unittest.main()
