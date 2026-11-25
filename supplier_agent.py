#!/usr/bin/env python3
"""
시장조사·아이템 발굴 에이전트 (1인 수입 무역 파이프라인의 1번 에이전트)

국내외 트렌드·검색량·경쟁 상품 가격·리뷰를 조사해 "수요는 있는데 공급(경쟁)이 적은" 수입 후보 품목을
찾아 점수를 매긴다. 공급처 섭외·협상·통관·원가 정산은 다른 에이전트의 일이므로 하지 않는다.

사용 예:
  python supplier_agent.py                                   # 후보 10개 점수표
  python supplier_agent.py --hint "주방·수납, 1만~3만원대"    # 방향 지정
  python supplier_agent.py --count 5 --provider openrouter

결과물:
  - reports/YYYY-MM-DD_HHMM_discover/ : candidates.html(점수표) · candidates.csv · raw.json
  - 공용 DB(trade_hub): 후보 품목(stage=candidate)과 점수 → 다음 단계 에이전트·슈퍼바이저가 읽음
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from trade_hub import db as trade_db      # 공용 DB (C:/workspace/trade_hub)
except ImportError:
    sys.exit("trade_hub 패키지가 필요합니다:  pip install -r requirements.txt  (../trade_hub를 설치합니다)")
import aliexpress_api as ali
import discover_store
import ebay_api as ebay
import naver_shopping
from naver_shopping import SHOP_CATEGORIES

try:
    import anthropic
except ImportError:
    sys.exit("anthropic 패키지가 필요합니다:  pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-sonnet-5-5"   # 더 깊은 조사가 필요하면 --model claude-opus-5-5
MAX_TOKENS = 16000

# 품목 하나만의 문제가 아니라 계속 진행해도 같은 오류가 날 API 오류 → 즉시 중단
FATAL_API_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.BadRequestError,
)

WEB_SEARCH_TOOL = {
    "type": "web_search_20260318",
    "name": "web_search",
    "max_uses": 15,
    "allowed_callers": ["direct"],
    "user_location": {
        "type": "approximate",
        "country": "KR",
        "timezone": "Asia/Seoul",
    },
}

# DeepSeek의 Anthropic 호환 API는 문서상 이 버전만 지원 (allowed_callers·user_location 언급 없음)
DEEPSEEK_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 15,
}


# OpenRouter 자체 웹 검색 서버 도구 (DeepSeek 등 어떤 모델에도 동작, 기본 엔진 Exa)
OPENROUTER_WEB_SEARCH_TOOL = {
    "type": "openrouter:web_search",
    "parameters": {"max_uses": 15, "max_results": 5},
}
OPENROUTER_BASE_URL = "https://openrouter.ai/api"   # SDK가 /v1/messages를 붙임
OPENROUTER_DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def make_backend(provider, model_arg):
    """접속처별 (client, model, 웹검색 도구, 추가 요청 옵션, 표시 이름)을 만든다.
    키는 각 접속처 것만 명시적으로 넘겨서, 다른 서비스 키가 섞여 전송되지 않게 한다."""
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            sys.exit("OPENROUTER_API_KEY를 .env에 설정하세요.")
        client = anthropic.Anthropic(auth_token=key, base_url=OPENROUTER_BASE_URL)
        model = model_arg or os.environ.get("OPENROUTER_MODEL") or OPENROUTER_DEFAULT_MODEL
        return client, model, OPENROUTER_WEB_SEARCH_TOOL, {}, "OpenRouter"

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY를 설정하세요. .env 파일에 넣거나 환경변수로 지정합니다. (README 참고)")
    client = anthropic.Anthropic(api_key=key)   # ANTHROPIC_BASE_URL이 있으면 그 주소 사용
    model = model_arg or DEFAULT_MODEL
    if "deepseek" in os.environ.get("ANTHROPIC_BASE_URL", "").lower():
        return client, model, DEEPSEEK_WEB_SEARCH_TOOL, {}, "DeepSeek"

    extra = {}
    effort = os.environ.get("ANTHROPIC_EFFORT", "").strip().lower()
    if effort in EFFORT_LEVELS:
        extra["output_config"] = {"effort": effort}
    elif effort:
        print(f"⚠️ ANTHROPIC_EFFORT={effort!r}는 무시합니다 ({'/'.join(EFFORT_LEVELS)} 중 하나여야 함)")
    return client, model, WEB_SEARCH_TOOL, extra, "Anthropic"


# 후보 품목 평가 항목 (키, 표시 이름, 총점 가중치, 평가 기준)
SCORE_ITEMS = [
    ("demand", "수요", 0.30, "검색량·판매량·리뷰 수·시즌 수요 근거가 강할수록 높게"),
    ("margin", "마진 여지", 0.25, "국내 판매가 대비 해외 FOB 단가가 낮을수록 높게"),
    ("competition", "경쟁 여유", 0.20, "쿠팡·네이버에 같은 상품 판매자가 적을수록 높게 (레드오션 = 1)"),
    ("regulation", "규제 용이", 0.15, "KC·전파·식약처 등 인증 부담이 낮을수록 높게"),
    ("logistics", "물류 용이", 0.10, "작고 가볍고 파손 위험이 낮을수록 높게"),
]

REF_URL_DESC = ("이 품목이 어떤 물건인지 보여 주는 대표 상품 페이지 URL 1~3개 (좋은 순서). "
                "검색 결과에서 실제로 본 URL만 (지어내지 말 것). 없으면 빈 배열")

# 추천 공급 국가 = 2번(공급처 발굴)이 조사할 수 있는 나라 (trade_hub db.SOURCE_COUNTRIES 와 같게)
SOURCE_COUNTRIES = {"China": "중국", "Japan": "일본", "USA": "미국"}
SOURCE_COUNTRY_DESC = ("이 품목을 들여올 가장 알맞은 공급 국가. China=중국(공산품·저단가·OEM), Japan=일본(일본 브랜드·문구·화장품·"
                       "캐릭터·품질 중시 생활용품), USA=미국(미국 브랜드·eBay 중고·희소품·빈티지·미국 전용 상품). "
                       "조사에서 확인한 주 생산지·판매처를 근거로 고르고, 애매하면 단가가 유리한 쪽")

SOURCE_PAGE_DESC = ("조사 중 실제로 열어 본 해외 구매처의 '상품 상세' 페이지 URL 1개 (추천 공급 국가의 판매자 우선). "
                    "예: alibaba.com/product-detail/..., detail.1688.com/offer/..., aliexpress.com/item/..., "
                    "item.rakuten.co.jp/..., ebay.com/itm/..., amazon.com/.../dp/.... 다음 단계가 이 판매자를 첫 후보로 검증합니다. "
                    "검색 결과·카테고리·쇼룸 페이지는 안 됨. 국내 쇼핑몰도 안 됨. 없으면 null (지어내지 말 것)")
# 해외 구매처 상품 상세 페이지로 인정하는 주소 (검색·카테고리·쇼룸 페이지는 제외)
SOURCE_PAGE_PATTERNS = [
    r"alibaba\.com/product-detail/", r"detail\.1688\.com/offer/", r"aliexpress\.(com|us)/item/",
    r"made-in-china\.com/.*product", r"(item\.taobao|detail\.tmall)\.com/item\.htm", r"dhgate\.com/product/",
    r"globalsources\.com/.*product", r"item\.rakuten\.co\.jp/", r"amazon\.(com|co\.jp)/(.*/)?(dp|gp/product)/",
    r"store\.shopping\.yahoo\.co\.jp/[^/]+/[^/]+\.html", r"netsea\.jp/.*(goods|item)", r"superdelivery\.com/(en/)?p/",
    r"ebay\.com/itm/", r"walmart\.com/ip/", r"thomasnet\.com/.*(product|profile)",
]


def is_source_page(url):
    return (isinstance(url, str) and re.match(r"https?://", url) is not None
            and any(re.search(pat, url, re.I) for pat in SOURCE_PAGE_PATTERNS))


SUBMIT_CANDIDATES_TOOL = {
    "name": "submit_candidates",
    "description": "조사한 수입 후보 품목들을 항목별 평가 점수와 함께 제출합니다. 확인 못한 값은 null로 둡니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "품목명 (한국어, 구체적으로)"},
                        "search_keyword_en": {"type": "string", "description": "공급처 검색용 영어 키워드"},
                        "category": {"type": "string", "description": "카테고리 (예: 주방, 캠핑, 반려동물)"},
                        "keywords_ko": {"type": "array", "items": {"type": "string"},
                                        "description": "네이버 검색량 조회용 짧은 한국어 키워드 1~3개. 소비자가 실제로 "
                                                       "검색할 말로 (예: '주방 벽선반', '무타공 선반'). 첫 번째가 대표 키워드"},
                        "shop_category": {"type": ["string", "null"], "enum": list(SHOP_CATEGORIES) + [None],
                                          "description": "네이버쇼핑 최상위 분야 (쇼핑인사이트 클릭 추세 조회용). "
                                                         "이 품목을 네이버쇼핑에서 찾으면 나오는 분야. 애매하면 null"},
                        "reason": {"type": "string", "description": "수요·트렌드 근거 (확인한 수치·출처 포함)"},
                        "est_retail_krw": {"type": ["number", "null"], "description": "국내 판매가 시세 (원, 중간값)"},
                        "est_fob_usd": {"type": ["number", "null"], "description": "해외 FOB 단가 시세 (USD, 중간값)"},
                        "regulations": {"type": ["string", "null"], "description": "필요한 인증·규제"},
                        "risk": {"type": ["string", "null"], "description": "주의할 위험 요소"},
                        "ref_urls": {"type": "array", "items": {"type": "string"}, "description": REF_URL_DESC},
                        "source_page_url": {"type": ["string", "null"], "description": SOURCE_PAGE_DESC},
                        "source_country": {"type": "string", "enum": list(SOURCE_COUNTRIES), "description": SOURCE_COUNTRY_DESC},
                        "source_country_reason": {"type": "string",
                                                  "description": "그 나라를 고른 이유 한 줄 (예: '주 생산지가 중국 선전, 알리바바 FOB 확인')"},
                        "scores": {
                            "type": "object",
                            "description": "각 항목 1~5점 (5가 가장 유리)",
                            "properties": {k: {"type": "number", "minimum": 1, "maximum": 5, "description": f"{label}: {rule}"}
                                           for k, label, _, rule in SCORE_ITEMS},
                            "required": [k for k, *_ in SCORE_ITEMS],
                        },
                        "score_notes": {"type": "string", "description": "점수 근거 요약 (항목별 한 줄)"},
                    },
                    "required": ["name", "search_keyword_en", "category", "keywords_ko", "reason", "source_country",
                                 "scores", "score_notes"],
                },
            }
        },
        "required": ["candidates"],
    },
}

SYSTEM_PROMPT = """당신은 한국에서 1인 수입 무역을 하는 사용자를 돕는 '시장조사·아이템 발굴 에이전트'입니다.

역할: 국내외 트렌드, 경쟁 상품 가격, 검색량, 리뷰를 조사해 "수요는 있는데 공급(경쟁)이 적은" 수입 후보 품목을 찾고 평가합니다.

원칙:
- 반드시 web_search로 실제 정보를 확인하고, 확인되지 않은 값은 null로 둡니다. 절대 추측으로 채우지 않습니다.
- 1인 수입에 적합한 조건(소량 발주, 작은 부피·무게, 규제 부담 낮음)을 우선합니다.
- 브랜드 위조품·의약품·배터리 내장 등 고위험 품목은 위험을 명확히 밝힙니다.
- 공급처 섭외, 견적 요청, 통관 서류, 원가 정산은 다른 에이전트의 일이므로 하지 않습니다. 품목 발굴과 평가에만 집중합니다.
- 조사가 끝나면 반드시 지정된 submit 도구로 결과를 제출합니다. 산문으로 답하지 않습니다."""

# ---------------------------------------------------------------------------
# 제출값 검증 (스키마의 필수 필드·타입·enum 확인)
# ---------------------------------------------------------------------------
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value, schema, path="입력"):
    """schema에 맞지 않는 부분을 오류 메시지 목록으로 돌려준다 (빈 목록이면 통과)."""
    errors = []
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        if not any(_TYPE_CHECKS[t](value) for t in types):
            return [f"{path}: {'/'.join(types)} 타입이어야 합니다 (받은 값: {value!r:.60})"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {schema['enum']} 중 하나여야 합니다 (받은 값: {value!r})")
    if _TYPE_CHECKS["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {schema['minimum']} 이상이어야 합니다 (받은 값: {value!r})")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {schema['maximum']} 이하여야 합니다 (받은 값: {value!r})")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 필수 항목이 없습니다")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


# ---------------------------------------------------------------------------
# 에이전트 실행 루프
# ---------------------------------------------------------------------------
def run_agent(backend, prompt, submit_tool, log=print, review=None, system=None, search=True):
    """web_search로 조사하게 한 뒤, submit_tool 호출 내용을 받아 반환한다.
    review(제출값)가 문자열을 돌려주면 그 내용(예: 실측 데이터)을 모델에게 보여주고 한 번 다시 제출받는다.
    system·search로 시스템 프롬프트와 웹 검색 사용 여부를 바꿀 수 있다 (슈퍼바이저 검토관이 재사용)."""
    client, model, search_tool, extra, _ = backend
    tools = [search_tool, submit_tool] if search else [submit_tool]
    messages = [{"role": "user", "content": prompt}]
    nudged = False
    reviewed = False
    retries = 0

    for _ in range(12):  # 안전장치
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system or SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            **extra,
        )

        searches = [b for b in resp.content if b.type == "server_tool_use"]
        for s in searches:
            q = getattr(s, "input", {}).get("query", "")
            log(f"    🔎 검색: {q}")

        submit = next(
            (b for b in resp.content if b.type == "tool_use" and b.name == submit_tool["name"]),
            None,
        )
        if resp.stop_reason == "refusal":
            d = resp.stop_details
            detail = f" (분류: {d.category}, 사유: {d.explanation})" if d else ""
            raise RuntimeError(f"모델이 안전 정책으로 요청을 거절했습니다{detail}")

        if submit:
            if resp.stop_reason == "max_tokens":
                errors = ["응답이 max_tokens 한도에서 잘려 제출 내용이 불완전합니다. 더 간결하게 다시 제출하세요."]
            else:
                errors = validate(submit.input, submit_tool["input_schema"])
            if not errors:
                feedback = None
                if review and not reviewed:
                    reviewed = True
                    feedback = review(submit.input)
                if not feedback:
                    return submit.input
                log("    ↺ 실측 데이터 전달 → 점수 재검토 요청")
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": submit.id, "content": feedback}]})
                continue
            if retries >= 2:
                raise RuntimeError("제출 형식 오류가 반복됩니다: " + "; ".join(errors[:5]))
            retries += 1
            log(f"    ↺ 제출 형식 오류 {len(errors)}건 → 재제출 요청")
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": submit.id,
                "is_error": True,
                "content": "제출 형식 오류를 고쳐 다시 제출하세요:\n" + "\n".join(errors[:20]),
            }]})
            continue

        if resp.stop_reason == "pause_turn":
            # 긴 검색이 일시정지됨 → 받은 내용 그대로 돌려보내 계속 진행
            messages.append({"role": "assistant", "content": resp.content})
            continue

        if resp.stop_reason in ("end_turn", "max_tokens") and not nudged:
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({
                "role": "user",
                "content": f"조사한 내용을 {submit_tool['name']} 도구로 제출해 주세요.",
            })
            nudged = True
            continue

        break

    raise RuntimeError("에이전트가 결과를 제출하지 않았습니다.")


# ---------------------------------------------------------------------------
# 네이버 실측 데이터 (네이버 클라우드 NAVER API HUB: 검색어 트렌드)
# 문서: https://api.ncloud-docs.com/docs/naver-api-hub-search-trend
# HUB에는 쇼핑 '상품 검색'(상품 수·최저가) API가 없으므로 검색량 추이만 사용한다.
# ---------------------------------------------------------------------------
NAVER_API_HOST = "https://naverapihub.apigw.ntruss.com"
NAVER_TREND_PATH = "/search-trend/v1/search"
NAVER_TREND_ANCHOR = "텀블러"   # 검색량 지수의 기준 키워드 (=100). 배치가 달라도 비교할 수 있게 매 요청에 함께 넣음


class NaverError(Exception):
    pass


def naver_keys():
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def _naver_call(path, keys, body):
    headers = {"X-NCP-APIGW-API-KEY-ID": keys[0], "X-NCP-APIGW-API-KEY": keys[1],
               "Content-Type": "application/json"}
    req = urllib.request.Request(NAVER_API_HOST + path, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
            msg = f"{err.get('message', '')} {err.get('details', '')}".strip()
        except Exception:
            msg = ""
        raise NaverError(f"HTTP {e.code} {msg}".strip()) from None
    except urllib.error.URLError as e:
        raise NaverError(f"연결 실패: {e.reason}") from None


def _month_window(today=None):
    """지난 15개월(이번 달 제외)의 첫날 목록과 조회 시작·끝 날짜."""
    today = today or date.today()
    y, m = today.year, today.month
    months = []
    for back in range(15, 0, -1):
        yy, mm = divmod((y * 12 + m - 1) - back, 12)
        months.append(date(yy, mm + 1, 1))
    end = date(y, m, 1) - timedelta(days=1)
    return months, months[0].isoformat(), end.isoformat()


def naver_trends(groups, keys, today=None, counter=None):
    """groups: {이름: [키워드...]} → {이름: {volume_idx, growth_3m, yoy}}.
    volume_idx: 최근 3개월 검색량을 기준 키워드(NAVER_TREND_ANCHOR)=100으로 환산
    growth_3m: 최근 3개월 / 직전 3개월 - 1,  yoy: 최근 3개월 / 1년 전 같은 3개월 - 1"""
    months, start, end = _month_window(today)
    names = list(groups)
    out = {}
    for i in range(0, len(names), 4):            # 요청당 최대 5그룹 = 후보 4 + 기준 1
        batch = names[i:i + 4]
        body = {"startDate": start, "endDate": end, "timeUnit": "month",
                "keywordGroups": [{"groupName": f"g{j}", "keywords": groups[n][:20]} for j, n in enumerate(batch)]
                + [{"groupName": "anchor", "keywords": [NAVER_TREND_ANCHOR]}]}
        res = _naver_call(NAVER_TREND_PATH, keys, body)
        if counter:
            counter.count("trend")
        series = {}
        for r in res.get("results", []):
            by_period = {d["period"][:7]: d["ratio"] for d in r.get("data", [])}
            series[r["title"]] = [by_period.get(mo.isoformat()[:7], 0.0) for mo in months]
        anchor = sum(series.get("anchor", [0] * 15)[-3:])
        for j, n in enumerate(batch):
            s = series.get(f"g{j}", [0.0] * 15)
            recent, prev, year_ago = sum(s[-3:]), sum(s[-6:-3]), sum(s[:3])
            out[n] = {
                "volume_idx": round(recent / anchor * 100, 2) if anchor else None,
                "growth_3m": round((recent / prev - 1) * 100) if prev else None,
                "yoy": round((recent / year_ago - 1) * 100) if year_ago else None,
            }
    return out


_NAVER_DISK = []   # 실행 중 하나만 쓰는 디스크 캐시 (같은 날 같은 키워드는 다시 안 부름)


def naver_disk(log=print):
    if not _NAVER_DISK:
        _NAVER_DISK.append(naver_shopping.NaverCache(log=log))
    return _NAVER_DISK[0]


def _naver_auth_hint(e, api):
    if str(e).startswith(("HTTP 401", "HTTP 403")):
        return (f"네이버 API HUB 인증/권한 오류({e}) → 콘솔의 NAVER API HUB 애플리케이션에 '{api}' 구독이 있는지, "
                "키가 그 애플리케이션 것인지 확인하세요.")
    return f"네이버 {api} 조회 실패: {e}"


def naver_lookup(candidates, keys, cache, log=print):
    """후보별 네이버 검색어 트렌드 + 쇼핑인사이트(분야를 고른 후보만)를 조회해 cache[품목명]에 넣는다.
    둘 다 실패하면 None. 같은 날 같은 키워드는 디스크 캐시(data/naver_cache.json)에서 꺼낸다."""
    todo = {c["name"]: [k for k in c.get("keywords_ko", []) if k.strip()][:3] or [c["name"]]
            for c in candidates if c["name"] not in cache}
    if not todo:
        return
    disk = naver_disk(log)
    trends, need = {}, {}
    for name, kws in todo.items():
        key = "trend|" + "/".join(kws)
        if disk.has(key):
            trends[name] = disk.get(key)
        else:
            need[name] = kws
    if need:
        try:
            for name, v in naver_trends(need, keys, counter=disk).items():
                trends[name] = v
                disk.put("trend|" + "/".join(need[name]), v)
        except NaverError as e:
            log("    ⚠️ " + _naver_auth_hint(e, "검색어 트렌드") + " → 검색량 없이 진행합니다.")
    shop = {}
    items = [(c["name"], todo[c["name"]][0], SHOP_CATEGORIES[c["shop_category"]]) for c in candidates
             if c["name"] in todo and c.get("shop_category") in SHOP_CATEGORIES]
    if items:
        try:
            shop = naver_shopping.shopping_trends(items, lambda path, body: _naver_call(path, keys, body), disk)
        except NaverError as e:
            log("    ⚠️ " + _naver_auth_hint(e, "쇼핑인사이트") + " → 쇼핑 클릭 추세 없이 진행합니다.")
    disk.save()
    picked = {c["name"]: c.get("shop_category") for c in candidates}
    for name, kws in todo.items():
        d = ({"keyword": kws[0], "keywords": kws, "checked_at": disk.today, **trends[name]}   # checked_at = 네이버 조회일
             if trends.get(name) else None)
        if shop.get(name):
            d = d or {"keyword": kws[0], "keywords": kws, "checked_at": disk.today}
            d["shopping_insight"] = shop[name]          # naver_json 안에 저장 (DB 구조 변경 없음)
        if d is not None:   # 1번 모델이 고른 분야 (null = 모델이 애매하다고 비워 둠 → 쇼핑인사이트 조회 안 함)
            d["shop_category"] = picked.get(name) if picked.get(name) in SHOP_CATEGORIES else None
        cache[name] = d


def naver_summary(name, d):
    if not d:
        return f"- {name}: 네이버 데이터 조회 실패"
    pct = lambda v: "미확인" if v is None else f"{v:+d}%"
    if "volume_idx" in d:
        vol = "미확인" if d.get("volume_idx") is None else f"{d['volume_idx']}"
        trend = f"검색량지수 {vol} · 최근3개월 성장 {pct(d.get('growth_3m'))} · 전년동기 대비 {pct(d.get('yoy'))}"
    else:
        trend = "검색어 트렌드 조회 실패"
    return f"- {name} [검색어 {', '.join(d['keywords'])}]: {trend} · {naver_shopping.summary(d.get('shopping_insight'))}"


def shop_trend_cell(d):
    """표·CSV용 짧은 쇼핑 클릭 추세 (예: '오름 +24% · 계절 12월')."""
    si = (d or {}).get("shopping_insight")
    if not si:
        return None
    sea = si.get("seasonal") or {}
    season = f" · 계절 {int(sea['peak_month'][5:])}월" if sea.get("is_seasonal") else ""
    if si.get("trend") is None:
        return ("최근 클릭 적음" + season) if sea else "데이터 적음"
    ch = "" if si.get("change_3m") is None else f" {si['change_3m']:+d}%"
    return si["trend"] + ch + season


def naver_review(keys, cache, log=print, allow_replace=True):
    """제출 후 네이버 검색어 트렌드를 모델에게 보여주고 점수를 다시 매기게 하는 review 함수.
    allow_replace=False(지정 품목 조사)면 품목을 바꾸지 말고 점수만 다시 매기게 한다."""
    def review(submitted):
        log("    📊 네이버 검색어 트렌드 조회 중...")
        naver_lookup(submitted["candidates"], keys, cache, log)
        lines = [naver_summary(c["name"], cache.get(c["name"])) for c in submitted["candidates"]]
        if not any(cache.get(c["name"]) for c in submitted["candidates"]):
            return None   # 전부 실패 → 재검토 없이 그대로 사용
        return (f"네이버 검색어 트렌드 실측 데이터입니다 (검색량지수는 '{NAVER_TREND_ANCHOR}'의 최근 3개월 "
                "네이버 검색량 = 100 기준, 성장률은 직전 3개월·전년 같은 3개월 대비. 쇼핑인사이트는 네이버쇼핑 그 분야 안에서의 "
                "12개월 클릭 추세로 그 품목 수요의 오름·내림·계절성만 보여 줌 — 키워드마다 따로 조회한 상대값이라 품목끼리 "
                "많고 적음을 비교하는 데는 쓰지 말고(비교는 검색량지수로), 가격·판매처 정보도 아님):\n" + "\n".join(lines) +
                "\n\n이 수치를 반영해 demand 점수를 다시 검토하고(필요하면 다른 점수도), score_notes에 근거 수치를 "
                "인용해 submit_candidates로 다시 제출하세요. " +
                ("검색량이 거의 없는 후보는 다른 품목으로 교체해도 됩니다." if allow_replace
                 else "사용자가 지정한 품목이므로 품목은 바꾸지 말고 점수와 근거만 고치세요."))
    return review


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def fmt(v, suffix=""):
    if v is None or v == "":
        return "미확인"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return f"{v:,}{suffix}"
    return str(v)


# ---------------------------------------------------------------------------
# HTML 리포트 (브라우저용, 외부 파일 없이 단독으로 열림)
# ---------------------------------------------------------------------------
HTML_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5d6676;--line:#e3e6eb;--accent:#2f6fde;
--good:#1b8a4b;--mid:#b07400;--bad:#c62f2f;--warnbg:#fff6e0;--warnline:#f0c35a;--code:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--text:#e6e9ee;--muted:#9aa3b2;
--line:#2e333c;--accent:#6ea0ff;--good:#4cc27e;--mid:#e0a63a;--bad:#ff6b6b;--warnbg:#2c2615;--warnline:#7a6224;--code:#252a33}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:20px;margin:0 0 12px}h3{font-size:15px;margin:20px 0 8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin:0 0 20px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 20px;margin:0 0 12px}
.facts div{font-size:14px}.facts b{display:block;color:var(--muted);font-weight:normal;font-size:12px}
.warn{background:var(--warnbg);border:1px solid var(--warnline);border-radius:8px;padding:8px 12px;font-size:14px;margin:8px 0 12px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}td.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
.badge{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:700;color:#fff}
.t상{background:var(--good)}.t중{background:var(--mid)}.t하{background:var(--bad)}
.neg{color:var(--bad);font-weight:700}.pos{color:var(--good);font-weight:700}
.pick{color:var(--accent);font-weight:700}
ul.notes{padding-left:18px;margin:0}ul.notes li{margin:0 0 8px}
.flag{color:var(--bad)}
details{margin-top:12px}summary{cursor:pointer;font-weight:600}
pre{background:var(--code);border-radius:8px;padding:12px;white-space:pre-wrap;font:13px/1.5 Consolas,monospace;margin:8px 0 0}
button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:13px;margin-top:8px}
.src{font-size:12px;color:var(--muted);word-break:break-all}
"""


# ---------------------------------------------------------------------------
# 후보 품목 점수표
# ---------------------------------------------------------------------------
def set_ref_url(candidates):
    """ref_urls 중 첫 번째를 참고 페이지(ref_url)로. 사진은 수집하지 않는다
    (확정 사진은 사람이 올리거나 공급사 사진에서 고르고, 참고 이미지는 대시보드가 화면에만 표시)."""
    for c in candidates:
        urls = [u for u in (c.get("ref_urls") or []) if isinstance(u, str) and re.match(r"https?://", u)]
        c["ref_url"] = urls[0] if urls else None
        src = c.get("source_page_url")
        if src and not is_source_page(src):          # 상품 상세 페이지가 아니면 버림 (2번이 엉뚱한 페이지를 검증하지 않게)
            print(f"  · 해외 판매 페이지 제외 (상품 상세 주소 아님): {c.get('name')} : {src}")
            src = None
        c["source_page_url"] = src or None


def score_candidates(candidates):
    """항목 점수로 총점(100점 만점)을 계산하고 총점 내림차순으로 정렬한다."""
    for c in candidates:
        weighted = sum(w * c["scores"][k] for k, _, w, _ in SCORE_ITEMS)
        c["_total"] = round(weighted / 5 * 100)
    return sorted(candidates, key=lambda c: c["_total"], reverse=True)


def handoff_command(candidates, n=3):
    """상위 후보를 다음 단계(공급처 발굴 에이전트)로 넘기는 명령어. DB 단계를 sourcing으로 바꾼다."""
    ids = " ".join(str(c["_id"]) for c in candidates[:n] if c.get("_id"))
    return f"trade_hub select {ids}"


TREND_COLOR = {"오름": "var(--good)", "보합": "var(--mid)", "내림": "var(--bad)"}


def candidate_map(candidates):
    """후보 지도: 가로 = 검색량지수(로그 눈금), 세로 = 총점, 점 크기 = 마진 여지 점수, 점 색 = 쇼핑 클릭 추세.
    파일로 열어도 보이게 외부 라이브러리 없이 인라인 SVG. 검색량을 모르는 후보는 아래에 이름만 적는다."""
    from html import escape
    import math
    pts = [c for c in candidates if isinstance((c.get("_naver") or {}).get("volume_idx"), (int, float))]
    if len(pts) < 2:
        return ""
    W, H, L, R, T, B = 720, 360, 44, 24, 28, 48
    PAD = 14                                                             # 축 바로 위에 점이 붙지 않게
    vol = lambda c: max(c["_naver"]["volume_idx"], 0.01)                 # 0은 로그 눈금에 못 찍어 0.01로
    lo, hi = math.floor(math.log10(min(map(vol, pts)))), math.ceil(math.log10(max(map(vol, pts))))
    hi = hi if hi > lo else lo + 1
    ymin = max(0, (min(c["_total"] for c in pts) - 5) // 10 * 10)
    ymax = min(100, -(-(max(c["_total"] for c in pts) + 5) // 10) * 10)
    sx = lambda v: L + PAD + (math.log10(v) - lo) / (hi - lo) * (W - L - R - PAD)
    sy = lambda v: T + (ymax - v) / (ymax - ymin) * (H - T - B)
    med = lambda xs: sorted(xs)[len(xs) // 2]
    mx, my = sx(med([vol(c) for c in pts])), sy(med([c["_total"] for c in pts]))
    g = [f'<rect x="{mx:.1f}" y="{T}" width="{W - R - mx:.1f}" height="{my - T:.1f}" fill="var(--good)" opacity=".07"/>',
         f'<text x="{W - R - 6}" y="{T + 14}" text-anchor="end" class="mlab">검색량·점수 모두 중간 이상</text>']
    for p in range(lo, hi + 1):                                         # 세로 눈금선: 10의 거듭제곱
        x = sx(10 ** p)
        g.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{H - B}" class="grid"/>'
                 f'<text x="{x:.1f}" y="{H - B + 16}" text-anchor="middle" class="tick">{10 ** p:g}</text>')
    for v in range(int(ymin), int(ymax) + 1, 10):
        y = sy(v)
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                 f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{v}</text>')
    g.append(f'<text x="{(L + W - R) / 2}" y="{H - 8}" text-anchor="middle" class="axis">'
             f'검색량지수 ({escape(NAVER_TREND_ANCHOR)}=100, 로그 눈금) →</text>'
             f'<text x="{L - 6}" y="{T - 12}" text-anchor="end" class="axis">총점 ↑</text>')
    placed = []                                                          # 번호 글자 겹침 피하기: 오른쪽이 막히면 왼쪽에
    def label(x, y, r, t):
        right = (x + r + 3, x + r + 3 + 7 * len(t))
        hit = any(abs(py - y) < 13 and px1 < right[1] and px2 > right[0] for px1, px2, py in placed) or right[1] > W - R
        x1 = x - r - 3 - 7 * len(t) if hit else right[0]
        placed.append((x1, x1 + 7 * len(t), y))
        return f'<text x="{x1:.1f}" y="{y + 4:.1f}" class="plab">{escape(t)}</text>'
    for c in pts:                                                        # 점 자리를 먼저 막아 둠 (글자가 점을 덮지 않게)
        r = 4 + c["scores"]["margin"] * 1.6
        placed.append((sx(vol(c)) - r, sx(vol(c)) + r, sy(c["_total"])))
    for c in sorted(pts, key=lambda c: -c["scores"]["margin"]):          # 큰 점부터 그려 작은 점이 위에
        n = c["_naver"]
        trend = (n.get("shopping_insight") or {}).get("trend")
        x, y, r = sx(vol(c)), sy(c["_total"]), 4 + c["scores"]["margin"] * 1.6
        sign = lambda v: "미확인" if v is None else f"{v:+d}%"
        tip = (f"#{c.get('_id') or '-'} {c['name']} · 총점 {c['_total']} · 검색량 {n['volume_idx']} · "
               f"3개월 {sign(n.get('growth_3m'))} · 전년 {sign(n.get('yoy'))} · 마진 여지 {c['scores']['margin']}점 · "
               f"쇼핑 추세 {shop_trend_cell(n) or '없음'}")
        dot = (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{TREND_COLOR.get(trend, "var(--muted)")}" '
               f'fill-opacity=".85" stroke="var(--card)" stroke-width="2"><title>{escape(tip)}</title></circle>'
               + label(x, y, r, str(c.get("_id") or "")))
        g.append(f'<a href="#c{c["_id"]}">{dot}</a>' if c.get("_id") else dot)
    legend = "".join(f'<span><i style="background:{col}"></i>{t}</span>' for t, col in
                     [*TREND_COLOR.items(), ("추세 없음", "var(--muted)")])
    miss = [c for c in candidates if c not in pts]
    miss_html = (f"<p class='small muted'>검색량 미확인으로 빠진 후보 {len(miss)}개: "
                 + ", ".join(f"#{escape(str(c.get('_id') or '-'))} {escape(c['name'])}" for c in miss) + "</p>"
                 if miss else "")
    return (f"<section class='card'><h2>후보 지도</h2>"
            f"<p class='sub' style='margin:0 0 8px'>오른쪽 위일수록 검색량도 많고 점수도 높은 후보입니다. "
            f"점 크기는 마진 여지 점수, 점 색은 네이버 쇼핑 클릭 추세입니다. 점에 마우스를 올리면 수치가 보이고, 누르면 표의 그 줄로 갑니다.</p>"
            f"<div class='legend'>{legend}<span class='muted'>· 큰 점 = 마진 여지 높음</span></div>"
            f"<div class='scroll'><svg class='cmap' viewBox='0 0 {W} {H}' role='img' aria-label='후보 지도: 검색량지수와 총점'>{''.join(g)}</svg></div>"
            f"{miss_html}</section>")


MAP_STYLE = """
.cmap{width:100%;min-width:560px;height:auto;display:block}.cmap .grid{stroke:var(--line);stroke-width:1}
.cmap .tick{fill:var(--muted);font-size:11px}.cmap .axis{fill:var(--muted);font-size:12px}
.cmap .plab{fill:var(--text);font-size:11px;font-weight:700;pointer-events:none}.cmap .mlab{fill:var(--good);font-size:11px}
.cmap a:hover circle{stroke:var(--text)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin:0 0 4px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.small{font-size:12px}.muted{color:var(--muted)}tr:target td{background:var(--warnbg)}
"""


def write_candidates(outdir, candidates, args, when=None):
    when = when or datetime.now()                                       # 예전 리포트를 다시 그릴 때는 원래 시각
    from html import escape
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "raw.json").write_text(
        json.dumps({"hint": args.hint, "candidates": candidates}, ensure_ascii=False, indent=2), encoding="utf-8")

    labels = [label for _, label, _, _ in SCORE_ITEMS]
    with open(outdir / "candidates.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["순위", "품목", "카테고리", "총점"] + labels +
                   ["DB번호", "추천공급국", "예상판매가(원)", "FOB시세(USD)", "실측사이트", "실측판매가(USD)", "실측참고",
                    "네이버검색어", f"검색량지수({NAVER_TREND_ANCHOR}=100)", "3개월성장(%)", "전년대비(%)", "쇼핑클릭추세(쇼핑인사이트)",
                    "규제", "위험", "선정근거", "점수근거", "영어키워드"])
        for i, c in enumerate(candidates, 1):
            n = c.get("_naver") or {}
            w.writerow([i, c["name"], c["category"], c["_total"]] + [c["scores"][k] for k, *_ in SCORE_ITEMS] +
                       [c.get("_id"), SOURCE_COUNTRIES.get(c.get("source_country"), c.get("source_country")),
                        c.get("est_retail_krw"), c.get("est_fob_usd"),
                        (m := c.get("_market") or {}).get("site"),
                        f"{m['price_min']:g}~{m['price_max']:g} (중간 {m['price_median']:g})" if m.get("count") else None,
                        market_extra(m),
                        ", ".join(n.get("keywords", [])), n.get("volume_idx"), n.get("growth_3m"), n.get("yoy"),
                        shop_trend_cell(n),
                        c.get("regulations"), c.get("risk"), c["reason"], c["score_notes"], c["search_keyword_en"]])

    def e(v, suffix=""):
        return escape(fmt(v, suffix))

    weights = " · ".join(f"{label} {wt*100:.0f}%" for _, label, wt, _ in SCORE_ITEMS)
    has_naver = any(c.get("_naver") for c in candidates)
    naver_note = (f"<br>네이버 데이터: 검색량지수는 최근 3개월 네이버 검색량('{escape(NAVER_TREND_ANCHOR)}'=100), "
                  "성장률은 직전 3개월·전년 같은 기간 대비 (NAVER API HUB 검색어 트렌드). "
                  "쇼핑 추세는 네이버 쇼핑인사이트 그 분야 안 12개월 클릭 추세 (수요 추세만, 가격 아님)."
                  if has_naver else "")
    hint = f" · 방향: {escape(args.hint)}" if args.hint else ""
    cmd = handoff_command(candidates)
    H = [f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>후보 품목 {when:%Y-%m-%d}</title><style>{HTML_STYLE}
.ref{{font-size:12px}}.pname{{min-width:220px}}.score{{font-weight:700}}.s5,.s4{{color:var(--good)}}.s3{{color:var(--mid)}}.s2,.s1{{color:var(--bad)}}{MAP_STYLE}</style></head><body><main>
<h1>후보 품목 점수표</h1>
<p class="sub">{when:%Y-%m-%d %H:%M}{hint} · 총점 가중치: {weights}<br>
각 항목 1~5점(5가 유리). 판매가·FOB 시세는 웹 검색 기준 참고값입니다.{naver_note}</p>
<section class="card"><h2>다음 단계</h2><p>진행할 품목의 <b>번호</b>를 골라 공급처 발굴 단계로 넘기세요 (공용 DB에서 sourcing 단계로 이동). 상위 3개 예:</p>
<pre id="cmd">{escape(cmd)}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('cmd').innerText).then(()=>{{this.textContent='복사됨 ✓'}})">명령어 복사</button></section>
<section class="card"><div class="scroll"><table><tr><th>번호</th><th>품목</th><th>카테고리</th><th>총점</th>"""
         + "".join(f"<th>{escape(l)}</th>" for l in labels)
         + "<th>추천 공급국</th><th>예상판매가</th><th>FOB시세</th>"
         + (f"<th title='최근 3개월 네이버 검색량, {escape(NAVER_TREND_ANCHOR)}=100'>검색량지수</th>"
            "<th>3개월 성장</th><th>전년 대비</th>"
            "<th title='네이버 쇼핑인사이트: 그 분야 안 12개월 클릭 추세 (수요 추세만, 가격 아님)'>쇼핑 추세</th>"
            if has_naver else "")
         + "</tr>"]
    for i, c in enumerate(candidates, 1):
        cells = "".join(f"<td class='num score s{round(c['scores'][k])}'>{e(c['scores'][k])}</td>" for k, *_ in SCORE_ITEMS)
        if has_naver:
            n = c.get("_naver") or {}
            g = lambda v: "미확인" if v is None else f"{v:+d}%"
            cells_n = (f"<td class='num'>{e(n.get('volume_idx'))}</td><td class='num'>{g(n.get('growth_3m'))}</td>"
                       f"<td class='num'>{g(n.get('yoy'))}</td><td>{e(shop_trend_cell(n))}</td>")
        else:
            cells_n = ""
        ref = (f"<br><a class='ref' href='{escape(c['ref_url'], quote=True)}' target='_blank' rel='noopener'>참고 페이지 ↗</a>"
               if c.get("ref_url") else "")
        if c.get("source_page_url"):
            ref += (f"{' · ' if ref else '<br>'}<a class='ref' href='{escape(c['source_page_url'], quote=True)}' "
                    "target='_blank' rel='noopener'>해외 판매 페이지 ↗</a>")
        H.append(f"<tr id='c{e(c.get('_id'))}'><td>{e(c.get('_id'))}</td><td class='pname'><b>{escape(c['name'])}</b>{ref}</td><td>{escape(c['category'])}</td>"
                 f"<td class='num'><b>{c['_total']}</b></td>{cells}"
                 f"<td title='{escape(c.get('source_country_reason') or '', quote=True)}'>"
                 f"{e(SOURCE_COUNTRIES.get(c.get('source_country'), c.get('source_country')))}</td>"
                 f"<td class='num'>{e(c.get('est_retail_krw'), '원')}</td>"
                 f"<td class='num'>{e(c.get('est_fob_usd'), ' USD')}</td>{cells_n}</tr>")
    H.append("</table></div></section>" + candidate_map(candidates) + "<section class='card'><h2>후보별 근거</h2><ul class='notes'>")
    for i, c in enumerate(candidates, 1):
        risk = f"<br><span class='flag'>⚠️ {escape(c['risk'])}</span>" if c.get("risk") else ""
        H.append(f"<li><b>[{e(c.get('_id'))}] {escape(c['name'])}</b> ({escape(c['search_keyword_en'])})<br>{escape(c['reason'])}"
                 f"<br><small>점수 근거: {escape(c['score_notes'])}</small>"
                 f"<br><small>추천 공급국: {e(SOURCE_COUNTRIES.get(c.get('source_country'), c.get('source_country')))}"
                 f" — {e(c.get('source_country_reason'))}</small>"
                 f"<br><small>규제: {e(c.get('regulations'))}</small>{risk}</li>")
    H.append("</ul></section></main></body></html>")
    (outdir / "candidates.html").write_text("\n".join(H), encoding="utf-8")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def load_dotenv(path):
    """KEY=VALUE 형식의 .env 파일을 읽어 환경변수로 설정한다.
    이미 설정된 환경변수와 빈 값은 건드리지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'") and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]       # 따옴표 안의 값만 (뒤 주석 무시)
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()   # 'KEY=값  # 설명' 형태의 주석 제거
        if key and value:
            os.environ.setdefault(key, value)


def main():
    # 출력이 파일로 리다이렉트되면 Windows는 cp949를 써서 이모지 출력 시 죽는다 → UTF-8 고정
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="시장조사·아이템 발굴 에이전트 (1번): 수입 후보 품목 점수표")
    ap.add_argument("--discover", action="store_true", help=argparse.SUPPRESS)   # 예전 명령 호환 (기본 동작과 같음)
    ap.add_argument("--hint", default=None,
                    help='품목 방향. 예: --hint "주방·수납, 1만~3만원대, 겨울 시즌"')
    ap.add_argument("--count", type=int, default=10, help="후보 품목 수 (기본 10)")
    ap.add_argument("--product", default=None,
                    help='발굴 대신 이 품목 하나만 조사·평가 (예: --product "무선 기계식 키보드 75%%"). DB에는 origin=manual')
    ap.add_argument("--fill-country", action="store_true",
                    help="조사 대신, 추천 공급 국가가 없는 기존 품목에 국가만 채움 (웹 검색 없이 모델 호출 1번)")
    ap.add_argument("--fill-shop", action="store_true",
                    help="조사 대신, 기존 품목에 네이버 쇼핑인사이트(수요 추세)를 채움 (분야 고르기 모델 호출 1번 + 네이버 조회, "
                         "점수는 그대로)")
    ap.add_argument("--reprice", action="store_true",
                    help="조사 대신, 진행 중 품목(+ --ids)의 국내 실제 판매가·낱개/묶음 기준을 다시 조사하고 "
                         "판매가가 크게 다른 변형은 별도 후보 품목으로 나눔 (품목마다 웹 검색)")
    ap.add_argument("--ids", default="", help="--reprice에 더할 품목 번호. 예: --ids 16,34,44")
    ap.add_argument("--only-ids", action="store_true", help="--reprice를 --ids 품목만 (실패한 품목 다시 조사용)")
    ap.add_argument("--ali-ideas", action="store_true",
                    help="AliExpress 드롭쉬핑 인기 상품(해외 판매량 상위)을 발굴 아이디어로 함께 줌. .env의 DISCOVER_ALI_IDEAS=1로도 켬")
    ap.add_argument("--provider", choices=["anthropic", "openrouter"],
                    default=None,
                    help="anthropic = Claude(ANTHROPIC_API_KEY), openrouter = DeepSeek 등(OPENROUTER_API_KEY). "
                         "생략 시 .env의 PROVIDER, 그것도 없으면 anthropic")
    ap.add_argument("--model", default=None,
                    help=f"모델 (기본: anthropic은 {DEFAULT_MODEL}, openrouter는 .env의 OPENROUTER_MODEL 또는 {OPENROUTER_DEFAULT_MODEL})")
    ap.add_argument("--out", default="reports", help="결과 저장 폴더")
    ap.add_argument("--no-open", action="store_true", help="끝난 뒤 브라우저로 리포트를 열지 않음 (정기 실행용)")
    args = ap.parse_args()
    if args.count < 1:
        ap.error("--count는 1 이상이어야 합니다")
    if args.product is not None:
        args.product = " ".join(args.product.split())[:100]
        if not args.product:
            ap.error("--product에 품목명을 적어 주세요")
    # 스크립트 폴더의 .env를 읽음 (작업 스케줄러처럼 다른 폴더에서 실행돼도 찾도록)
    load_dotenv(Path(__file__).resolve().parent / ".env")
    if not args.ali_ideas:   # 대시보드·슈퍼바이저 실행에도 켜지게 .env로 기본값
        args.ali_ideas = os.environ.get("DISCOVER_ALI_IDEAS", "").strip().lower() in ("1", "true", "yes", "on")
    if args.provider is None:
        args.provider = os.environ.get("PROVIDER", "anthropic").strip().lower()
        if args.provider not in ("anthropic", "openrouter"):
            ap.error(f".env의 PROVIDER={args.provider!r}는 anthropic 또는 openrouter여야 합니다")
    backend = make_backend(args.provider, args.model)
    _, model, _, extra, label = backend
    effort = f" · effort: {extra['output_config']['effort']}" if extra.get("output_config") else ""
    print(f"▶ 접속: {label} · 모델: {model}{effort}")
    today = datetime.now().strftime("%Y년 %m월 %d일")
    hint = (f"\n사용자가 원하는 방향: {args.hint}\n이 방향(카테고리·가격대·시즌 등)에 맞는 품목만 고르세요.\n"
            if args.hint else "")

    conn = trade_db.connect()
    if args.fill_country:
        with trade_db.agent_run(conn, "discover", provider=args.provider, model=model, args={"fill_country": True}):
            fill_countries(backend, conn)
        return
    if args.reprice:
        import reprice
        try:
            ids = [int(x) for x in args.ids.replace(" ", "").split(",") if x]
        except ValueError:
            ap.error("--ids는 쉼표로 나눈 품목 번호여야 합니다 (예: 16,34,44)")
        with trade_db.agent_run(conn, "discover", provider=args.provider, model=model,
                                args={"reprice": True, "ids": ids, "only_ids": args.only_ids}) as run:
            reprice.run(backend, conn, run, ids, args.out, only_ids=args.only_ids)
        return
    if args.fill_shop:
        keys = naver_keys()
        if not keys:
            sys.exit(".env에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없어 쇼핑인사이트를 조회할 수 없습니다.")
        with trade_db.agent_run(conn, "discover", provider=args.provider, model=model, args={"fill_shop": True}):
            fill_shop_insight(backend, conn, keys)
        return
    with trade_db.agent_run(conn, "discover", provider=args.provider, model=model,
                            args={"product": args.product} if args.product else
                            {"hint": args.hint, "count": args.count, **({"ali_ideas": True} if args.ali_ideas else {})}) as run:
        html_path = run_discover(backend, args, today, hint, conn, run)
    if not args.no_open:
        import webbrowser
        webbrowser.open(html_path.as_uri())


def new_outdir(base, suffix=""):
    """reports/YYYY-MM-DD_HHMM{suffix} — 같은 분에 또 실행하면 _2, _3을 붙여 덮어쓰지 않는다."""
    stem = Path(base) / (datetime.now().strftime("%Y-%m-%d_%H%M") + suffix)
    outdir, i = stem, 2
    while outdir.exists():
        outdir, i = stem.with_name(f"{stem.name}_{i}"), i + 1
    return outdir


def product_task(product):
    """지정 품목 조사 프롬프트 앞부분 (발굴 대신 사용자가 정한 품목 하나를 평가)."""
    return f"""사용자가 수입을 검토하는 품목: "{product}"
이 품목 하나만 조사·평가해서 submit_candidates로 제출하세요 (candidates 배열에 1개).
- 다른 품목으로 바꾸지 마세요. name은 이 품목을 가리키는 구체적인 한국어 품목명으로 적습니다
  (사용자가 쓴 표현을 살리고, 필요하면 규격·형태만 덧붙입니다).
- 국내 판매 현황(네이버·쿠팡 가격대, 주요 경쟁 상품과 리뷰 수), 해외 트렌드, 해외 FOB 시세, 시즌성을 조사합니다.
- 수입에 불리한 점(인증 부담, 경쟁 포화, 마진 부족 등)도 숨기지 말고 점수와 risk에 반영합니다."""


COUNTRY_TOOL = {
    "name": "submit_countries",
    "description": "품목별 추천 공급 국가를 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "품목 번호"},
                           "source_country": {"type": "string", "enum": list(SOURCE_COUNTRIES), "description": SOURCE_COUNTRY_DESC},
                           "source_country_reason": {"type": "string", "description": "그 나라를 고른 이유 한 줄"}},
            "required": ["id", "source_country", "source_country_reason"]}}},
        "required": ["items"],
    },
}


def fill_countries(backend, conn, log=print):
    """추천 공급 국가가 없는 기존 품목에 국가만 채운다 (웹 검색 없이 모델 호출 1번 — 이미 조사한 품목 정보로 판단)."""
    rows = conn.execute("""SELECT p.id, p.name, p.category, p.search_keyword_en, p.reason, s.est_fob_usd
        FROM products p LEFT JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
        WHERE p.source_country IS NULL AND p.stage != 'dropped' ORDER BY p.id""").fetchall()
    if not rows:
        log("추천 공급 국가가 비어 있는 품목이 없습니다.")
        return 0
    client, model, _, _, _ = backend
    lines = "\n".join(f"- [{r['id']}] {r['name']} ({r['search_keyword_en'] or ''}) · 카테고리 {r['category'] or '-'} · "
                       f"FOB {r['est_fob_usd'] or '-'} USD · 근거: {(r['reason'] or '')[:200]}" for r in rows)
    log(f"▶ 추천 공급 국가 채우기: {len(rows)}개 품목 (웹 검색 없음)")
    msg = client.messages.create(
        model=model, max_tokens=4000, tools=[COUNTRY_TOOL],   # 이 모델은 tool_choice 강제를 지원하지 않아 지시로 호출하게 함
        messages=[{"role": "user", "content": "한국 1인 수입 무역용 후보 품목들입니다. 각 품목을 들여올 가장 알맞은 공급 국가를 "
                   "China/Japan/USA 중에서 골라 반드시 submit_countries 도구 한 번으로 모든 품목을 제출하세요 (산문 답변 금지). 일본·미국 브랜드나 그 나라에서만 파는 상품이 "
                   "아니면 보통 China입니다.\n" + lines}])
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError("모델이 submit_countries를 호출하지 않았습니다")
    errors = validate(block.input, COUNTRY_TOOL["input_schema"])
    if errors:
        raise RuntimeError("제출값 오류: " + "; ".join(errors[:5]))
    valid = {r["id"]: r["name"] for r in rows}
    n = 0
    with conn:
        for it in block.input["items"]:
            if it["id"] in valid:
                trade_db.upsert_product(conn, valid[it["id"]], source_country=it["source_country"],
                                        source_country_reason=it["source_country_reason"])
                log(f"  [{it['id']}] {valid[it['id']]} → {SOURCE_COUNTRIES[it['source_country']]} ({it['source_country_reason']})")
                n += 1
    return n


SHOP_CATEGORY_TOOL = {
    "name": "submit_shop_categories",
    "description": "품목별 네이버쇼핑 최상위 분야를 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "품목 번호"},
                           "shop_category": {"type": ["string", "null"], "enum": list(SHOP_CATEGORIES) + [None],
                                             "description": "이 품목을 네이버쇼핑에서 찾으면 나오는 최상위 분야. 애매하면 null"}},
            "required": ["id", "shop_category"]}}},
        "required": ["items"],
    },
}


def _latest_scores(conn):
    """중단하지 않은 품목마다 가장 최근 점수 줄 (naver_json 포함)."""
    return conn.execute("""SELECT p.id, p.name, p.category, p.search_keyword_en, p.keywords_ko_json,
               s.id AS score_id, s.naver_json
        FROM products p JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
        WHERE p.stage != 'dropped' ORDER BY p.id""").fetchall()


def fill_shop_insight(backend, conn, keys, log=print):
    """기존 품목에 네이버 쇼핑인사이트(수요 추세)를 뒤늦게 채운다.
    1) 분야(shop_category)를 아직 안 정한 품목만 골라 모델 호출 1번으로 분야를 정하고 (웹 검색 없음)
    2) 쇼핑인사이트를 조회해 가장 최근 점수 줄의 naver_json에 shop_category·shopping_insight를 더한다.
    이미 검색어 트렌드가 있는 줄은 그 값(점수 근거)을 건드리지 않고 두 칸만 더하고,
    naver_json이 비어 있던 줄(네이버 연동 전 품목)은 검색어 트렌드도 함께 채운다. 점수·순위는 바꾸지 않는다."""
    rows = [r for r in _latest_scores(conn)
            if "shop_category" not in json.loads(r["naver_json"] or "{}")]
    if not rows:
        log("쇼핑 분야를 아직 정하지 않은 품목이 없습니다.")
        return 0
    client, model, _, _, _ = backend
    lines = "\n".join(f"- [{r['id']}] {r['name']} ({r['search_keyword_en'] or ''}) · 카테고리 {r['category'] or '-'}"
                      for r in rows)
    log(f"▶ 쇼핑인사이트 채우기: {len(rows)}개 품목 (분야 고르기 모델 호출 1번, 웹 검색 없음)")
    msg = client.messages.create(
        model=model, max_tokens=4000, tools=[SHOP_CATEGORY_TOOL],
        messages=[{"role": "user", "content": "한국 1인 수입 무역용 후보 품목들입니다. 각 품목을 네이버쇼핑에서 찾으면 나오는 "
                   "최상위 분야를 다음 중에서 골라 반드시 submit_shop_categories 도구 한 번으로 모든 품목을 제출하세요 "
                   "(산문 답변 금지). 애매하면 null.\n분야: " + ", ".join(SHOP_CATEGORIES) + "\n" + lines}])
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError("모델이 submit_shop_categories를 호출하지 않았습니다")
    errors = validate(block.input, SHOP_CATEGORY_TOOL["input_schema"])
    if errors:
        raise RuntimeError("제출값 오류: " + "; ".join(errors[:5]))
    picked = {it["id"]: it["shop_category"] for it in block.input["items"]}
    cands, by_name = [], {}
    for r in rows:
        if r["id"] not in picked:
            continue
        old = json.loads(r["naver_json"] or "null")
        kws = (old or {}).get("keywords") or json.loads(r["keywords_ko_json"] or "[]") or [r["name"]]
        cands.append({"name": r["name"], "keywords_ko": kws, "shop_category": picked[r["id"]]})
        by_name[r["name"]] = (r, old)
    cache = {}
    naver_lookup(cands, keys, cache, log)
    n = 0
    with conn:
        for name, (r, old) in by_name.items():
            got = cache.get(name)
            if got is None or (got.get("shop_category") and not got.get("shopping_insight")):
                log(f"  [{r['id']}] {name}: 네이버 조회 실패 → 기록하지 않음 (다음 --fill-shop 때 다시 시도)")
                continue
            if old:   # 기존 검색어 트렌드(점수 근거)는 그대로, 두 칸만 더함
                new = {**old, "shop_category": got.get("shop_category")}
                if got.get("shopping_insight"):
                    new["shopping_insight"] = got["shopping_insight"]
            else:
                new = got
            conn.execute("UPDATE product_scores SET naver_json = ? WHERE id = ?",
                         (json.dumps(new, ensure_ascii=False), r["score_id"]))
            log(f"  [{r['id']}] {name} → {new.get('shop_category') or '분야 미정'} · "
                f"{shop_trend_cell(new) or ('조회 안 함' if not new.get('shop_category') else '데이터 없음')}")
            n += 1
    log(f"  · {n}개 품목에 채움")
    return n


# 추천 국가별 '상품 상세 페이지'를 찾을 검색 예시 (검색 결과에서 상세 주소를 고르게)
SOURCE_SEARCH_EXAMPLES = {
    "China": '"alibaba.com product-detail <영문 키워드>" 또는 "aliexpress.com item <영문 키워드>"',
    "Japan": '"item.rakuten.co.jp <일본어 키워드>" 또는 "amazon.co.jp <일본어 키워드>"',
    "USA": '"ebay.com itm <영문 키워드>" 또는 "amazon.com <영문 키워드>"',
}
SOURCE_SEARCHES_PER_CANDIDATE = 1   # 후보마다 해외 상품 상세 페이지 확인용 검색 1회 (기본 15회에 더함)


def existing_products(conn):
    """이미 DB에 있는 품목 이름 (후보·진행·중단 모두). 발굴이 같은 품목을 다시 찾아오지 않게 프롬프트에 넣는다."""
    return [r["name"] for r in conn.execute("SELECT name FROM products ORDER BY id")]


def exclusion_block(names):
    if not names:
        return ""
    return ("\n- 아래는 이미 조사한 품목입니다 (후보·진행 중·중단 포함). 이것과 같은 품목은 다시 고르지 마세요. "
            "이름·소재·색상·구성만 조금 다른 것도 같은 품목으로 봅니다 (예: '실리콘 찜기'와 '접이식 실리콘 찜기 바스켓'). "
            "점수 재검토로 후보를 교체할 때도 마찬가지입니다.\n"
            + "\n".join(f"  · {n}" for n in names))


def with_more_searches(backend, extra_uses):
    """웹 검색 도구의 max_uses만 늘린 backend (원본 dict는 그대로)."""
    client, model, tool, extra, name = backend
    tool = dict(tool)
    if "parameters" in tool:   # OpenRouter
        tool["parameters"] = {**tool["parameters"], "max_uses": tool["parameters"].get("max_uses", 15) + extra_uses}
    else:
        tool["max_uses"] = tool.get("max_uses", 15) + extra_uses
    return client, model, tool, extra, name


# 추천 공급 국가별 실측 사이트: 국가 → (이름, 모듈, 이 사이트 가격의 성격). 모듈은 status/search/summarize/summary_text
MARKETS = {
    "China": ("AliExpress", ali, "한국 배송 기준 낱개 소매가 USD — 공장 도매가(FOB)는 보통 이보다 낮음"),
    "USA": ("eBay", ebay, "미국 내 판매자의 새 상품 즉시 구매가 USD — 미국 배대지까지 배송비·국제 배송비 별도"),
}
MARKET_ERRORS = (ali.AliError, ebay.EbayError)


def market_key(c):
    """(추천 국가, 영문 키워드). 실측 사이트가 없는 국가(일본 등)나 키워드가 없으면 None."""
    kw = (c.get("search_keyword_en") or "").strip()
    return (c.get("source_country"), kw) if c.get("source_country") in MARKETS and kw else None


def market_status():
    """{국가: (사용 가능, 이유)}"""
    return {country: mod.status() for country, (_, mod, _) in MARKETS.items()}


def market_lookup(candidates, cache, enabled, log=print):
    """추천 국가의 실측 사이트에서 영문 키워드로 검색해 cache[(국가, 키워드)]에 요약을 넣는다 (같은 키워드는 한 번만).
    enabled = 쓸 수 있는 국가들. 실패하면 None을 넣고 넘어간다."""
    for c in candidates:
        k = market_key(c)
        if not k or k[0] not in enabled or k in cache:
            continue
        name, mod, _ = MARKETS[k[0]]
        try:
            total, items = mod.search(k[1])
            cache[k] = mod.summarize(k[1], total, items)
        except MARKET_ERRORS as e:
            log(f"    · {name} 조회 실패 ({k[1]}): {e}")
            cache[k] = None
    return cache


def market_review_text(candidates, cache):
    parts = []
    for country, (name, mod, basis) in MARKETS.items():
        lines = [f"- {c['name']}: {mod.summary_text(cache[k])}" for c in candidates
                 if (k := market_key(c)) and k[0] == country and cache.get(k) is not None]
        if lines:
            parts.append(f"{name} 실측 데이터입니다 ({SOURCE_COUNTRIES[country]} 추천 후보, {basis}):\n" + "\n".join(lines))
    if not parts:
        return None
    return ("\n\n".join(parts)
            + "\n이 수치로 margin(해외 판매가가 국내 판매가에 가까우면 마진 여지가 작음)과 competition(같은 상품·판매자·주문이 "
              "많으면 이미 해외직구로 많이 팔리는 중)을 검토하고 score_notes에 근거 수치를 인용하세요. "
              "이 소매가를 est_fob_usd에 그대로 넣지 마세요.")


def combined_review(naver_rev, cache, enabled, log=print, allow_replace=True):
    """제출 후 해외 실측(AliExpress·eBay)과 네이버 검색어 트렌드를 한 번에 보여 주고 다시 제출받는 review 함수."""
    def review(submitted):
        market_t = None
        if enabled:
            log("    🛒 해외 판매 실측 조회 중 (" + ", ".join(MARKETS[c][0] for c in enabled) + ")...")
            market_lookup(submitted["candidates"], cache, enabled, log)
            market_t = market_review_text(submitted["candidates"], cache)
        naver_t = naver_rev(submitted) if naver_rev else None
        if naver_t:                          # 네이버 안내문 끝에 재제출 지시가 있음
            return f"{market_t}\n\n{naver_t}" if market_t else naver_t
        if market_t:
            return market_t + " 반영해 submit_candidates로 다시 제출하세요." + (
                "" if allow_replace else " 사용자가 지정한 품목이므로 품목은 바꾸지 말고 점수와 근거만 고치세요.")
        return None
    return review


def apply_market(candidates, cache):
    """최종 후보에 실측 요약을 붙이고(점수 근거에 한 줄), 해외 판매 페이지가 비었으면 검색 결과 상품 주소로 채운다.
    {사이트 이름: 채운 개수}를 돌려준다."""
    filled = {}
    for c in candidates:
        k = market_key(c)
        s = cache.get(k) if k else None
        c["_market"] = {"site": MARKETS[k[0]][0], **s} if s else None
        if not s or not s.get("count"):
            continue
        name, mod, _ = MARKETS[k[0]]
        note = f"[{name} 실측] " + mod.summary_text(s)
        if note not in (c.get("score_notes") or ""):
            c["score_notes"] = " / ".join(x for x in (c.get("score_notes"), note) if x)
        if not c.get("source_page_url") and s.get("page_url"):
            c["source_page_url"] = s["page_url"]
            c["_source_page_from"] = name
            filled[name] = filled.get(name, 0) + 1
    return filled


def market_extra(m):
    """CSV '실측참고' 칸: AliExpress는 최다 주문, eBay는 판매자 수."""
    if not m or not m.get("count"):
        return None
    if "top_orders" in m:
        return f"최다 주문 {m['top_orders']:,}건"
    if "sellers" in m:
        return f"판매자 {m['sellers']}명"
    return None


def ali_ideas_block(items):
    lines = "\n".join(f"  · [{i['category']}] {i['title'][:80]} · ${i['price']:g} · 최근 판매 {i['volume']:,}건"
                      for i in items)
    return ("\n- 참고: AliExpress 드롭쉬핑 인기 상품 목록입니다 (전 세계 판매량 기준이라 한국 수요와 다를 수 있음). "
            "이 중 네이버·쿠팡에서 한국 수요가 확인되고 국내 경쟁이 적은 품목이 있으면 후보로 검토하세요. "
            "브랜드 가품·라이선스 캐릭터 상품은 제외합니다. 맞는 것이 없으면 쓰지 않아도 됩니다. 이 목록에서 고른 후보는 reason 앞에 '[알리 인기]'를 붙입니다.\n" + lines)


def run_discover(backend, args, today, hint, conn, run):
    """후보 품목을 조사·평가해 점수표를 만들고 공용 DB에 candidate로 등록한다.
    args.product가 있으면 발굴 대신 그 품목 하나만 조사한다 (DB origin=manual)."""
    product = getattr(args, "product", None)
    if product:
        print(f"▶ 지정 품목 조사·평가 중: {product}")
    else:
        print(f"▶ 후보 품목 {args.count}개 조사·평가 중..." + (f" (방향: {args.hint})" if args.hint else ""))
    criteria = "\n".join(f"  · {k} ({label}): {rule}" for k, label, _, rule in SCORE_ITEMS)
    keys, naver_cache = naver_keys(), {}
    print("  · 네이버 실측 데이터: " + ("사용 (제출 후 네이버 검색어 트렌드로 점수 재검토)" if keys
                                   else "사용 안 함 (.env에 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 없음)"))
    known = [] if product else existing_products(conn)
    if known:
        print(f"  · 이미 조사한 품목 {len(known)}개는 제외하고 새 품목을 찾습니다")
    st = market_status()
    enabled = [country for country, (ok, _) in st.items() if ok]
    market_cache, ideas = {}, ""
    for country, (ok, why) in st.items():
        name = MARKETS[country][0]
        print(f"  · {name} 실측: " + (f"사용 ({SOURCE_COUNTRIES[country]} 추천 후보의 실제 판매가·상품 페이지 확인)" if ok
                                     else f"사용 안 함 ({why})"))
    ali_ok, ali_why = st["China"]
    if not product and getattr(args, "ali_ideas", False):
        try:
            if not ali_ok:
                raise ali.AliError(ali_why)
            top = ali.bestsellers()
            ideas = ali_ideas_block(top)
            print(f"  · AliExpress 인기 상품 {len(top)}개를 발굴 아이디어로 전달")
        except ali.AliError as e:
            print(f"  · AliExpress 인기 상품 목록 사용 안 함 ({e})")
    task = product_task(product) if product else f"""{hint}
한국 시장의 최근 소비 트렌드, 네이버·쿠팡 인기 카테고리와 리뷰 수, 해외(아마존·틱톡 등) 트렌드, 해외직구 인기 품목,
다가오는 시즌 수요를 조사해서 "수요는 있는데 공급(경쟁)이 적은"
1인 수입 무역 후보 품목 {args.count}개를 골라 submit_candidates로 제출하세요.
- 서로 겹치지 않는 품목으로 고릅니다 (같은 품목의 색상·사이즈 변형은 하나로 봅니다).{exclusion_block(known)}{ideas}"""
    n_cand = 1 if product else args.count
    backend = with_more_searches(backend, SOURCE_SEARCHES_PER_CANDIDATE * n_cand)
    source_examples = "\n".join(f"    {SOURCE_COUNTRIES[k]}: {v}" for k, v in SOURCE_SEARCH_EXAMPLES.items())
    data = run_agent(backend, f"""오늘은 {today}입니다.
{task}
- 각 후보를 아래 5개 항목에 대해 1~5점(5가 가장 유리)으로 평가하고 score_notes에 항목별 근거를 한 줄씩 적습니다.
{criteria}
- 국내 판매가(est_retail_krw)와 해외 FOB 단가(est_fob_usd)는 검색으로 확인한 시세로 적고, 못 찾으면 null로 둡니다.
- ref_urls에는 사람이 어떤 물건인지 확인할 수 있는 대표 상품 페이지 주소 1~3개를 검색 결과에서 골라 적습니다.
- source_page_url에는 해외 구매처의 상품 상세 페이지 1개를 적습니다 (ref_urls와 별개). 다음 단계가 이 판매자를 첫 후보로 검증합니다.
  후보를 정한 뒤 후보마다 추천 공급 국가의 구매 사이트를 한 번 검색해서, 검색 결과 중 상품 상세 주소
  (alibaba.com/product-detail/…, item.rakuten.co.jp/…, ebay.com/itm/… 등)를 고릅니다. 검색 예:
{source_examples}
  국내 쇼핑몰·검색 결과 목록·카테고리 페이지는 안 됩니다. 검색해도 상세 주소가 없으면 null (지어내지 말 것).
- shop_category에는 이 품목이 네이버쇼핑에서 속하는 최상위 분야를 고릅니다 (쇼핑 클릭 추세 조회용, 애매하면 null).
- source_country에는 이 품목을 들여올 공급 국가(China/Japan/USA 중 하나)를, source_country_reason에 그 이유를 한 줄 적습니다.
  다음 단계(공급처 발굴)가 이 나라에서 공급처를 찾습니다. FOB 시세도 그 나라 기준으로 적습니다.
- 점수를 후하게 주지 마세요. 근거가 약하면 3점 이하로 줍니다.""", SUBMIT_CANDIDATES_TOOL,
                     review=combined_review(naver_review(keys, naver_cache, allow_replace=not product) if keys else None,
                                            market_cache, enabled, allow_replace=not product)
                     if (keys or enabled) else None)
    if product:
        data["candidates"] = data["candidates"][:1]
    if keys:   # 재제출 때 교체된 후보도 조회
        naver_lookup(data["candidates"], keys, naver_cache)
    for c in data["candidates"]:
        c["_naver"] = naver_cache.get(c["name"])
    set_ref_url(data["candidates"])
    if enabled:
        market_lookup(data["candidates"], market_cache, enabled)          # 재검토 때 교체된 후보도
        for name, n in apply_market(data["candidates"], market_cache).items():
            print(f"  · 해외 판매 페이지 {n}개를 {name} 검색 결과로 채움")
    candidates = score_candidates(data["candidates"])
    outdir = new_outdir(args.out, "_product" if product else "_discover")
    run.report_dir = str(outdir.resolve())
    ids = discover_store.save_discover(conn, run.id, candidates,    # {품목명: DB 번호}
                                       origin="manual" if product else "discover")
    for c in candidates:
        c["_id"] = ids.get(c["name"])
    write_candidates(outdir, candidates, args)
    print()
    for i, c in enumerate(candidates, 1):
        nv = c.get("_naver") or {}
        extra = (f" · 검색량지수 {fmt(nv.get('volume_idx'))} · 전년 대비 "
                 f"{'미확인' if nv.get('yoy') is None else format(nv['yoy'], '+d') + '%'}" if nv else "")
        print(f"  [{c['_id']:>3}] {c['_total']:>3}점  {c['name']} ({c['category']}){extra}")
    html_path = (outdir / "candidates.html").resolve()
    if product and candidates:
        print(f"RESULT_PRODUCT_ID={candidates[0]['_id']}")    # trade_hub serve가 결과 화면으로 이동할 때 사용
    print(f"\n✅ 완료: {html_path}")
    print(f"   💾 DB 저장: 후보 {len(ids)}개 (실행 #{run.id}) · 대괄호 안 숫자가 DB 번호")
    print(f"   다음 단계 — 진행할 품목을 공급처 발굴 단계로 넘기기 (예: 상위 3개): {handoff_command(candidates)}")
    return html_path


if __name__ == "__main__":
    try:
        main()
    except FATAL_API_ERRORS as e:
        sys.exit(f"\n✗ API 오류로 중단했습니다 ({type(e).__name__}): {getattr(e, 'message', e)}")
    except RuntimeError as e:  # --auto 품목 선정 단계 실패 등
        sys.exit(f"\n✗ 중단: {e}")
