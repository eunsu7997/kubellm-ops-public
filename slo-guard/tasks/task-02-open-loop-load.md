목표: 실제 GPU 용량 곡선 측정과 SLO-Guard 실험에 쓸 개방형(open-loop) 스트리밍 부하 생성기를 새 스크립트 scripts/load-test/open_loop.py로 만든다. (SLO-Guard MVP 2단계)

현재 문제: 기존 scripts/load-test/load_test.py는 고정 작업자 수로 도는 닫힌 루프(closed-loop) 방식이고 stream=false라서, 과부하 때 도착률을 유지하지 못하고 TTFT를 잴 수 없다. 기존 도구와 그 테스트는 그대로 둔다.

기대 동작:
- 도착 일정: 초당 도착률 --rate, 실행 시간 --duration-s, 시드 --seed로 포아송 도착 시각을 미리 계산한다. 같은 시드면 같은 일정이 나온다. 선택 옵션 --burst START_S DURATION_S MULTIPLIER가 있으면 그 구간만 도착률에 MULTIPLIER를 곱한다(구간별 일정한 도착률의 포아송).
- 개방형 전송: 각 요청을 정해진 시각에 독립 작업으로 보내고, 앞선 요청의 완료를 기다리지 않는다. 동시 진행 요청이 --max-in-flight에 도달하면 그 요청은 보내지 않고 dropped_client_cap으로 기록한다.
- 우선순위: --high-ratio 비율(0~1)만큼 요청을 같은 시드의 난수로 high, 나머지를 low로 정하고 헤더 x-priority: high|low를 붙인다.
- 요청 본문: {"model", "messages":[{"role":"user","content": 고정 단어를 --prompt-words개 반복한 문장}], "max_tokens": --max-tokens, "stream": true, "stream_options": {"include_usage": true}}.
- 요청별 측정: 응답 SSE의 "data:" 줄을 읽어 첫 내용 청크 도착까지 ttft_s, 끝까지 e2e_s, 청크 수, usage가 오면 completion_tokens, completion_tokens가 2 이상이면 tpot_s=(e2e_s-ttft_s)/(completion_tokens-1)을 기록한다.
- 결과 분류: success, rate_limited(429), http_502, http_504, http_4xx, http_5xx, timeout, connection_error, transport_error, stream_interrupted(헤더를 받은 뒤 끊김), invalid_response(200인데 내용 청크 없음), dropped_client_cap.
- 워밍업: --warmup-s 이전에 예정된 요청은 phase=warmup으로 표시하고 요약 통계에서 뺀다.
- 출력: 새 디렉터리(--output-dir, 이미 있으면 오류)에 requests.csv와 summary.json을 쓴다.
  - requests.csv 열: request_id, priority, phase, scheduled_offset_s, send_offset_s, send_lag_s, status_code, category, ttft_s, e2e_s, chunks, completion_tokens, tpot_s
  - summary.json: 실행 계획 전체(시드 포함), 분류별·우선순위별 개수, 우선순위별 ttft_s와 e2e_s의 p50/p95/p99(선형 보간 방식, 사용한 방식 이름도 기록), --slo-ttft-s가 주어지면 우선순위별 goodput(측정 구간에서 ttft_s가 SLO 이하인 success 수 / 측정 구간 초), send_lag_s 최댓값.
- 프롬프트·응답 본문과 헤더 값은 어떤 출력에도 저장하지 않는다.

안전장치(기존 load_test.py와 같은 원칙):
- --execute가 없으면 계획(예정 요청 수, 우선순위별 수, 최대 순간 도착률 포함)만 출력하고, 네트워크 클라이언트를 만들지 않으며 파일도 쓰지 않는다. --execute에는 --base-url이 반드시 필요하다.
- 엔드포인트는 숫자 루프백(127.0.0.1, [::1])만 허용한다. localhost, 자격증명, 쿼리, 프래그먼트, 임의 경로는 거부한다. --allow-non-loopback이 있어야만 원격을 허용한다. 환경 프록시를 무시하고 리다이렉트를 따라가지 않는다.
- 예정 요청 300개 초과, 최대 순간 도착률 30 req/s 초과, --max-in-flight 64 초과 중 하나라도 해당하면 --allow-large-plan 없이는 dry-run에서도 거부한다.
- 실행이 끝까지 돌면 요청 실패가 있어도 종료 코드 0이다(과부하 실험은 실패가 정상 결과다). 설정 오류만 0이 아닌 코드로 끝낸다.

제약: 새 파일 scripts/load-test/open_loop.py와 tests/test_open_loop.py만 만든다. 기존 파일은 하나도 수정하지 않는다. 표준 라이브러리와 httpx만 쓰고 의존성을 추가하지 않는다. 테스트는 실제 네트워크를 쓰지 않는다(httpx.MockTransport). 테스트가 실제 시간만큼 기다리지 않도록, 일정 실행 함수는 시계와 sleep 함수를 주입받을 수 있게 만든다.

검증 기준(tests/test_open_loop.py, 새 파일):
(1) 같은 시드는 같은 일정, 다른 시드는 다른 일정을 만든다.
(2) --burst 구간의 도착 수가 같은 길이의 다른 구간보다 뚜렷이 많다.
(3) --high-ratio에 따라 우선순위가 나뉘고 x-priority 헤더가 붙는다.
(4) 스트리밍 모의 응답에서 ttft_s < e2e_s이고, usage가 있으면 completion_tokens와 tpot_s가 계산된다.
(5) 429, 502, 504, 시간 초과, 연결 실패, 헤더 뒤 끊김, 내용 없는 200이 각각 올바르게 분류된다.
(6) max-in-flight 초과 요청은 보내지 않고 dropped_client_cap으로 기록된다.
(7) dry-run은 네트워크 클라이언트를 만들지 않고 파일도 쓰지 않는다.
(8) 위험한 URL은 거부되고, 큰 계획은 --allow-large-plan 없이 거부된다.
(9) 출력 디렉터리가 이미 있으면 거부되고, requests.csv 열이 위 목록과 같으며, 출력 어디에도 프롬프트 문장이 없다.
(10) warmup 요청은 요약 통계에서 빠지고, 백분위수와 goodput이 손으로 계산한 값과 같다.
(11) 기존 테스트 전부와 ruff 검사(ruff check .)가 통과한다.
