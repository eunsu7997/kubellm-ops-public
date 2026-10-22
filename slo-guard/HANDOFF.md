# SLO-Guard 인수인계 (2026-10-20 갱신, 학원 PC → 다음 PC)

## 현재 재개 · 2026-10-22

취업용 마감 후 사용자가 나머지 작업 재개를 지시했습니다. [현재 README](README.md)의 평가 기반 단계가 최신입니다.
실패-inclusive SLO 분석기와 기존5개 자료 재분석, 안전 하네스 아래 실제 혼합 요청12건을 확인했습니다.
정책 강제·용량 C·개선 효과는 여전히 미검증입니다. 아래 보류/컨테이너 이름/109 tests 등은 날짜가 있는 과거 snapshot이며 실행 지시가 아닙니다.

## 우선순위 변경 — 사용자 요청 2026-10-21

**SLO-Guard 추가 실험·용량·정책 작업은 보류한다. 취업용 KubeLLM-Ops 마감이 먼저다.**
기존 실행 자료와 서비스는 유지한다. 마감 결과 보고에서 사용자에게 SLO-Guard를 다음 확장으로
추가하자고 알리고, 별도 재개 지시 전 부하·모델·서비스 변경을 하지 않는다.
마지막 A는 15건 성공(워밍업 4·측정 11), B 27건은 GPU free baseline 복귀 gate에서 건너뛰었다.
[당시 결과](capacity/20261021-low-rate-progression-01/README.md). 10MiB 차이는 OOM·GPU 부족 판정이 아니다.

## 집 PC 후속 결과 · 2026-10-21

- 기존 private vLLM과 모니터링을 유지한 채 사용자 승인으로 Gateway만 스트리밍 이미지로 교체했다.
- 실제 SSE smoke: HTTP 200, content delta와 [DONE], 첫 청크 histogram count 0→1 확인.
- 0.5 req/s·10초·seed 42·max-in-flight 1·max-tokens 16의 파일럿: 6건 예정,
  5건 성공(78 생성 토큰), 1건 dropped_client_cap(서버에 보내지 않음), 최종 count=6.
- GPU 최대 관측 온도 64°C, Gateway/vLLM scrape UP. **GPU 포화·용량 C·SLO 달성률은 미확정**이다.
- 후속 승인으로 같은 6건·seed의 상한만 4로 바꿔 실행: **6건 성공·미전송 0건**,
  count 6→12, 최고 59°C. [비교 원자료](capacity/20261021-streaming-pilot-02-cap4/README.md).
  성공-only p95는 128.82ms(5표본)→189.30ms(6표본); 지연 개선·성능 차이로 해석하지 않는다.
- 현재 `kubellm-gateway-streaming-candidate-20261021`이 localhost 18003을 사용한다.
  원래 `kubellm-gpu-secure-gateway-1`은 중지·보존. 동시에 시작하거나 전체 Compose up을 하지 않는다.
- [실제 원자료와 한계](capacity/20261021-streaming-pilot-01/README.md),
  [승인한 교체·롤백 계획](tasks/home-streaming-validation-20261021.md).
- 아래 10/20 설계·검증 기록은 당시 기준이다. 후속 준비 계획은 실제 실행으로 간주하지 않는다.

SKT ALEPH 아이디어 해커톤(2026-11-17~18)과 이후 논문을 위해 KubeLLM-Ops 위에 **SLO-Guard**를 만들고 있다.
코드는 dev-supervisor(Claude 개발 → Codex 검수 → Claude 재검수)로 과제 단위로 개발하고, 사람이 검토한 뒤 반영한다.

표기: **[검증]** 실제로 실행해 확인함 · **[미검증]** 아직 실행하지 않음 · **[계획]** 앞으로 할 일이나 가설

## 1. 무엇을 만드나 (설계 요약)

GPU 1장(RTX 4060 8GB)에 요청이 몰릴 때, Gateway가 vLLM 상태를 보고 저우선 요청을 줄이거나 막아서
고우선 요청의 TTFT를 지키는 "LLM 서빙용 호 수락 제어". 통신망의 호 수락 제어(CAC)/QoS를 LLM 서빙에 옮긴 것이다.

- 판단 근거: vLLM `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`(둘 다 vLLM 0.30.0에서 이름 확인됨) + Gateway가 잰 TTFT p95
- 결정: 통과 / 대기 / max_tokens 축소 / 429 + Retry-After. 결정마다 근거 지표값을 로그로 남긴다(설명은 LLM이 아니라 템플릿)
- 사람 승인: 차단은 자동(되돌릴 수 있음), 임계값·vLLM 설정 변경만 서명 승인
- 비교 정책 4개 [계획]: P0 제어 없음 / P1 vLLM 내장 우선순위 스케줄링 / P2 고정 임계 차단(Gateway API Inference Extension식, KV 80%·대기 5) / P3 SLO 피드백 Guard
- 실험 [계획]: 4정책 × 부하 3단계(측정 용량 C의 0.8·1.2·1.5배) × 각 10회. 원자료 CSV 전부 보관, 실패 실행 포함
- **주지표: 고우선 SLO 달성률** [계획, 사전 등록 때 확정]
  - 정의: 측정 구간(phase=measure)의 **고우선 요청 전체** 중 `category=success`이고 `ttft_s ≤ SLO`인 요청의 비율
  - 분모에는 429·timeout·stream_interrupted·dropped_client_cap 등 **실패도 모두 포함**한다. 원자료는 `open_loop.py`의 `requests.csv`
  - 이유: `summary.json`의 TTFT 백분위수는 **성공 요청만으로** 계산한다. 과부하에서 실패한 요청이 빠지면, 요청을 많이 실패시킨 정책이 오히려 좋아 보이는 생존자 편향이 생긴다(2026-10-20 점검에서도 고우선 21건 중 429 5건이 p95에서 빠졌다)
  - 보조지표: 고우선 TTFT p95(실패를 무한대로 보고 계산. 실패가 5%를 넘으면 p95는 "SLO 위반"으로 표기), 전체 goodput, 저우선 차단·축소 비율
- 가설 초안 [계획, 수치는 용량 곡선 측정 후 사전 등록 커밋에서 확정]
  - H1: 1.5C에서 P3의 고우선 SLO 달성률이 P0보다 X%p 이상 높다
  - H2: 1.5C에서 P3의 전체 goodput ≥ P0
  - H3: P3의 고우선 SLO 달성률이 P1(vLLM 내장 우선순위)보다 Y%p 이상 높다 (기각되면 "내장 기능으로 충분"이 결론)
  - H4: 짧은 프롬프트로 맞춘 임계값을 처음 보는 긴 프롬프트 트래픽에 써도 P3는 H1 개선폭의 50% 이상 유지
  - 안전 조건: 0.8C에서 Guard 차단 0건, 고우선 TTFT p95 증가 ≤ 5%
- 주의 [검증, 2026-10-11 기동 로그]: Qwen2.5-0.5B·gpu-memory-utilization 0.4에서 KV 캐시 107,008토큰(2048토큰 요청 52.25개 동시). 짧은 요청으로는 KV 포화가 안 오므로, 용량 곡선을 재고 과부하 조건(긴 프롬프트, 1.5B 모델, KV 상한 등)을 정한 뒤 사전 등록한다.

## 2. 지금까지 한 것

| 커밋 | 내용 | 상태 |
|---|---|---|
| `329d83c` | `stream=true` 요청을 SSE로 그대로 전달, 첫 청크까지 시간 지표 `gateway_time_to_first_chunk_seconds` | [검증] 과제 `tasks/task-01-streaming.md` |
| `526b2ae` | 클라이언트 연결 끊김을 `gateway_client_disconnects_total`로 분리(백엔드 장애 `stream_error`와 구분), 비밀값 모양 테스트 문자열 교체 | [검증] 과제 `tasks/task-01b-stream-metrics.md` |
| `b36a0b4` | 개방형 스트리밍 부하 생성기 `scripts/load-test/open_loop.py` + 테스트 54개 | [검증] 과제 `tasks/task-02-open-loop-load.md` |

- [검증] 2026-10-20 `test-image` 컨테이너(네트워크 없음, 읽기 전용)에서 `ruff check .` 오류 0, `pytest` 전체 109개 통과
- task-01b의 마지막 수정본은 예산 상한 때문에 AI 검수를 받지 못했다. 대신 diff를 사람이 검토하고 위 테스트로 확인한 뒤 반영했다.
- [후속 검증 10/21] 실제 vLLM 스트리밍 smoke와 6건 파일럿을 실행했다. 용량 곡선·Guard 정책 검증은 미완료다.

## 3. task-02 부하 생성기 (완료)

- dev-supervisor 판정 "완료"(20/20, 차단 0). AI 호출 8회
- [검증] 실제 소켓 점검(2026-10-20, 컨테이너 안 vLLM 흉내 SSE 서버, 외부 네트워크 없음, 5초·10 req/s·버스트 2.5배)
  - dry-run은 계획만 출력하고 파일을 만들지 않음
  - 결과 분류: 측정 구간 54건 = 성공 46 + 429 8(의도한 대로)
  - TTFT 약 52ms(서버 지연 50ms), TPOT 약 25ms, 토큰 6개가 정확히 기록됨
  - 예정 시각 대비 전송 지연 최대 1.9ms, 같은 시드는 같은 일정, 출력에 프롬프트 문장 없음
- 사용 메모
  - `--execute`가 없으면 계획만 출력한다. 예정 요청 300개 초과, 최대 순간 도착률 30 req/s 초과, `--max-in-flight` 64 초과는 `--allow-large-plan`이 있어야 한다
  - 실패가 있어도 실행이 끝까지 돌면 종료 코드 0이다. 결과는 `requests.csv`(요청별)와 `summary.json`(요약)
  - `send_lag_s`가 커지면 부하 생성기 자체가 밀린 것이다. GPU와 같은 PC에서 돌리므로 매 실행마다 최댓값을 확인한다
  - TTFT 백분위수는 성공 요청만으로 계산한다. 논문 주지표는 1절의 SLO 달성률로 `requests.csv`에서 따로 계산한다

## 4. 새 PC 준비

```powershell
# 1) 저장소. 일부 PC는 Git 시스템 설정이 줄바꿈을 CRLF로 바꾸므로 반드시 LF로 받는다
git -c core.autocrlf=false clone https://github.com/eunsu7997/kubellm-ops.git <LOCAL_USER>\Desktop\kubellm-ops
cd <LOCAL_USER>\Desktop\kubellm-ops; git config core.autocrlf false

# 2) Docker Desktop을 켠 뒤 테스트 이미지 빌드(패키지는 빌드 때만 내려받음)
docker build -t kubellm-test:2 slo-guard/test-image
docker images --digests kubellm-test          # 나온 sha256 값을 아래 <DIGEST>에 쓴다. PC마다 값이 다르다

# 3) 오케스트레이션과 로그인 확인
git clone https://github.com/eunsu7997/dev-supervisor.git <LOCAL_USER>\Desktop\dev-supervisor
claude auth status        # loggedIn true, authMethod claude.ai
codex login status        # Logged in using ChatGPT
```

이미지 확인: `docker run --rm --network none kubellm-test:2 python -c "import fastapi, httpx, pytest, yaml"`가 오류 없이 끝나야 한다.

## 5. 과제 실행 방법 (dev-supervisor)

이 Claude 대화 창 안이 아니라 **별도 PowerShell 창**에서 실행한다. 아래는 형식 예시다(task-02는 이미 끝났다). `<과제>`와 `--writable`은 과제마다 바꾼다.

```powershell
cd <LOCAL_USER>\Desktop\dev-supervisor
python -B supervisor.py run --project <LOCAL_USER>\Desktop\kubellm-ops --task <LOCAL_USER>\Desktop\kubellm-ops\slo-guard\tasks\<과제>.md --docker-image kubellm-test@sha256:<DIGEST> --writable <수정할 파일> <새 테스트 파일> --test "python -m ruff check --no-cache . && python -m pytest -q" --max-usd 5 --max-rounds 2
```

1. `APPROVE` 입력(전송 파일 목록 확인 후)
2. 체크리스트에서 과제의 검증 기준이 다 들어 있는지 보고 Enter (빠졌으면 `e`로 고친 뒤 진행)
3. 끝나면 `runs\<실행폴더>\REPORT.md` 확인
4. `python -B supervisor.py apply --run runs\<실행폴더>` 로 미리보기 → 화면에 나온 **실제 plan_binding 값**으로 `--yes --plan-binding <값>`
5. `git status`로 바뀐 파일 확인 → 커밋 → 검토 후 푸시. 마음에 안 들면 `rollback --run runs\<실행폴더> --yes`

## 6. 이번에 배운 운영 규칙

- **dev-supervisor는 이미 있는 테스트 파일을 고치면 무조건 차단한다**("편법 통과 의심", 예외 옵션 없음). 새 테스트는 항상 **새 파일**에 쓰게 하고, 그 파일을 `--writable`에 넣는다.
- `--test`에 ruff를 같이 넣어야 검수자가 ruff 통과를 증거로 인정한다. 순서는 ruff 먼저, pytest 나중(supervisor가 끝에 `-p no:cacheprovider`를 붙임).
- 실행당 Claude 비용(구독 한도 환산): task-01 약 $2.35, task-01b 약 $2.60, task-02 약 $4.71. 저장소 전체(150여 개 파일)를 읽기 때문이다. 상한이 개발 직후·검수 직전에 걸리면 마지막 수정본이 검수 없이 남으므로 `--max-usd 5 --max-rounds 2` 정도로 넉넉히 둔다.
- 반영 미리보기에서 대상 파일 목록을 꼭 본다. 사본을 만든 뒤 원본에 새로 넣은 파일(예: `slo-guard/`)은 반영 대상이 아니어서 건드리지 않는다(task-02에서 확인).
- `--light`는 작은 과제에만 쓴다.
- 기본 이미지 `supervisor-py:1`에는 fastapi가 없고, `--deps`는 루트에 `requirements.txt`가 있어야 해서 이 저장소에는 안 맞는다. 반드시 `--docker-image`로 `test-image`를 쓴다.
- 보고서가 "미완료"여도 이유를 읽고 직접 재검증하면 반영할 수 있다(01b가 그 경우).

## 7. 다음 할 일 (순서대로)

1. ~~task-02 부하 생성기~~ 완료(`b36a0b4`)
2. **실제 GPU 용량 곡선 측정** [계획, 사람이 직접. AI 과제 아님]
   - vLLM을 띄우고 Gateway를 거쳐 `open_loop.py`로 도착률을 단계적으로 올린다(예: 1, 2, 4, 8, 12, 16 req/s, 단계마다 새 `--output-dir`)
   - 단계마다 고우선 SLO 달성률과 `send_lag_s` 최댓값, vLLM `num_requests_waiting`·`kv_cache_usage_perc`를 기록한다
   - 달성률이 무너지기 시작하는 도착률을 C로 정한다. 짧은 프롬프트로는 KV 포화가 안 오므로(1절 주의) `--prompt-words`·`--max-tokens`를 키운 조건도 잰다
   - 먼저 dry-run으로 계획을 보고, 30 req/s·300건을 넘으면 `--allow-large-plan`이 필요하다
3. 가설·임계값·분석 방법을 사전 등록 커밋(1절 주지표 정의 + C·SLO·X·Y 값)
4. [10/21 재검증] 현재 private vLLM은 호스트 포트 게시 없음, 8002/18002 리스너 없음.
   중지된 구형 공개 vLLM을 재시작하면 노출이 복원될 수 있다. 최신 Compose 재생성·롤백은 미검증.
5. task-03 Guard 정책 엔진: vLLM 지표 수집기, P0~P3 정책, 결정 로그, 지표 수집 실패 시 통과(fail-open)
6. 남은 사소한 것: `metrics.py`의 `LATENCY_BUCKETS =(` 띄어쓰기, 스트림 중 예상 밖 예외가 결과 카운터에 안 잡힘
7. 본 실험(120회) → 분석 → 시연 화면·3분 대본·녹화 백업 → 논문 초고·ZIP (11/15 동결 목표)

## 8. 확인 안 된 것

- 대회 공식 규정(사전 개발물 허용 여부, 주제 제한)과 심사 기준: 공개 자료에서 찾지 못함. **가장 먼저 확인할 것**
- 실제 소규모 스트리밍 파일럿 1회 수행. 반복 부하·용량·과부하·장애 실험과 Kubernetes GPU Pod·DCGM은 미검증
- Codex판 오케스트레이션(ALEPH-AUTO-COMMANDER v1.13)용 KubeLLM-Ops 등록 설정(`jobs/default.json`)은 학원 PC에만 있다. 지금은 dev-supervisor를 주력으로 쓴다
