#!/usr/bin/env python3
"""
해외 공급처 발굴 에이전트 (1인 수입 무역용)

사용 예:
  python supplier_agent.py "실리콘 주방용품"            # 품목 지정
  python supplier_agent.py "LED 무드등" "요가매트"       # 여러 품목
  python supplier_agent.py --auto                       # 유망 품목을 에이전트가 직접 선정
  python supplier_agent.py --auto --count 2 --suppliers 4 --usd-krw 1390

결과물: reports/YYYY-MM-DD_HHMM/ 폴더에
  - report.md      : 품목별 공급처 비교, 원가·마진, RFQ 이메일 초안
  - suppliers.csv  : 엑셀에서 바로 열리는 공급처 표
  - raw.json       : 원본 데이터
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
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
def run_agent(backend, prompt, submit_tool, log=print):
    """web_search로 조사하게 한 뒤, submit_tool 호출 내용을 받아 반환한다."""
    client, model, search_tool, extra, _ = backend
    tools = [search_tool, submit_tool]
    messages = [{"role": "user", "content": prompt}]
    nudged = False
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
                return submit.input
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
        value = value.strip().strip('"').strip("'")
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
    ap.add_argument("--count", type=int, default=3, help="--auto 시 선정할 품목 수 (기본 3)")
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

    if not args.products and not args.auto:
        ap.error("품목을 입력하거나 --auto를 사용하세요. 예: python supplier_agent.py \"요가매트\"")
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
    products_meta = []
    targets = [{"name": p, "search_keyword_en": None, "est_retail_krw": None} for p in args.products]

    # 1단계: 품목 자동 선정
    if args.auto:
        print(f"▶ 유망 수입 품목 {args.count}개 선정 중...")
        today = datetime.now().strftime("%Y년 %m월 %d일")
        data = run_agent(backend, f"""오늘은 {today}입니다.
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
