# 시장조사·아이템 발굴 에이전트 (1번)

1인 수입 무역 파이프라인(시장조사 → 공급처 발굴 → 협상 → 서류 → 통관 → 원가 → 판매 → 재무) 중
**1번 시장조사·아이템 발굴**만 맡습니다. 국내외 트렌드·검색량·경쟁 가격·리뷰를 조사해
"수요는 있는데 공급(경쟁)이 적은" 수입 후보 품목을 찾아 점수표로 만듭니다.

결과는 공용 DB(**[trade_hub](../trade_hub)**)에 후보 품목으로 기록되고, 다음 단계 에이전트와 슈퍼바이저가 읽습니다.
공급처 섭외·견적·통관·원가 계산은 다른 에이전트의 일이라 하지 않습니다.

| 파일 | 역할 |
|---|---|
| `supplier_agent.py` | 후보 품목 조사·평가 → 점수표 |
| `discover_store.py` | 결과를 공용 DB에 저장 (+ 예전 리포트 가져오기) |

## 1. 설치

```bash
pip install -r requirements.txt     # anthropic + ../trade_hub (공용 DB 패키지)
```

`../trade_hub` 폴더가 있어야 합니다. 프로젝트 폴더의 `.env`에 키를 넣습니다 (git에 올라가지 않음).

```
ANTHROPIC_API_KEY=sk-ant-...              # Claude
ANTHROPIC_EFFORT=high                     # (선택) 조사 깊이: low/medium/high/xhigh/max
OPENROUTER_API_KEY=sk-or-...              # (선택) OpenRouter로 DeepSeek 등 사용
OPENROUTER_MODEL=deepseek/deepseek-v4-pro # 모델 ID
PROVIDER=anthropic                        # (선택) 기본 접속처: anthropic 또는 openrouter
NAVER_CLIENT_ID=...                       # (선택) NAVER API HUB 검색어 트렌드
NAVER_CLIENT_SECRET=...
```

각 접속처에는 그 서비스의 키만 전송됩니다. 환경변수로 직접 설정해도 되며, 환경변수가 `.env`보다 우선합니다.

**네이버 검색량(선택)**: 네이버 클라우드 플랫폼 콘솔 → All Services > Application Services > **NAVER API HUB** →
Application 등록 후 **검색어 트렌드** 구독, 그 애플리케이션의 Client ID / Secret을 넣습니다.
([문서](https://api.ncloud-docs.com/docs/naver-api-hub-search-trend))

## 2. 1번 에이전트: 후보 품목 발굴

```bash
python supplier_agent.py                                        # 후보 10개
python supplier_agent.py --hint "주방·수납, 1만~3만원대, 겨울 시즌"  # 방향 지정
python supplier_agent.py --count 5 --provider openrouter
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--hint` | 방향 (카테고리·가격대·시즌 등) | 없음 |
| `--count` | 후보 품목 수 | 10 |
| `--provider` | `anthropic`(Claude) 또는 `openrouter`(DeepSeek 등) | `.env`의 PROVIDER, 없으면 anthropic |
| `--model` | 모델 | anthropic: claude-sonnet-5-5 / openrouter: `.env`의 OPENROUTER_MODEL |
| `--no-open` | 끝난 뒤 브라우저를 열지 않음 (정기 실행용) | 꺼짐 |

동작:
1. 모델이 웹 검색으로 후보를 찾아 수요(30%)·마진 여지(25%)·경쟁 여유(20%)·규제 용이(15%)·물류 용이(10%)를 1~5점으로 평가
2. 네이버 키가 있으면 후보별 검색량지수('텀블러'=100)·직전 3개월/전년 대비 성장률을 조회해 모델이 점수를 재검토
   (검색량이 거의 없는 후보는 교체)
3. 총점(100점 만점) 순으로 정렬해 `reports/날짜_시간_discover/`에 `candidates.html`·`candidates.csv`·`raw.json` 저장
4. 공용 DB(trade_hub)에 후보 품목(`stage=candidate`)과 점수를 기록 (점수표의 **번호** = DB 품목 번호)

점수는 모델의 판단이고 판매가·FOB 시세는 웹 검색 기준 참고값입니다. 검색량은 판매량이 아닙니다.

### 다음 단계로 넘기기

진행할 품목은 사람이 골라 공급처 발굴 단계로 넘깁니다 (trade_hub 명령). 2번 에이전트는 `stage=sourcing`인 품목을 처리합니다.

```bash
trade_hub candidates          # 후보 목록 (번호·최신 점수)
trade_hub select 3 7 12       # 3·7·12번을 sourcing 단계로
trade_hub drop 5 "검색량 부족" # 중단 (사유 기록)
python discover_store.py import-reports   # 예전 reports/*_discover 결과를 DB로 (이미 있는 건 건너뜀)
```

## 3. 정기 실행

슈퍼바이저(trade_hub)가 `supervisor.json` 설정에 따라 주 1회 실행합니다. 직접 등록할 필요는 없습니다.

## 4. 주의

- 후보 정보는 웹에서 찾은 것이라 직접 확인하세요. 규제 항목은 1차 참고이며 최종 확인은 5번(통관·규제) 단계에서 합니다.
- 1회 실행(후보 10개) 시 웹 검색 15회 안팎과 API 토큰 비용이 발생합니다.
  OpenRouter 웹 검색은 검색 1회당 약 $0.007이 별도로 부과됩니다.
