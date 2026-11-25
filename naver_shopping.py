"""네이버 쇼핑인사이트(NAVER API HUB Data Lab) — 키워드별 12개월 쇼핑 클릭 추세, 그리고 네이버 API 캐시·사용량.

'수요 추세'로만 쓴다 (가격·판매처 정보가 아님). 값(ratio)은 조회 구간 최대=100인 상대값이라 키워드끼리 크기 비교는 하지
않고, 키워드마다 최근 3개월과 그 전 3개월의 비율(추세)과 특정 달 쏠림(계절성)만 본다.
API: POST {NAVER_API_HOST}/shopping/v1/category/keywords — 분야(cid) 하나 안에서 키워드 최대 5개
     (문서: https://api.ncloud-docs.com/docs/naver-api-hub-shopping-insight-keywords)
키: supplier_agent/.env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (검색어 트렌드와 같은 앱). 값은 로그·리포트에 쓰지 않음.
"""
import json
import os
from datetime import date
from pathlib import Path

SHOPPING_PATH = "/shopping/v1/category/keywords"

# --- 설정 (2026-11-24, buyagent 세션 요청서 기준) ---------------------------------------------
# 네이버쇼핑 최상위 분야 cid (쇼핑인사이트 분야 코드 = 네이버쇼핑 카테고리 URL의 cat_id). 면세점은 제외
SHOP_CATEGORIES = {
    "패션의류": "50000000", "패션잡화": "50000001", "화장품/미용": "50000002", "디지털/가전": "50000003",
    "가구/인테리어": "50000004", "출산/육아": "50000005", "식품": "50000006", "스포츠/레저": "50000007",
    "생활/건강": "50000008", "여가/생활편의": "50000009", "도서": "50005542",
}
TREND_UP, TREND_DOWN = 1.15, 0.85      # 최근 3개월 평균 ÷ 그 전 3개월 평균: 이상이면 오름, 이하면 내림, 사이면 보합
SEASONAL_PEAK_RATIO = 2.0              # 12개월 중 최고 달이 12개월 평균의 이 배 이상이면 계절성 있음
MONTHLY_LIMIT = {"shopping": 50_000, "trend": 50_000}   # NAVER API HUB 앱 'trade-agent' 월 한도 (2026-11 확인)
KEYWORDS_PER_CALL = 1                  # 키워드마다 자기 최대=100 (2026-11-24 실제 응답 확인: 묶으면 작은 키워드가 0 근처로)
WARN_AT = 0.8                          # 월 사용량이 한도의 80%를 넘으면 경고 (이 에이전트가 부른 횟수만 셈)
CACHE_FILE = Path(__file__).resolve().parent / "data" / "naver_cache.json"   # data/ 는 git 제외
# --------------------------------------------------------------------------------------------


def months_back(today=None, n=12):
    """이번 달을 뺀 최근 n개월의 첫날 목록과 조회 시작·끝 날짜."""
    today = today or date.today()
    first = date(today.year, today.month, 1)
    months = []
    for back in range(n, 0, -1):
        y, m = divmod(today.year * 12 + today.month - 1 - back, 12)
        months.append(date(y, m + 1, 1))
    end = date.fromordinal(first.toordinal() - 1)
    return months, months[0].isoformat(), end.isoformat()


class NaverCache:
    """같은 날 같은 조회는 한 번만 부르게 하는 캐시 + 월별 호출 횟수. 파일 하나에 저장 (오늘 것만 남김)."""

    def __init__(self, path=CACHE_FILE, today=None, log=print):
        self.path, self.log = Path(path), log
        self.today = (today or date.today()).isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.entries = data.get("entries", {}) if data.get("date") == self.today else {}
        self.usage = data.get("usage", {})
        self.warned = set()

    def get(self, key):
        return self.entries.get(key)

    def has(self, key):
        return key in self.entries

    def put(self, key, value):
        self.entries[key] = value

    def count(self, kind):
        """API 호출 1회를 센다 (kind = shopping / trend). 한도의 WARN_AT을 넘으면 한 번 경고."""
        month = self.today[:7]
        by = self.usage.setdefault(month, {})
        by[kind] = by.get(kind, 0) + 1
        limit = MONTHLY_LIMIT.get(kind)
        if limit and by[kind] >= limit * WARN_AT and kind not in self.warned:
            self.warned.add(kind)
            name = {"shopping": "쇼핑인사이트", "trend": "검색어 트렌드"}.get(kind, kind)
            self.log(f"    ⚠️ 네이버 {name} 이번 달 호출 {by[kind]:,}회 — 월 한도 {limit:,}회의 {WARN_AT:.0%} 넘음 "
                     "(이 에이전트가 부른 횟수만 셈, 같은 앱 키를 쓰는 다른 프로그램 사용량은 콘솔에서 확인)")

    def save(self):
        months = sorted(self.usage)[-3:]                       # 사용량은 최근 3개월만 보관
        data = {"date": self.today, "entries": self.entries, "usage": {m: self.usage[m] for m in months}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            self.log(f"    · 네이버 캐시 저장 실패 (다음에 다시 조회함): {e}")


def analyze(series, months):
    """12개월 ratio 목록 → 추세·계절성. 값이 거의 없으면 trend None."""
    out = {"months": [m.isoformat()[:7] for m in months], "series": [round(v, 2) for v in series],
           "change_3m": None, "trend": None, "seasonal": None}
    if len(series) < 6 or not any(series):
        return out
    recent, prev = sum(series[-3:]) / 3, sum(series[-6:-3]) / 3
    if prev > 0:
        r = recent / prev
        out["change_3m"] = round((r - 1) * 100)
        out["trend"] = "오름" if r >= TREND_UP else "내림" if r <= TREND_DOWN else "보합"
    elif recent > 0:
        out["trend"] = "오름"
    mean = sum(series) / len(series)
    peak_i = max(range(len(series)), key=lambda i: series[i])
    ratio = series[peak_i] / mean if mean else 0
    out["seasonal"] = {"is_seasonal": ratio >= SEASONAL_PEAK_RATIO, "peak_month": out["months"][peak_i],
                       "peak_to_mean": round(ratio, 2)}
    return out


def shopping_trends(items, call, cache, today=None):
    """items: [(이름, 키워드, cid)] → {이름: 쇼핑인사이트 dict}. call(path, body) = 네이버 API 호출.
    같은 날 같은 (cid, 키워드)는 캐시에서. API는 한 번에 키워드 5개까지 되지만 값이 묶음 안 최대=100 기준이라
    작은 키워드가 0 근처로 뭉개져 추세를 못 본다 → 키워드마다 따로 부른다 (KEYWORDS_PER_CALL)."""
    months, start, end = months_back(today)
    out, todo = {}, {}
    for name, kw, cid in items:
        key = f"shopping|{cid}|{kw}"
        if cache.has(key):
            out[name] = cache.get(key)
        else:
            todo.setdefault(cid, []).append((name, kw, key))
    for cid, rows in todo.items():
        for i in range(0, len(rows), KEYWORDS_PER_CALL):
            batch = rows[i:i + KEYWORDS_PER_CALL]
            body = {"startDate": start, "endDate": end, "timeUnit": "month", "category": cid,
                    "keyword": [{"name": f"k{j}", "param": [kw]} for j, (_, kw, _) in enumerate(batch)]}
            res = call(SHOPPING_PATH, body)
            cache.count("shopping")
            got = {}
            for r in res.get("results", []):
                by = {d["period"][:7]: d["ratio"] for d in r.get("data", [])}
                got[r.get("title")] = [float(by.get(m.isoformat()[:7], 0.0)) for m in months]
            for j, (name, kw, key) in enumerate(batch):
                info = {"cid": cid, "category": next((k for k, v in SHOP_CATEGORIES.items() if v == cid), cid),
                        "keyword": kw, "checked_at": (today or date.today()).isoformat(), **analyze(got.get(f"k{j}", [0.0] * len(months)), months)}
                cache.put(key, info)
                out[name] = info
    return out


def summary(si):
    """한 줄 요약 (모델·리포트용). 수요 추세만."""
    if not si:
        return "쇼핑인사이트 분야 미정"
    s = si.get("seasonal") or {}
    season = (f" · 계절성 있음(최고 {int(s['peak_month'][5:])}월, 평균의 {s['peak_to_mean']:g}배)" if s.get("is_seasonal")
              else " · 계절성 약함" if s else "")
    if si.get("trend") is None:
        if not s:
            return f"쇼핑인사이트({si['category']}) 12개월 클릭 데이터 거의 없음"
        return f"쇼핑인사이트({si['category']}) 최근 6개월 클릭 거의 없음{season}"   # 예: 겨울 한철 품목
    ch = "" if si.get("change_3m") is None else f" {si['change_3m']:+d}%"
    return f"쇼핑인사이트({si['category']}) 12개월 클릭 추세: 최근 3개월{ch} {si['trend']}{season}"
