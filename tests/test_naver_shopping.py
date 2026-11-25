"""네이버 쇼핑인사이트(수요 추세)·네이버 캐시 — 가짜 응답으로만 시험 (실제 API 호출 없음)."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import naver_shopping as ns
import supplier_agent as sa

TODAY = date(2026, 11, 24)
MONTHS = [f"2025-{m:02d}" for m in range(11, 13)] + [f"2026-{m:02d}" for m in range(1, 11)]   # 2025-11 ~ 2026-10


def shop_response(body, series_by_kw):
    """요청 body의 키워드 순서대로 가짜 결과."""
    return {"results": [{"title": k["name"], "keyword": k["param"],
                         "data": [{"period": f"{m}-01", "ratio": v} for m, v in zip(MONTHS, series_by_kw[k["param"][0]])]}
                        for k in body["keyword"]]}


class AnalyzeTest(unittest.TestCase):
    def test_window(self):
        months, start, end = ns.months_back(TODAY)
        self.assertEqual((start, end, len(months)), ("2025-11-01", "2026-10-31", 12))

    def test_trend_and_seasonal(self):
        months, _, _ = ns.months_back(TODAY)
        up = ns.analyze([10] * 6 + [10, 10, 10, 20, 20, 20], months)
        self.assertEqual((up["trend"], up["change_3m"]), ("오름", 100))
        flat = ns.analyze([10] * 12, months)
        self.assertEqual(flat["trend"], "보합")
        self.assertFalse(flat["seasonal"]["is_seasonal"])
        winter = ns.analyze([60, 100, 50, 10, 5, 5, 5, 5, 5, 5, 8, 12], months)   # 12월에 몰림
        self.assertTrue(winter["seasonal"]["is_seasonal"])
        self.assertEqual(winter["seasonal"]["peak_month"], "2025-12")
        self.assertIsNone(ns.analyze([0] * 12, months)["trend"])                    # 데이터 없음
        off = ns.analyze([75, 75, 100] + [0] * 9, months)                          # 겨울 한철 (실제 '손난로' 응답 모양)
        self.assertIsNone(off["trend"])
        self.assertTrue(off["seasonal"]["is_seasonal"])
        self.assertEqual(ns.summary({"category": "생활/건강", **off}),
                         "쇼핑인사이트(생활/건강) 최근 6개월 클릭 거의 없음 · 계절성 있음(최고 1월, 평균의 4.8배)")
        self.assertEqual(sa.shop_trend_cell({"shopping_insight": off}), "최근 클릭 적음 · 계절 1월")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "naver_cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_day_reuse_and_next_day_reset(self):
        c = ns.NaverCache(self.path, today=TODAY)
        c.put("k", {"v": 1})
        c.count("shopping")
        c.save()
        again = ns.NaverCache(self.path, today=TODAY)
        self.assertEqual(again.get("k"), {"v": 1})
        self.assertEqual(again.usage["2026-11"]["shopping"], 1)
        tomorrow = ns.NaverCache(self.path, today=date(2026, 11, 25))
        self.assertFalse(tomorrow.has("k"))                                         # 하루 지나면 다시 조회
        self.assertEqual(tomorrow.usage["2026-11"]["shopping"], 1)                  # 사용량은 이어서

    def test_warn_at_80_percent(self):
        logs = []
        c = ns.NaverCache(self.path, today=TODAY, log=logs.append)
        c.usage = {"2026-11": {"shopping": int(ns.MONTHLY_LIMIT["shopping"] * ns.WARN_AT) - 1}}
        c.count("shopping")
        c.count("shopping")
        self.assertEqual(len(logs), 1)                                              # 한 번만 경고
        self.assertIn("80%", logs[0])


class ShoppingTrendsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = ns.NaverCache(Path(self.tmp.name) / "c.json", today=TODAY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_by_category_and_cache(self):
        series = {"실리콘 찜기": [5] * 9 + [9, 9, 9], "손난로": [60, 100, 50, 10, 5, 5, 5, 5, 5, 5, 8, 12],
                  "키캡": [10] * 12}
        calls = []

        def fake(path, body):
            calls.append(body)
            self.assertEqual(path, ns.SHOPPING_PATH)
            return shop_response(body, series)

        living, digital = ns.SHOP_CATEGORIES["생활/건강"], ns.SHOP_CATEGORIES["디지털/가전"]
        items = [("찜기", "실리콘 찜기", living), ("손난로", "손난로", living), ("키캡", "키캡", digital)]
        out = ns.shopping_trends(items, fake, self.cache, today=TODAY)
        self.assertEqual(len(calls), 3)                                             # 키워드마다 따로 (자기 최대=100)
        self.assertEqual([c["category"] for c in calls], [living, living, digital])
        self.assertEqual(len(calls[0]["keyword"]), 1)
        self.assertEqual(out["찜기"]["trend"], "오름")
        self.assertEqual(out["찜기"]["category"], "생활/건강")
        self.assertEqual(out["찜기"]["checked_at"], "2026-11-24")
        self.assertTrue(out["손난로"]["seasonal"]["is_seasonal"])
        self.assertEqual(out["키캡"]["trend"], "보합")
        ns.shopping_trends(items, fake, self.cache, today=TODAY)
        self.assertEqual(len(calls), 3)                                             # 같은 날 다시 부르지 않음
        self.assertEqual(self.cache.usage["2026-11"]["shopping"], 3)
        self.assertIn("계절성 있음(최고 12월", ns.summary(out["손난로"]))
        self.assertEqual(ns.summary(None), "쇼핑인사이트 분야 미정")


class AgentTest(unittest.TestCase):
    """supplier_agent.naver_lookup: 검색어 트렌드 + 쇼핑인사이트를 naver_json(cache[품목명]) 안에 같이 넣는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sa._NAVER_DISK[:] = [ns.NaverCache(Path(self.tmp.name) / "c.json", today=TODAY, log=lambda *a: None)]

    def tearDown(self):
        sa._NAVER_DISK.clear()
        self.tmp.cleanup()

    def fake_call(self, path, keys, body):
        if path == ns.SHOPPING_PATH:
            return shop_response(body, {"실리콘 찜기": [5] * 9 + [9, 9, 9]})
        months = sa._month_window()[0]
        return {"results": [{"title": g["groupName"], "data": [{"period": m.isoformat(), "ratio": 10}
                                                               for m in months]} for g in body["keywordGroups"]]}

    def test_lookup_stores_shopping_insight(self):
        cands = [{"name": "실리콘 찜기", "keywords_ko": ["실리콘 찜기"], "shop_category": "생활/건강"},
                 {"name": "분야 모름", "keywords_ko": ["무언가"], "shop_category": None}]
        cache = {}
        with mock.patch.object(sa, "_naver_call", side_effect=self.fake_call) as m:
            sa.naver_lookup(cands, ("id", "secret"), cache, log=lambda *a: None)
            calls = m.call_count
            sa.naver_lookup(cands, ("id", "secret"), {}, log=lambda *a: None)     # 같은 날 새 실행 → 디스크 캐시
            self.assertEqual(m.call_count, calls)
        si = cache["실리콘 찜기"]["shopping_insight"]
        self.assertEqual((si["cid"], si["trend"]), (ns.SHOP_CATEGORIES["생활/건강"], "오름"))
        self.assertNotIn("shopping_insight", cache["분야 모름"])
        self.assertEqual(cache["실리콘 찜기"]["shop_category"], "생활/건강")
        self.assertEqual(cache["실리콘 찜기"]["checked_at"], "2026-11-24")              # 네이버 조회일
        self.assertIsNone(cache["분야 모름"]["shop_category"])                        # 분야 미정이 기록에 남음
        self.assertIn("volume_idx", cache["분야 모름"])
        self.assertEqual(sa.shop_trend_cell(cache["실리콘 찜기"]), "오름 +80%")
        self.assertIn("쇼핑인사이트(생활/건강)", sa.naver_summary("실리콘 찜기", cache["실리콘 찜기"]))
        self.assertIn("분야 미정", sa.naver_summary("분야 모름", cache["분야 모름"]))
        json.dumps(cache, ensure_ascii=False)                                        # DB naver_json으로 저장 가능

    def test_shopping_failure_keeps_search_trend(self):
        def fail_shop(path, keys, body):
            if path == ns.SHOPPING_PATH:
                raise sa.NaverError("HTTP 403 구독 없음")
            return self.fake_call(path, keys, body)
        cache, logs = {}, []
        with mock.patch.object(sa, "_naver_call", side_effect=fail_shop):
            sa.naver_lookup([{"name": "찜기", "keywords_ko": ["찜기"], "shop_category": "생활/건강"}],
                            ("id", "secret"), cache, log=logs.append)
        self.assertIn("volume_idx", cache["찜기"])
        self.assertNotIn("shopping_insight", cache["찜기"])
        self.assertTrue(any("쇼핑인사이트" in x and "구독" in x for x in logs))

    def test_tool_schema_accepts_category(self):
        item = sa.SUBMIT_CANDIDATES_TOOL["input_schema"]["properties"]["candidates"]["items"]
        self.assertEqual(sa.validate("생활/건강", item["properties"]["shop_category"]), [])
        self.assertEqual(sa.validate(None, item["properties"]["shop_category"]), [])
        self.assertTrue(sa.validate("주방", item["properties"]["shop_category"]))


if __name__ == "__main__":
    unittest.main()
