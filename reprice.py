"""국내 판매가 다시 조사(--reprice): 품목마다 국내 실제 시세·낱개/묶음 기준을 확인하고,
판매가가 크게 다른 변형이 한 품목에 섞여 있으면 별도 후보 품목으로 나눈다.

2026-11-24 4번(비용·마진) 요청 (2번 세션 경유):
  1) 판매가가 크게 다른 변형은 별도 품목으로 (#34 M5 박스 / M6a 30주년 박스, #16 2구 / 4구 / LED형 등)
  2) est_retail_krw = 그 상품의 국내 실제 시세, products.notes 에 '낱개/묶음 기준'
  3) 리셀 상품은 확인 날짜·출처도 notes 에
정한 방식 (사용자 결정 2026-11-24):
  - notes 는 사람이 적는 메모 칸이라, 1번은 맨 앞에 '[1번 시세 날짜]' 태그 줄만 넣고 바꾼다. 사람 메모는 그대로.
  - 나눈 원래 품목은 단계·공급처를 건드리지 않고 notes 태그와 이벤트 기록으로 '#N, #M으로 나눔'만 남긴다.
    새 변형 품목은 candidate (공급처 조사로 보낼지는 사람이 정함).
"""
import json
import re

import supplier_agent as sa
from supplier_agent import trade_db

SPLIT_PRICE_RATIO = 1.5      # 변형끼리 대표 판매가가 이 배 이상 차이 나면 나눔 (4번 요청: '판매가가 크게 다른 변형')
MAX_VARIANTS = 4
TAG = "1번 시세"             # notes 태그: '[1번 시세 2026-11-24] ...' 한 줄

REPRICE_TOOL = {
    "name": "submit_retail",
    "description": "품목의 국내 실제 판매가(변형별)를 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "split": {"type": "boolean",
                      "description": "판매가가 크게 다른(대표가 1.5배 이상 차이) 변형이 섞여 있어 별도 품목으로 나눠야 하면 true"},
            "variants": {"type": "array", "description": "split=false면 1개, true면 나눌 변형 2~4개", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "변형을 가리키는 구체적인 한국어 품목명 (split=false면 원래 이름 그대로)"},
                    "existing_id": {"type": ["integer", "null"],
                                    "description": "split=true일 때 이 변형이 '이미 있는 품목' 목록의 품목과 같으면 그 번호 (새로 만들지 않음). 아니면 null"},
                    "search_keyword_en": {"type": ["string", "null"], "description": "해외 공급처 검색용 영문 키워드 (변형 기준)"},
                    "basis": {"type": "string", "description": "est_retail_krw의 판매 단위. 예: '낱개', '묶음 5개', '박스(30팩)'"},
                    "est_retail_krw": {"type": ["integer", "null"], "minimum": 0,
                                       "description": "그 단위의 국내 대표 판매가(원). 확인 못 하면 null"},
                    "unit_price_krw": {"type": ["integer", "null"], "minimum": 0,
                                       "description": "묶음·박스면 개당 환산가(원), 낱개면 est_retail_krw와 같음"},
                    "price_range_krw": {"type": "string", "description": "확인한 가격 범위. 예: '3,500~8,000원'"},
                    "resale": {"type": "boolean", "description": "정가가 아니라 리셀(웃돈) 시세로 거래되는 상품이면 true"},
                    "sources": {"type": "array", "description": "검색 결과에서 실제로 확인한 판매 페이지 2~4개 (지어내지 말 것)",
                                "items": {"type": "object", "properties": {
                                    "site": {"type": "string", "description": "예: 쿠팡, 네이버쇼핑, 아트박스, 번개장터"},
                                    "url": {"type": ["string", "null"]},
                                    "price_krw": {"type": ["integer", "null"]},
                                    "unit": {"type": "string", "description": "그 가격의 단위. 예: 낱개, 5개 묶음"}},
                                    "required": ["site", "price_krw", "unit"]}},
                },
                "required": ["name", "basis", "est_retail_krw", "unit_price_krw", "price_range_krw", "resale", "sources"]}},
            "reason": {"type": "string", "description": "나누거나 나누지 않은 이유 한두 줄 (변형별 가격 차이 근거)"},
        },
        "required": ["split", "variants", "reason"],
    },
}


def targets(conn, ids=(), with_active=True):
    """다시 조사할 품목: 진행 중(후보·중단이 아닌) 품목 + 지정한 번호. with_active=False면 지정한 번호만."""
    rows = conn.execute("""SELECT p.*, s.id AS score_id FROM products p
        LEFT JOIN product_scores s ON s.id = (SELECT MAX(id) FROM product_scores WHERE product_id = p.id)
        WHERE ((? AND p.stage NOT IN ('candidate', 'dropped')) OR p.id IN (SELECT value FROM json_each(?)))
        ORDER BY p.id""", (with_active, json.dumps(list(ids)))).fetchall()
    return rows


def others(conn, pid):
    """중복 변형을 만들지 않게 모델에게 보여 줄 이미 있는 품목 (중단 제외, 자기 자신 제외)."""
    return [(r["id"], r["name"], r["est_retail_krw"]) for r in
            conn.execute("SELECT id, name, est_retail_krw FROM products WHERE stage != 'dropped' AND id != ? ORDER BY id", (pid,))]


def supplier_hints(conn, pid, n=8):
    """2번이 본 공급처 가격 메모 (변형 구분 참고용)."""
    rows = conn.execute("""SELECT moq, price_note, unit_price_usd FROM product_suppliers
        WHERE product_id = ? AND (price_note IS NOT NULL OR moq IS NOT NULL) ORDER BY score DESC LIMIT ?""",
                        (pid, n)).fetchall()
    return [" · ".join(str(x) for x in (r["moq"], r["price_note"] or (r["unit_price_usd"] and f"USD {r['unit_price_usd']}"))
                       if x) for r in rows]


def prompt(row, today, hints, existing=()):
    kws = ", ".join(json.loads(row["keywords_ko_json"] or "[]"))
    hint = ("\n2번(공급처 조사)이 본 공급처 가격 메모 (변형 구분 참고):\n" + "\n".join(f"  - {h}" for h in hints)) if hints else ""
    return f"""오늘은 {today}입니다. 한국 1인 수입 무역 품목의 '국내 실제 판매가'를 다시 확인합니다.
품목 #{row['id']}: {row['name']}
- 지금 적힌 국내 판매가(est_retail_krw): {row['est_retail_krw'] or '없음'}원 (어떤 단위 기준인지 불분명)
- 분야: {row['category'] or '-'} · 검색어: {kws or '-'} · 영문: {row['search_keyword_en'] or '-'}
- 선정 근거: {(row['reason'] or '')[:300]}{hint}

할 일
1. 네이버쇼핑·쿠팡·11번가·전문몰(아트박스·다이소 등)에서 이 품목의 실제 판매 페이지를 검색해 가격과 판매 단위(낱개·묶음 몇 개·박스)를 확인합니다.
   한정판·트레이딩카드처럼 정가보다 웃돈 시세로 거래되면 번개장터·크림 등 리셀 시세도 확인하고 resale=true로 둡니다.
2. 이 이름 안에 판매가가 크게 다른 변형(대표 판매가가 {SPLIT_PRICE_RATIO}배 이상 차이, 예: 2구/4구/LED형, 일반 박스/기념 박스)이
   섞여 있으면 split=true로 하고 변형마다 하나씩(최대 {MAX_VARIANTS}개, 실제로 따로 팔리는 것만) 적습니다.
   색상·디자인만 다르고 가격이 비슷한 것, 같은 상품을 낱개·박스처럼 판매 단위만 달리 파는 것은 나누지 않습니다
   (단위 차이는 basis·unit_price_krw로 적습니다). 나누지 않으면 variants에 1개만 (name은 원래 이름 그대로).
   나눌 변형이 아래 '이미 있는 품목'과 같은 상품이면 existing_id에 그 번호를 적습니다 (같은 품목을 두 번 만들지 않게).
3. est_retail_krw는 소비자가 실제로 사는 단위의 대표가(검색 결과 가격의 중간쯤)이고, basis에 그 단위를 적습니다.
   묶음·박스면 unit_price_krw에 개당 환산가를 적습니다. 할인 전 정가가 아니라 실제 판매가 기준입니다.
4. sources에는 검색 결과에서 실제로 본 판매 페이지 2~4개를 사이트·주소·가격·단위와 함께 적습니다. 못 찾으면 가격을 null로 두고, 지어내지 마세요.
{existing_block(existing)}
조사가 끝나면 submit_retail 도구로 제출하세요."""


def existing_block(existing):
    if not existing:
        return ""
    lines = [f"  #{i} {n} · " + (f"{p:,}원" if p else "-") for i, n, p in existing]
    return "\n이미 있는 품목 (번호 · 이름 · 국내 판매가):\n" + "\n".join(lines)


def tag_line(v, today, extra=""):
    src = ", ".join(f"{s['site']} {s['price_krw']:,}원/{s['unit']}" if s.get("price_krw") is not None
                    else f"{s['site']} 가격 미확인" for s in v.get("sources", [])[:4])
    price = f"{v['est_retail_krw']:,}원" if v.get("est_retail_krw") is not None else "미확인"
    unit = (f", 개당 {v['unit_price_krw']:,}원" if v.get("unit_price_krw") is not None
            and v.get("unit_price_krw") != v.get("est_retail_krw") else "")
    return (f"[{TAG} {today}] {v['basis']} 기준 {price}{unit} (범위 {v['price_range_krw']})"
            + (" · 리셀 시세" if v.get("resale") else "") + (f" · 출처: {src}" if src else "") + extra)


def with_tag(notes, line):
    """notes 맨 앞의 '[1번 시세 ...]' 줄만 바꾸고 사람이 적은 메모는 그대로 둔다."""
    kept = [x for x in (notes or "").splitlines() if not x.startswith(f"[{TAG} ")]
    return "\n".join([line, *kept]).strip()


def sources_text(v):
    return "\n".join(f"  - {s['site']}: {s.get('price_krw')}원/{s['unit']} {s.get('url') or ''}".rstrip()
                     for s in v.get("sources", []))


def apply(conn, row, data, run_id, today, log=print):
    """제출값을 DB에 반영. 반영 결과 dict를 돌려준다."""
    variants = data["variants"][:MAX_VARIANTS]
    pid = row["id"]
    if not data["split"] or len(variants) < 2:
        v = variants[0]
        trade_db.upsert_product(conn, row["name"], run_id, est_retail_krw=v.get("est_retail_krw"),
                                notes=with_tag(row["notes"], tag_line(v, today)))
        log(f"  [{pid}] {row['name']} → {tag_line(v, today)}")
        return {"id": pid, "split": False, "variant": v}
    made = []
    for v in variants:
        ex = v.get("existing_id")
        if ex and ex != pid and conn.execute("SELECT 1 FROM products WHERE id = ? AND stage != 'dropped'", (ex,)).fetchone():
            made.append((ex, conn.execute("SELECT name FROM products WHERE id = ?", (ex,)).fetchone()[0], v, True))
            continue                  # 이미 있는 품목 → 연결만 (그 품목은 건드리지 않음)
        name = " ".join(v["name"].split())[:100]
        if conn.execute("SELECT 1 FROM products WHERE name = ?", (name,)).fetchone():
            name = f"{name} (#{pid}에서 나눔)"
            if conn.execute("SELECT 1 FROM products WHERE name = ?", (name,)).fetchone():
                log(f"    · 이미 있는 이름이라 건너뜀: {name}")
                continue
        new_id = trade_db.upsert_product(
            conn, name, run_id, origin="discover", category=row["category"],
            search_keyword_en=v.get("search_keyword_en") or row["search_keyword_en"],
            keywords_ko_json=row["keywords_ko_json"],
            reason=f"#{pid} '{row['name']}'에서 판매가가 다른 변형으로 나눔. {data['reason']}\n(원래 근거) {row['reason'] or ''}",
            regulations=row["regulations"], risk=row["risk"], est_retail_krw=v.get("est_retail_krw"),
            source_country=row["source_country"], source_country_reason=row["source_country_reason"],
            notes=tag_line(v, today, f" · #{pid}에서 나눈 변형"))
        if row["score_id"]:   # 대시보드 후보 목록에 보이게 원래 품목 점수를 복사 (마진은 이 변형 판매가로 다시 봐야 함)
            conn.execute("""INSERT INTO product_scores (product_id, run_id, demand, margin, competition, regulation, logistics,
                    total, score_notes, est_fob_usd, naver_json)
                SELECT ?, ?, demand, margin, competition, regulation, logistics, total,
                       ? || COALESCE(score_notes, ''), est_fob_usd, naver_json FROM product_scores WHERE id = ?""",
                         (new_id, run_id, f"[#{pid}에서 나눈 변형 — 점수는 원래 품목 평가 그대로, 마진은 이 변형 판매가 기준으로 다시 봐야 함] ",
                          row["score_id"]))
        made.append((new_id, name, v, False))
    if not made:
        return {"id": pid, "split": True, "made": []}
    summary = ", ".join((f"#{i} {v['basis']} {v['est_retail_krw']:,}원" if v.get("est_retail_krw") is not None
                         else f"#{i} 미확인") + (" (기존 품목)" if old else "") for i, _, v, old in made)
    line = f"[{TAG} {today}] 판매가가 다른 변형이 섞여 있어 나눔 → {summary} · 이 품목의 판매가는 변형 혼합 (단계·공급처는 그대로)"
    trade_db.upsert_product(conn, row["name"], run_id, notes=with_tag(row["notes"], line))
    conn.execute("INSERT INTO product_events (product_id, run_id, from_stage, to_stage, note) VALUES (?, ?, ?, ?, ?)",
                 (pid, run_id, row["stage"], row["stage"], "1번: 변형별로 나눔 → " + ", ".join(f"#{i}" for i, _, _, _ in made)))
    log(f"  [{pid}] {row['name']} → 나눔: " + " / ".join(f"#{i} {n}" + (" (기존)" if old else "") for i, n, _, old in made))
    return {"id": pid, "split": True,
            "made": [{"id": i, "name": n, "variant": v, "existing": old} for i, n, v, old in made]}


def run(backend, conn, run, ids=(), out="reports", log=print, only_ids=False):
    from datetime import date
    today = date.today().isoformat()
    rows = targets(conn, ids, with_active=not only_ids)
    if not rows:
        log("다시 조사할 품목이 없습니다.")
        return []
    log(f"▶ 국내 판매가 다시 조사: {len(rows)}개 품목 (" + ", ".join(f"#{r['id']}" for r in rows) + ")")
    results, raw = [], []
    for row in rows:
        log(f"  ▷ #{row['id']} {row['name']}")
        try:
            data = sa.run_agent(backend, prompt(row, today, supplier_hints(conn, row["id"]), others(conn, row["id"])),
                                REPRICE_TOOL, log=log)
        except Exception as e:   # 한 품목 실패로 나머지를 멈추지 않음
            log(f"    ⚠️ 실패: {e}")
            raw.append({"id": row["id"], "name": row["name"], "error": str(e)})
            continue
        with conn:
            res = apply(conn, row, data, run.id, today, log)
        results.append(res)
        raw.append({"id": row["id"], "name": row["name"], "submitted": data, "applied": res})
    outdir = sa.new_outdir(out, "_reprice")
    outdir.mkdir(parents=True, exist_ok=True)
    run.report_dir = str(outdir.resolve())
    (outdir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# 국내 판매가 다시 조사 ({today})", ""]
    for r in raw:
        if "error" in r:
            md += [f"## #{r['id']} {r['name']}", f"실패: {r['error']}", ""]
            continue
        d = r["submitted"]
        md += [f"## #{r['id']} {r['name']}" + (" → 나눔" if r["applied"]["split"] else ""), d["reason"], ""]
        for v in d["variants"]:
            md += [f"- **{v['name']}** · {v['basis']} {v['est_retail_krw']}원 (범위 {v['price_range_krw']})"
                   + (" · 리셀" if v["resale"] else ""), sources_text(v)]
        md.append("")
    (outdir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    log(f"  · 결과: {outdir / 'summary.md'}")
    if not results:   # 전부 실패면 실행 기록도 실패로
        raise RuntimeError(f"{len(rows)}개 품목 모두 조사 실패 (자세한 내용: {outdir / 'raw.json'})")
    return results
