# 5개 프로젝트 통합 실행 — 실측과 남은 범위

RTX 4060 한 장에서 기존 Qwen vLLM 서빙을 보존하고 배포 전 검사·batch 점유 조회·실제 SSE·SLO 판정·제한 재시도·Copilot 검증을 한 실행으로 연결했다. 이는 5개 프로젝트의 일부 실제 코드를 호출하는 **로컬 통합 실험**이다. 5개 production 서비스를 배포하거나 다중 GPU 확장을 달성한 것은 아니다.

## 실제로 연결한 계약

| 프로젝트 | 이번 실행에서 호출한 코드와 역할 |
| --- | --- |
| gpu-deploy-guard | 기존 Deployment를 단일 문서로 추출해 GPU/CPU/메모리/probe 정적 검사. 실패하면 부하 전에 중단 |
| gpu-queue-lab | 실제 Kueue Workload 조회 결과를 gpuq.report로 요약. 활성 GPU batch나 조회 불명이면 중단. HTTP 토큰 큐가 아님 |
| kubellm-ops | 기존 Gateway → vLLM Qwen의 실제 content+DONE, 별도 localhost admission 어댑터, 안전 하네스 |
| llm-slo-lab | 검토·고정한 perf/metrics.py 순수 지표 함수를 실제 SSE 결과에 적용. 전체 프로젝트 QA 통과를 주장하지 않음 |
| incident-copilot | IncidentInput/LogEntry와 validator에 같은 실행 근거 및 Qwen 초안을 전달. 초안의 문장을 실행하지 않음 |

## 최근 통합 실행

실행 ID `d93e1bff87184b5e81a82f6be0915ee4`. [선별·비식별한 SUMMARY](../evidence/public-integration-20261023/latest/SUMMARY.json).

| 지표 | 기준 token-budget | 명시적 429만 제한 재시도 |
| --- | ---: | ---: |
| 예정 logical 요청 | 40 | 40 |
| 최종 성공 | 30 | 40 |
| 최종 오류율 | 25% | 0% |
| 성공 요청 전체 SSE E2E P99 | 약 0.653초 | 약 1.343초 |
| 예시 목표 판정 | FAIL | PASS |

예시 목표는 실행 전에 고정한 전체 SSE E2E P99≤2.5초·최종 오류율0%다. 재시도 대기시간도 포함한다. 기준 지연이 더 짧은 것은 실패한 요청의 지연을 성공-only 지연에서 제외하기 때문이며, 오류율을 함께 봐야 한다. 40개 표본은 작고 정책은 순차 실행했으며 배경 desktop GPU 사용은 격리하지 않았다. 보편적인 개선 효과나 운영 SLO 보장이 아니다.

재시도는 수락 전 명시적 429만 최대 2번 시도한다. Retry-After를 지키고 jitter와 logical 2.5초 기한을 적용한다. 수락된 200·부분 SSE·전송 오류·5xx는 자동 재생하지 않는다.

Copilot 초안은 unknowns 조건 미달로 채택하지 않았다. 근거 리포트를 대체 결과로 사용했다. 전체 상태는 `COMPLETED_WITH_LIMITATIONS`다. scale preview는 실제 지표 입력과 합성 capacity 분기를 구분한 오프라인 계산이며 KEDA/HPA 적용이나 replica 변경은 없었다.

## 더 긴 반복 비교

[20회 phase × 정책별 160개 결과](../evidence/public-integration-20261023/repeat-320/p2-metrics.json): 재시도 없음 120/160 성공, 제한 재시도 160/160 성공. HTTP 시도는 160→200으로 1.25배 증가했다. 재시도 포함 성공 TTFT P99는 약 1.09초이며 예시 1초 TTFT 달성률은 두 정책 모두 75%였다. 성공률 개선을 모든 지연 지표 개선으로 확대하지 않는다. 전체 SSE E2E와 TTFT는 다른 지표다.

## 이전 실험과의 관계

Docker/WSL k3s GPU 추론·복구의 이전 검증은 [야간 보고서](OVERNIGHT-REPORT-20261022.md)에 보존했다. 120분 관측의 strict FAIL, readiness 503 원인 PARTIAL은 이번 짧은 시연 성공으로 덮지 않는다. 이번 라운드는 Kubernetes GPU 실험을 실행하지 않았다.

## 남은 항목

- 장시간 안정성, 다른 trace와 더 큰 표본, 실제 GPU 과부하 보호 및 최대 용량
- 다른 GPU 호스트의 깨끗한 설치와 전체 재현, 다중 GPU 확장 처리량
- Copilot 생성 품질 개선. 참조 ID 존재 확인은 의미적 진실성을 보장하지 않음
- 학원 PC에서 개인 tailnet 접속 검증. 집 PC HTTPS 검증만 완료
- 공개본은 과거 이력과 사설 환경 식별자를 제외한 선별본이며 비공개 원본을 보존

## 증거 보존

[선별 증거와 원본/파생본 해시](../evidence/public-integration-20261023/EXPORT-MANIFEST.json)는 개인정보 식별 문자열 일부를 비식별한 파생본이다. 계정 이메일·실제 Tailscale 주소는 게시하지 않았다. 원본은 집 PC에 보존했고, 원본과 파생본의 hash를 구분한다. 이미지·기존 Git 이력은 별도 공개 점검 대상이다. 전체 원시 로그를 공개 안전성 확인 없이 새로 올리지 않는다.
