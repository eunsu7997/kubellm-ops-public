"""/metrics 엔드포인트가 Prometheus가 읽을 수 있는 형식으로 지표를 노출하는지 확인한다."""


def test_metrics_endpoint_exposes_expected_counters(client):
    client.get("/healthz")  # 아무 요청이나 하나 발생시켜서 카운터가 0이 아님을 보장

    resp = client.get("/metrics")

    assert resp.status_code == 200
    body = resp.text
    for metric_name in [
        "gateway_requests_total",
        "gateway_requests_success_total",
        "gateway_requests_error_total",
        "gateway_backend_failures_total",
        "gateway_request_latency_seconds",
    ]:
        assert metric_name in body, f"{metric_name} 이 /metrics 응답에 없음"
