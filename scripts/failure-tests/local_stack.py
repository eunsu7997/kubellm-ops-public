"""로컬(Docker/Kubernetes 없이) Gateway + Mock Backend를 실제 프로세스로 띄우는 helper.

Docker나 Kubernetes 클러스터가 없는 환경에서도, uvicorn으로 두 서비스를 실제
localhost 포트에 띄워서 "진짜 HTTP 요청"으로 장애 시나리오를 재현하기 위해 만들었다.
pytest의 TestClient(메모리 내 호출)와 달리, 이건 실제 TCP 연결/타임아웃/연결 거부까지
그대로 재현한다 — 그래서 connection failure 시나리오를 진짜로 검증할 수 있다.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class ManagedProcess:
    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str]):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen | None = None
        self._log_file = None
        self._output = ""

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError(f"{self.name} is already running")
        self.stop()
        self._output = ""
        # stdout=PIPE인 채로 아무도 읽지 않으면, 요청이 많아 로그가 쌓일 때 OS 파이프 버퍼가
        # 가득 차서 자식 프로세스(uvicorn)의 쓰기가 막히고 결국 요청 처리 자체가 멈춘다.
        # 부하 테스트처럼 요청이 많은 시나리오에서 실제로 이 문제가 재현됐기 때문에,
        # 파이프 대신 임시 로그 파일로 흘려보낸다.
        self._log_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=f"_{self.name}.log", delete=False
        )
        try:
            self.process = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                env=self.env,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except BaseException:
            path = Path(self._log_file.name)
            self._log_file.close()
            self._log_file = None
            path.unlink(missing_ok=True)
            raise

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_file is not None:
            path = Path(self._log_file.name)
            self._output = self.read_output()
            self._log_file.close()
            self._log_file = None
            path.unlink(missing_ok=True)

    def read_output(self) -> str:
        if self._log_file is None:
            return self._output
        with open(self._log_file.name, encoding="utf-8", errors="replace") as f:
            return f.read()


def python_executable() -> str:
    # 프로젝트 venv의 python을 그대로 사용한다 (의존성이 이미 설치돼 있음)
    return sys.executable


def make_mock_backend(port: int, slow_delay_seconds: float = 5.0, extra_env: dict | None = None) -> ManagedProcess:
    env = _base_env({"MOCK_SLOW_DELAY_SECONDS": str(slow_delay_seconds)})
    if extra_env:
        env.update(extra_env)
    cmd = [
        python_executable(), "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]
    return ManagedProcess("mock-backend", cmd, REPO_ROOT / "mock-backend", env)


def make_gateway(
    port: int, backend_url: str, model_name: str = "mock-model", request_timeout: float = 2.0
) -> ManagedProcess:
    env = _base_env(
        {
            "VLLM_BASE_URL": backend_url,
            "MODEL_NAME": model_name,
            "REQUEST_TIMEOUT": str(request_timeout),
        }
    )
    cmd = [
        python_executable(), "-m", "uvicorn", "gateway.app.main:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]
    return ManagedProcess("gateway", cmd, REPO_ROOT, env)


def _base_env(overrides: dict[str, str]) -> dict[str, str]:
    # Windows에서 python/uvicorn이 소켓·DNS 등을 정상 동작시키려면 SYSTEMROOT 등
    # 시스템 환경변수가 필요하므로, 최소 env가 아니라 현재 환경 전체를 복사해서 덮어쓴다.
    import os

    env = os.environ.copy()
    env.update(overrides)
    return env


def wait_until_healthy(url: str, timeout_seconds: float = 15.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False
