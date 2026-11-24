"""AliExpress Open Platform (드롭쉬핑 DS 앱) 조회 — 1번 시장조사용, 읽기 전용.

- 후보 검증: 키워드 검색(aliexpress.ds.text.search) → 실제 소매가·주문 수·상품 상세 주소
- 발굴 아이디어(선택): 드롭쉬핑 인기 상품 목록(aliexpress.ds.recommend.feed.get) → 해외 판매량 상위 상품

키: .env의 ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET (제휴 Affiliate API는 이 앱에 권한이 없음 → DS API만 씀).
로그인 토큰: buyagent가 발급·갱신하는 토큰 파일을 **읽기만** 한다. 갱신하면 토큰이 바뀌어 다른 에이전트가 가진 토큰이
무효가 될 수 있어서, 갱신은 한 곳(buyagent)에서만 한다. 경로는 ALIEXPRESS_TOKEN_FILE로 바꿀 수 있다.
키·토큰이 없거나 만료·오류면 AliError → 1번은 이 조회만 건너뛰고 조사는 평소대로 한다.
"""
import hashlib
import hmac
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GATEWAY = "https://api-sg.aliexpress.com/sync"
DEFAULT_TOKEN_FILE = Path(__file__).resolve().parent.parent / "buyagent" / "buyagent" / "data" / "aliexpress_token.json"
# 발굴 아이디어용 인기 상품 목록 (DS 앱에서 조회되는 목록 중 생활·주방·스포츠·뷰티·전자·자동차·신상품·전 카테고리 판매 상위)
IDEA_FEEDS = (
    "DS_Home&Kitchen_bestsellers", "DS_Sports&Outdoors_bestsellers", "DS_Beauty_bestsellers",
    "DS_ConsumerElectronics_bestsellers", "DS_Automobile&Accessories_bestsellers", "DS_NewArrivals",
    "AEB_i69_FullCategory_TopSellers_20241225",
)
TIMEOUT = 20   # 초


class AliError(RuntimeError):
    pass


def token_file():
    return Path(os.environ.get("ALIEXPRESS_TOKEN_FILE") or DEFAULT_TOKEN_FILE)


def _keys():
    key = os.environ.get("ALIEXPRESS_APP_KEY", "").strip()
    secret = os.environ.get("ALIEXPRESS_APP_SECRET", "").strip()
    if not key or not secret:
        raise AliError(".env에 ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET 없음")
    return key, secret


def _token(now_ms=None):
    path = token_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AliError(f"로그인 토큰 파일 없음 ({path}) — buyagent에서 토큰 발급 필요") from None
    except (OSError, ValueError) as e:
        raise AliError(f"로그인 토큰 파일을 읽지 못함: {e}") from None
    tok = (data.get("access_token") or "").strip()
    if not tok:
        raise AliError("로그인 토큰 파일에 access_token 없음")
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    try:
        expire = float(data.get("expire_time") or 0)
    except (TypeError, ValueError):
        expire = 0
    if expire and now_ms >= expire - 60_000:
        raise AliError("로그인 토큰 만료 — buyagent에서 토큰 갱신 필요")
    return tok


def status():
    """(사용 가능 여부, 이유). 네트워크 호출 없이 키·토큰만 확인."""
    try:
        _keys()
        _token()
    except AliError as e:
        return False, str(e)
    return True, ""


def sign(params, secret):
    """IOP 서명: 이름순 '이름+값' 이어 붙여 HMAC-SHA256(App Secret) → 대문자 16진수."""
    text = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


def call(method, params):
    key, secret = _keys()
    p = {"method": method, "app_key": key, "sign_method": "sha256",
         "timestamp": str(int(time.time() * 1000)), "session": _token(), **{k: str(v) for k, v in params.items()}}
    p["sign"] = sign(p, secret)
    req = urllib.request.Request(GATEWAY, data=urllib.parse.urlencode(p).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AliError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AliError(f"접속 실패: {e}") from None
    except ValueError:
        raise AliError("응답 형식 오류") from None
    if "error_response" in data:
        e = data["error_response"] or {}
        raise AliError(f"API 오류 {e.get('code', '')}: {e.get('msg') or e.get('sub_msg') or ''}".strip())
    return data


def _num(v, cast=float):
    try:
        return cast(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def item_url(raw):
    """//www.aliexpress.com/item/123.html?sku... → https://www.aliexpress.com/item/123.html (추적 파라미터 제거)."""
    if not raw:
        return None
    url = ("https:" + raw) if raw.startswith("//") else raw
    return url.split("?", 1)[0]


def parse_search(data):
    """ds.text.search 응답 → (전체 상품 수, [상품]). 상품 = {title, price, orders, rating, url, id}."""
    body = data.get("aliexpress_ds_text_search_response") or {}
    if str(body.get("code", "00")) not in ("00", "0"):
        raise AliError(f"검색 오류 {body.get('code')}: {body.get('msg') or ''}".strip())
    d = body.get("data") or {}
    raw = ((d.get("products") or {}).get("selection_search_product")) or []
    items = []
    for it in raw:
        price = _num(it.get("targetSalePrice"))
        url = item_url(it.get("itemUrl"))
        if not price or not url:
            continue
        items.append({"id": str(it.get("itemId") or ""), "title": it.get("title") or "", "price": price,
                      "orders": _num(it.get("orders"), int) or 0, "rating": _num(it.get("score")), "url": url})
    return _num(d.get("totalCount"), int) or len(items), items


def search(keyword, n=10):
    """한국 배송 기준 USD 소매가로 키워드 검색."""
    data = call("aliexpress.ds.text.search", {"keyWord": keyword, "local": "en_US", "countryCode": "KR",
                                               "currency": "USD", "pageSize": n, "pageIndex": 1})
    return parse_search(data)


def summarize(keyword, total, items, top=5):
    """검색 결과 요약. 상품 상세 주소는 관련도 상위 top개 중 주문이 가장 많은 상품."""
    if not items:
        return {"keyword": keyword, "total": total, "count": 0}
    prices = sorted(i["price"] for i in items)
    best = max(items[:top], key=lambda i: i["orders"])
    return {"keyword": keyword, "total": total, "count": len(items),
            "price_min": prices[0], "price_median": round(statistics.median(prices), 2), "price_max": prices[-1],
            "top_orders": max(i["orders"] for i in items),
            "page_url": best["url"], "page_title": best["title"], "page_orders": best["orders"]}


def summary_text(s):
    if not s or not s.get("count"):
        return f"AliExpress '{(s or {}).get('keyword', '')}' 검색 결과 없음"
    return (f"AliExpress '{s['keyword']}': 상품 {s['total']:,}개 · 상위 {s['count']}개 소매가 "
            f"${s['price_min']:g}~${s['price_max']:g} (중간 ${s['price_median']:g}) · 최다 주문 {s['top_orders']:,}건")


def parse_feed(data):
    res = (data.get("aliexpress_ds_recommend_feed_get_response") or {}).get("result") or {}
    raw = ((res.get("products") or {}).get("traffic_product_d_t_o")) or []
    out = []
    for it in raw:
        price = _num(it.get("target_sale_price"))
        if not price:
            continue
        out.append({"id": str(it.get("product_id") or ""), "title": (it.get("product_title") or "").strip(),
                    "category": it.get("second_level_category_name") or it.get("first_level_category_name") or "",
                    "price": price, "volume": _num(it.get("lastest_volume"), int) or 0,
                    "url": item_url(it.get("product_detail_url"))})
    return out


def bestsellers(feeds=IDEA_FEEDS, per_feed=50, limit=40, log=print):
    """여러 인기 목록에서 상품을 받아 최근 판매량 순 상위 limit개 (같은 상품은 한 번). 전부 실패하면 AliError."""
    seen, items, errors = set(), [], []
    for feed in feeds:
        try:
            got = parse_feed(call("aliexpress.ds.recommend.feed.get", {
                "feed_name": feed, "target_currency": "USD", "target_language": "KO", "country": "KR",
                "page_size": per_feed, "page_no": 1}))
        except AliError as e:
            errors.append(f"{feed}: {e}")
            continue
        for it in got:
            if it["id"] and it["id"] not in seen:
                seen.add(it["id"])
                items.append(it)
    if not items:
        raise AliError("인기 상품 목록을 하나도 받지 못함" + (f" ({errors[0]})" if errors else ""))
    for e in errors:
        log(f"    · AliExpress 인기 목록 건너뜀: {e}")
    items.sort(key=lambda i: i["volume"], reverse=True)
    return items[:limit]
