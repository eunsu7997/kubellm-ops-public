# 통합 후속 실행 결과 — 2026-10-22

**현재 판정:** 제한된 실제 HTTP admission·토큰 예산·취소·증거 리포트 연결을 확인했다. 과부하 보호/성능 개선, 안정적인 모집단 P99, 완성된5모듈 통합 또는 자동 복구는 검증하지 않았다.

이 문서는 [P0 조사 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/INTEGRATION-P0-20261022.md)와 [첫 P1 비교 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/INTEGRATION-P1-20261022.md)의 후속이다. 기존 결과는 수정하지 않았다. 원격 후속6개 커밋은 문서 변경이며 실행 코드 기준선은14e2488, 추가 실험 코드는 현재 미커밋 작업으로 보존한다.

## 추가한 연결과 경계

1. **gpuq → 사전 검사:** 기존 `gpuq.report.summarize/team_stats`에 실제 k3s Kueue Workload 조회 결과를 전달했다. 완료6개·실행 중 GPU 배치0개를 확인했다. 다른 k3s GPU Pod도 별도 조회해 중복 점유를 차단한다. Kubernetes 변경 없이 조회만 수행한다. Kueue가 HTTP 요청을 스케줄링하는 것은 아니다.
2. **vLLM KV → admission:** 기존 컨테이너의 `/metrics`를 직접 읽어 `kv_cache_usage_perc`의0~1 fraction을 사용한다. 임계85%, 허용 age4초. 누락/NaN/범위 오류/오래된 값은 신규 수락 보류 후429. GPU VRAM 지표와 별개다. 높은 압력 분기는 CPU 테스트, 실제 GPU의 측정 표본은0이어서 압력 차단 효과는 미검증이다.
3. **HTTP → 토큰 예산:** 같은 trace에서 입력+출력 예약만의 예산을 비교했다. 긴 요청 상한은4로 느슨하게 해 총예산과 분리했다. 짧은42+16=58, 긴162+64=226 tokens, 고/저 priority 모두 두 길이를 사용한다.
4. **증거 → Copilot:** `IncidentInput/LogEntry`와 실제 Copilot validator를 연결했다. 결정론적 로컬 Markdown 리포트와 존재하지 않는 인용 ID negative control을 검증했다. 기존 Qwen Gateway로 실제 LLM 초안도 한 건 생성했으며 아래 실패를 보존했다. Ollama나 추가 GPU 모델은 시작하지 않았다.

`llm-slo-lab`의 CPU mock을 실제 GPU 관측 모듈로 취급하지 않았으며, `gpu-deploy-guard`의 배포 카나리도 새로 구현했다고 주장하지 않는다. 이 두 프로젝트와의 production 통합은 완료가 아니다.

## P1/P2 최신 실제 비교 — comparison-03

동일 RTX4060·Qwen·stock 이미지/revision/options, 기존 Gateway→vLLM 경로. 임시 어댑터만 localhost18023에 실행 후 종료했다. 모델/컨테이너/볼륨을 재생성하지 않았다. Window desktop의 다른 GPU 소비자는 격리하지 않았다.

8건 고정 trace를 정책별3회, 총96건 재생했다. 4건씩20ms 간격 burst, burst 간격2초. inflight2·대기열2·최대대기600ms·aging200ms·토큰 예산250. off는 정책 제한 없이 동일 유한 trace만 보냈다. 실행 전 plan.json에 예시 TTFT1초, 복구 기준과180초 hard wall을 기록했다. preflight chat/SSE는 warmup으로 제외했다.

| 정책 | 실제 성공/예정 | 429/예정 | 수락 후 실패 | 성공-only client P99(ms) | 출력 tokens/s | 최대 예약 tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| off | 24/24 | 0/24 | 0 | 91.2 | 49.72 | 568 |
| basic | 24/24 | 0/24 | 0 | 237.9 | 46.76 | 284 |
| token budget250 | 18/24 | 6/24 | 0 | 389.0 | 33.03 | 226 |
| token budget250 + direct KV | 18/24 | 6/24 | 0 | 359.5 | 32.95 | 226 |

**96건 처리 = 성공84 + queue_full 429거절12**이며 미전송/transport failure0이다. supervisor의 정상 terminal96을 모델 성공96이라고 해석하면 안 된다. 정책별 성공-only P99의 표본은 off/basic24, token18이고 각각의 phase는8건뿐이다. nearest rank P99는 사실상 최대값이다.

- 토큰 정책의 전체 예정 대비 성공률과 예시1초 attainment는75%. 실패 포함 P99는 `unbounded`, JSON seconds=null이다. 429를 성공 분모에서 빼지 않았다.
- client TTFT는 로컬 HTTP 전송부터 첫 content까지이며 어댑터 큐 대기를 포함한다. 서버 도착→content의 별도 phase별 P99도 `report-03/p2-metrics.json`에 보존했다.
- 출력 tokens/s는 phase completion usage 합계 / 해당 phase의 첫 예정 도착부터 drain 종료+50ms까지 관측시간을 합쳐 계산했다. 최대 처리량이나 GPU 용량이 아니다.
- 제한을 켜면 총예약량이568/284에서226으로 줄었지만 일부 요청이 거절되고 전체 성공률/TPS도 낮아졌다. off가 정상인 작은 부하이므로 메모리 보호·고우선 성능 개선·과부하 억제의 증명이 아니다. priority와 도착 순서의 영향도 남아 있다.
- dynamic KV의 실측은 정상/낮은 압력 경로만 확인했다. 높은 cache 압력으로 인한 실제 GPU 차단을 일부러 유발하지 않았다.
- GPU 센서 최고54°C·최소여유1153MiB. hard wall/온도80°C/여유512MiB/센서 오류/연속 전송실패3회의 기존 guard를 유지했다. 각 phase 종료 active/queued/reserved0.

## 실패한 실행도 보존

- **comparison-01:** budget300, 정책별3회·96/96 성공. 최대예약284로 토큰 상한이 실질적으로 작동하지 않아 정책 효과 근거로 부적합했다. 원본/report-01 보존.
- **comparison-02: FAIL.** budget250의 첫 off phase8건 뒤 Windows Proactor WinError10054와 어댑터 shutdown TimeoutError. 전체 계획96 중 terminal8·나머지88 미실행이다. 이것을96/96 성공으로 집계하지 않는다. production outage는 관측하지 않았고 복구 chat/SSE/stock은 정상. 원본 `FAILURE.json`, `supervisor/child.log` 보존.
- worker를 SelectorEventLoop로 바꾸고 어댑터 graceful shutdown3초·모니터 finally 정리를 명시한 뒤 **comparison-03을 별도 경로에서 실행, 정상 종료**했다. GPU 서버 설정은 변경하지 않았다. 이 결과가 Windows 네트워크 문제 전반의 해결 보장은 아니다.

## 취소·재시도

`cancel-01`에서 긴 합성 요청의 실제 SSE 첫 content 후 클라이언트 연결을 종료했다. active1/예약226이 약18.2ms 뒤 active0/예약0으로 관측됐다. 약410.3ms 뒤 vLLM running0·waiting0·KV fraction0을 직접 관측했다. 단일 표본의 관측 상한이며 엔진 내부 abort 시각·GPU VRAM deallocation 시간·일반 회수시간이 아니다. 취소된 요청을 완전한 SSE 성공으로 세지 않는다.

`bounded_retry.py`의 별도 CPU 계약 검사는429만 최대2 attempts, Retry-After를 줄이지 않는 backoff+jitter,2.5초 전체 예산, malformed header/5xx/accepted stream 재시도 금지를 검증했다. mock429→200과 영구429 모두 attempts≤2. 실제 SSE 비교는 자동 재시도0회이므로 최신96예정/96전송으로 증폭1.0배다. 실제429 요청을 재시도하는 streaming 클라이언트의 보호 효과는 미검증이다.

## P3 실제 LLM 실패와 채택 차단

`copilot-llm-01`은 comparison-01의 근거 요약을 실제 기존 Qwen에 전달했다. HTTP200·JSON schema는 통과했지만 로그 ID 대신 `bounded-comparison-20261022`라는 incident ID를 인용했고 unknowns도 빈 배열이었다. Copilot validator가 없는 인용과 불확실성 누락을 경고했다.

`acceptance.json`: **eligible_draft=false**, auto_actions_allowed=false. 원문을 수정하거나 실패를 지우지 않았고 재시도해서 성공처럼 고르지 않았다. 이 생성 결과는 비교03의 분석 결과로 소급 사용하지 않는다. 최신 comparison-03은 별도 결정론적 `report-03/REPORT.md`와 근거 ID 검사 결과를 사용한다. 존재하는 ID가 원인 설명의 정확성을 보증하는 것은 아니다.

최신 report는 원인 후보/근거/반대 증거/제안/미확정을 구분한다. Docker 로그는 해당 실행 시각 이후의 timestamp·method·허용 API path·status만 화이트리스트로 추출했다. 프롬프트·토큰·환경변수·인증값·개인 프로세스 목록은 리포트에 넣지 않는다. RAG·Slack·자동 조치는 미실행이다.

## 재현 명령

GPU 실험은 기존 stock 서버와 센서·Prometheus·gpuq 소스가 필요하고 중첩 작업을 거부한다. 출력 폴더는 반드시 새 경로를 사용한다.

```powershell
# CPU 계약 검사
.\.venv\Scripts\python.exe -m pytest tests/test_http_admission.py tests/test_admission_followup.py tests/test_integration_report.py -q
# 현재 고정 trace·budget250 비교 (명시적 실행)
.\.venv\Scripts\python.exe scripts/ops/compare_admission.py --execute --followup --output-dir evidence/fresh-comparison
# 실제 취소 한 건
.\.venv\Scripts\python.exe scripts/ops/probe_gpu_cancel.py --execute --output-dir evidence/fresh-cancel
# GPU 요청 없는 지표/증거 리포트, 실제 기존 Copilot validator 사용
.\.venv\Scripts\python.exe scripts/ops/integration_report.py evidence/fresh-comparison --copilot-repo <LOCAL_USER>\Documents\incident-copilot --output-dir evidence/fresh-report
```

읽기용 gpuq 소스는 확인된 `https://github.com/eunsu7997/gpu-queue-lab`의 `a0e04f48f795602c84310b13d4079b0f2613cd94`를 `.runtime/vendor/gpu-queue-lab`에 checkout한 사본이다. 다른 로컬 저장소는 변경하지 않았다.

## 완료와 남은 범위

- **확인:** 실제 예산 제한/429, 실패 포함 지표, 직접 KV 정상 경로·stale/pressure CPU 분기, bounded retry CPU 계약, 실제 취소 후 관측 idle, gpuq 조회→사전 검사, 실제 Copilot 입력/검증기→리포트·잘못된 초안 차단.
- **아직 미검증:** 진짜 과부하에서의 보호 효과, 높은 KV pressure 실제 차단, 대규모/장기 반복의 안정적 P99·SLO, 실제 streaming 재시도 증폭 제어, 다른 모델 비용 추정 오차, 엔진 내부 회수시간, 실장애 감지→조치→복구시간/MTTR, 정확한 생성형 Copilot/RAG, production5모듈 전체 연동.
- **P4:** 추가 GPU와 scale-out 실행 환경이 없다. 단일 물리 GPU의 공유 슬롯/Pod 증가를 용량 증가라고 보고하지 않는다. 사용자 제한에 따라 Kubernetes GPU 실험을 하지 않았으며 KEDA/HPA/Replica 변경도 하지 않았다. 실제 throughput scale-out은 현재 환경에서 완료할 수 없다.
- 기존 모델/데이터/컨테이너/TSDB·과거 증거 보존. 공유기 포트 개방·Funnel·Tailscale 변경·Slack 전송 없음. 자동 적용 없이 localhost 실험만 종료했다.

실제 원본과 품질 로그는 `evidence/integration-followup-20261022/`에 있으며, 최신 리포트는 `report-03/REPORT.md`다.

최종 Selector loop 수정 후 전체 검사는 **302 passed·1 skipped·234 warnings**, Ruff 및 diff 검사 통과. 최종 readiness200·chat200·SSE DONE·stock identity 확인, 기존6개 컨테이너 실행 유지, 임시18023 listener와 실험 lease 없음. 최종GPU 표본61°C·여유1159MiB는 실험 종료 후 데스크톱과 공유하는 상태이며 위 부하 구간 최고54°C와 별개다.
