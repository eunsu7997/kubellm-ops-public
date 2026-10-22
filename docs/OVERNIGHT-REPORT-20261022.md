# KubeLLM-Ops 야간 마감 보고 · 2026-10-22

**상태: 최종 보고(2026-10-22 KST).** 모든 실제 부하·진단을 마치고 stock 구성으로 복원한 뒤 문서·증거 검수를 마감했습니다.
Evidence 폴더 이름의 `20261021`은 작업 시작 UTC 날짜입니다. GitHub와 Notion은 Private/기존 권한을 유지합니다.
개인 파일과 다른 프로젝트는 변경하지 않았습니다.

**최종 전달 검수:** [GitHub·Notion 실제 반영 및 원본 bytes 보존 보완 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/FINAL-DELIVERY-20261022.md).
기존 Notion 두 페이지를 실제 갱신·재조회했고, 과거 Git의 LF 정규화 문제는 원문을 바꾸지 않은 별도 ZIP으로 보완했습니다.

**한 줄 판정:** 실제 GPU 서빙·관측·복구 경로는 반복 측정으로 확인했지만, 120분 관측의 엄격 phase 판정은 **FAIL**이고
readiness 503의 구성 요소 원인은 **PARTIAL**(JIT 관련 관측 근거를 좁힘)입니다. 12개 하위 항목 전부나 production 완료를 주장하지 않습니다.

## 증거의 종류

| 종류 | 이번 라운드 내용 | 해석 |
|---|---|---|
| 실제 GPU 실험 | 90초 회귀, 120분 관측, 상세 JIT 진단 2구간, stock 복원 후 cold/warm 2구간, 최종 runtime 확인 | 실제 RTX4060·private Docker Qwen vLLM·Gateway 응답 |
| 합성/격리 검증 | 센서 timeout·NaN·heartbeat·hard wall·연속 실패·프로세스 트리 종료·helper armed/cancel | 합성 센서와 격리 Python 프로세스. GPU 고장이나 서비스 장애를 일으킨 증거 아님 |
| 오프라인 fixture·소스 확인 | 복구 스크립트의 k3s GPU 충돌/legacy 차단 분기, vLLM JIT 경고 호출부 확인, 과거 CSV 재분석 | 실행 분기 테스트·코드 읽기·재계산이며 실제 장애 재현 아님 |

## 이번에 보완한 것

기존 GPU 서빙 기능을 추가하는 대신 실험의 안전 종료와 판정·설명 오류를 보완했습니다.
Gateway/vLLM 제품 코드·고정 이미지·모델/revision·메모리 옵션·클러스터 구성은 변경하지 않았습니다.

**임시 변경은 실제로 있었습니다.** 진단 중 vLLM 컨테이너 한 개만 `--jit-monitor-verbose` 인자를 추가해 재생성했고,
진단 뒤 stock args로 다시 재생성해 복원했습니다. 최종 vLLM 컨테이너는 그 복원 재생성의 결과입니다.
복원 후 image·args·GPU Count=-1의 stock identity, 실제 채팅·SSE를 확인했습니다.
Gateway/관측 서비스·원본 TSDB·모델 캐시·기존 증거와 다른 프로젝트는 보존했습니다.

| 빈틈 | 반영 내용 | 근거와 한계 |
|---|---|---|
| 센서 멈춤·NaN이 안전으로 통과 가능 | 3초 센서 timeout·유효성·독립 heartbeat·중단 | [안전 하네스](../scripts/ops/safety_harness.py), [테스트](../tests/test_ops_safety_harness.py) |
| 종료 기한·실패 전파 부족 | hard wall·전송 완료 순서 연속 실패 3회·자식 정리·실패 exit | 실제 GPU 장애가 아닌 격리 프로세스로 중단 분기 검사 |
| Windows venv 실행기만 종료 가능 | 생성기 프로세스 트리 종료, 실패 cleanup도 실패 처리 | [소스 hash 포함 7종 실제 격리 검증 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/harness-source-hashed-final/verification.json) |
| 관측 thread가 조용히 멈춤 | vLLM 표본 누락/파싱 실패를 supervisor로 전파 | GPU 센서와 HTTP 관측은 독립. 단일 관측 실패에도 안전하게 중단 |
| 미전송·성공 0건도 통과 가능 | success>0, 전송 실패0·미전송0, 유효 표본·E2E 복구 요구 | readiness/경보는 별도 공개하며 phase pass와 가용성 보장은 다름 |
| SSE EOF만으로 success | content와 `[DONE]` 모두 필수, 새 summary에 계약 표시 | 과거 CSV의 DONE 여부를 소급 보증하지 않음 |
| 프로세스 중단 시 요청 결과 소실 | 각 요청 metadata JSONL flush, 부분/중복/수 불일치 검사 | 전원 장애 fsync 영속성 보장 아님 |
| 복구 중 k3s GPU 중복 가능 | stopped 모델은 k3s GPU Pod/조회 불명·legacy·센서 이상 차단 | 차단 분기는 오프라인 fixture. 실제 cold conflict를 재현하지 않음 |
| 부정확한 helper armed/cancel 표현 | 실제 helper 대기 진입 JSON, 취소 acknowledgement | 격리 검사 + 실제 진단 1회에서 취소 ack exit0 |
| 진단과 다른 부하의 중첩 | 실제 부하 프로세스 조회·exclusive lease, lease 중 일반 load CLI 거부 | 120분 실행 중 실제 부하 프로세스 4개 감지·변경 없음([positive control · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/jit-overlap-positive-control.json)). lease 거부 분기는 테스트 |
| drain을 포함한 성능 수치·서버 오류 단정 | 창 안 완료와 cohort/drain을 분리, timeout 관측 주체 정정 | [15개 원본 CSV 재분석 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/historical-load-reanalysis.json) |

소스·사용법·stop/go 기준은 [상세 검수/운영 절차](SAFETY-HARNESS-CLOSURE.md)에 있습니다.

## 실제 관측

모든 부하 구간은 1 req/s Poisson, client in-flight cap 8, 입력 256단어, 최대 128 tokens, private Docker Qwen vLLM 경로입니다.
success는 content와 SSE `[DONE]`을 모두 받은 요청입니다. 미전송은 client cap 때문에 보내지 않은 도착이며 전송 실패와 구분합니다.

| 구간 | 성공 / 미전송 / 전송 실패 | readiness 표본 | CLI exit · 엄격 phase | client TTFT p95 / E2E p95 |
|---|---:|---|---|---:|
| 첫 90초 회귀(stock) | 93 / 9 / 0 | 비정상 3 | 당시 `ok=true`(기준 강화 전) · 현재 기준 미통과 | — |
| 120분 관측(stock) | 7,190 / 7 / 0 | 200×1,263 · 503×1 | 1 · **FAIL** | 0.110초 / 2.019초 |
| 상세 JIT 재생성 직후 | 93 / 9 / 0 | 200×12 · 503×4 | 수집 CLI 0 · FAIL | 4.420초 / 9.383초 |
| 상세 JIT 같은 프로세스 warm | 102 / 0 / 0 | 200×17 | 수집 CLI 0 · PASS | 0.083초 / 1.538초 |
| stock 복원 직후 | 89 / 13 / 0 | 200×12 · 503×3 | 1 · FAIL | 9.221초 / 12.246초 |
| stock warm 대조(재생성 없음, 사전 선언 1회) | 102 / 0 / 0 | 200×17 | 0 · PASS | 0.096초 / 1.566초 |

지연은 성공 요청 집단의 client 측 조건부 분포이며 SLO가 아닙니다. 같은 호스트의 문서·검수 작업과 cache를 통제하지 않았습니다.

### 120분 관측

7,197개 예정 요청의 CSV/JSONL/summary/guard ID·범주·상태가 일치했습니다. 실제 경과 7,202.1초(계획 7,200초)입니다.
경보는 `GatewayInferenceNotReady` pending 1표본입니다. GPU 센서 3,366표본의 최고 63°C·최소 여유 2,362MiB, unsafe 표본 0입니다.
자식 정리와 실제 E2E 복구를 확인했습니다. usage 기반 client TPOT 추정 p95는 0.01613초/토큰입니다.
**미전송 7건 때문에 CLI exit1, 엄격 phase 및 깨끗한 관측 판정은 FAIL**입니다. 전송 성공만 세어 무실패 soak라고 쓰지 않습니다.
시작 시 소스 hash를 기록하지 않아 이 장시간 실행의 정확한 로드 코드 동일성은 **NOT VERIFIED**입니다.
[독립 판정 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/long-soak-independent-verdict.json) ·
[TPOT/분모별 통계 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/long-soak-extra-statistics.json).

### JIT 상세 진단

설치 이미지의 실제 CLI 옵션을 확인한 뒤 `--jit-monitor-verbose`만 임시 추가했습니다(image/model/revision/GPU/메모리 동일).
120분 부하 종료와 실제 프로젝트 부하 프로세스 0을 확인한 뒤 실행했습니다. 두 구간은 같은 seed 6999입니다.
재생성 직후 구간에서 readiness timeout counter가 3 증가했고 JIT 경고 12건(`_gumbel_sample_kernel`, `_topk_topp_kernel`,
`_topp_sb_mask/stats/step_kernel` 5종)이 있었습니다. warm 구간은 timeout 증가 0·JIT 경고 0이었습니다.
진단 controller는 stock identity·E2E 복원, helper 취소 acknowledgement exit0을 기록했고 ignored lease 파일도 제거됐습니다.
사용한 11개 소스 snapshot/hash는 디스크·Git `00ded7d` 내용과 CRLF 정규화만 허용한 비교에서 불일치 0입니다.
[독립 비교·복구/취소 확인 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/jit-diagnostic-independent-comparison.json) ·
[raw · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/jit-readiness-20261021T172031705825Z).

### stock 복원 후 cold/warm

stock(상세 로그 없음) 복원 직후 source-hash를 기록한 90초 회귀는 89 성공·13 미전송·503 3표본, CLI exit1로 보존했습니다.
사전 선언한 단 1회의 warm 대조(재생성 없음)는 102 성공·0 미전송·readiness 17/17×200, CLI exit0이었습니다.
두 기록의 소스 hash는 디스크·Git과 CRLF 정규화 비교에서 일치합니다. 이후 통과를 만들기 위한 재시도는 하지 않았습니다.
[stock 비교 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/stock-cold-warm-independent-comparison.json) ·
[cold raw · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/safety-fixed-warmup-20261021T172919386329Z) ·
[warm raw · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/safety-fixed-warmup-20261021T173139094469Z) ·
[사전 계획 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/stock-warm-control-plan.json).

**해석:** 상세 로그 유무와 관계없이 새 vLLM 프로세스의 첫 부하에서 미전송·503이 나타나고, 같은 프로세스의 다음 구간에서는 사라졌습니다.
추론 중 JIT와 첫 부하 지연의 관측 근거를 좁혔지만 **모든 503의 원인 확정이나 일반적 warmup 해결책을 증명하지 않습니다.**
120분 관측에서도 503 1회가 있었으므로 첫 부하 현상만으로 모든 503을 설명하지 않습니다. 상세 로그 overhead·가변 모델 출력·순차 1회 비교·호스트/cache 경합도 한계입니다.
설치 이미지의 경고 hook은 POST_COMPILE + `warning_once`이므로 경고가 없는 구간을 컴파일 없음으로 해석하지 않습니다.

### 최종 runtime

[최종 상태 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-runtime.json): 프로젝트 부하 프로세스 0, 진단 lease 없음, stock identity true.
ready 200·실제 채팅 200(assistant 응답)·SSE 200+DONE. 프로젝트 컨테이너 6개 running, vLLM host port 없음,
Gateway `127.0.0.1:18003`, Prometheus 19091·Grafana 13001·Alertmanager 19093·receiver 19094(모두 loopback).
Prometheus gateway/vLLM target UP, 활성 경보 없음. 과거 같은 timestamp의 `sum(gateway_requests_total)=1705` 유지, 원본 TSDB volume 존재.
k3s node Ready, GPU capacity/allocatable 4(time-slicing 공유 슬롯), trial Deployment 5개 모두 0 replicas.
kubeconfig는 메타데이터(`root:eunsu 640`)만 확인했고 내용은 읽지 않았습니다. Host GPU: RTX 4060, driver 616.92, 8188MiB.

### 과거 성능 해석 정정

과거 queue timeout 49건은 모두 HTTP200 이후 클라이언트 timeout입니다. 서버 HTTP 오류 49건이 아닙니다.
60분 soak의 과거 `server_side_failures` 필드는 서버 오류를 판정한 값이 아니며, 당시 결과는 성공 3517·미전송 2로만 인용합니다.
과거의 약 30–35라는 수치는 60초 유입 cohort의 drain 완료까지 합산한 비율이라 지속 처리량 plateau·포화·최대 용량 근거가 아닙니다.
원본 로그는 그대로 보존했습니다.

## 보내준 12개 체크리스트의 현재 판정

아래는 10/12 스크린샷 항목과 최신 증거의 대응입니다. 모든 하위 검증이 완료됐다는 뜻은 아닙니다.

| # | 스크린샷 작업 | 판정·완료 범위 |
|---|---|---|
| 1 | Docker vLLM 시작·Qwen 로드 | VERIFIED: 실제 모델 채팅·SSE와 고정 이미지/모델 args(진단 뒤 stock 복원 확인) |
| 2 | 비공개 Gateway→vLLM | VERIFIED: 사용자 bridge·내부 DNS·실제 응답 |
| 3 | Prometheus/Grafana 연결 | VERIFIED: 실제 targets/query/health. 부하 정상 여부와 별개 |
| 4 | 호스트8002 미노출 | VERIFIED: 현재 vLLM host ports 없음·Gateway는 127.0.0.1:18003 |
| 5 | Gateway Readiness False 원인·Ready | PARTIAL: 실제 추론 정지 감지/복구·현재 Ready. 새 프로세스 첫 부하·JIT와의 상관은 관측, 구성 요소 원인은 미확정 |
| 6 | Grafana 최신 데이터·TTFT/TPOT | VERIFIED: 실제 수집·쿼리·검증용 복사본 화면. 기본 dashboard 전체 수정 아님 |
| 7 | 실제 GPU 반복 부하 지표 | VERIFIED(측정으로서): 실제 반복 측정·오류/미전송 공개. 120분 엄격 phase FAIL, 포화·최대 용량·운영 안정성 미입증 |
| 8 | Kubernetes GPU Pod 추론 | VERIFIED(과거 실행): 기존 WSL k3s의 Qwen/Gateway/Service DNS 채팅·SSE. kind GPU 성공으로 표현하지 않음 |
| 9 | DCGM/GPU 전력 포함 시각화 | PARTIAL: 사용률·VRAM·온도. WSL power N/A, 전력 NOT VERIFIED |
| 10 | GPU 부족·모델 실패·장애 복구 | VERIFIED: 공유 4슬롯에 5요청 Pending, offline 모델 실패, API liveness·Pod 삭제 복구. 모든 장애 자동 복구 보장 아님 |
| 11 | 버전·재현·증거·공유 점검 | PARTIAL: digest/revision·같은 호스트 캐시 재현·원본 hash 재검증. fresh GPU host·캐시 없는 설치·외부 공유 미검증/미실행 |
| 12 | README·아키텍처·면접·최종본 | VERIFIED(이번 문서 마감 범위): README·이 보고·기존 Notion 두 페이지 실제 반영과 재조회·GitHub 게시. [최종 전달 검수 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/FINAL-DELIVERY-20261022.md). 실제 외부 검토자 공유는 미실행 |

[Kubernetes 원문 인덱스](KUBERNETES-LIVE-VALIDATION.md) · [실제 적용/롤백](OPERATIONAL-CLOSURE.md) ·
[TSDB/설정 복원·권한 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/EXTENDED-OPERATIONS.md) · [후속 경보·재시작 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/docs/LOCAL-OPS-EXPERIMENTS.md).
물리 GPU는 RTX4060 **1장**이고, k3s 광고 4는 time-slicing 공유 슬롯입니다. 전용 GPU/VRAM 4개로 해석하지 않습니다.
현재 k3s 실험 Deployment 5개는 0 replicas로 보존하고 Docker가 serving합니다. kind와 다른 프로젝트는 건드리지 않았습니다.

## 독립 검수와 검증 상태

- Claude Code 읽기 전용 검수 6회(01–04, 05-jit, 06-jit)와 자체 재검수. 지적·반영의 경계는 [상세 문서](SAFETY-HARNESS-CLOSURE.md)에 있습니다.
- **최종 로컬 품질(모든 부하 종료 후):** 237 passed·1 skipped(Windows POSIX mode 시험)·206 deprecation warnings, Ruff·`git diff --check` exit0.
  [명령·exit·소스 hash · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/quality-final/verification.json).
- 과거 기록: 코드 commit `0be7919`의 CI 39628031385는 당시 223 passed·1 skipped.
  진단 준비 commit `00ded7d`의 [CI 39641353623 SUCCESS](https://github.com/eunsu7997/kubellm-ops-public/actions/runs/39641353623)는
  Linux 237 passed·1 skipped·1 warning(Windows 트리 종료 시험 skip, POSIX 시험 실행), Ruff·YAML 성공입니다.
  [CI 수신 기록 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/jit-preparation-ci.json).
- 이번 문서 마감 commit 이후 제품 코드·테스트는 변경하지 않았습니다. 이 commit의 CI 결과는 별도 수신 기록
  `evidence/ops-safety-closure-20261021/publication-ci-receipt.json`에 남깁니다.

### 최종 감사

[감사 결과 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-publication-audit-03/audit.json) ·
[새 evidence SHA256 manifest · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-publication-audit-03/evidence-sha256.json) ·
[감사 스크립트 사본 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-publication-audit-03/audit-script.py.txt).

- 기존 5개 manifest hash 항목 151+28+7+43+174=403개(고유 경로 403개)를 실제 파일로 재계산: 불일치 0.
- Claude 원본 baseline 134개 hash: 불일치 0.
- 새로 staged된 evidence blob을 filter 없이 실제 bytes와 비교: 불일치 0. 기존 commit evidence 변경/삭제 0.
  CRLF 정규화는 Git 소스 동일성 비교에만 사용했고 raw 파일에는 적용하지 않았습니다.
- 공개해 둘 기존 한계: `local-ops-experiments-20261021`의 raw 110개는 `-text` 규칙(`0be7919`)보다 먼저(`806920d`) commit돼
  Git 이력에는 LF 정규화 blob으로 저장돼 있습니다. 디스크의 원본 CRLF bytes는 Claude 원본 baseline SHA256과 정확히 일치합니다.
  기존 commit evidence는 바꾸지 않았습니다. 최종 전달에서 원본134개를 [별도 ZIP · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-delivery/README.md)에 byte 그대로 복사하고 각 entry hash를 baseline과 대조했습니다. 그 밖의 과거 text 파일 61개는 Windows checkout의 CRLF 차이뿐입니다.
- Claude 게시 감사는 3회 실행했고 최종 전달 감사04를 별도로 추가했습니다. 앞선 두 결과([01 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-publication-audit/audit.json),
  [02 · 비공개 원본](https://github.com/eunsu7997/kubellm-ops/blob/master/evidence/ops-safety-closure-20261021/final-publication-audit-02/audit.json))는 수정하지 않았습니다.
  01은 checkout CRLF 차이를, 02는 위 110개와 01 스크립트 사본의 정규식 문자열을 실패로 분류했고, 03에서 명시적으로 구분했습니다.
- 동결 소스 50개: quality-final hash·HEAD 대비 불일치 0. staged 변경은 README/HANDOFF/docs/새 evidence 경로뿐입니다.
- 로컬 Markdown 링크 555개 대상 존재(누락 0, 디렉터리 링크 허용), YAML 48개 파싱 실패 0.
  anchor 0개·외부 URL 16개는 네트워크/anchor 검증하지 않았습니다.
- 강한 credential 패턴(개인키·인증서 블록·토큰·JWT·API key 등) 후보 0건, tracked cache/build/tmp/kubeconfig 파일 0개.
  경로·줄만 기록하는 제한 스캔이며 보안 인증이 아닙니다.
- `.gitattributes`의 새 evidence 경로는 `-text`와 CR-at-EOL whitespace 정책으로 bytes를 보존합니다.
  정책 이전 표준 staged check exit2와 이후 exit0 기록은 그대로 보존했습니다.

## 남은 한계와 다음 작업

| 항목 | 남긴 이유 / 다음에 필요한 증거 |
|---|---|
| readiness timeout 구성 요소 | JIT·부하·host 경합을 분리하는 통제 실험 필요. timeout 값을 늘려 성공을 꾸미지 않음 |
| 120분 관측 provenance | 시작 소스 hash 미기록. 이후 실행은 hash를 기록하지만 소급 보증하지 않음 |
| local-ops raw 110개의 과거 Git blob | 역사적 blob은 LF 정규화본으로 유지. 원본 bytes는 별도 ZIP134개 entry의 baseline hash 일치로 보완했으며 과거 경로/이력은 바꾸지 않음 |
| 깨끗한 일정 부하 수용 | 미전송/ready flaps가 있어 운영 허용량·SLO 달성 미입증 |
| warmup 절차 | 1회 순차 대조만 있음. 표준 절차·일반 해결책으로 채택하려면 반복 통제 실험 필요 |
| 포화/최대 용량/OOM | 이번 안전 관측의 범위 밖. 부하 램프만으로 GPU 포화 주장 금지 |
| fresh GPU 호스트/캐시 없는 설치 | 독립 장비/캐시 환경 필요 |
| off-host 백업·재해복구 | 외부 저장 대상과 접근/전달 정책 필요. 현재는 같은 호스트의 복원 |
| 전력/DCGM profiling | WSL N/A 한계, 지원 장비/환경에서 별도 검증 필요 |
| Windows 전체 재부팅·자동 시작 | Docker Desktop/WSL 재시작 증거와 구분. restart 정책은 변경하지 않음 |
| multi-node·독점 GPU 격리 | 단일 물리 GPU time-slicing 증거로 대체 불가 |
| 외부 알림·검토자 공유 | 로컬 receiver만 사용. 새 공유 권한·외부 메시지는 실행하지 않음 |
| SLO-Guard 확장 | 사용자 요청으로 취업용 마감 후 **별도 다음 단계**. 현재 성과로 포장하지 않음 |

## 채용용 소개

**한 줄:** RTX4060 기반 Qwen LLM 서빙을 Docker와 WSL k3s에서 실행하고, Gateway·관측·장애 복구와 검증 도구의 판정 한계까지 원본 증거로 점검한 AI 인프라 프로젝트.

**30초 설명:** FastAPI Gateway와 Qwen vLLM을 Docker와 WSL k3s에서 실제 실행했습니다.
정상 응답뿐 아니라 GPU 슬롯 부족, 모델 실패, API 정지와 Pod 삭제 후 scheduler·probe·응답 복구를 확인했습니다.
추론 정지를 놓치는 HTTP health의 한계를 발견해 추론 readiness를 적용했고, 새 vLLM 프로세스의 첫 부하에서 timeout이 남는 사실과
JIT 경고와의 상관을 진단해 공개했습니다. 실험 도구의 안전 종료와 SSE 완료 판정을 보완하고,
2시간 관측이 엄격 기준에서 실패한 이유까지 원본 증거로 남겼습니다.
단일 공유 GPU의 제한된 관측이며 최대 용량이나 production readiness를 검증했다고 주장하지 않습니다.
