"""eBay 공식 Browse API 조회 — 1번 시장조사용(미국 추천 후보 검증), 읽기 전용.

키워드 검색(buy/browse/v1/item_summary/search) → 미국 내 새 상품·즉시 구매 판매가, 판매자 수, 상품 상세 주소.
키: .env의 EBAY_CLIENT_ID (App ID) / EBAY_CLIENT_SECRET (Cert ID) — Production 키셋.
토큰은 키로 그때그때 받는 Application 토큰(OAuth client credentials, 약 2시간, 사용자 로그인 없음)이라
파일 저장·갱신 관리가 없다 (실행 중에만 메모리에 재사용). 키가 없거나 오류면 EbayError → 1번은 이 조회만 건너뜀.
"""
import base64
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
# 미국 내 판매자의 새 상품·즉시 구매만 (중고·경매 제외) — 배대지(미국)로 받는 기준
FILTER = "conditions:{NEW},buyingOptions:{FIXED_PRICE},itemLocationCountry:US,deliveryCountry:US,priceCurrency:USD"
MIN_FEEDBACK = 98.0   # 상품 상세 주소로 고를 판매자 평가 하한 (%)
TIMEOUT = 20
_TOKEN = ["", 0.0]    # (토큰, 만료 시각) — 실행 중 재사용


class EbayError(RuntimeError):
    pass


def _keys():
    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise EbayError(".env에 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 없음")
    return cid, secret


def status():
    """(사용 가능 여부, 이유). 네트워크 호출 없이 키만 확인."""
    try:
        _keys()
    except EbayError as e:
        return False, str(e)
    return True, ""


def _error_text(e):
    try:
        d = json.loads(e.read().decode("utf-8", "replace"))
    except (ValueError, OSError, AttributeError):
        return ""
    if d.get("error_description"):
        return d["error_description"]
    errs = d.get("errors") or [{}]
    return errs[0].get("message", "")


def token():
    tok, until = _TOKEN
    if tok and time.time() < until - 60:
        return tok
    cid, secret = _keys()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL, method="POST",
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        hint = " (Production 키셋인지, App ID·Cert ID를 정확히 넣었는지 확인)" if e.code == 401 else ""
        raise EbayError(f"토큰 발급 실패 HTTP {e.code} {_error_text(e)}{hint}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise EbayError(f"토큰 발급 실패: {e}") from None
    if not d.get("access_token"):
        raise EbayError("토큰 발급 실패: 응답에 토큰 없음")
    _TOKEN[:] = [d["access_token"], time.time() + float(d.get("expires_in") or 7200)]
    return _TOKEN[0]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_search(data):
    """검색 응답 → (전체 상품 수, [상품]). 상품 = {title, price, seller, feedback, url, id}."""
    if data.get("errors"):
        e = data["errors"][0] or {}
        raise EbayError(f"API 오류 {e.get('errorId', '')}: {e.get('message', '')}".strip())
    items = []
    for it in data.get("itemSummaries") or []:
        price = it.get("price") or {}
        value = _num(price.get("value"))
        url = str(it.get("itemWebUrl") or "").split("?", 1)[0]
        if price.get("currency") != "USD" or not value or not url:
            continue
        seller = it.get("seller") or {}
        items.append({"id": str(it.get("itemId") or ""), "title": str(it.get("title") or "").strip(), "price": value,
                      "seller": str(seller.get("username") or ""), "feedback": _num(seller.get("feedbackPercentage")),
                      "url": url})
    return int(data.get("total") or len(items)), items


def search(keyword, n=20):
    req = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode({"q": keyword, "filter": FILTER, "limit": n}),
        headers={"Authorization": f"Bearer {token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise EbayError(f"검색 실패 HTTP {e.code} {_error_text(e)}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise EbayError(f"검색 실패: {e}") from None
    return parse_search(data)


def summarize(keyword, total, items, top=5):
    """검색 결과 요약. 상품 상세 주소는 관련도 상위 top개 중 판매자 평가가 기준 이상인 첫 상품 (없으면 첫 상품)."""
    if not items:
        return {"keyword": keyword, "total": total, "count": 0}
    prices = sorted(i["price"] for i in items)
    good = [i for i in items[:top] if (i["feedback"] or 0) >= MIN_FEEDBACK]
    best = (good or items[:1])[0]
    return {"keyword": keyword, "total": total, "count": len(items),
            "price_min": prices[0], "price_median": round(statistics.median(prices), 2), "price_max": prices[-1],
            "sellers": len({i["seller"] for i in items if i["seller"]}),
            "page_url": best["url"], "page_title": best["title"]}


def summary_text(s):
    if not s or not s.get("count"):
        return f"eBay '{(s or {}).get('keyword', '')}' 미국 내 새 상품 검색 결과 없음"
    return (f"eBay '{s['keyword']}': 미국 내 새 상품 {s['total']:,}개 · 상위 {s['count']}개 판매가 "
            f"${s['price_min']:g}~${s['price_max']:g} (중간 ${s['price_median']:g}) · 판매자 {s['sellers']}명")
