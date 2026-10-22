"""pytest 공용 fixture.

여기서 만드는 TestClient는 실제 uvicorn 서버를 띄우지 않고, FastAPI 앱을 메모리 안에서
직접 호출한다(ASGI 프로토콜을 그대로 흉내낸다) — 그래서 서버를 따로 실행하지 않아도
`pytest`만으로 전부 테스트할 수 있다.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.app import main as gateway_main  # noqa: E402


@pytest.fixture
def client():
    with TestClient(gateway_main.app) as c:
        yield c


@pytest.fixture
def gateway_module():
    """gateway.app.main 모듈 자체 — 테스트에서 _http_client를 monkeypatch하기 위해 필요하다."""
    return gateway_main
