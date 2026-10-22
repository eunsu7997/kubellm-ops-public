# 집 PC(RTX 4060)에서 Claude Code에 줄 프롬프트

kubellm-ops 폴더를 연 Claude Code 세션에 아래 블록을 그대로 붙여 넣는다.

```text
너는 SLO-Guard 프로젝트의 AI 인프라 실험 보조다. 이 PC는 RTX 4060이 있는 집 PC다.
먼저 slo-guard/HANDOFF.md를 끝까지 읽고 지금까지의 결정과 규칙을 따른다.

목표: (A) 실험 환경 준비, (B) 기존 vLLM 8002 포트 노출 정리, (C) 실제 GPU 용량 곡선 측정.
결과로 사전 등록에 쓸 용량 C와 TTFT SLO 후보를 근거와 함께 제안한다. 가설 수치는 내가 정한다.

지킬 규칙
- 실제로 실행해서 본 것만 VERIFIED로 적는다. 추정·계획은 NOT VERIFIED로 구분한다. 결과를 지어내지 않는다.
- 다음은 하기 전에 반드시 나에게 묻는다: 실행 중인 컨테이너 중지·삭제·재생성, 모델 다운로드, 드라이버·Docker 설정 변경, 부하 실행(측정 계획 전체를 한 번에 보여 주고 승인받아도 됨), git push.
- 기존 evidence/ 파일은 수정하지 않는다. 새 결과는 새 폴더에만 쓴다.
- 이상 신호(5xx·timeout 급증, GPU 온도 85도 이상, 메모리 부족, 다른 작업 간섭)가 보이면 즉시 멈추고 보고한다. 자동으로 재시도하거나 부하를 올리지 않는다.

(A) 준비
1. git pull --ff-only (줄바꿈 LF 유지: core.autocrlf=false). nvidia-smi로 GPU·드라이버를 확인한다.
2. Docker Desktop 상태 확인 후 HANDOFF.md 4절대로 slo-guard/test-image를 빌드하고 digest를 기록한다.
3. Docker 안에서 ruff check .와 pytest가 통과하는지 확인한다.

(B) 8002 노출 정리
1. 먼저 읽기만 한다: docker ps, 기존 vLLM 컨테이너의 포트 바인딩(HostIp)과 네트워크를 확인해 보고한다.
2. docs/docker-gpu-security-migration.md 절차와 docker-compose.vllm-secure.yml(필요하면 localhost override)로 전환하는 계획과 되돌리기 방법을 보여 주고, 내 승인 후 실행한다.
3. 전환 뒤 직접 추론, Gateway 경유 stream=true 추론(첫 청크 지표 증가), vLLM /metrics 접근을 확인한다.

(C) 용량 곡선 측정
1. 측정 도구는 scripts/load-test/open_loop.py(개방형, 시드 고정)를 Gateway 경유로 쓴다. 고우선 비율은 0.2로 고정한다.
2. 두 가지 프로파일을 잰다.
   - short: 짧은 프롬프트, 짧은 출력(예: --prompt-words 8 --max-tokens 64)
   - long: KV 압력을 주는 긴 프롬프트·긴 출력. 먼저 요청 1건으로 usage.prompt_tokens를 확인하고 프롬프트+출력이 max-model-len(2048)을 넘지 않게 값을 정한다.
3. 부하 전 유휴 상태에서 요청 몇 건으로 기준 TTFT를 잰다.
4. 도착률을 단계적으로 올린다(예: 1, 2, 4, 8, 12, 16 req/s, 단계당 워밍업 15초 + 측정 60초). 무너지는 지점 근처는 같은 시드로 3번 반복한다. 실행마다 새 --output-dir를 쓰고, 먼저 dry-run 계획을 보여 준다. 30 req/s·300건을 넘으면 --allow-large-plan이 필요하다.
5. 단계마다 기록한다: 고우선 SLO 달성률(HANDOFF.md 1절 정의, 분모에 실패 포함, requests.csv에서 계산), 고우선 TTFT p50·p95, 성공·실패 분류, send_lag_s 최댓값, 측정 전후 vLLM num_requests_waiting·kv_cache_usage_perc·num_preemptions(있으면), nvidia-smi 1초 샘플(사용률·메모리·온도).
6. SLO 달성률 계산은 작은 분석 스크립트(slo-guard/analysis/ 아래 새 파일)로 하고, 손으로 계산한 값과 한 건 이상 대조한다.
7. send_lag_s가 커지면 부하 생성기 쪽 병목이므로 그 단계 결과는 따로 표시한다.

결과물
- 원자료: slo-guard/capacity/<날짜>/ 아래 단계별 폴더(requests.csv, summary.json, 지표 스냅샷, nvidia-smi 로그)
- 요약: slo-guard/capacity/<날짜>/README.md에 단계별 표, 용량 C 후보, TTFT SLO 후보와 근거(예: 유휴 기준 TTFT 대비 배수), short와 long 프로파일 차이, 한계
- HANDOFF.md 2절·7절 갱신(무엇을 했고 무엇이 남았는지)
- 커밋은 해도 되지만 push는 내게 묻고 한다.

먼저 (A)부터 시작하고, 단계가 끝날 때마다 짧게 보고해 줘.
```
