"""KubeLLM-Ops Gateway.

이 서비스는 얇은 프록시(gateway)다 — 직접 LLM을 실행하지 않고, vLLM(또는 로컬 검증용
mock-backend)의 OpenAI 호환 API로 요청을 그대로 전달한 뒤 응답을 돌려준다.

용어 설명(입문자용):
- FastAPI: Python으로 웹 API를 빠르게 만드는 프레임워크.
- Liveness Probe(라이브니스 프로브): "이 프로세스가 죽지 않고 살아있는가"를 확인하는 신호.
  실패하면 Kubernetes가 컨테이너를 재시작한다. 여기서는 /healthz가 그 역할을 한다.
- Readiness Probe(레디니스 프로브): "지금 요청을 받아도 되는 상태인가"를 확인하는 신호.
  실패하면 Kubernetes가 이 Pod로 트래픽을 보내지 않는다(재시작은 안 함). /readyz가 이 역할이다.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import settings
from .metrics import (
    BACKEND_FAILURE_COUNT,
    BACKEND_READY,
    CLIENT_DISCONNECT_COUNT,
    CONTENT_TYPE_LATEST,
    ERROR_COUNT,
    READINESS_CACHE_HITS,
    READINESS_CHECKS,
    READINESS_DURATION,
    READINESS_ENABLED,
    READINESS_FAILURES,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    SUCCESS_COUNT,
    TIME_TO_FIRST_CHUNK,
    generate_metrics,
)

logger = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_http_client: httpx.AsyncClient | None = None
_ready = False
_readiness_lock: asyncio.Lock | None = None
_readiness_until = 0.0
_backend_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _ready, _readiness_lock, _readiness_until, _backend_ready
    _http_client = httpx.AsyncClient(timeout=settings.request_timeout)
    _readiness_lock = asyncio.Lock()
    _readiness_until = 0.0
    _backend_ready = False
    READINESS_ENABLED.set(int(settings.readiness_mode != "local"))
    BACKEND_READY.set(-1)
    _ready = True
    logger.info(
        "gateway startup complete backend=%s model=%s timeout=%.1fs",
        settings.vllm_base_url,
        settings.model_name,
        settings.request_timeout,
    )
    yield
    _ready = False
    if _http_client is not None:
        await _http_client.aclose()
    logger.info("gateway shutdown complete")


app = FastAPI(title="KubeLLM-Ops Gateway", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Liveness probe용. 이 함수가 응답할 수 있으면 프로세스는 살아있는 것이다."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Local by default; opt-in cached backend/inference check. Never used for liveness."""
    if not _ready or _http_client is None:
        raise HTTPException(status_code=503, detail="not ready")
    if settings.readiness_mode != "local" and not await _check_backend_readiness():
        raise HTTPException(status_code=503, detail="backend not ready")
    return {"status": "ready"}


async def _check_backend_readiness():
    global _readiness_until, _backend_ready
    if _readiness_lock is None:
        return False
    async with _readiness_lock:
        if time.monotonic() < _readiness_until:
            READINESS_CACHE_HITS.inc()
            return _backend_ready
        _backend_ready = False
        checked_at = time.monotonic()
        failure_reason = "invalid_response"
        try:
            # Total deadline as well as per-phase HTTP limits; no unbounded queued canaries.
            async with asyncio.timeout(settings.readiness_timeout):
                if settings.readiness_mode == "backend":
                    response = await _http_client.get(
                        f"{settings.vllm_base_url}/health", timeout=settings.readiness_timeout,
                    )
                    _backend_ready = response.status_code == 200
                    failure_reason = "http_status"
                else:
                    response = await _http_client.post(
                        f"{settings.vllm_base_url}/v1/chat/completions",
                        json={"model": settings.model_name, "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 1, "stream": False, "temperature": 0},
                        timeout=settings.readiness_timeout,
                    )
                    failure_reason = "http_status" if response.status_code != 200 else "invalid_response"
                    if response.status_code == 200:
                        body = response.json()
                        choices = body.get("choices") if isinstance(body, dict) else None
                        _backend_ready = bool(isinstance(choices, list) and choices
                                              and isinstance(choices[0], dict)
                                              and isinstance(choices[0].get("message"), dict)
                                              and isinstance(choices[0]["message"].get("content"), str))
        except (httpx.TimeoutException, TimeoutError):
            failure_reason = "timeout"
        except httpx.HTTPError:
            failure_reason = "transport"
        except ValueError:
            failure_reason = "invalid_response"
        # Negative results are cached too; this check adds real model work in inference mode.
        _readiness_until = time.monotonic() + settings.readiness_cache_seconds
        BACKEND_READY.set(int(_backend_ready))
        READINESS_CHECKS.labels(result="success" if _backend_ready else "failure").inc()
        if not _backend_ready:
            READINESS_FAILURES.labels(reason=failure_reason).inc()
        READINESS_DURATION.observe(time.monotonic() - checked_at)
        return _backend_ready


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    start = time.perf_counter()
    REQUEST_COUNT.inc()

    try:
        body = await request.json()
    except Exception:
        ERROR_COUNT.labels(status="400").inc()
        raise HTTPException(status_code=400, detail="invalid JSON body")

    if _http_client is None:
        ERROR_COUNT.labels(status="503").inc()
        raise HTTPException(status_code=503, detail="gateway not ready")

    body.setdefault("model", settings.model_name)
    url = f"{settings.vllm_base_url}/v1/chat/completions"

    if body.get("stream") is True:
        return await _stream_chat_completions(_http_client, body, url, start)

    try:
        resp = await _http_client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise _backend_call_failed(exc, url, start)

    REQUEST_LATENCY.observe(time.perf_counter() - start)

    if resp.status_code >= 400:
        return _backend_error_response(resp, url)

    SUCCESS_COUNT.inc()
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


async def _stream_chat_completions(client: httpx.AsyncClient, body: dict, url: str, start: float):
    """stream=true 요청: 백엔드의 SSE 응답을 받는 즉시 클라이언트로 그대로 흘려보낸다.

    응답 헤더를 받기 전까지의 실패(연결 실패/타임아웃/4xx/5xx)는 비스트리밍 요청과 같은
    규칙으로 처리한다. 헤더를 받은 뒤에는 이미 200을 보낸 상태라 상태 코드를 바꿀 수 없으므로,
    전달 도중 끊기면 stream_error 지표만 남기고 스트림을 닫는다.
    """
    request = client.build_request("POST", url, json=body)
    try:
        resp = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _backend_call_failed(exc, url, start)

    if resp.status_code >= 400:
        try:
            if resp.status_code >= 500:
                await resp.aclose()  # 5xx 본문은 클라이언트에 전달하지 않으므로 읽을 필요가 없다.
            else:
                await resp.aread()
        except httpx.HTTPError as exc:
            await resp.aclose()
            raise _backend_call_failed(exc, url, start)
        REQUEST_LATENCY.observe(time.perf_counter() - start)
        return _backend_error_response(resp, url)

    return StreamingResponse(
        _relay_stream(resp, url, start),
        status_code=resp.status_code,
        media_type="text/event-stream",
    )


async def _relay_stream(resp: httpx.Response, url: str, start: float):
    """백엔드 청크를 받은 순서대로 내보낸다.

    결과는 정확히 한 번 집계한다. 끝까지 전달하면 성공, 백엔드 쪽에서 끊기면(httpx.HTTPError)
    stream_error, 클라이언트 쪽 사유(연결 종료로 인한 작업 취소, 제너레이터 강제 종료)로 멈추면
    gateway_client_disconnects_total이다. 클라이언트 쪽 중단은 백엔드 장애가 아니므로
    stream_error에 넣지 않는다. 백엔드 쪽 오류는 이미 200을 보낸 뒤라 삼키고 스트림을 닫지만,
    취소/종료 신호는 그대로 다시 전파한다.
    본문이 비어 있는 200 응답은 끝까지 전달한 것이므로 성공으로 보되, 받은 청크가 없으니
    첫 청크 시간은 기록하지 않는다.
    """
    first_chunk = True
    outcome = None
    try:
        async for chunk in resp.aiter_bytes():
            if not chunk:
                continue
            if first_chunk:
                TIME_TO_FIRST_CHUNK.observe(time.perf_counter() - start)
                first_chunk = False
            yield chunk
        outcome = "success"
    except httpx.HTTPError:
        outcome = "stream_error"
        logger.warning("backend stream interrupted url=%s", url)
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_disconnect"
        logger.info("stream closed before completion (client disconnect or cancellation) url=%s", url)
        raise
    except BaseException:
        logger.exception("unexpected error while relaying stream url=%s", url)
        raise
    finally:
        REQUEST_LATENCY.observe(time.perf_counter() - start)
        if outcome == "success":
            SUCCESS_COUNT.inc()
        elif outcome == "stream_error":
            BACKEND_FAILURE_COUNT.labels(reason="stream_error").inc()
        elif outcome == "client_disconnect":
            CLIENT_DISCONNECT_COUNT.inc()
        # 취소 중이어도 백엔드 연결은 반드시 닫히도록 정리 구간을 취소로부터 보호한다.
        with anyio.CancelScope(shield=True):
            await resp.aclose()


def _backend_call_failed(exc: httpx.HTTPError, url: str, start: float) -> HTTPException:
    """백엔드 호출 자체가 실패했을 때 지표/로그를 남기고 클라이언트에 돌려줄 예외를 만든다."""
    REQUEST_LATENCY.observe(time.perf_counter() - start)
    # 민감한 내부 정보(백엔드 URL, 원본 예외 메시지)는 응답에 넣지 않는다 — 로그에만 남긴다.
    if isinstance(exc, httpx.ConnectError):
        BACKEND_FAILURE_COUNT.labels(reason="connection_error").inc()
        ERROR_COUNT.labels(status="502").inc()
        logger.warning("backend connection failed url=%s", url)
        return HTTPException(status_code=502, detail="backend connection failed")
    if isinstance(exc, httpx.TimeoutException):
        BACKEND_FAILURE_COUNT.labels(reason="timeout").inc()
        ERROR_COUNT.labels(status="504").inc()
        logger.warning("backend request timed out url=%s timeout=%.1fs", url, settings.request_timeout)
        return HTTPException(status_code=504, detail="backend request timed out")
    BACKEND_FAILURE_COUNT.labels(reason="http_error").inc()
    ERROR_COUNT.labels(status="502").inc()
    logger.error("unexpected error calling backend url=%s", url, exc_info=exc)
    return HTTPException(status_code=502, detail="backend request failed")


def _backend_error_response(resp: httpx.Response, url: str) -> JSONResponse:
    """백엔드가 4xx/5xx를 돌려줬을 때의 처리. 5xx는 502로 감싸고, 4xx는 그대로 전달한다."""
    if resp.status_code >= 500:
        BACKEND_FAILURE_COUNT.labels(reason="backend_5xx").inc()
        ERROR_COUNT.labels(status=str(resp.status_code)).inc()
        logger.warning("backend returned %s url=%s", resp.status_code, url)
        raise HTTPException(status_code=502, detail="backend returned an error")

    ERROR_COUNT.labels(status=str(resp.status_code)).inc()
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return {"detail": "backend returned a non-JSON response"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_metrics(), media_type=CONTENT_TYPE_LATEST)
