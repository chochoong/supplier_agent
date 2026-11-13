# 해외 공급처 발굴 에이전트

품목을 입력하면 Claude가 Alibaba, 1688, Global Sources 등을 웹 검색해 공급처 후보를 찾고,
원가·마진을 계산해 리포트와 엑셀용 표로 저장합니다. 메일 발송이나 결제는 하지 않고 **조사·정리만** 합니다.

## 1. 설치

```bash
pip install -r requirements.txt
```

API 키는 https://console.anthropic.com 에서 발급받아, 프로젝트 폴더의 `.env` 파일에 넣습니다.

```
ANTHROPIC_API_KEY=sk-ant-...
```

DeepSeek 등 다른 모델은 OpenRouter(https://openrouter.ai)를 통해 쓸 수 있습니다. 두 키를 함께 넣어 두고 실행할 때 고르면 됩니다.

```
ANTHROPIC_API_KEY=sk-ant-...              # Claude용
ANTHROPIC_EFFORT=high                     # (선택) Claude 조사 깊이: low/medium/high/xhigh/max
OPENROUTER_API_KEY=sk-or-...              # OpenRouter용
OPENROUTER_MODEL=deepseek/deepseek-v4-pro # 모델 ID (화면 표시명 "DeepSeek V4 Pro"가 아님)
PROVIDER=anthropic                        # (선택) 기본 접속처: anthropic 또는 openrouter
```

각 접속처에는 그 서비스의 키만 전송됩니다.

`.env`는 키가 들어 있으므로 공유하거나 git에 올리지 마세요(`.gitignore`에 등록되어 있음).
환경변수로 직접 설정해도 되며, 이 경우 환경변수가 `.env`보다 우선합니다.

```bash
# macOS / Linux
export ANTHROPIC_API_KEY="sk-ant-..."

# Windows (PowerShell)
$env:ANTHROPIC_API_KEY="sk-ant-..."
```

## 2. 사용법

```bash
# 품목이 정해졌을 때
python supplier_agent.py "실리콘 주방용품"
python supplier_agent.py "LED 무드등" "요가매트"

# 품목이 미정일 때: 에이전트가 트렌드를 조사해 유망 품목 3개를 고르고 각각 공급처 조사
python supplier_agent.py --auto

# OpenRouter로 DeepSeek 사용
python supplier_agent.py "요가매트" --provider openrouter
python supplier_agent.py "요가매트" --provider openrouter --model deepseek/deepseek-v4-flash

# 옵션
python supplier_agent.py --auto --count 2 --suppliers 4 --usd-krw 1385 --fee 0.12
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--auto` | 유망 품목 자동 선정 | 꺼짐 |
| `--count` | 자동 선정 품목 수 | 3 |
| `--suppliers` | 품목당 공급처 수 | 5 |
| `--usd-krw` | 원/달러 환율 (**실행 전 최신값 입력 권장**) | 1400 |
| `--fee` | 판매 플랫폼 수수료율 | 0.11 |
| `--provider` | `anthropic`(Claude) 또는 `openrouter`(DeepSeek 등) | `.env`의 PROVIDER, 없으면 anthropic |
| `--model` | 사용할 모델. Claude는 `claude-opus-5-5`로 바꾸면 더 깊게 조사(비용 증가) | anthropic: claude-sonnet-5-5 / openrouter: `.env`의 OPENROUTER_MODEL |

## 3. 결과물

`reports/날짜_시간/` 폴더에 생성되고, 실행이 끝나면 `report.html`이 브라우저에서 자동으로 열립니다.
(자동으로 열지 않으려면 `--no-open`)

- `report.html` — 브라우저용 리포트 (마진 색상 표시, ★ 먼저 연락할 곳, RFQ 이메일 복사 버튼)
- `report.md` — 품목별 공급처 비교표, 신뢰도 근거·위험 신호, HS코드·관세·인증, 추천 2곳, 영어 RFQ 이메일 초안
- `suppliers.csv` — 엑셀에서 바로 열리는 공급처 표 (정렬·필터용)
- `raw.json` — 원본 데이터

## 4. 원가·마진 계산 방식

계산은 모델이 아니라 스크립트가 직접 합니다.

- 개당 원가 = (FOB 단가 + 개당 운임) × 환율 → 관세 → 부가세 10%
- 관세율을 확인하지 못하면 일반 8%로 가정하고, 운임을 확인하지 못하면 0원으로 계산합니다. 이 경우 리포트에 ⚠️ 표시, CSV `원가가정` 열에 내용이 기록됩니다.
- 마진율 = 판매가에서 판매 부가세와 플랫폼 수수료를 뺀 금액 대비 이익 (수입 부가세는 매입세액 공제 가정)

국내 통관 수수료, 국내 택배비, 인증 비용, 광고비는 빠져 있으니 실제 마진은 이보다 낮습니다.

## 5. 주의

- 공급처 정보는 웹에서 찾은 것이라 **반드시 직접 확인**하세요. 특히 '미확인' 항목과 위험 신호가 있는 업체.
- HS코드와 관세율은 후보일 뿐이므로 최종 판단은 관세사나 관세청 UNI-PASS에서 확인하세요.
- 1회 실행(품목 3개 기준) 시 웹 검색 수십 회와 API 토큰 비용이 발생합니다.
  OpenRouter 웹 검색은 토큰 비용과 별도로 검색 1회당 약 $0.007이 부과됩니다.

## 6. 정기 실행 (선택)

매주 월요일 오전 9시에 자동 실행하려면:

```bash
# macOS / Linux: crontab -e
0 9 * * 1 cd /경로/supplier-agent && /usr/bin/python3 supplier_agent.py --auto --no-open >> run.log 2>&1
```

Windows는 작업 스케줄러에서 `python supplier_agent.py --auto --no-open`을 등록하면 됩니다.
