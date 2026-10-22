# KubeLLM-Ops

RTX 4060 한 장에서 실제 LLM 서빙·관측·장애 복구를 검증하고, **배포 전 검사 → GPU batch 점유 확인 → 실제 SSE → SLO 판정 → 제한 재시도 → Copilot 검증**을 연결한 AI 인프라 포트폴리오입니다.

[최신 통합 결과·범위](docs/PORTFOLIO-20261023.md) · [실행·채팅·발표 안내](docs/DEMO-TOMORROW.md) · [선별 실행 증거](evidence/public-integration-20261023/README.md) · [GitHub CI](https://github.com/eunsu7997/kubellm-ops-public/actions)

## 최근 실제 실행

Qwen2.5-0.5B-Instruct / 기존 vLLM / RTX 4060. 실행 ID `d93e1bff87184b5e81a82f6be0915ee4`.

| 지표 | 기준 정책 | 명시적429 제한 재시도 |
| --- | ---: | ---: |
| 논리 요청 | 40 | 40 |
| 최종 완료 | 30 | 40 |
| 최종 오류율 | 25% | 0% |
| 성공 요청 전체 SSE E2E P99 | 약0.653초 | 약1.343초 |
| 실행 전에 고정한 예시 목표 | FAIL | PASS |

재시도 대기를 포함한 E2E입니다. 기준 지연의 성공-only 수치가 더 짧다고 기준 정책이 우월한 것은 아닙니다. 오류율도 함께 판정합니다.
단일 GPU·작은 표본·순차 정책 실행의 관측이며 운영 SLO나 보편적인 성능 개선 보장이 아닙니다.
20회 반복의 별도320요청 비교에서는 성공120/160→160/160, HTTP 시도1.25배, 예시1초 TTFT 달성률은 두 정책 모두75%였습니다.

## 연결한 5개 프로젝트

| 프로젝트 | 실제 역할 |
| --- | --- |
| gpu-deploy-guard | 기존 Deployment의 자원·probe 정적 검사. 실패하면 추론 전 차단 |
| gpu-queue-lab | 실제 Kueue batch 점유 조회·요약. HTTP 토큰 큐와 구분 |
| kubellm-ops | Gateway·vLLM 실제 GPU SSE, 별도 localhost admission·안전 하네스 |
| llm-slo-lab | 고정 upstream 지표 함수로 실제 SSE E2E·오류율 판정 |
| incident-copilot | 같은 실행의 근거·런북·Qwen 초안의 schema·인용·불확실성 검사 |

5개 프로젝트 일부 코드의 로컬 통합 실험입니다. 5개 production 서비스 배포나 자동 GPU 확장 성과가 아닙니다.
Copilot 초안은 조건 미달로 채택하지 않았고 근거 리포트를 대체 결과로 사용했습니다. AI 문장은 명령으로 실행하지 않습니다.
scale preview는 오프라인 계산뿐이며 Kubernetes GPU 실험·KEDA/HPA 적용·실제 scale-out은 이번에 실행하지 않았습니다.

## 사용하기

Python 개발 환경:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

GPU 없이 사용하는 CPU echo Mock은 기존 Compose 환경으로 API 연결·오류·메트릭을 시험합니다. 실제 모델 대화와 구분합니다.

```powershell
docker compose up -d --build
```

실제 GPU 모델은 [고정 Docker 보안 구성과 복구 절차](docs/docker-gpu-security-migration.md)를 따릅니다.
기존 모델 서버를 준비한 집 PC에서:

```powershell
powershell -ExecutionPolicy Bypass -File .\START-CHAT.ps1
powershell -ExecutionPolicy Bypass -File .\START-DEMO.ps1
```

채팅 화면은 `http://127.0.0.1:18004/`입니다. GPT가 아닌 로컬 Qwen 모델이며 작은 모델의 답변은 부정확할 수 있습니다.
통합 실행에는 별도 프로젝트 checkout과 기존 Docker/WSL 조회 환경이 필요합니다. 설치·구성 자동화나 새 GPU 호스트 전체 재현 완료를 뜻하지 않습니다.
Gateway의 정상 확인은 `/readyz`와 실제 `/v1/chat/completions`입니다. `/`와 `/v1/models`의404를 장애 기준으로 쓰지 않습니다.

## 기존 검증과 남은 항목

- [Docker/WSL k3s GPU·복구 검증](docs/KUBERNETES-LIVE-VALIDATION.md): 과거 각 실험의 실제 기록. 현재 Kubernetes 상시 서빙을 뜻하지 않음
- [야간 관측](docs/OVERNIGHT-REPORT-20261022.md): 120분 strict FAIL·readiness503 원인 PARTIAL을 보존
- [안전 하네스](docs/SAFETY-HARNESS-CLOSURE.md): 온도·여유 VRAM·지표 오류·시간 제한·소유 자식 프로세스 정리
- [실험용 SLO 평가기](slo-guard/README.md): 실패·미전송을 포함하는 분모. TTFT와 전체 SSE E2E를 구분
- 남음: 장시간 안정성, 다른 부하·호스트, 최대 용량·GPU 과부하 보호, 다중 GPU, production readiness
- 개인정보 보호: 새 계정 이메일·실제 사설 접속 주소는 게시하지 않음. [공개 전 점검](docs/PUBLICATION-REVIEW-20261023.md)에서 현재 파일과 과거 Git 이력의 공개 범위를 구분

과거 원문과 이력은 보존합니다. H100 가상 비교 수치와 설계상의 기능을 실측 성과로 사용하지 않습니다.
