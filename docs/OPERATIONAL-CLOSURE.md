# KubeLLM-Ops — 실제 적용·운영 빈틈 보완 결과

**후속 현재 결과:** 지속 관측·TSDB/설정 복원·로컬 경보·k3s 권한 제한 (비공개 원본 `docs/EXTENDED-OPERATIONS.md`).
이 보고서의 134 tests 및 kubeconfig 0644/미수정 설명은 해당 검증 시점 snapshot이다.
후속 승인 실행에서 163 tests, root:eunsu 0640과 재시작 후 접근 통제를 확인했다.

기준일: 2026-10-21, 후보 검증 보고 (비공개 원본 `docs/FINAL-REVIEW.md`) 이후의 승인된 후속 실행이다.
실제 서비스 중지·교체·롤백과 격리 namespace의 GPU 전환 실험을 명시 승인받아 진행했다.
기존 클러스터 삭제, 드라이버/WSL/runtime/plugin 변경, 공개 전환, AWS 생성은 하지 않았다.

## 현재 결론

새 추론 readiness를 **실제 Compose Gateway에 적용**했다. 실제 이전 Gateway로 롤백한 뒤
새 Gateway로 복귀했고, 각 단계에서 채팅·SSE를 확인했다. 기존 후보 수동 실행과 Compose의 차이를 줄였다.
WSL k3s에서도 새 이미지·readiness 설정을 적용해 **EndpointSlice Ready=false→true**와
주입 프로세스 종료 후 **독립 안전 재개**, 복구 후 Service DNS 경유 채팅을 확인했다.

| 이전 빈틈 | 이번 실제 결과 |
|---|---|
| 후보 검증만 하고 기존 서비스 미적용 | 새 Compose Gateway가 loopback 18003에서 inference 모드로 실행 |
| 롤백 문서만 준비 | 이전 Gateway 채팅·SSE 실제 확인 후 새 Gateway 복귀 |
| Kubernetes 새 readiness 미검증 | GPU 엔진 정지 중 EndpointSlice false, 복귀 후 true·실제 Service 채팅 OK |
| finally 복구만 검증 | 주입 프로세스가 os._exit한 뒤 detached 20초 SIGCONT가 복구. finally 이전 채팅 성공 |
| 고정 짧은 non-streaming 부하만 측정 | 입력 3종·실제 16/128/256 출력 토큰, 워밍업 제외 SSE 72/72 성공 |
| readiness 부하 중 오탐 미확인 | 이번 제한 부하 중 관측 23회 모두 200. 일반적 오탐률 보장 아님 |
| 로컬 base 이미지 의존 | 실제 Python base digest와 Linux 의존성 버전 고정, 별도 clean 문맥 no-cache 빌드·import 성공 |
| 채용 문서/상세 이력 혼재 | 현재 보고서·채용 요약을 최신 인덱스로 구분. Notion도 별도 짧은 최종본으로 연결 |

전력 수집·최대 용량/포화·fresh GPU 호스트 설치·공유 kubeconfig 권한과 대외 공유 권한은
아래처럼 별도 한계다. 실행할 수 없는 것을 문서만으로 VERIFIED로 바꾸지 않는다.

## 1. 실제 Docker 교체와 롤백

[전용 실행 스크립트](../scripts/kubernetes/close-docker-gap.py)는 기본 dry-run이며,
`--execute` 때만 고정된 Gateway 두 개를 전환한다. vLLM·Prometheus·Grafana를 재생성하지 않는다.

- Compose config 검증 (비공개 원본 `evidence/operational-closure-20261021/docker-cutover/00-compose-valid.txt`)
- 새 Gateway 채팅/SSE (비공개 원본 `evidence/operational-closure-20261021/docker-cutover/04-new.json`)
- 실제 이전 Gateway 롤백 (비공개 원본 `evidence/operational-closure-20261021/docker-cutover/07-rollback.json`)
- 최종 새 Gateway 복귀 (비공개 원본 `evidence/operational-closure-20261021/docker-cutover/10-final.json`)

최종 활성 컨테이너는 `kubellm-gpu-secure-gateway-1`이다. 이전
`kubellm-gateway-streaming-candidate-20261021`는 중지·보존한다.
두 개가 같은 18003 포트를 동시에 사용하지 않는다. 이전 Compose Gateway 객체는 새 이미지로 교체됐다.
Rollback 보존 대상은 별도 이전 streaming candidate다.

스크립트는 Grafana를 선택하지 않는 gateway-only 명령의 Compose 변수 해석에만 임시 난수를 사용했다.
그 값은 로그/Git에 저장하거나 실제 Grafana 비밀번호로 적용하지 않았다.
**전체 Compose를 올릴 때 이 방법을 쓰지 않는다.** 실제 Grafana 세션 비밀번호를 별도 비밀 저장소에서 제공해야 한다.
이번 검증은 `up --no-deps --no-build gateway`만 실행했다. 전체 스택 재생성/기존 Grafana 데이터 복구 검증이 아니다.

### 같은 호스트의 롤백 명령

```powershell
docker stop kubellm-gpu-secure-gateway-1
docker start kubellm-gateway-streaming-candidate-20261021
# readyz, 실제 chat 및 SSE 확인 후 새 구성으로 복귀
docker stop kubellm-gateway-streaming-candidate-20261021
docker start kubellm-gpu-secure-gateway-1
```

이는 보존된 컨테이너가 있는 현재 호스트에서 실제 시험한 절차다. 다른 PC에는 해당 이름의 컨테이너가 없을 수 있다.
새 PC에서는 이전 이미지 ID/설정·포트·네트워크를 먼저 확보해야 하며 이름만 복사해서 실행하지 않는다.

## 2. Kubernetes EndpointSlice와 독립 재개

입력은 [새 Gateway manifest](../k8s/validation/wsl-gateway-trial.yaml)다.
inference readiness timeout 2초·cache 5초, probe timeout 4초·period 5초·failureThreshold 2,
liveness는 `/healthz`다. GPU vLLM의 digest/model revision은 기존 검증값을 유지했다.

[실행 스크립트](../scripts/kubernetes/close-kubernetes-gap.py)는 trial Deployment가 모두 0 replicas일 때만 진행한다.
Docker vLLM을 중지하고 전용 namespace만 시작한다. 다른 프로젝트의 GPU 점유/클러스터 설정은 바꾸지 않는다.
전체 실행 결과 (비공개 원본 `evidence/operational-closure-20261021/kubernetes-readiness/result.json`):

1. 정상 Gateway endpoint Ready=true를 확인했다.
2. EngineCore PID/start time을 확인하고 detached SIGCONT helper를 먼저 생성했다.
3. 주입 Python 프로세스는 SIGSTOP 후 `os._exit(0)`으로 종료했다. 그 프로세스의 finally 복구는 없다.
4. 시작 후 첫 Ready=false 관측은 **약 13.88초**, 첫 Ready=true 복귀는 **약 28.77초**였다.
5. 20초 지연 helper 외에는 복구 후 Service 채팅 전 SIGCONT를 실행하지 않았다.
   Service DNS 경유 실제 채팅 HTTP 200·assistant `OK`를 확인했다.
6. 그 뒤 finally에서 방어적 SIGCONT·trial scale 0·실제 GPU Pod 삭제 대기를 거쳐 Docker 모델을 복구했다.

20초는 helper에 지정한 지연이고, 28.77초는 cache/probe/관측 간격까지 포함한 endpoint 복귀 시점이다.
정확한 SIGCONT 실행 시각이나 일반적인 복구 보장 시간이 아니다. 한 번의 실제 실험이다.
실패 중 패킷 추적이나 외부 클라이언트 연결 실패 응답을 별도로 저장하지는 않았다.
직접 확인한 것은 EndpointSlice 조건 변화와 복구 후 실제 Service 요청 성공이다.

**자동 장애 복구와 구분:** 이 helper는 알려진 SIGSTOP 실험을 되돌리는 안전장치다.
원인 불명의 실제 엔진 장애를 진단해 자동 재시작하는 운영 controller가 아니다.
전체 호스트/컨테이너 종료·helper 자체 장애에 대한 보장도 아니다. readiness 실패를 자동 재시작으로 연결하지 않는다.

## 3. 실제 스트리밍 측정과 오탐 관측

대상은 복구 후 새 **Docker Compose Gateway → 실제 Qwen vLLM**이다. Kubernetes 성능 측정이 아니다.
프로필당 워밍업 2건 제외, 12건 × 2회 = 24건. 총 측정 72건 모두 성공했다.
고정 prefix·temperature 0·closed loop이며 cache/백그라운드 영향을 통제하지 않았다.

| 입력 프로필 | 동시성 | 실제 출력 토큰 | client TTFT p95 | client TPOT 추정 p95 |
|---|---|---|---|---|
| short | 1 | 16 | 23.79ms | 6.583ms/token |
| medium, context 256회 반복 | 4 | 128 | 31.81ms | 6.721ms/token |
| long, context 768회 반복 | 4 | 256 | 37.73ms | 6.816ms/token |

p95는 프로필당 성공 24건을 합친 nearest rank다. TTFT는 클라이언트가 첫 content를 읽은 시각이고
worker 대기는 제외한다. TPOT 추정은 첫/마지막 content 도착 간격을 서버 usage의 `completion_tokens-1`로
나눈 값이다. SSE chunk 개수를 토큰 수로 취급하지 않았다. vLLM 서버 histogram이나 모델 커널 시간을 뜻하지 않는다.
원문 (비공개 원본 `evidence/operational-closure-20261021/streaming-load/result.json`) ·
독립 집계 (비공개 원본 `evidence/operational-closure-20261021/10-streaming-analysis.json`).

readiness 23회 관측은 모두 200이었다. GPU 23표본 최고 64°C, 최소 여유 VRAM 2667MiB.
표본 사이 상태·다른 부하·다른 모델에서의 오탐과 장기간 안정성을 보장하지 않는다.
이전 216건 non-streaming 결과와 합쳐 하나의 통제 성능 실험으로 쓰지 않는다.
포화·최대 안정 처리량·OOM·SLO 달성은 **NOT VERIFIED**다.

## 4. 재현성 보완

실제 빌드에 사용된 `python:3.12-slim` digest를 Dockerfile에 고정하고,
실행 중 이미지에서 확인한 Linux Python 3.12 패키지를 `gateway/requirements.lock.txt`에 고정했다.
이는 버전 snapshot이며 hash-verified 패키지 공급망 인증/취약점 검사나 multi-architecture 호환 보장이 아니다.

별도 임시 디렉터리에 Dockerfile·requirements·app Python 소스만 복사해
`docker build --no-cache`를 실행했다. 빌드 결과는 raw 출력 (비공개 원본 `evidence/operational-closure-20261021/05-clean-context-build-retry.txt`),
네트워크 없는 일회성 컨테이너에서 import/config 확인은 실행 출력 (비공개 원본 `evidence/operational-closure-20261021/06-clean-context-import.txt`)이다.
현재 서비스와 clean 이미지 비교 (비공개 원본 `evidence/operational-closure-20261021/11-repro-equivalence.json`)에서
앱 파일의 줄바꿈 정규화 SHA256과 설치 패키지 목록이 같았다.
활성 표준 빌드 이미지와 clean 고정 빌드 이미지는 패키징 metadata/이미지 ID가 다르며 동일 OCI byte라고 주장하지 않는다.

첫 수집은 Windows cp949로 UTF-8 빌드 로그를 읽다 실패했다. 재시도는 raw bytes를 저장하도록 수정했다.
처음 실패를 성공 증거로 사용하지 않는다.

### 새 호스트에 필요한 조건과 순서

1. Docker GPU access, WSL k3s·NVIDIA runtime/plugin, 모델 캐시가 먼저 필요하다. 설치/드라이버 설정 변경을 자동 수행하지 않는다.
2. `docker build -t kubellm-ops-gateway:readiness-20261021 gateway`로 새 Gateway를 만든다.
3. 모델 digest·Qwen revision·weights SHA256·VRAM 여유를 기존 재현 가이드 (비공개 원본 `docs/wsl-k3s-gpu-validation.md`)와 대조한다.
4. Docker와 k3s 모델을 동시에 시작하지 않는다. k3s에는 새 Gateway 이미지도 먼저 import한다.
5. `k8s/validation/` 전체 apply를 하지 않는다. GPU 부족/fault용 Pod가 섞여 있다.
   정상 vLLM/Gateway manifests만 선택하고 rollout → endpoints → 실제 채팅/SSE 순서로 검사한다.
6. 최초 실제 GPU 호스트 설치부터의 end-to-end 재현은 별도 장비에서 실행하지 않았다.
   clean 컨테이너 빌드 성공을 fresh Windows/WSL/GPU 호스트 전체 성공으로 확대하지 않는다.

## 5. 보안·제출 경계와 남은 외부 결정

실제 바인딩/권한 metadata (비공개 원본 `evidence/operational-closure-20261021/09-security-boundary-review.json`):
vLLM host port 없음, Gateway/Prometheus/Grafana는 127.0.0.1만 게시한다.
API 인증·TLS·사용자별 rate limit은 없다. 로컬 사용자/Docker·k3s 관리자와 bridge 내 프로세스를 신뢰하는 개발 환경이다.
외부 서비스로 공개하려면 인증/접근 제어를 별도로 설계·검증해야 한다.

공유 `/etc/rancher/k3s/k3s.yaml`은 여전히 root:root 0644다. 내용은 읽거나 저장하지 않았다.
이번 승인은 전용 namespace/image/scale 작업이며 다른 프로젝트가 쓰는 공유 kubeconfig 권한/서비스 시작 옵션 변경은 포함하지 않는다.
소유자가 비-root 소비자를 확인하고 root-only 0600 및 k3s 재생성 시 권한 정책을 함께 적용해야 한다.
현재 절차는 root `k3s kubectl`을 쓰지만 그것만으로 world-readable 원본 위험이 해결되는 것은 아니다.

GitHub는 Private, Notion 공유 권한은 그대로다. 검토자 이름/접근 대상이 없으므로 초대/공개 전환을 임의로 하지 않는다.
채용 요약과 대표 증거를 제공했지만 대외 배포/실제 채용 제출 완료라고 쓰지 않는다.
SLO-Guard 정책 연구는 기존 결정대로 보류이며 이번 운영 빈틈 보완과 구분한다.

## 최종 판정

최종 실제 실행 상태 (비공개 원본 `evidence/operational-closure-20261021/12-final-runtime.json`),
전체 테스트 134 passed (비공개 원본 `evidence/operational-closure-20261021/07-pytest.txt`),
추가 재검수 결과 (비공개 원본 `evidence/operational-closure-20261021/final-audit.json`)를 함께 확인한다.
정적·오프라인 테스트와 GPU 런타임 성공은 서로 다른 근거다.
소스·문서 whitespace 검사는 통과하되 raw evidence CRLF 경고는 원문 보존을 위해 남긴다.
최종 추가 저장소 credential 패턴 검사 (비공개 원본 `evidence/operational-closure-20261021/repository-credential-scan.json`)는
해당 commit의 추적 텍스트 491개에서 고특이도 material 패턴 검출 0건이었다. 비밀정보가 절대 없다는 보증은 아니다.
마지막 CI에서 발견한 Node 20 종료·ubuntu-latest 전환 경고는 공식
[checkout](https://github.com/actions/checkout)·[setup-python](https://github.com/actions/setup-python) 문서를 확인해
Node 24 v6 action commit SHA 및 Ubuntu 24.04 runner로 고정했다. CI 권한은 contents:read이며
checkout credential persistence를 끈다. 애플리케이션 테스트 범위는 그대로 유지한다.

실제 Gateway 적용·롤백, Kubernetes endpoint 제외/복귀, 독립 실험 안전 재개,
제한된 다양한 입력 SSE 측정·readiness 관측, clean 컨테이너 재현 빌드는 **COMPLETE**다.
모든 환경/보안/성능 확장까지 완료한 상태는 아니다. 전력·포화/최대 용량·fresh GPU 호스트 재현,
공유 kubeconfig 수정·대외 공유 권한 결정은 **남아 있다**. production ready라고 주장하지 않는다.
