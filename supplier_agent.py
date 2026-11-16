#!/usr/bin/env python3
"""
해외 공급처 발굴 에이전트 (1인 수입 무역용)

사용 예:
  python supplier_agent.py "실리콘 주방용품"            # 품목 지정
  python supplier_agent.py "LED 무드등" "요가매트"       # 여러 품목
  python supplier_agent.py --auto                       # 유망 품목을 에이전트가 직접 선정
  python supplier_agent.py --auto --count 2 --suppliers 4 --usd-krw 1390
  python supplier_agent.py --discover --hint "주방·수납, 1만~3만원대"   # 후보 점수표만 (저렴)

결과물: reports/YYYY-MM-DD_HHMM/ 폴더에
  - report.md      : 품목별 공급처 비교, 원가·마진, RFQ 이메일 초안
  - suppliers.csv  : 엑셀에서 바로 열리는 공급처 표
  - raw.json       : 원본 데이터
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
    import anthropic
except ImportError:
    sys.exit("anthropic 패키지가 필요합니다:  pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-sonnet-5-5"   # 더 깊은 조사가 필요하면 --model claude-opus-5-5
MAX_TOKENS = 16000
VAT_RATE = 0.10                       # 한국 수입 부가세
DEFAULT_DUTY_RATE = 0.08              # 관세율 미확인 시 가정값

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


# 에이전트가 최종 결과를 구조화된 형태로 제출하는 도구 (스크립트가 받아서 처리)
SUBMIT_PRODUCTS_TOOL = {
    "name": "submit_products",
    "description": "조사 끝에 선정한 유망 수입 품목 목록을 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "품목명 (한국어)"},
                        "search_keyword_en": {"type": "string", "description": "공급처 검색용 영어 키워드"},
                        "reason": {"type": "string", "description": "선정 이유 (수요·트렌드 근거)"},
                        "est_retail_krw": {"type": "number", "description": "예상 국내 판매가 (원, 중간값)"},
                        "regulations": {"type": "string", "description": "필요한 인증·규제 (KC, 식약처 등)"},
                        "risk": {"type": "string", "description": "주의할 위험 요소"},
                    },
                    "required": ["name", "search_keyword_en", "reason", "est_retail_krw", "regulations"],
                },
            }
        },
        "required": ["products"],
    },
}

# --discover: 후보 품목 평가 항목 (키, 표시 이름, 총점 가중치, 평가 기준)
SCORE_ITEMS = [
    ("demand", "수요", 0.30, "검색량·판매량·리뷰 수·시즌 수요 근거가 강할수록 높게"),
    ("margin", "마진 여지", 0.25, "국내 판매가 대비 해외 FOB 단가가 낮을수록 높게"),
    ("competition", "경쟁 여유", 0.20, "쿠팡·네이버에 같은 상품 판매자가 적을수록 높게 (레드오션 = 1)"),
    ("regulation", "규제 용이", 0.15, "KC·전파·식약처 등 인증 부담이 낮을수록 높게"),
    ("logistics", "물류 용이", 0.10, "작고 가볍고 파손 위험이 낮을수록 높게"),
]

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
                        "reason": {"type": "string", "description": "수요·트렌드 근거 (확인한 수치·출처 포함)"},
                        "est_retail_krw": {"type": ["number", "null"], "description": "국내 판매가 시세 (원, 중간값)"},
                        "est_fob_usd": {"type": ["number", "null"], "description": "해외 FOB 단가 시세 (USD, 중간값)"},
                        "regulations": {"type": ["string", "null"], "description": "필요한 인증·규제"},
                        "risk": {"type": ["string", "null"], "description": "주의할 위험 요소"},
                        "scores": {
                            "type": "object",
                            "description": "각 항목 1~5점 (5가 가장 유리)",
                            "properties": {k: {"type": "number", "minimum": 1, "maximum": 5, "description": f"{label}: {rule}"}
                                           for k, label, _, rule in SCORE_ITEMS},
                            "required": [k for k, *_ in SCORE_ITEMS],
                        },
                        "score_notes": {"type": "string", "description": "점수 근거 요약 (항목별 한 줄)"},
                    },
                    "required": ["name", "search_keyword_en", "category", "keywords_ko", "reason", "scores", "score_notes"],
                },
            }
        },
        "required": ["candidates"],
    },
}

SUBMIT_SUPPLIERS_TOOL = {
    "name": "submit_suppliers",
    "description": "한 품목에 대해 조사한 공급처 후보와 비용 추정치를 제출합니다. 확인 못한 값은 null로 둡니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product": {"type": "string"},
            "hs_code": {"type": ["string", "null"], "description": "HS코드 후보 (6~10자리)"},
            "duty_rate": {"type": ["number", "null"], "description": "한국 기본 관세율 (0.08 = 8%)"},
            "fta_note": {"type": ["string", "null"], "description": "FTA 협정세율 적용 가능성 (예: 한중 FTA)"},
            "est_retail_krw": {"type": ["number", "null"], "description": "국내 판매가 추정 (원)"},
            "est_shipping_per_unit_usd": {"type": ["number", "null"], "description": "개당 예상 국제운송비 (USD, 해상 LCL 또는 특송)"},
            "regulations": {"type": ["string", "null"]},
            "suppliers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "country": {"type": ["string", "null"]},
                        "type": {"type": ["string", "null"], "description": "제조사 / 무역상 / 미확인"},
                        "years_in_business": {"type": ["number", "null"]},
                        "certifications": {"type": ["string", "null"]},
                        "platform_badge": {"type": ["string", "null"], "description": "Gold Supplier, Verified 등"},
                        "moq": {"type": ["string", "null"], "description": "최소주문수량 (단위 포함)"},
                        "unit_price_usd": {"type": ["number", "null"], "description": "FOB 단가 (USD, 범위면 중간값)"},
                        "price_note": {"type": ["string", "null"], "description": "가격 범위·조건 원문"},
                        "url": {"type": ["string", "null"]},
                        "trust": {"type": "string", "enum": ["상", "중", "하"]},
                        "trust_reason": {"type": "string"},
                        "red_flags": {"type": ["string", "null"]},
                    },
                    "required": ["company", "trust", "trust_reason"],
                },
            },
            "top_picks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "먼저 연락할 공급처 2곳의 업체명",
            },
            "rfq_email": {"type": "string", "description": "top_picks에 보낼 영어 견적요청(RFQ) 이메일 초안"},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "참고한 URL"},
        },
        "required": ["product", "suppliers", "top_picks", "rfq_email"],
    },
}

SYSTEM_PROMPT = """당신은 한국에서 1인 수입 무역을 하는 사용자를 돕는 '해외 공급처 발굴 에이전트'입니다.

원칙:
- 반드시 web_search로 실제 정보를 확인하고, 확인되지 않은 값은 null로 둡니다. 절대 추측으로 채우지 않습니다.
- 1인 수입에 적합한 조건(소량 MOQ, 작은 부피·무게, 규제 부담 낮음)을 우선합니다.
- 업력이 짧거나, 정보가 부족하거나, 시세보다 지나치게 싼 업체는 위험 신호로 표시합니다.
- 브랜드 위조품·의약품·배터리 내장 등 고위험 품목은 위험을 명확히 밝힙니다.
- 조사가 끝나면 반드시 지정된 submit 도구로 결과를 제출합니다. 산문으로 답하지 않습니다."""


# ---------------------------------------------------------------------------
# 제출값 검증 (스키마의 필수 필드·타입·enum 확인)
# ---------------------------------------------------------------------------
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
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
def run_agent(backend, prompt, submit_tool, log=print, review=None):
    """web_search로 조사하게 한 뒤, submit_tool 호출 내용을 받아 반환한다.
    review(제출값)가 문자열을 돌려주면 그 내용(예: 실측 데이터)을 모델에게 보여주고 한 번 다시 제출받는다."""
    client, model, search_tool, extra, _ = backend
    tools = [search_tool, submit_tool]
    messages = [{"role": "user", "content": prompt}]
    nudged = False
    reviewed = False
    retries = 0

    for _ in range(12):  # 안전장치
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
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


def naver_trends(groups, keys, today=None):
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


def naver_lookup(candidates, keys, cache, log=print):
    """후보별 네이버 검색어 트렌드를 조회해 cache[품목명]에 넣는다. 실패하면 None."""
    todo = {c["name"]: [k for k in c.get("keywords_ko", []) if k.strip()][:3] or [c["name"]]
            for c in candidates if c["name"] not in cache}
    if not todo:
        return
    try:
        trends = naver_trends(todo, keys)
    except NaverError as e:
        if str(e).startswith(("HTTP 401", "HTTP 403")):
            log(f"    ⚠️ 네이버 API HUB 인증/권한 오류({e}) → 네이버 데이터 없이 진행합니다. "
                "콘솔의 NAVER API HUB 애플리케이션에 '검색어 트렌드' 구독이 있는지, 키가 그 애플리케이션 것인지 확인하세요.")
        else:
            log(f"    ⚠️ 네이버 검색어 트렌드 조회 실패: {e}")
        trends = {}
    for name, kws in todo.items():
        cache[name] = {"keyword": kws[0], "keywords": kws, **trends[name]} if trends.get(name) else None


def naver_summary(name, d):
    if not d:
        return f"- {name}: 네이버 데이터 조회 실패"
    pct = lambda v: "미확인" if v is None else f"{v:+d}%"
    vol = "미확인" if d.get("volume_idx") is None else f"{d['volume_idx']}"
    return (f"- {name} [검색어 {', '.join(d['keywords'])}]: 검색량지수 {vol} · "
            f"최근3개월 성장 {pct(d.get('growth_3m'))} · 전년동기 대비 {pct(d.get('yoy'))}")


def naver_review(keys, cache, log=print):
    """--discover 제출 후 네이버 검색어 트렌드를 모델에게 보여주고 점수를 다시 매기게 하는 review 함수."""
    def review(submitted):
        log("    📊 네이버 검색어 트렌드 조회 중...")
        naver_lookup(submitted["candidates"], keys, cache, log)
        lines = [naver_summary(c["name"], cache.get(c["name"])) for c in submitted["candidates"]]
        if not any(cache.get(c["name"]) for c in submitted["candidates"]):
            return None   # 전부 실패 → 재검토 없이 그대로 사용
        return (f"네이버 검색어 트렌드 실측 데이터입니다 (검색량지수는 '{NAVER_TREND_ANCHOR}'의 최근 3개월 "
                "네이버 검색량 = 100 기준, 성장률은 직전 3개월·전년 같은 3개월 대비):\n" + "\n".join(lines) +
                "\n\n이 수치를 반영해 demand 점수를 다시 검토하고(필요하면 다른 점수도), score_notes에 근거 수치를 "
                "인용해 submit_candidates로 다시 제출하세요. 검색량이 거의 없는 후보는 다른 품목으로 교체해도 됩니다.")
    return review


# ---------------------------------------------------------------------------
# 원가·마진 계산 (모델이 아니라 코드로 계산)
# ---------------------------------------------------------------------------
def landed_cost(unit_price_usd, shipping_usd, duty_rate, usd_krw):
    """개당 수입원가(원). 과세가격 = CIF(제품가+운임), 관세 → 부가세 순."""
    if unit_price_usd is None:
        return None
    shipping_usd = shipping_usd or 0.0  # 미확인 시 0 → cost_assumptions()로 리포트에 표시
    duty_rate = duty_rate if duty_rate is not None else DEFAULT_DUTY_RATE
    cif = (unit_price_usd + shipping_usd) * usd_krw
    duty = cif * duty_rate
    vat = (cif + duty) * VAT_RATE
    return round(cif + duty + vat)


def cost_assumptions(result):
    """원가 계산에 확인된 값 대신 가정값을 쓴 항목 목록."""
    notes = []
    if result.get("est_shipping_per_unit_usd") is None:
        notes.append("운임 미반영(0원으로 계산 → 실제 원가는 더 높음)")
    if result.get("duty_rate") is None:
        notes.append(f"관세율 미확인({DEFAULT_DUTY_RATE*100:.0f}%로 가정)")
    return notes


def margin(retail_krw, cost_krw, platform_fee):
    """판매가에서 플랫폼 수수료와 부가세(매출세액)를 뺀 실수익 기준 마진율."""
    if not retail_krw or cost_krw is None:
        return None, None
    net_revenue = retail_krw / (1 + VAT_RATE) * (1 - platform_fee)
    # 수입 시 낸 부가세는 매입세액 공제 → 원가에서 제외
    cost_ex_vat = cost_krw / (1 + VAT_RATE)
    profit = net_revenue - cost_ex_vat
    return round(profit), round(profit / net_revenue * 100, 1)


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


def write_outputs(outdir, products_meta, results, args):
    outdir.mkdir(parents=True, exist_ok=True)

    # raw.json
    (outdir / "raw.json").write_text(
        json.dumps({"products": products_meta, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # suppliers.csv (utf-8-sig: 엑셀에서 한글 안 깨짐)
    cols = ["품목", "업체명", "국가", "유형", "업력(년)", "인증", "플랫폼배지", "MOQ",
            "FOB단가(USD)", "가격메모", "개당원가(원)", "예상판매가(원)", "개당이익(원)", "마진율(%)", "원가가정",
            "신뢰도", "신뢰근거", "위험신호", "링크"]
    with open(outdir / "suppliers.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in results:
            for s in r["suppliers"]:
                w.writerow([
                    r["product"], s.get("company"), s.get("country"), s.get("type"),
                    s.get("years_in_business"), s.get("certifications"), s.get("platform_badge"),
                    s.get("moq"), s.get("unit_price_usd"), s.get("price_note"),
                    s.get("_landed_krw"), r.get("est_retail_krw"), s.get("_profit_krw"),
                    s.get("_margin_pct"), " / ".join(r.get("_cost_assumptions", [])), s.get("trust"), s.get("trust_reason"),
                    s.get("red_flags"), s.get("url"),
                ])

    # report.md
    L = []
    L.append(f"# 공급처 발굴 리포트 — {datetime.now():%Y-%m-%d}\n")
    L.append(f"- 적용 환율: 1 USD = {args.usd_krw:,.0f}원 · 플랫폼 수수료 가정: {args.fee*100:.0f}% · 부가세 10%")
    L.append("- 원가 = (FOB단가 + 개당운임) × 환율 + 관세 + 부가세. 마진은 판매 부가세·수수료 차감, 수입 부가세 공제 기준.")
    L.append("- 표의 '미확인' 값은 공급처에 직접 확인이 필요합니다.\n")

    if products_meta:
        L.append("## 이번에 선정된 품목\n")
        for p in products_meta:
            L.append(f"**{p['name']}** — {p['reason']}")
            L.append(f"  - 규제: {p.get('regulations') or '미확인'} · 위험: {p.get('risk') or '-'}\n")

    for r in results:
        L.append(f"## {r['product']}\n")
        L.append(f"- HS코드 후보: {fmt(r.get('hs_code'))} · 기본관세율: "
                 f"{fmt(round(r['duty_rate']*100, 1) if r.get('duty_rate') is not None else None, '%')}"
                 f" · FTA: {fmt(r.get('fta_note'))}")
        L.append(f"- 필요 인증·규제: {fmt(r.get('regulations'))}")
        L.append(f"- 예상 판매가: {fmt(r.get('est_retail_krw'), '원')} · "
                 f"개당 운임 추정: {fmt(r.get('est_shipping_per_unit_usd'), ' USD')}")
        if r.get("_cost_assumptions"):
            L.append(f"- ⚠️ 원가 계산 가정: {' · '.join(r['_cost_assumptions'])} — 아래 원가·마진은 참고용")
        L.append("")

        L.append("| 업체 | 국가 | 유형 | 업력 | MOQ | FOB단가 | 개당원가 | 마진율 | 신뢰도 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for s in r["suppliers"]:
            name = f"[{s['company']}]({s['url']})" if s.get("url") else s["company"]
            L.append(
                f"| {name} | {fmt(s.get('country'))} | {fmt(s.get('type'))} | "
                f"{fmt(s.get('years_in_business'), '년')} | {fmt(s.get('moq'))} | "
                f"{fmt(s.get('unit_price_usd'), ' USD')} | {fmt(s.get('_landed_krw'), '원')} | "
                f"{fmt(s.get('_margin_pct'), '%')} | {s['trust']} |"
            )
        L.append("")
        for s in r["suppliers"]:
            note = f"- **{s['company']}**: {s['trust_reason']}"
            if s.get("red_flags"):
                note += f"  ⚠️ {s['red_flags']}"
            L.append(note)
        L.append("")
        L.append(f"**먼저 연락할 곳:** {', '.join(r.get('top_picks', []))}\n")
        L.append("<details><summary>견적요청(RFQ) 이메일 초안</summary>\n")
        L.append("```\n" + r.get("rfq_email", "") + "\n```\n</details>\n")
        if r.get("sources"):
            L.append("출처: " + " · ".join(f"<{u}>" for u in r["sources"][:10]) + "\n")

    (outdir / "report.md").write_text("\n".join(L), encoding="utf-8")
    write_html(outdir, products_meta, results, args)


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


def write_html(outdir, products_meta, results, args):
    from html import escape

    def e(v, suffix=""):
        return escape(fmt(v, suffix))

    def link(url, text):
        if url and str(url).startswith(("http://", "https://")):
            return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(text)}</a>'
        return escape(text)

    H = [f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>공급처 리포트 {datetime.now():%Y-%m-%d}</title><style>{HTML_STYLE}</style></head><body><main>
<h1>공급처 발굴 리포트</h1>
<p class="sub">{datetime.now():%Y-%m-%d %H:%M} · 환율 1 USD = {args.usd_krw:,.0f}원 · 플랫폼 수수료 {args.fee*100:.0f}% · 부가세 10%<br>
원가 = (FOB단가 + 개당운임) × 환율 + 관세 + 부가세. 마진은 판매 부가세·수수료 차감, 수입 부가세 공제 기준.
'미확인' 값은 공급처에 직접 확인하세요.</p>"""]

    if products_meta:
        H.append('<section class="card"><h2>이번에 선정된 품목</h2><ul class="notes">')
        for p in products_meta:
            H.append(f"<li><b>{escape(p['name'])}</b> — {escape(p['reason'])}<br>"
                     f"<small>규제: {e(p.get('regulations'))} · 위험: {escape(p.get('risk') or '-')}</small></li>")
        H.append("</ul></section>")

    for i, r in enumerate(results):
        duty = round(r["duty_rate"] * 100, 1) if r.get("duty_rate") is not None else None
        picks = set(r.get("top_picks", []))
        H.append(f'<section class="card"><h2>{escape(r["product"])}</h2><div class="facts">'
                 f"<div><b>HS코드 후보</b>{e(r.get('hs_code'))}</div>"
                 f"<div><b>기본 관세율</b>{e(duty, '%')}</div>"
                 f"<div><b>예상 판매가</b>{e(r.get('est_retail_krw'), '원')}</div>"
                 f"<div><b>개당 운임 추정</b>{e(r.get('est_shipping_per_unit_usd'), ' USD')}</div>"
                 f"<div style='grid-column:1/-1'><b>FTA</b>{e(r.get('fta_note'))}</div>"
                 f"<div style='grid-column:1/-1'><b>필요 인증·규제</b>{e(r.get('regulations'))}</div></div>")
        if r.get("_cost_assumptions"):
            H.append(f'<div class="warn">⚠️ 원가 계산 가정: {escape(" · ".join(r["_cost_assumptions"]))} — 원가·마진은 참고용</div>')

        H.append('<div class="scroll"><table><tr><th>업체</th><th>국가</th><th>유형</th><th>업력</th><th>MOQ</th>'
                 '<th>FOB단가</th><th>개당원가</th><th>개당이익</th><th>마진율</th><th>신뢰도</th></tr>')
        for s in r["suppliers"]:
            m = s.get("_margin_pct")
            mcls = "" if m is None else ("neg" if m < 0 else "pos")
            star = ' <span class="pick" title="먼저 연락할 곳">★</span>' if s["company"] in picks else ""
            H.append(f"<tr><td>{link(s.get('url'), s['company'])}{star}</td><td>{e(s.get('country'))}</td>"
                     f"<td>{e(s.get('type'))}</td><td class='num'>{e(s.get('years_in_business'), '년')}</td>"
                     f"<td>{e(s.get('moq'))}</td><td class='num'>{e(s.get('unit_price_usd'), ' USD')}</td>"
                     f"<td class='num'>{e(s.get('_landed_krw'), '원')}</td>"
                     f"<td class='num'>{e(s.get('_profit_krw'), '원')}</td>"
                     f"<td class='num {mcls}'>{e(m, '%')}</td>"
                     f"<td><span class='badge t{escape(s['trust'])}'>{escape(s['trust'])}</span></td></tr>")
        H.append("</table></div><h3>업체별 판단 근거</h3><ul class='notes'>")
        for s in r["suppliers"]:
            flag = f"<br><span class='flag'>⚠️ {escape(s['red_flags'])}</span>" if s.get("red_flags") else ""
            H.append(f"<li><b>{escape(s['company'])}</b>: {escape(s['trust_reason'])}{flag}</li>")
        H.append("</ul>")
        H.append(f"<p><b>먼저 연락할 곳:</b> <span class='pick'>{escape(', '.join(r.get('top_picks', [])))}</span></p>")
        H.append(f"<details><summary>견적요청(RFQ) 이메일 초안</summary><pre id='rfq{i}'>{escape(r.get('rfq_email', ''))}</pre>"
                 f"<button onclick=\"navigator.clipboard.writeText(document.getElementById('rfq{i}').innerText)"
                 f".then(()=>{{this.textContent='복사됨 ✓'}})\">이메일 복사</button></details>")
        if r.get("sources"):
            H.append("<details><summary>출처</summary><p class='src'>"
                     + "<br>".join(link(u, u) for u in r["sources"][:20]) + "</p></details>")
        H.append("</section>")

    H.append("</main></body></html>")
    (outdir / "report.html").write_text("\n".join(H), encoding="utf-8")


# ---------------------------------------------------------------------------
# --discover: 후보 품목 점수표 (공급처 조사 없이 저렴하게 후보만 비교)
# ---------------------------------------------------------------------------
def score_candidates(candidates, args):
    """항목 점수로 총점(100점 만점)과 대략적 마진을 계산하고 총점 내림차순으로 정렬한다."""
    for c in candidates:
        weighted = sum(w * c["scores"][k] for k, _, w, _ in SCORE_ITEMS)
        c["_total"] = round(weighted / 5 * 100)
        # 운임 0·관세 8% 가정의 거친 추정 → 표에서 '참고용'으로 표시
        c["_landed_krw"] = landed_cost(c.get("est_fob_usd"), None, None, args.usd_krw)
        c["_profit_krw"], c["_margin_pct"] = margin(c.get("est_retail_krw"), c["_landed_krw"], args.fee)
    return sorted(candidates, key=lambda c: c["_total"], reverse=True)


def next_command(candidates, n=3):
    """상위 후보로 공급처 조사를 이어서 실행할 명령어."""
    names = " ".join(f'"{c["name"]}"' for c in candidates[:n])
    return f"python supplier_agent.py {names}"


def write_candidates(outdir, candidates, args):
    from html import escape
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "raw.json").write_text(
        json.dumps({"hint": args.hint, "candidates": candidates}, ensure_ascii=False, indent=2), encoding="utf-8")

    labels = [label for _, label, _, _ in SCORE_ITEMS]
    with open(outdir / "candidates.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["순위", "품목", "카테고리", "총점"] + labels +
                   ["예상판매가(원)", "FOB시세(USD)", "추정마진(%)",
                    "네이버검색어", f"검색량지수({NAVER_TREND_ANCHOR}=100)", "3개월성장(%)", "전년대비(%)",
                    "규제", "위험", "선정근거", "점수근거", "영어키워드"])
        for i, c in enumerate(candidates, 1):
            n = c.get("_naver") or {}
            w.writerow([i, c["name"], c["category"], c["_total"]] + [c["scores"][k] for k, *_ in SCORE_ITEMS] +
                       [c.get("est_retail_krw"), c.get("est_fob_usd"), c.get("_margin_pct"),
                        ", ".join(n.get("keywords", [])), n.get("volume_idx"), n.get("growth_3m"), n.get("yoy"),
                        c.get("regulations"), c.get("risk"), c["reason"], c["score_notes"], c["search_keyword_en"]])

    def e(v, suffix=""):
        return escape(fmt(v, suffix))

    weights = " · ".join(f"{label} {wt*100:.0f}%" for _, label, wt, _ in SCORE_ITEMS)
    has_naver = any(c.get("_naver") for c in candidates)
    naver_note = (f"<br>네이버 데이터: 검색량지수는 최근 3개월 네이버 검색량('{escape(NAVER_TREND_ANCHOR)}'=100), "
                  "성장률은 직전 3개월·전년 같은 기간 대비 (NAVER API HUB 검색어 트렌드)."
                  if has_naver else "")
    hint = f" · 방향: {escape(args.hint)}" if args.hint else ""
    cmd = next_command(candidates)
    H = [f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>후보 품목 {datetime.now():%Y-%m-%d}</title><style>{HTML_STYLE}
.score{{font-weight:700}}.s5,.s4{{color:var(--good)}}.s3{{color:var(--mid)}}.s2,.s1{{color:var(--bad)}}</style></head><body><main>
<h1>후보 품목 점수표</h1>
<p class="sub">{datetime.now():%Y-%m-%d %H:%M}{hint} · 총점 가중치: {weights}<br>
각 항목 1~5점(5가 유리). 추정마진은 운임 0원·관세 8% 가정의 참고값이며 실제보다 높게 나옵니다.{naver_note}</p>
<section class="card"><h2>다음 단계</h2><p>마음에 드는 품목만 골라 공급처를 조사하세요. 상위 3개 예:</p>
<pre id="cmd">{escape(cmd)}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('cmd').innerText).then(()=>{{this.textContent='복사됨 ✓'}})">명령어 복사</button></section>
<section class="card"><div class="scroll"><table><tr><th>#</th><th>품목</th><th>카테고리</th><th>총점</th>"""
         + "".join(f"<th>{escape(l)}</th>" for l in labels)
         + "<th>예상판매가</th><th>FOB시세</th><th>추정마진</th>"
         + (f"<th title='최근 3개월 네이버 검색량, {escape(NAVER_TREND_ANCHOR)}=100'>검색량지수</th>"
            "<th>3개월 성장</th><th>전년 대비</th>" if has_naver else "")
         + "</tr>"]
    for i, c in enumerate(candidates, 1):
        cells = "".join(f"<td class='num score s{round(c['scores'][k])}'>{e(c['scores'][k])}</td>" for k, *_ in SCORE_ITEMS)
        if has_naver:
            n = c.get("_naver") or {}
            g = lambda v: "미확인" if v is None else f"{v:+d}%"
            cells_n = (f"<td class='num'>{e(n.get('volume_idx'))}</td><td class='num'>{g(n.get('growth_3m'))}</td>"
                       f"<td class='num'>{g(n.get('yoy'))}</td>")
        else:
            cells_n = ""
        H.append(f"<tr><td>{i}</td><td><b>{escape(c['name'])}</b></td><td>{escape(c['category'])}</td>"
                 f"<td class='num'><b>{c['_total']}</b></td>{cells}"
                 f"<td class='num'>{e(c.get('est_retail_krw'), '원')}</td>"
                 f"<td class='num'>{e(c.get('est_fob_usd'), ' USD')}</td>"
                 f"<td class='num'>{e(c.get('_margin_pct'), '%')}</td>{cells_n}</tr>")
    H.append("</table></div></section><section class='card'><h2>후보별 근거</h2><ul class='notes'>")
    for i, c in enumerate(candidates, 1):
        risk = f"<br><span class='flag'>⚠️ {escape(c['risk'])}</span>" if c.get("risk") else ""
        H.append(f"<li><b>{i}. {escape(c['name'])}</b> ({escape(c['search_keyword_en'])})<br>{escape(c['reason'])}"
                 f"<br><small>점수 근거: {escape(c['score_notes'])}</small>"
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

    ap = argparse.ArgumentParser(description="해외 공급처 발굴 에이전트")
    ap.add_argument("products", nargs="*", help="조사할 품목 (한국어 OK)")
    ap.add_argument("--auto", action="store_true", help="유망 수입 품목을 에이전트가 직접 선정")
    ap.add_argument("--discover", action="store_true",
                    help="공급처 조사 없이 후보 품목 점수표만 만듦 (저렴). 마음에 드는 품목만 골라 이어서 조사")
    ap.add_argument("--hint", default=None,
                    help='--auto/--discover 시 품목 방향. 예: --hint "주방·수납, 1만~3만원대, 겨울 시즌"')
    ap.add_argument("--count", type=int, default=None, help="선정할 품목 수 (기본: --auto 3, --discover 10)")
    ap.add_argument("--suppliers", type=int, default=5, help="품목당 공급처 후보 수 (기본 5)")
    ap.add_argument("--usd-krw", type=float, default=1400.0, help="원/달러 환율 (기본 1400, 최신값 넣기 권장)")
    ap.add_argument("--fee", type=float, default=0.11, help="판매 플랫폼 수수료율 (기본 0.11 = 11%)")
    ap.add_argument("--provider", choices=["anthropic", "openrouter"],
                    default=None,
                    help="anthropic = Claude(ANTHROPIC_API_KEY), openrouter = DeepSeek 등(OPENROUTER_API_KEY). "
                         "생략 시 .env의 PROVIDER, 그것도 없으면 anthropic")
    ap.add_argument("--model", default=None,
                    help=f"모델 (기본: anthropic은 {DEFAULT_MODEL}, openrouter는 .env의 OPENROUTER_MODEL 또는 {OPENROUTER_DEFAULT_MODEL})")
    ap.add_argument("--out", default="reports", help="결과 저장 폴더")
    ap.add_argument("--no-open", action="store_true", help="끝난 뒤 브라우저로 리포트를 열지 않음 (정기 실행용)")
    args = ap.parse_args()

    if args.discover and (args.auto or args.products):
        ap.error("--discover는 품목 입력이나 --auto와 함께 쓸 수 없습니다")
    if not args.products and not args.auto and not args.discover:
        ap.error("품목을 입력하거나 --auto / --discover를 사용하세요. 예: python supplier_agent.py \"요가매트\"")
    if args.hint and not (args.auto or args.discover):
        ap.error("--hint는 --auto 또는 --discover와 함께 씁니다")
    if args.count is None:
        args.count = 10 if args.discover else 3
    if args.count < 1:
        ap.error("--count는 1 이상이어야 합니다")
    # 스크립트 폴더의 .env를 읽음 (작업 스케줄러처럼 다른 폴더에서 실행돼도 찾도록)
    load_dotenv(Path(__file__).resolve().parent / ".env")
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

    # 후보 점수표만 만들고 끝 (공급처 조사 없음)
    if args.discover:
        print(f"▶ 후보 품목 {args.count}개 조사·평가 중..." + (f" (방향: {args.hint})" if args.hint else ""))
        criteria = "\n".join(f"  · {k} ({label}): {rule}" for k, label, _, rule in SCORE_ITEMS)
        keys, naver_cache = naver_keys(), {}
        print("  · 네이버 실측 데이터: " + ("사용 (제출 후 네이버 검색어 트렌드로 점수 재검토)" if keys
                                       else "사용 안 함 (.env에 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 없음)"))
        data = run_agent(backend, f"""오늘은 {today}입니다.
{hint}
한국 시장의 최근 소비 트렌드, 네이버·쿠팡 인기 카테고리와 리뷰 수, 해외직구 인기 품목, 다가오는 시즌 수요를 조사해서
1인 수입 무역 후보 품목 {args.count}개를 골라 submit_candidates로 제출하세요.
- 서로 겹치지 않는 품목으로 고릅니다 (같은 품목의 색상·사이즈 변형은 하나로 봅니다).
- 각 후보를 아래 5개 항목에 대해 1~5점(5가 가장 유리)으로 평가하고 score_notes에 항목별 근거를 한 줄씩 적습니다.
{criteria}
- 국내 판매가(est_retail_krw)와 해외 FOB 단가(est_fob_usd)는 검색으로 확인한 시세로 적고, 못 찾으면 null로 둡니다.
- 점수를 후하게 주지 마세요. 근거가 약하면 3점 이하로 줍니다.""", SUBMIT_CANDIDATES_TOOL,
                         review=naver_review(keys, naver_cache) if keys else None)
        if keys:   # 재제출 때 교체된 후보도 조회
            naver_lookup(data["candidates"], keys, naver_cache)
        for c in data["candidates"]:
            c["_naver"] = naver_cache.get(c["name"])
        candidates = score_candidates(data["candidates"], args)
        outdir = Path(args.out) / datetime.now().strftime("%Y-%m-%d_%H%M_discover")
        write_candidates(outdir, candidates, args)
        print()
        for i, c in enumerate(candidates, 1):
            n = c.get("_naver") or {}
            extra = (f" · 검색량지수 {fmt(n.get('volume_idx'))} · 전년 대비 "
                     f"{'미확인' if n.get('yoy') is None else format(n['yoy'], '+d') + '%'}" if n else "")
            print(f"  {i:>2}. [{c['_total']:>3}점] {c['name']} ({c['category']}){extra}")
        html_path = (outdir / "candidates.html").resolve()
        print(f"\n✅ 완료: {html_path}")
        print(f"   다음 단계 (상위 3개 공급처 조사): {next_command(candidates)}")
        if not args.no_open:
            import webbrowser
            webbrowser.open(html_path.as_uri())
        return

    products_meta = []
    targets = [{"name": p, "search_keyword_en": None, "est_retail_krw": None} for p in args.products]

    # 1단계: 품목 자동 선정
    if args.auto:
        print(f"▶ 유망 수입 품목 {args.count}개 선정 중..." + (f" (방향: {args.hint})" if args.hint else ""))
        data = run_agent(backend, f"""오늘은 {today}입니다.
{hint}
한국 시장의 최근 소비 트렌드, 네이버·쿠팡 인기 카테고리, 해외직구 인기 품목, 다가오는 시즌 수요를 조사해서
1인 수입 무역에 적합한 유망 품목 {args.count}개를 골라 submit_products로 제출하세요.
조건: 소량 발주 가능, 부피·무게가 작음, 인증·규제가 명확하거나 부담이 낮음, 마진 여지 있음.
이미 너무 포화된 레드오션 품목은 피하세요.""", SUBMIT_PRODUCTS_TOOL)
        products_meta = data["products"][: args.count]
        for p in products_meta:
            print(f"  ✓ {p['name']} — {p['reason'][:60]}")
        targets += products_meta

    # 2단계: 품목별 공급처 조사
    results = []
    for t in targets:
        print(f"\n▶ [{t['name']}] 공급처 {args.suppliers}곳 조사 중...")
        kw = f" (영어 키워드: {t['search_keyword_en']})" if t.get("search_keyword_en") else ""
        try:
            r = run_agent(backend, f"""품목: {t['name']}{kw}

이 품목의 해외 공급처 후보 {args.suppliers}곳을 찾아 submit_suppliers로 제출하세요.
- Alibaba, 1688, Global Sources, Made-in-China, IndiaMART, 제조사 공식 사이트 등을 검색합니다.
- 각 업체: 국가, 제조사/무역상 구분, 업력, 인증, 플랫폼 배지, MOQ, FOB 단가(USD), 링크를 확인합니다.
- 신뢰도(상/중/하)와 근거, 위험 신호를 적습니다.
- 한국 수입 시 HS코드 후보, 기본 관세율, FTA 적용 가능성, 필요한 인증(KC·전파·식약처 등)을 조사합니다.
- 국내 판매가(네이버쇼핑·쿠팡 시세)와 개당 국제운송비를 추정합니다.
- 먼저 연락할 2곳을 고르고, 그 업체들에 보낼 영어 RFQ 이메일 초안을 작성합니다
  (샘플 가능 여부, MOQ, 단가 구간, 리드타임, 인증서 보유 여부, 결제조건 문의 포함).""", SUBMIT_SUPPLIERS_TOOL,
                log=print)
        except FATAL_API_ERRORS:
            raise  # 키·권한·잔액·요청 형식 문제 → 다음 품목도 실패하므로 중단
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            continue

        if r.get("est_retail_krw") is None and t.get("est_retail_krw"):
            r["est_retail_krw"] = t["est_retail_krw"]
        r["_cost_assumptions"] = cost_assumptions(r)

        for s in r["suppliers"]:
            s["_landed_krw"] = landed_cost(
                s.get("unit_price_usd"), r.get("est_shipping_per_unit_usd"), r.get("duty_rate"), args.usd_krw)
            s["_profit_krw"], s["_margin_pct"] = margin(r.get("est_retail_krw"), s["_landed_krw"], args.fee)
        results.append(r)
        print(f"  ✓ {len(r['suppliers'])}곳 확보 · 추천: {', '.join(r.get('top_picks', []))}")

    if not results:
        sys.exit("결과가 없습니다.")

    outdir = Path(args.out) / datetime.now().strftime("%Y-%m-%d_%H%M")
    write_outputs(outdir, products_meta, results, args)
    html_path = (outdir / "report.html").resolve()
    print(f"\n✅ 완료: {html_path}")
    print(f"   (같은 폴더에 report.md, 엑셀용 suppliers.csv도 있습니다)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(html_path.as_uri())


if __name__ == "__main__":
    try:
        main()
    except FATAL_API_ERRORS as e:
        sys.exit(f"\n✗ API 오류로 중단했습니다 ({type(e).__name__}): {getattr(e, 'message', e)}")
    except RuntimeError as e:  # --auto 품목 선정 단계 실패 등
        sys.exit(f"\n✗ 중단: {e}")
