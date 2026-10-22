"""healthz / readyz 테스트.

- healthz (라이브니스): 프로세스가 살아있으면 항상 200.
- readyz (레디니스): 앱 시작(lifespan startup)이 끝나서 HTTP 클라이언트가 준비되면 200.
"""


def test_healthz_returns_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_returns_ready_after_startup(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
