# 스크립트 안내

모든 부하·장애 스크립트는 기본이 dry run이거나 명시적 opt-in 플래그가 있어야 실제로 동작합니다. 실험 결과 파일은 덮어쓰지 않고 새 폴더에 씁니다.

## 처음 볼 것

| 스크립트 | 하는 일 |
| --- | --- |
| `load-test/concurrency_sweep.py` | 동시성을 올리며 처리량–지연 곡선과 꺾이는 지점을 찾음. Gateway 동시성 제한값의 근거 |
| `load-test/mock_rehearsal.py` | GPU 없이 mock(합성 용량 모델)과 Gateway를 띄워 sweep과 동시성 제한을 리허설 |
| `load-test/open_loop.py` | 도착률을 고정한 SSE 부하 생성 (open-loop) |
| `load-test/evaluate_slo.py` | 실패·미전송을 분모에 넣은 SLO 판정 (오프라인) |
| `ops/safety_harness.py` | GPU 온도·여유 VRAM·진행 상태를 감시하다 위험하면 부하를 멈추는 감독기 |
| `ops/five_module_pipeline.py` | 배포 전 검사 → GPU 점유 확인 → SSE 부하 → SLO 판정 → 장애 근거 정리를 한 번에 실행 |
| `failure-tests/scenario1~5` | Pod 삭제, 백엔드 불능, GPU 부족 Pending, 모델 로드 실패, 과부하 재현 |

## 폴더별

**`load-test/`** 부하와 SLO
- `load_test.py` 기본 부하, `supervised-soak.py` 단계별 장시간 부하(GPU 가드 포함), `analyze-soak.py` 결과 분석
- `repeated_gpu_review.py` 반복 벤치마크, `streaming-readiness-review.py` 입력 길이를 바꾼 SSE 측정

**`ops/`** 운영 실험 (2026-10-21~22, Docker GPU 환경)
- `exp01` 모델 revision 고정과 롤백 · `exp02` readiness 503 재현 · `exp03a~c` Prometheus·Alertmanager 장애와 복구
- `exp04`, `exp04b` 경보 발생·해제 · `exp06a~c` 컨테이너·WSL·Docker Desktop 재시작 복구 · `exp07` 부하 · `exp08` cold/warm readiness 진단
- `http_admission.py`, `bounded_retry.py`, `compare_admission.py` 별도 admission 실험 경로와 429 제한 재시도 비교
- `restore_stack.py` Docker 재시작 후 서빙 스택 순서대로 복구 · `scale_preview.py` 확장 제안 계산(실제 변경 없음)
- `integration_*`, `crosscheck_llmslo.py`, `copilot_local_probe.py`, `local_runbooks.py` 다른 프로젝트와의 통합 어댑터
- `demo.py`, `chat_demo.py`, `check_demo_endpoint.py` 로컬 시연용
- `analyze_exp02.py`, `analyze_exp07_ramp.py`, `reanalyze_load.py` 저장된 결과 재분석(네트워크 없음)

**`kubernetes/`** WSL k3s GPU 실측 (2026-10-21)
- `pod-recovery.py` vLLM Pod 삭제 복구 · `model-load-failure.py` 모델 로드 실패와 원복 · `verify-local-alert.py` 엔진 일시정지 경보 확인
- `close-docker-gap.py`, `close-kubernetes-gap.py`, `docker-readiness-trial.py` Docker ↔ k3s 전환과 readiness 실험
- `persist-monitoring.py`, `restore-grafana-config.py`, `collect-trial-metrics.py`, `create-trial-grafana-secret.py` 모니터링 영속화·복원
- `harden-shared-kubeconfig.py` kubeconfig 권한 제한 · `verify-gateway-metrics-image.py` 이미지 지표 확인

**루트**
- `k8s-local-verify.ps1` Docker Desktop Kubernetes에 mock 구성 배포·확인 · `vllm-gpu-verify.ps1`, `gpu-docker-check.ps1` GPU 컨테이너 사전 점검
- `ai_infra_publication_dry_run.py`, `build-review-packet.py` 공개 전 개인정보 점검과 검토 자료 생성
