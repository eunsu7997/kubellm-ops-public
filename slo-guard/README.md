# SLO-Guard · 현재는 평가 기반 단계

**2026-10-22 사용자 재개 지시 후:** 실패와 미전송을 포함하는 오프라인 SLO 평가기를 구현하고, 실제 저율 혼합 요청12건으로 회계 경로를 확인했습니다. **Gateway 차단·우선순위 보호 정책은 아직 구현/적용하지 않았습니다.** 취업용 KubeLLM-Ops 마감은 [기존 최종본](../docs/FINAL-DELIVERY-20261022.md)으로 보존합니다.

GPU 요청이 몰렸을 때 저우선 요청을 제한해 고우선 요청의 지연을 보호하는 것이 향후 목표입니다. 지금은 정책의 성과를 평가할 때 실패한 요청을 분모에서 빼는 오류부터 막았습니다.

## 실행 방법

저장소 루트의 기존 개발 환경(`requirements-dev.txt`)을 사용합니다. 분석 명령은 네트워크 요청을 보내거나 서비스를 바꾸지 않습니다.

```powershell
.\.venv\Scripts\python.exe scripts/load-test/evaluate_slo.py evidence/slo-foundation-20261022/priority-baseline/run
```

현재 baseline의 plan에는 실행 전에 예시 TTFT 목표1초를 기록했습니다. 실제 서비스의 SLA나 합의된 운영 SLO는 아닙니다.
원래 목표가 없던 과거 자료를 탐색하려면 명시적으로 시나리오 값을 넣습니다.

```powershell
.\.venv\Scripts\python.exe scripts/load-test/evaluate_slo.py evidence/ops-safety-closure-20261021/safety-fixed-soak-20261021T151847836001Z/soak --slo-ttft-s 1
```

`--output 새파일.json`으로 결과를 저장할 수 있고, 기존 파일을 덮어쓰지 않습니다. 유효한 분석은 exit0, 입력/계약/출력 경로 오류는 exit2입니다. **exit0은 실험이나 SLO 성공을 뜻하지 않습니다.** 임의로 GPU 부하를 시작하는 명령은 이 문서의 분석 명령에 포함하지 않습니다.

## 무엇을 계산하나

- 분모: `phase=measure`의 **예정 요청 전체**. 429·timeout·stream incomplete·client cap 미전송도 포함합니다. 실제 warmup 요청만 제외합니다.
- 분자: recorder가 content와 SSE `[DONE]`을 확인한 success 중 client first-content TTFT가 지정 기준 이하인 요청.
- `all` / `high` / `low` 각각 분모·분자·비율을 제공합니다. 고우선 표본0이면 `NO_SAMPLES`와 null이며 0%나100%로 꾸미지 않습니다.
- 성공-only p95와 **실패-inclusive p95**를 나눕니다. 후자는 실패를 무한대로 놓은 nearest rank `ceil(.95*N)`이며 결과가 무한대이면 JSON에는 `kind=unbounded`, `seconds=null`로 표시합니다. 성공-only는 기존 선형 보간 방식입니다.
- drain을 포함한 성공 cohort/명목 시간과 실제 측정 창 안에 완료된 요청/측정 시간을 별도로 표시합니다. 어느 쪽도 최대 용량이 아닙니다.
- CSV 전체 수·중복ID·seed 재현 일정·priority·phase·offset·상태/범주·유한 수치·성공 timing을 검증합니다. summary의 범주·우선순위·warmup·지연·goodput·send lag도 CSV 재계산과 대조합니다. 누락/불일치를 조용히 제외하지 않습니다.
- client 전송 지연의 최소/최대와 조기 전송 수를 priority별로 표시합니다. 현재 TTFT 기준은 실제 send부터이며 예정 도착부터의 지연이 아닙니다. 혼잡 시 send lag를 별도로 검토해야 합니다.
- `requests.csv`·`summary.json`·분석기·현재 일정 생성기 SHA256을 기록합니다. 현재 seed 재현은 과거 실행 시작의 코드 provenance를 소급 증명하지 않습니다.

과거 CSV의 `success`가 DONE을 필수로 하지 않았으면 기본 거부합니다. `--allow-legacy-success`는 탐색용 opt-in이며 `LEGACY_UNVERIFIED`와 null attainment/p95/rate를 유지합니다. 계산 가능한 legacy 수치는 `exploratory_classifier_metrics`에만 분리합니다. 원래 CSV에는 SSE event 원문이 없으므로 이 계약은 recorder 선언·알려진 설명·행 일관성을 신뢰하는 것이며 위조 방지나 과거 SSE 복원이 아닙니다.

Claude 읽기 전용 검수의 legacy 지표·summary 불일치·send lag 지적을 반영했습니다. 수정 전 분석은 보존했고 [현재 수정 후 재분석](../evidence/slo-foundation-20261022/reanalysis-v2/priority-baseline.json)을 최신으로 사용합니다. 쓰기 중 디스크 오류가 발생하면 부분 파일이 남을 수 있으며 자동 삭제/덮어쓰기는 하지 않습니다.

## 실제 결과

| 자료 | 1초 기준의 수치 | 해석 |
| --- | --- | --- |
| 새 실제 기준선 | 예정12·성공12·미전송0·전송실패0. 고우선10/10, 저우선2/2 | 20초·0.5req/s·cap4·16tokens·8단어. ratio=.5였지만 실제 seed 일정은10/2. 표본이 매우 작고 정책 미적용 |
| 기존120분 | 7,184/7,197 | 사후 시나리오. 고우선0건이라 고우선 SLO는 평가 불가. 기존 strict phase FAIL 그대로 |
| 기존 stock cold | 81/102 | 사후 시나리오. 실패·미전송도 분모 포함 |
| 기존 stock warm | 102/102 | 사후 시나리오. 순차 대조를 정책 개선 효과로 해석하지 않음 |
| 옛 cap1 / cap4 | classifier 기준5/6·6/6 | legacy 성공 계약. verified attainment는 null |

새 기준선은 예시1초 기준을 **실행 전에** 명시했으며 자동 임계값 탐색·재시도·부하 확대를 하지 않았습니다.
설정 warmup은2초지만 해당 seed의 실제 warmup 요청은0개였습니다. 기존 safety harness가 GPU80°C/여유512MiB/센서 오류/연속 전송 실패3회/65초 hard wall을 감시했고, 실제 소스 hash·CSV·journal 일치·Ready/chat/SSE·stock 구성을 확인했습니다. 모델·Gateway·관측 컨테이너를 재생성하지 않았습니다.

[원자료·실제 명령·검증](../evidence/slo-foundation-20261022/README.md) · [구현](../scripts/load-test/evaluate_slo.py) · [테스트](../tests/test_slo_evaluation.py)

## 남은 단계

1. 실사용자가 원하는 TTFT 목표·최소 달성률·저우선 비용·실패 budget을 먼저 정의합니다. 현재1초 예시는 운영 목표가 아닙니다.
2. 관측 누락/오래된 값 처리와 KV cache 지표 단위를 고정한 **shadow 정책**을 만들고, 결정만 기록해 실제 수락 경로와 분리 검증합니다.
3. 정책 off가 기본인 opt-in 수락 제어와 동시성 lease·취소/스트림 종료 반환·429/Retry-After를 검증합니다.
4. P0/P2/P3 같은 일정 비교를 사전 등록하고 안전 범위에서 반복합니다. 1회 기준선에서 성능 개선을 주장하지 않습니다.
5. 최대 용량·과부하 조건·일반화는 별도 실험입니다. OOM·서비스 중단·모델/driver 변경·새 GPU 점유를 임의로 하지 않습니다.

**NOT VERIFIED:** 우선순위 강제, 고우선 보호 효과, 정책별 개선, 용량 C, 포화/최대 처리량, production readiness.

최종 보완 후 로컬 전체 테스트는 **280 passed·1 skipped·206 warnings**, 평가기 테스트는43개입니다. [최신 quality-03](../evidence/slo-foundation-20261022/quality-03/verification.json)의 Ruff·diff도 통과했습니다. 야간237개·최초263개·중간279개 및 quality-01/02는 당시 snapshot입니다.
