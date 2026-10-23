# 운영 실험 도구 안전 검수 · 2026-10-22

상태: **마감(2026-10-22 KST)**. 모든 실제 부하·진단을 마치고 stock으로 복원했습니다.
종합 판정·12개 항목 대응은 [야간 최종 보고](reports/2026-10-22-overnight-observation.md)를 따릅니다.

## 기존 결과에서 정정한 해석

- 과거 CSV 15개를 네트워크 호출 없이 다시 분석했습니다. 원본 CSV/summary/log는 수정하지 않았습니다.
- queue 실험의 timeout 49건은 모두 HTTP 200 헤더 뒤의 클라이언트 timeout입니다. 서버 HTTP 오류 49건이라고 단정하지 않습니다.
- 60초 유입 cohort의 drain 이후 완료까지 더해 60으로 나누면 지속 처리량을 과장할 수 있습니다.
  아래 두 분모는 서로 다른 관측을 설명하며 어느 쪽도 최대 용량을 증명하지 않습니다.

| Offered req/s | 60초 안 완료 / 60초 | 전체 성공 cohort / drain 포함 관측초 |
|---|---:|---:|
| 32 | 28.267 | 29.104 |
| 64 | 24.500 | 22.546 |
| 100 | 30.550 | 31.273 |
| 150 | 26.583 | 27.797 |

과거 success는 SSE `[DONE]` 필수가 아니었습니다. 기존 raw CSV만으로 각 스트림의 DONE을 소급 증명할 수 없습니다.
새 결과에는 `success_requires_sse_done=true`를 기록하며 content와 DONE을 모두 요구합니다.
분석 결과·각 원본 SHA256 (비공개 원본 `evidence/ops-safety-closure-20261021/historical-load-reanalysis.json`).

## 안전 하네스

- 독립 GPU 센서: `nvidia-smi` 3초 timeout, 2초 주기, heartbeat 8초.
- 80°C 이상, 여유 512MiB 미만, 센서 누락/NaN/오류는 부하 시작 차단 또는 부하 중단입니다.
- hard wall은 실행 시간+45초입니다. HTTP phase timeout과 별도로 감독합니다.
- 완료 순서 기준 전송 요청 실패 3회 연속이면 중단합니다. 미전송은 실패 순서를 리셋하지 않습니다.
- Windows에서는 부하 생성기의 자식 프로세스 트리까지 종료합니다. vLLM/Gateway 프로세스를 종료하는 장치가 아닙니다.
- vLLM 관측 누락도 중단하고, ramp는 미전송 발생 시 증가하지 않습니다.
- phase 통과는 전송 실패 0·미전송 0·성공 응답 1개 이상·유효 관측·실제 E2E 복구를 요구합니다.
  readiness 비정상/경보는 별도로 공개하며, 전송 phase 통과만으로 readiness 안정성을 주장하지 않습니다.
- JSONL은 각 요청 후 flush합니다. 프로세스 중단 시 부분 증거를 남기지만 전원 장애까지 fsync 영속성을 보장하지 않습니다.

하네스의 센서 장애·heartbeat·deadline·연속 실패 검증은 **합성 센서 + 실제 격리 Python 프로세스**입니다.
GPU 자체 고장이나 서비스 장애를 발생시킨 증거로 분류하지 않습니다.
현재 소스 hash 포함 검증 7종 · Windows worker 시작·트리 종료 포함 (비공개 원본 `evidence/ops-safety-closure-20261021/harness-source-hashed-final/verification.json`).

## 실제 90초 회귀 관측

1 req/s Poisson, cap 8, 입력 256단어, 최대 128 tokens, 현재 private Docker vLLM을 사용했습니다.
93건 content+DONE, 미전송 9건, 전송 실패 0건, readiness 비정상 3표본, `GatewayInferenceNotReady` 관측입니다.
GPU 최대 64°C·최소 여유 2623MiB, 종료 뒤 ready/chat/SSE 복구를 확인했습니다.
이 3회는 하네스의 readiness 표본입니다. 선별 Gateway 접근 로그에는 다른 호출도 포함돼 503이5줄이며 분모가 다릅니다.
당시 launcher의 `ok=true`는 이후 미전송 불허 기준 강화 전 결과입니다. **현재 기준의 깨끗한 phase 통과로 재분류하지 않습니다.**

120분 낮은 부하 관측은 완료됐고 **CLI exit1·엄격 phase FAIL**입니다. 예정 7,197건 중 content+DONE 성공 7,190·미전송 7·전송 실패 0,
readiness 200×1,263·503×1, `GatewayInferenceNotReady` pending 1표본, GPU unsafe 표본 0·최고 63°C·최소 여유 2,362MiB, 실제 E2E 복구입니다.
CSV/JSONL/summary/guard의 ID·범주·상태는 일치했습니다.
이 실행의 시작 이벤트에는 소스 hash가 빠져 있어 실행 코드 버전의 동일성은 **NOT VERIFIED**이며,
raw CSV·summary·supervisor result·복구 이벤트를 대조해 현재의 명시적 기준으로 별도 판정했습니다.
독립 판정 (비공개 원본 `evidence/ops-safety-closure-20261021/long-soak-independent-verdict.json`).
이후 실행부터는 시작 이벤트에 Python 버전과 하네스/생성기/관측 helper의 phase 시작 시 디스크 SHA256을 기록합니다.
실행 도중 소스를 편집하지 않고 해당 bytes와 Git의 줄바꿈 정규화를 대조해야 코드 대응을 설명할 수 있습니다.

회귀 시각의 실제 vLLM 로그에는 추론 중 Triton JIT 컴파일 경고 4건이 있습니다. 지연 단서이며 503의 인과 원인으로 확정하지 않습니다.
503은 경고 이전과 이후에도 있었습니다. 이 회귀 시점에는 통제 실험이 없었고, 이후 진단 결과는 아래 "JIT 진단 결과와 계속 남는 한계" 절에 있습니다.
선별 로그·실제 조회 명령/exit (비공개 원본 `evidence/ops-safety-closure-20261021/warmup-observed-log-excerpts.json`).
설치된 고정 이미지 소스에서는 이 경고가 `jit_post_compile_hook`에서 호출되고,
실제 경고 줄141은 `warning_once`입니다. 따라서 경고 시각은 컴파일 시작 시각이 아니며
경고가 없는 구간을 컴파일 없음으로 해석할 수도 없습니다.
실제 구현 발췌/hash (비공개 원본 `evidence/ops-safety-closure-20261021/vllm-jit-warning-source.json`) ·
호출부 (비공개 원본 `evidence/ops-safety-closure-20261021/vllm-jit-warning-callsites.json`).

## 복구 스크립트

기존 컨테이너만 순서대로 시작합니다. 모델이 멈춰 있으면 실제 k3s GPU Pod 또는 조회 불명 상태를 차단하고,
paused/restarting 상태와 외부 인터페이스 포트 게시도 차단합니다. 완료 후 ready/chat/SSE와 관측 서비스의 실제 응답을 확인합니다.
현재 모두 running 상태에서 이 검증은 통과했습니다. 새 conflict 차단 분기는 offline fixture 테스트이며,
이번에 실제 GPU 중복 점유를 만들거나 Docker Desktop 재시작을 반복하지 않았습니다.
실제 실행 중 상태 검증 (비공개 원본 `evidence/ops-safety-closure-20261021/restore-running-verification.txt`).
이 파일은 추가 host-network/legacy gate 이전 버전의 실행 기록입니다. 현재 cold-start 차단 gate를 실제 발동한 증거로 사용하지 않습니다.

추가 차단은 host network·vLLM host port·실행 중인 legacy 모델·GPU 센서 실패도 포함합니다.
센서 여유 기준은 모델 시작의 전체 메모리 적합성 계산이 아니며 다른 GPU 프로세스의 격리도 보장하지 않습니다.
queue 실험 helper는 PID 생성만으로 armed라고 하지 않고 실제 대기 진입 JSON을 확인하며,
취소 파일을 읽고 종료했다는 acknowledgement를 확인합니다. Docker를 대체한 격리 Python 명령으로 검사했고,
실제 사용은 아래 JIT 진단 1회(취소 ack exit0)뿐입니다. queue fault 실험을 새 helper로 반복하지는 않았습니다.

## 사용법과 stop/go

프로젝트 루트 PowerShell 기준입니다. 기본 명령은 dry-run이고 생성되는 증거 폴더는 매번 새 경로를 사용합니다.

```powershell
# 센서와 Docker를 건드리지 않는 계획 확인
.\.venv\Scripts\python.exe scripts\ops\exp07_load.py soak --minutes 30

# 합성 센서 + 격리 Python 프로세스 자체 검증
$trialStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$guardPath = "evidence/ops-safety-closure-20261021/manual-guard-$trialStamp"
.\.venv\Scripts\python.exe scripts\ops\verify_safety_harness.py --output-dir $guardPath --execute
if ($LASTEXITCODE -ne 0) { throw 'Guard self-test failed; do not send load' }

# 실제 serving이 사용 중인 단일 GPU와 공유되므로 승인된 유지보수/측정 시간에만 실행
.\.venv\Scripts\python.exe scripts\ops\exp07_load.py warmup --execute
if ($LASTEXITCODE -ne 0) { throw 'Warmup not qualified; inspect outcomes before another trial' }
.\.venv\Scripts\python.exe scripts\ops\exp07_load.py soak --minutes 30 --execute
# exit 0 여부뿐 아니라 미전송·readiness·경보·GPU 표본과 recovery 이벤트도 확인
```

위 30분 예제 자체를 이번에 실행했다는 뜻은 아닙니다. 실제 이번 관측의 명령/시간은 해당 evidence를 따릅니다.
raw `requests.csv`, `summary.json`, 요청별 `*-outcomes.jsonl`, `*-monitor.jsonl`,
`*-supervisor/result.json`, `events.jsonl`을 함께 읽습니다. 중단 시 summary가 없을 수 있으며 JSONL 부분 결과를 보존합니다.
다음 실행으로 덮어써서 실패를 지우지 않습니다. Windows Ctrl+C는 부하 트리를 정리하고 130으로 종료하며,
강제 전원 종료·부모 프로세스 강제 kill까지 복구를 보장하지 않습니다.

```powershell
# 기본 dry-run: 컨테이너 상태와 순서만 확인
.\.venv\Scripts\python.exe scripts\ops\restore_stack.py
# 승인된 복구 시에만: 기존 private 컨테이너 start, 재생성/자동 설정 변경 없음
.\.venv\Scripts\python.exe scripts\ops\restore_stack.py --execute
```

복구 exit는 0(검증 통과), 2(사전 차단), 3(start 실패), 4(vLLM health 기한 초과), 5(E2E/관측 미충족)입니다.
외부 Docker daemon/inspect 예외가 발생하면 별도 예외 exit가 날 수 있고 성공으로 처리하지 않습니다.
stopped 모델의 k3s 조회가 불명하면 차단합니다. Kubernetes를 쓰지 않는 다른 호스트용 범용 복구 도구가 아닙니다.
Docker Desktop 자동 실행이나 컨테이너 restart 정책은 설정하지 않습니다.

## 독립 검수와 품질

Claude Code는 Read/Glob/Grep만 허용한 읽기 전용 모드로 6차례 검토했습니다(5·6차는 아래 JIT 진단 준비 검수).
코드 실행·수정·개인 파일 조회를 맡기지 않았으며, Claude의 정적 의견을 실제 실험 성공으로 대체하지 않았습니다.
1차 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-01.json`) ·
2차 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-02.json`) ·
3차 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-03.json`) ·
delta 검수 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-04.json`).

주요 반영: 관측 thread 실패 전파, 전송 성공 0/미전송 pass 방지, 생성기 트리 종료, helper armed/cancel 확인,
오래된 서버측 실패·처리량 해석 정정, 코드 provenance 한계 공개와 이후 실행의 hash 기록입니다.
코드 동결 시점(당시) 로컬 223 passed·1 skipped였습니다(기록 (비공개 원본 `evidence/ops-safety-closure-20261021/quality-code-freeze/verification.json`)).
진단/lease 보완 후 **모든 부하 종료 뒤 최종 로컬 품질은 237 passed·1 skipped(Windows POSIX mode 시험)·206 deprecation warnings**,
Ruff·`git diff --check` exit0입니다. Linux CI(commit `00ded7d`, run 39641353623)는 237 passed·1 skipped입니다.
최종 명령·exit·검증 소스 hash (비공개 원본 `evidence/ops-safety-closure-20261021/quality-final/verification.json`).

처음의 전체 staged `git diff --check`는 새 Windows 원문 CRLF를 trailing whitespace로 분류해 exit2였습니다.
소스/문서의 표준 검사는 exit0이고 원문은 재작성하지 않았습니다. 새 evidence 경로에만 `.gitattributes`로
CR-at-EOL을 유효하게 처리하는 whitespace 정책을 명시했습니다. 나머지 trailing blank/EOF/space-before-tab 검사는 유지합니다.
정책 변경 전 검토·별도 검사 (비공개 원본 `evidence/ops-safety-closure-20261021/staged-whitespace-review.json`).

자체 재검수에서 과거 one-off 분석기 두 개가 기존 파생 요약을 덮어쓸 수 있던 경로도 차단했습니다.
기존 report가 있으면 분석/네트워크 호출 전에 거부하며 최종 생성은 exclusive 모드입니다.
실제 기존 경로 거부·전후 hash 일치 (비공개 원본 `evidence/ops-safety-closure-20261021/archived-analysis-refusal.json`).
새 보고에는 과거 분석기의 명목 비율을 재사용하지 말고 `reanalyze_load.py --output NEW_PATH`를 사용합니다.

## JIT 진단 결과와 계속 남는 한계

readiness 구성 요소를 좁힐 추가 진단을 120분 관측 종료·실제 부하 프로세스 0 확인 뒤 **1회 실제 실행**했습니다.
설치된 이미지에서 지원을 확인한 `--jit-monitor-verbose`만 임시 추가해 vLLM 컨테이너 하나를 재생성했고(image/model/revision/GPU/메모리 동일),
같은 seed 6999·1req/s·cap8·90초를 두 번 관측한 뒤 stock args로 다시 재생성해 복원했습니다.
controller는 실행 중인 부하 프로세스 조회·최신 run 종료 기록·exclusive lease를 함께 확인하고, helper 실제 armed/stock identity/E2E/cancel acknowledgement를 요구합니다.
지원하는 일반 load CLI도 해당 lease가 있으면 새 부하를 거부합니다. PID/lock 파일 존재만으로 작업 종료를 추정하지 않습니다.

| 구간 | 성공 / 미전송 | readiness | timeout counter | JIT 경고 | TTFT p95 | 엄격 phase |
|---|---:|---|---:|---:|---:|---|
| 재생성 직후(최소 ready/chat 확인 뒤) | 93 / 9 | 200×12·503×4 | +3 | 12건·5종 kernel | 4.420초 | FAIL |
| 같은 프로세스 warm 대조 | 102 / 0 | 200×17 | +0 | 0 | 0.083초 | PASS |

수집/복원 CLI는 exit0입니다. 복원 시 stock image/args/GPU Count=-1을 helper 취소 전에 확인했고, 실제 ready/chat/SSE 성공,
helper 취소 acknowledgement exit0, ignored `.runtime/gpu-diagnostic.lock` 제거를 확인했습니다. 지연된 helper는 남아 있지 않습니다.
11개 소스 snapshot/hash는 디스크·Git과 CRLF 정규화만 허용한 비교에서 일치했고 실행 중 소스는 동결 상태였습니다.
독립 비교 (비공개 원본 `evidence/ops-safety-closure-20261021/jit-diagnostic-independent-comparison.json`).

stock(상세 로그 없음) 복원 직후 별도 source-hash 기록 90초 회귀는 89 성공·13 미전송·503 3표본·CLI exit1이었고,
사전 선언한 1회 warm 대조(재생성 없음)는 102 성공·0 미전송·17/17×200·CLI exit0이었습니다. 이후 부하는 모두 중단했습니다.
stock 비교 (비공개 원본 `evidence/ops-safety-closure-20261021/stock-cold-warm-independent-comparison.json`).
새 프로세스 첫 부하와 JIT의 상관을 관측했지만 모든 503의 원인 확정·일반 warmup 해결책 증명은 아닙니다.
상세 로그가 지연에 영향을 줄 수 있고, 최소 ready 확인 이후의 새 프로세스를 cold로 부르므로 fresh host/cold cache나 인과 증명으로 확대하지 않습니다.
사전 계획 (비공개 원본 `evidence/ops-safety-closure-20261021/jit-diagnostic-plan.json`) ·
[진단 controller](../scripts/ops/exp08_jit_readiness.py) ·
5차 검수 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-05-jit.json`) ·
6차 검수 (비공개 원본 `evidence/ops-safety-closure-20261021/claude-review-06-jit.json`).

readiness timeout의 구성 요소 원인(PARTIAL), 120분 관측의 실행 코드 provenance, 장기 안정성, 통제된 포화·최대 용량, fresh GPU 호스트 재현,
off-host 백업, Windows 재부팅, multi-node, WSL 전력 측정, production readiness는 미검증입니다.
서비스 자동 시작 정책과 외부 알림·공유 권한 변경, SLO-Guard 확장은 이번 범위에 포함하지 않습니다.
기존 WSL k3s GPU 서빙 성공의 범위는 [실제 Kubernetes 검증](KUBERNETES-LIVE-VALIDATION.md)을 따릅니다.
