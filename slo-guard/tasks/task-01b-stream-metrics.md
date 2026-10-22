목표: task-01에서 만든 Gateway 스트리밍 전달의 지표 의미를 바로잡는다. (SLO-Guard MVP 1단계 보완)

현재 문제:
1. gateway/app/main.py의 _relay_stream은 클라이언트 연결 종료, 작업 취소, 제너레이터 조기 종료까지 gateway_backend_failures_total{reason="stream_error"}로 집계한다. 백엔드 문제가 아닌 클라이언트 쪽 중단이 백엔드 장애로 보여서, 부하 실험에서 클라이언트가 시간 초과로 끊으면 장애 지표가 오염된다.
2. tests/test_gateway_streaming.py의 5xx 테스트가 비밀값처럼 보이는 문자열(db_password=...)을 써서 비밀값 검사기에 걸린다.

기대 동작:
- 백엔드 쪽 중단(httpx.HTTPError로 스트림이 끝난 경우)만 gateway_backend_failures_total{reason="stream_error"}를 1 올린다.
- 클라이언트 연결 종료, 작업 취소, 제너레이터 조기 종료는 gateway/app/metrics.py에 새로 정의한 Counter gateway_client_disconnects_total을 1 올린다. 이때 성공 카운터와 gateway_backend_failures_total은 올리지 않는다. 취소·종료 신호는 지금처럼 다시 전파한다.
- 백엔드 응답 닫기와 gateway_request_latency_seconds 기록은 지금처럼 모든 경우에 정확히 한 번 한다.
- 5xx 테스트의 비밀값 모양 문자열을 비밀값처럼 보이지 않는 표식(예: "INTERNAL-ONLY-MARKER")으로 바꾸고, 그 표식이 클라이언트 응답에 노출되지 않는지 계속 확인한다.

보존할 동작: task-01의 나머지 동작(청크를 받은 순서대로 즉시 전달, gateway_time_to_first_chunk_seconds 기록, 헤더 전 오류의 502/504/4xx 처리, 빈 본문 200의 성공 처리, stream=false 요청의 JSON 응답)과 기존 지표의 이름과 의미.

제약: 의존성 추가 금지. gateway/app/main.py, gateway/app/metrics.py, tests/test_gateway_streaming.py 외 파일 수정 금지. 테스트는 실제 네트워크를 쓰지 않는다.

검증 기준:
(1) 백엔드가 전달 도중 끊기면 stream_error가 1 오르고 gateway_client_disconnects_total은 그대로다.
(2) 첫 청크 뒤 작업을 취소하면 gateway_client_disconnects_total이 1 오르고, stream_error와 성공 카운터는 그대로이며, 취소가 다시 전파되고 백엔드 응답이 닫힌다.
(3) 제너레이터를 조기 종료하면 gateway_client_disconnects_total이 1 오르고 stream_error는 그대로다.
(4) 기존 테스트 전부와 ruff 검사(ruff check .)가 통과한다.
