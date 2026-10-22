목표: Gateway의 POST /v1/chat/completions가 요청 본문에 "stream": true가 있으면 백엔드(vLLM)의 SSE 응답을 받는 즉시 클라이언트로 그대로 전달하고, 첫 청크까지 걸린 시간을 지표로 남긴다. (SLO-Guard MVP 1단계)

현재 문제: gateway/app/main.py는 백엔드 응답 전체를 받은 뒤 JSON으로 해석한다. 그래서 stream=true 요청은 끝까지 기다린 뒤 상태 200과 "backend returned a non-JSON response"로 끝나고, 성공 카운터까지 증가한다. 게이트웨이에서 TTFT를 잴 수 없다.

기대 동작:
- stream=true 요청은 httpx 스트리밍으로 백엔드에 보내고, 받은 바이트를 순서대로 StreamingResponse(media_type="text/event-stream")로 전달한다.
- 응답 헤더를 받기 전 실패는 기존 규칙과 같다: 연결 실패 502 "backend connection failed", 시간 초과 504 "backend request timed out", 백엔드 5xx는 502 "backend returned an error", 4xx는 상태 코드 그대로.
- 첫 청크가 도착한 시점까지의 시간을 새 Histogram gateway_time_to_first_chunk_seconds에 기록한다(버킷은 기존 latency 버킷과 같게).
- 스트림을 끝까지 전달하면 gateway_requests_success_total을 1 올린다. 전달 도중 끊기면 gateway_backend_failures_total{reason="stream_error"}를 1 올린다.

보존할 동작: stream이 없거나 false인 요청의 응답 본문, 상태 코드, 기존 지표 이름과 의미를 바꾸지 않는다. 기존 테스트 전부 통과.

제약: 의존성 추가 금지(FastAPI와 httpx만 사용). gateway/app/main.py, gateway/app/metrics.py 외 파일 수정 금지. 테스트는 실제 네트워크를 쓰지 않는다(httpx.MockTransport 등으로 백엔드를 흉내낸다).

검증 기준: 새 테스트 tests/test_gateway_streaming.py에서 (1) 여러 청크가 순서대로 전달됨 (2) gateway_time_to_first_chunk_seconds의 count가 1 증가 (3) 백엔드 5xx이면 502 (4) 연결 실패이면 502 (5) stream=false 요청은 기존과 같은 JSON 응답. 기존 테스트와 ruff 검사 통과.
