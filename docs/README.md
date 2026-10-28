# 문서 안내

각 문서는 실행 당시의 기록이라 증거 경로와 판정 표가 촘촘합니다. 아래 요약을 먼저 읽고 필요한 부분만 들어가세요.

## 결과 문서

**[RESULTS.md](RESULTS.md) · 5개 프로젝트 통합 실행 (2026-10-22)**
배포 전 검사부터 SLO 판정까지 한 번에 돌린 결과입니다. 429 제한 재시도로 오류율은 25%→0%가 됐지만 E2E P99는 0.65초→1.34초로 늘었습니다.

**[KUBERNETES-LIVE-VALIDATION.md](KUBERNETES-LIVE-VALIDATION.md) · WSL k3s GPU 실측 (2026-10-21)**
- k3s에서 GPU Pod, vLLM 모델 로드, Gateway → Service DNS → vLLM 채팅·SSE까지 동작을 확인했습니다.
- Prometheus가 Gateway·vLLM·DCGM을 수집하고 Grafana에 TTFT·TPOT·GPU 사용률을 표시했습니다.
- vLLM Pod 삭제, 없는 모델로 로드 실패, GPU 슬롯 부족 Pending, liveness 재시작을 각각 일으키고 복구를 확인했습니다.
- 한계: 부하는 동시성 1·2·4에서 36건뿐이고, WSL에서 GPU 전력 지표는 수집되지 않습니다.

**[OPERATIONAL-CLOSURE.md](OPERATIONAL-CLOSURE.md) · 운영 적용과 롤백 (2026-10-21)**
- 실제 추론으로 판단하는 readiness를 Docker Gateway에 적용하고, 이전 버전으로 롤백했다가 다시 복귀했습니다.
- k3s에서 GPU 엔진을 멈추면 EndpointSlice가 Ready=false로 바뀌어 트래픽이 빠지고, 재개하면 돌아오는 것을 확인했습니다.
- 이미지 base digest와 의존성 버전을 고정하고 캐시 없는 빌드를 확인했습니다.

**[SAFETY-HARNESS-CLOSURE.md](SAFETY-HARNESS-CLOSURE.md) · 실험 안전장치 (2026-10-22)**
- 공유 GPU에서 부하를 걸 때 온도·여유 VRAM·센서 오류·시간 제한을 감시해 위험하면 멈추는 감독기를 만들었습니다.
- 과거 CSV 15개를 다시 분석해 성공 요청만 세던 해석을 바로잡았습니다.

## 실험 보고

**[reports/2026-10-22-overnight-observation.md](reports/2026-10-22-overnight-observation.md) · 120분 관측과 cold start 진단**
- 120분 저부하 관측: 예정 7,197건 중 7,190건 성공, 7건 미전송으로 엄격 판정은 **FAIL**로 기록했습니다.
- **vLLM 프로세스를 새로 띄운 직후 첫 부하**에서 readiness 503과 미전송이 나오고(TTFT P99 4~9초), 같은 프로세스의 다음 구간에서는 사라졌습니다(0.1초 미만). 그 구간에 추론 커널 JIT 컴파일 경고가 집중됐습니다.
- 원인을 JIT 쪽으로 좁혔지만 모든 503을 설명한다고 확정하지는 않았습니다. 재시작 직후 warmup 요청을 보내야 하는 근거입니다.

**[reports/2026-10-22-integration-followup.md](reports/2026-10-22-integration-followup.md) · admission 정책 비교**
- 같은 trace 96건을 제한 없음·기본·토큰 예산·KV cache 연동 정책으로 비교했습니다.
- 이 부하에서는 **제한 없음이 24/24 성공, P99 91ms로 가장 좋았습니다.** 제한을 걸면 429가 생기고 지연이 늘었습니다. 즉 이 부하는 GPU 포화 지점보다 낮았고, 제한값은 포화 지점을 먼저 측정한 뒤 정해야 합니다(`scripts/load-test/concurrency_sweep.py`).

**[reports/2026-10-23-publication-review.md](reports/2026-10-23-publication-review.md), [public-snapshot](reports/2026-10-23-public-snapshot.md)**
비공개 원본에서 공개본을 만들 때 개인정보를 어떻게 걸렀는지 기록합니다.

## 공개본의 출처

[snapshot 출처](reports/2026-10-23-public-snapshot.md)의 source manifest는 선별 시작 시점의 소스 기록입니다. [실행 증거](../evidence/public-integration-20261023/README.md)의 manifest는 비식별 파생본 기록입니다. 현재 코드의 후속 개선은 Git 이력에서 확인합니다.

## 가이드

- [RUN-LOCAL.md](RUN-LOCAL.md): GPU PC에서 채팅·통합 실행·복구
- [docker-gpu-security-migration.md](docker-gpu-security-migration.md): Docker GPU 구성 보안 강화 절차 (당시 준비 절차; 실제 적용·롤백 결과는 [OPERATIONAL-CLOSURE.md](OPERATIONAL-CLOSURE.md) 참조)
- [../k8s/README.md](../k8s/README.md), [../scripts/README.md](../scripts/README.md)
