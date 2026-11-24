"""해외 실측(AliExpress·eBay 검증, AliExpress 발굴 아이디어) — 네트워크 없이 응답 파싱과 1번 연결 부분만 시험."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aliexpress_api as ali
import ebay_api as ebay
import supplier_agent as sa

SEARCH = {"aliexpress_ds_text_search_response": {"code": "00", "data": {"totalCount": 120, "products": {
    "selection_search_product": [
        {"itemId": "1", "title": "A", "targetSalePrice": "7.17", "orders": "32", "score": "4.8",
         "itemUrl": "//www.aliexpress.com/item/1.html?skuId=9&x=1"},
        {"itemId": "2", "title": "B", "targetSalePrice": "3.50", "orders": "1,200",
         "itemUrl": "//www.aliexpress.com/item/2.html?skuId=8"},
        {"itemId": "3", "title": "가격 없음", "targetSalePrice": "", "itemUrl": "//www.aliexpress.com/item/3.html"},
    ]}}}}


class ParseTest(unittest.TestCase):
    def test_search_and_summary(self):
        total, items = ali.parse_search(SEARCH)
        self.assertEqual(total, 120)
        self.assertEqual([i["url"] for i in items], ["https://www.aliexpress.com/item/1.html",
                                                    "https://www.aliexpress.com/item/2.html"])   # 추적 파라미터 제거
        s = ali.summarize("steamer", total, items)
        self.assertEqual((s["price_min"], s["price_max"], s["top_orders"]), (3.5, 7.17, 1200))
        self.assertEqual(s["page_url"], "https://www.aliexpress.com/item/2.html")              # 주문 많은 상품
        self.assertTrue(sa.is_source_page(s["page_url"]))
        self.assertIn("최다 주문 1,200건", ali.summary_text(s))

    def test_error_response(self):
        with self.assertRaises(ali.AliError):
            ali.parse_search({"aliexpress_ds_text_search_response": {"code": "500", "msg": "x"}})

    def test_feed(self):
        data = {"aliexpress_ds_recommend_feed_get_response": {"result": {"products": {"traffic_product_d_t_o": [
            {"product_id": 5, "product_title": "찜기", "second_level_category_name": "주방", "target_sale_price": "2.1",
             "lastest_volume": 300, "product_detail_url": "https://ko.aliexpress.com/item/5.html?pdp=1"}]}}}}
        self.assertEqual(ali.parse_feed(data)[0]["url"], "https://ko.aliexpress.com/item/5.html")


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.json"
        self.env = mock.patch.dict(os.environ, {"ALIEXPRESS_TOKEN_FILE": str(self.path),
                                                "ALIEXPRESS_APP_KEY": "k", "ALIEXPRESS_APP_SECRET": "s"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_missing_and_expired(self):
        self.assertFalse(ali.status()[0])
        self.path.write_text(json.dumps({"access_token": "x", "expire_time": 1000}), encoding="utf-8")
        ok, why = ali.status()
        self.assertFalse(ok)
        self.assertIn("만료", why)
        self.path.write_text(json.dumps({"access_token": "x", "expire_time": 9e15}), encoding="utf-8")
        self.assertEqual(ali.status(), (True, ""))


EBAY = {"total": 3144, "itemSummaries": [
    {"itemId": "v1|1|0", "title": "Card game", "price": {"value": "14.83", "currency": "USD"},
     "seller": {"username": "a", "feedbackPercentage": "95.0"}, "itemWebUrl": "https://www.ebay.com/itm/111?hash=x"},
    {"itemId": "v1|2|0", "title": "Card game 2", "price": {"value": "9.99", "currency": "USD"},
     "seller": {"username": "b", "feedbackPercentage": "99.6"}, "itemWebUrl": "https://www.ebay.com/itm/222?hash=y"},
]}


class EbayParseTest(unittest.TestCase):
    def test_search_and_summary(self):
        total, items = ebay.parse_search(EBAY)
        s = ebay.summarize("card game", total, items)
        self.assertEqual((total, s["sellers"], s["price_min"]), (3144, 2, 9.99))
        self.assertEqual(s["page_url"], "https://www.ebay.com/itm/222")        # 판매자 평가 98% 이상 우선
        self.assertTrue(sa.is_source_page(s["page_url"]))
        self.assertIn("판매자 2명", ebay.summary_text(s))

    def test_error_and_keys(self):
        with self.assertRaises(ebay.EbayError):
            ebay.parse_search({"errors": [{"errorId": 1001, "message": "bad"}]})
        with mock.patch.dict(os.environ, {"EBAY_CLIENT_ID": "", "EBAY_CLIENT_SECRET": ""}):
            self.assertFalse(ebay.status()[0])


class AgentTest(unittest.TestCase):
    def cands(self):
        return [{"name": "실리콘 찜기", "search_keyword_en": "silicone steamer", "source_country": "China",
                 "score_notes": "수요 3", "source_page_url": None},
                {"name": "일본 문구", "search_keyword_en": "japanese stationery", "source_country": "Japan",
                 "score_notes": "", "source_page_url": None},
                {"name": "영문 카드게임", "search_keyword_en": "card game", "source_country": "USA",
                 "score_notes": "", "source_page_url": None}]

    def fakes(self):
        return (mock.patch.object(ali, "search", return_value=ali.parse_search(SEARCH)),
                mock.patch.object(ebay, "search", return_value=ebay.parse_search(EBAY)))

    def test_lookup_by_country_and_apply(self):
        fa, fe = self.fakes()
        with fa as ma, fe as me:
            cache = sa.market_lookup(self.cands(), {}, ["China", "USA"])
            sa.market_lookup(self.cands(), cache, ["China", "USA"])            # 같은 키워드는 다시 안 부름
        self.assertEqual((ma.call_count, me.call_count), (1, 1))
        c = self.cands()
        self.assertEqual(sa.apply_market(c, cache), {"AliExpress": 1, "eBay": 1})
        self.assertEqual(c[0]["source_page_url"], "https://www.aliexpress.com/item/2.html")
        self.assertEqual(c[2]["source_page_url"], "https://www.ebay.com/itm/222")
        self.assertIn("[AliExpress 실측]", c[0]["score_notes"])
        self.assertIn("[eBay 실측]", c[2]["score_notes"])
        self.assertIsNone(c[1]["source_page_url"])                           # 일본은 실측 사이트 없음
        self.assertEqual(sa.market_extra(c[0]["_market"]), "최다 주문 1,200건")
        self.assertEqual(sa.market_extra(c[2]["_market"]), "판매자 2명")
        c[0]["source_page_url"] = "https://detail.1688.com/offer/1.html"
        sa.apply_market([c[0]], cache)
        self.assertEqual(c[0]["source_page_url"], "https://detail.1688.com/offer/1.html")   # 모델이 찾은 주소 우선

    def test_disabled_country_not_called(self):
        fa, fe = self.fakes()
        with fa as ma, fe as me:
            cache = sa.market_lookup(self.cands(), {}, ["China"])               # eBay 키 없음
        self.assertEqual((ma.call_count, me.call_count), (1, 0))
        self.assertEqual(sa.apply_market(self.cands(), cache), {"AliExpress": 1})

    def test_lookup_failure_is_skipped(self):
        with mock.patch.object(ali, "search", side_effect=ali.AliError("접속 실패")),                 mock.patch.object(ebay, "search", side_effect=ebay.EbayError("HTTP 500")):
            cache = sa.market_lookup(self.cands(), {}, ["China", "USA"], log=lambda *a: None)
        self.assertEqual(sa.apply_market(self.cands(), cache), {})

    def test_combined_review(self):
        sub = {"candidates": self.cands()}
        fa, fe = self.fakes()
        with fa, fe:
            only = sa.combined_review(None, {}, ["China", "USA"], log=lambda *a: None)(sub)
            both = sa.combined_review(lambda s: "네이버 데이터… 다시 제출하세요.", {}, ["China"], log=lambda *a: None)(sub)
        self.assertIn("AliExpress 실측", only)
        self.assertIn("eBay 실측", only)
        self.assertIn("submit_candidates로 다시 제출", only)
        self.assertTrue(both.startswith("AliExpress 실측") and both.endswith("다시 제출하세요."))
        self.assertNotIn("eBay", both)
        self.assertIsNone(sa.combined_review(None, {}, [])(sub))


if __name__ == "__main__":
    unittest.main()
