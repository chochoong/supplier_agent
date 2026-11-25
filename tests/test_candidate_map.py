"""리포트 후보 지도(인라인 SVG) — 네트워크 없음."""
import unittest

import supplier_agent as sa


def cand(pid, total, vol, margin=3, trend="오름"):
    return {"_id": pid, "name": f"품목{pid}", "_total": total, "scores": {"margin": margin},
            "_naver": None if vol is None else {"volume_idx": vol, "growth_3m": 5, "yoy": None,
                                                "shopping_insight": {"trend": trend}}}


class CandidateMapTest(unittest.TestCase):
    def test_draws_points_links_and_missing(self):
        html = sa.candidate_map([cand(1, 70, 8.5), cand(2, 50, 0.0, trend="내림"), cand(3, 60, None)])
        self.assertIn("<svg class='cmap'", html)
        self.assertEqual(html.count("<circle"), 2)
        self.assertIn('href="#c1"', html)
        self.assertIn("var(--bad)", html)                       # 내림 색
        self.assertIn("빠진 후보 1개: #3 품목3", html)
        self.assertIn("전년 미확인", html)

    def test_skips_when_too_few(self):
        self.assertEqual(sa.candidate_map([cand(1, 70, 3), cand(2, 60, None)]), "")


if __name__ == "__main__":
    unittest.main()
