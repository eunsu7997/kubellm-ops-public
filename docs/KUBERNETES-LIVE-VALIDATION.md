# KubeLLM-Ops — 실제 WSL k3s GPU 검증

기준일: 2026-10-21. 이 문서는 실제 실행 범위와 남은 한계를 묶는 검토용 인덱스다.
Docker Desktop Kubernetes와 GPUDeploy Guard kind는 이번 GPU 검증 대상이 아니다.

후속 최종 보완·검수 보고서 (비공개 원본 `docs/FINAL-REVIEW.md`)는 선택적 Gateway readiness와 Docker 반복 측정을 다룬다.
이 문서의 114개 테스트·36건 Kubernetes 부하와 후속 129개 테스트·216건 Docker 부하를 구분한다.

## 핵심 결과

기존 Ubuntu-24.04 WSL k3s에서 RTX 4060 CUDA Pod뿐 아니라 **Qwen vLLM 모델 로드와
Kubernetes Gateway → Service DNS → 실제 vLLM 채팅·SSE**까지 확인했다.
Prometheus는 Gateway/vLLM/DCGM 세 target을 수집하고 Grafana 검증용 복사본에서
실제 TTFT·TPOT·처리량 및 GPU 그래프를 표시했다. 기본 대시보드는 수정하지 않았다.

이는 한 PC의 공유 GPU 개발 환경 검증이며 모든 WSL/GPU/Kubernetes 조합의 공식 지원,
독점 GPU 격리, 최대 처리량, production readiness를 증명하지 않는다.

## 실제 환경과 고정한 입력

| 항목 | 실제 확인 값 |
|---|---|
| GPU | NVIDIA GeForce RTX 4060, 8188MiB |
| Cluster | 기존 Ubuntu-24.04 WSL k3s v1.36.4+k3s1 |
| Node / runtime | `<PRIVATE_HOST>`, containerd 2.3.4-k3s1.36, NVIDIA RuntimeClass |
| Device Plugin | 기존 v0.17.1, time-slicing replicas=4 |
| GPU capacity/allocatable | 4 공유 슬롯. 물리 GPU는 1장, VRAM 격리 없음 |
| vLLM | 0.30.0, image digest `sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90` |
| Model | `Qwen/Qwen2.5-0.5B-Instruct` |
| Model revision | `7ae557604adf67be50417f59c2c2f167def9a775` |
| Weights SHA256 | `fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe` |
| vLLM options | GPU memory utilization 0.4, max model length 2048, replica 1 |
| Namespace | `kubellm-gpu-validation` — 다른 프로젝트와 분리 |

이미지는 Docker의 기존 파일을 k3s로 복사했다. CRI가 표시한 import index는
`f415ab29...`이며, 그 자식이 원래 `8a69ffad...` manifest다. 다른 모델이나 이미지로
바꾼 것으로 오해하지 않도록 실제 manifest 참조 관계 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/50-image-manifest-chain.txt`)를 보존했다.
모델 캐시 중 가중치가 공용 blob으로 연결돼 있어 모델 디렉터리와 해당 blob 하나만 복사했다.
인증 파일이나 전체 Hugging Face 캐시는 복사하지 않았다.

## 증거로 확인할 순서

증거 루트: evidence/kubernetes-vllm-live/20261021 (비공개 원본 `evidence/kubernetes-vllm-live/20261021`).

| 검증 | 실제 근거 |
|---|---|
| GPU Pod CUDA 연산·종료 0 | CUDA smoke (비공개 원본 `evidence/kubernetes-cuda-smoke-20261021/summary.md`) |
| Docker vLLM 중지 후 k3s 한 개 실행 | 중지 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/23-docker-vllm-stop.txt`), apply (비공개 원본 `evidence/kubernetes-vllm-live/20261021/24-vllm-apply.txt`) |
| Qwen 가중치 로드·엔진 시작 | 모델 로그 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/28-vllm-model-load-log.txt`), rollout (비공개 원본 `evidence/kubernetes-vllm-live/20261021/26-vllm-rollout.txt`) |
| Probe 구성·Ready·Service endpoints | describe (비공개 원본 `evidence/kubernetes-vllm-live/20261021/31-vllm-probes.txt`) |
| Gateway 실제 채팅 HTTP 200·assistant 응답 | E2E (비공개 원본 `evidence/kubernetes-vllm-live/20261021/30-initial-gateway-e2e.json`) |
| 실제 SSE·20 content chunks·DONE | SSE 원문 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/37-streaming-response.txt`), 요약 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/38-streaming-summary.json`) |
| Prometheus 세 target UP·Grafana 쿼리 | targets (비공개 원본 `evidence/kubernetes-vllm-live/20261021/metrics-baseline-retry/targets.json`), 쿼리 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/metrics-after-load/panel-queries.json`) |
| DCGM GPU 사용률·VRAM·온도 실제 값 | raw metrics (비공개 원본 `evidence/kubernetes-vllm-live/20261021/33-dcgm-raw-metrics.txt`) |
| GPU 5슬롯 요청 → Pending / Insufficient | scheduler 원문 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/20-gpu-shortage-describe.txt`), 임시 Pod 정리 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/21-gpu-shortage-cleanup.txt`) |
| 없는 offline 모델 → 실패·원래 args 복구·Ready | 실패 로그 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/model-failure/03-failed-logs.txt`), 복구 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/model-failure/result.json`) |
| backend 실패와 Gateway readiness의 차이 | readiness 200 / 채팅 502 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/43-during-model-fault.json`) |
| 실제 API liveness 재시작·Ready 복귀 | 이벤트 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/api-liveness-recovery/04-events.txt`), 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/api-liveness-recovery/result.json`), 복구 후 채팅 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/51-after-liveness-chat.json`) |
| vLLM Pod 삭제·새 Pod Ready·실제 추론 | 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/pod-delete-recovery/result.json`), 복구 후 채팅 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/55-after-pod-delete-chat.json`) |
| Gateway 저장소/이미지 소스 일치 | 줄바꿈만 정규화한 SHA256 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/57-gateway-source-match.json`) |
| 원래 Docker 경로 복구 | GPU 해제 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/60-trial-gpu-release.txt`), 최종 채팅·관측 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/64-restored-docker-e2e.json`) |

API 정지 후 첫 Ready 복귀 관측은 약 210.3초, Pod 삭제 후 새 Pod의 첫 Ready는 약
83.5초였다. 각각 한 번의 3초 간격 관측이며 무중단·평균 복구 시간·보장값이 아니다.
과거 Mock Gateway 약 9초 복구와 GPU 모델 복구를 혼동하지 않는다.

## 요청한 12개 작업의 판정

| 작업 | 판정과 경계 |
|---|---|
| 1. 전환 사전점검 | 실행: 기존 GPU/캐시/다른 workload 확인 및 복구 지점 확보 |
| 2. Kubernetes vLLM | VERIFIED: 고정 이미지·Qwen revision·가중치 검증 후 실제 로드 |
| 3. API·Probe | VERIFIED: HTTP 응답, startup 대기, API 정지에 대한 readiness/liveness·재시작 |
| 4. Gateway E2E | VERIFIED: 실제 채팅·SSE·복구 후 응답 |
| 5. 메트릭·Grafana | VERIFIED: 실제 수집/쿼리/검증용 화면. 기본 dashboard 전체 수정 아님 |
| 6. GPU 관측 범위 | PARTIAL: DCGM 사용률·VRAM·온도 VERIFIED, 전력은 WSL N/A·series 없음 |
| 7. 장애 스크립트 | 원래 args 복구·UID 보호·dry-run 기본값·안전 재개 및 오프라인 회귀 검사 추가 |
| 8. 실제 장애·복구 | VERIFIED: 슬롯 부족·없는 offline 모델·API liveness·Pod 삭제. 엔진 정지 미검출도 보존 |
| 9. 제한된 부하 | VERIFIED: 36/36 성공. 최대 용량·실제 GPU 포화/OOM은 NOT VERIFIED |
| 10. 재현·복구 | 같은 호스트/캐시/runtime 전제의 절차와 실제 원래 Docker 경로 복구 확인 |
| 11. 문서·포트폴리오 | 최신 결과 인덱스·한계·아키텍처·면접 설명을 구분. Notion에는 최종 commit/CI 확인 후 반영 |
| 12. GitHub 보존 | 비밀정보·diff·회귀 검사 후 master에 일반 commit/push. 원격 CI는 Actions 결과로 별도 확인 |

12개 흐름 모두 다뤘다는 사실을 모든 하위 기능이 VERIFIED라는 뜻으로 쓰지 않는다.
전력·엔진 정지 자동 감지·포화 성능과 fresh-host 설치 재현은 해결 또는 검증되지 않았다.

## 실제 화면

실제 Kubernetes vLLM TTFT·TPOT (비공개 원본 `evidence/kubernetes-vllm-live/20261021/grafana-vllm-ttft-tpot.png`)

GPU 사용률·메모리 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/grafana-dcgm-panels.png`) ·
온도와 전력 No data (비공개 원본 `evidence/kubernetes-vllm-live/20261021/grafana-temperature-power-boundary.png`).
화면은 실제 검증용 복사본이다. 숫자를 합성하거나 No data를 지우지 않았다.

## 메트릭 해석

실제로 노출된 vLLM 이름을 그대로 사용한다.

- TTFT: `vllm:time_to_first_token_seconds_*`
- TPOT: `vllm:request_time_per_output_token_seconds_*`
- 생성 토큰: `vllm:generation_tokens_total`
- 대기·실행 요청: `vllm:num_requests_waiting`, `vllm:num_requests_running`
- KV cache: `vllm:kv_cache_usage_perc`

부하 후 raw metrics의 TTFT/TPOT count는 각각 38, 생성 토큰 counter는 380이었다.
이는 단일 엔진의 해당 시점 누적값이며 Pod 재시작 후 reset된다. rate/quantile 그래프를
서로 다른 시점·다른 Pod의 원시 count와 혼동하지 않는다.

DCGM `GPU_UTIL`, `FB_USED`, `GPU_TEMP`는 실제 값을 확보했다. `POWER_USAGE`는
collector 설정에 있지만 실제 metric series가 없었다. **전력은 NOT VERIFIED**로 둔다.
GPU별 전체 장치 지표이며 Pod별 귀속이나 profiling/DCP는 검증하지 않았다.
WSL 자체 power 조회도 N/A (비공개 원본 `evidence/kubernetes-vllm-live/20261021/53-wsl-power-query.txt`)였다.

## 제한된 부하 결과

기존 load harness로 동시성 1·2·4에서 각각 12건, 총 36건을 실행했고 모두 성공했다.
각 요청 max_tokens=64, timeout=15초, 각 단계 supervisor 한도 90초였다.
GPU 85°C 또는 여유 VRAM 512MiB 미만 중단 조건은 발생하지 않았다.
관측 표본 5개의 최고 온도는 64°C, 최소 여유 VRAM은 3005MiB였다.
감독 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/41-load-supervision.json`).

짧은 closed-loop smoke이며 warmup 제외나 반복 통제 실험을 하지 않았다. 포화·최대 용량,
운영 규모 안정 처리량, 지연 개선, SLO 달성을 주장하지 않는다. 순간 peak는 표본 사이에
발생할 수 있다. 실제 GPU OOM·포화 과부하 실험은 NOT VERIFIED다.

## 실패도 보존한 이유

- 첫 이미지 digest-only import에는 명명된 image가 보이지 않아 태그/digest alias를 확인해 재가져왔다.
- 모델 캐시 복사 시 공용 blob 링크가 빠진 것을 확인하고, 해당 blob만 추가 복사해 SHA256을 검증했다.
- Grafana 첫 Secret 생성은 임의 바이트에 NUL이 포함돼 실패했다. 환경변수용 문자열로 수정하고
  실제 Ready를 확인했으며 회귀 테스트를 추가했다. 비밀번호는 Git/로그에 저장하지 않았다.
- Prometheus 재시작으로 이전 port-forward가 stale해져 첫 metrics 수집이 실패했다. 새 Pod에
  다시 연결한 `metrics-baseline-retry`가 성공 결과다. 부분 파일도 지우지 않았다.
- vLLM 이미지의 실행 파일은 `python3`이므로 첫 `python` 주입은 실행되지 않았다.
- EngineCore만 정지했을 때에는 150초 안에 probe 재시작이 발생하지 않아 SIGCONT로 복구했다.
  미검출 결과 (비공개 원본 `evidence/kubernetes-vllm-live/20261021/liveness-recovery-retry/result.json`).
  `/health` 및 Gateway readiness만으로 추론 진척을 보장하지 않는다.
- 초기 WSL 오류 출력 한 파일은 원래 UTF-16 오류가 잘못 디코딩돼 읽기 어렵다. 성공 증거로
  사용하지 않으며 후속 정상 조회와 구분한다. 원본 출력/과거 실패 파일은 보존한다.

## 운영·재현 한계

[실행한 manifests](../k8s/validation/)와 전환·복구 절차 (비공개 원본 `docs/wsl-k3s-gpu-validation.md`)를 따른다.
원래 Docker 모델과 Kubernetes 모델을 동시에 시작하지 않는다. 단일 GPU 메모리를 공유한다.
기존 k3s의 Kueue/team-a/team-b, Docker Desktop/kind cluster, 드라이버/runtime/plugin 설정은
수정하지 않았다. 별도 namespace의 실험 자원만 생성·수정·정리한다.

검증 종료 시 trial Deployment 5개는 0 replicas로 보존했다. 기존 Docker vLLM을
다시 시작한 뒤 원래 Gateway 실제 채팅 200, Prometheus 두 target UP, Grafana health 200을
확인했다. 임시 port-forward는 종료했고 Kubernetes trial은 상시 서비스가 아니다.
원래 Docker의 private bridge·vLLM 포트 미게시 구성은 유지한다.
Prometheus/Grafana trial의 저장소는 emptyDir이므로 scale-down 뒤 과거 화면 데이터가
계속 남는다고 보장하지 않는다. raw metrics·쿼리 결과·스크린샷은 Git evidence로 보존한다.

k3s kubeconfig의 기존 권한 644는 발견 사항으로 남겼다. 인증 내용은 수집하지 않았으며
다른 프로젝트가 쓰는 설정을 임의로 바꾸지 않았다. 이 검증은 전체 보안 인증이 아니다.
SLO-Guard 확장은 계속 보류한다. GPU 전력·profiling·MIG·독점 메모리 격리·멀티노드·
클라우드·production readiness는 검증 범위 밖이다.

## 최종 품질 검사와 보존

기존 109개 테스트에 운영 스크립트 안전 계약 5개를 추가해
114 passed, 75 warnings (비공개 원본 `evidence/kubernetes-vllm-live/20261021/65-final-pytest.txt`)를 확인했다.
Ruff PASS (비공개 원본 `evidence/kubernetes-vllm-live/20261021/66-final-ruff.txt`), YAML 30개 파싱,
문서 링크 301개 존재 확인 및 변경 파일 credential 패턴 검사 결과는
최종 검사 snapshot (비공개 원본 `evidence/kubernetes-vllm-live/20261021/67-final-audit.json`)에 있다.
회귀 검사는 오프라인이며 GPU 성공 근거는 위 실제 실행 자료와 구분한다.
원문 로그의 끝 공백·native 오류 인코딩은 보존하므로 evidence를 포함한 whitespace 검사에는
경고가 있을 수 있다. 원문을 고쳐 성공처럼 만들지 않았다.
새 evidence 경로에만 `.gitattributes -text`를 적용해 Git EOL 변환을 막았고,
SHA256 manifest (비공개 원본 `evidence/kubernetes-vllm-live/20261021/evidence-sha256.json`)의 151개 원본 파일과
staged blob이 바이트 단위로 같음을 확인했다. 이전 evidence의 속성이나 내용은 바꾸지 않았다.

최신 원격 검사는 [GitHub Actions](https://github.com/eunsu7997/kubellm-ops-public/actions)에서
해당 commit의 실행을 확인한다. 저장소는 Private이며 공개 권한은 변경하지 않는다.
