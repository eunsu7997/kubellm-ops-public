"""429-only client retry budget for the experiment; no retries for accepted/partial streams."""
import asyncio
import math
import time
from contextlib import asynccontextmanager


def retry_delay(status, retry_after, attempt, elapsed, *, max_attempts=2, deadline=2.5, jitter=0):
    if status != 429 or attempt >= max_attempts or not 0 <= jitter <= .1:
        return None
    try:
        delay = float(retry_after)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(delay) or delay < 0 or not math.isfinite(elapsed) or elapsed < 0:
        return None
    # Never shorten the server's requested wait to fit the local deadline.
    delay = max(delay, .1 * 2 ** (attempt - 1)) + jitter
    return delay if elapsed + delay < deadline else None


async def request_with_budget(client, url, body, *, jitter=.05):
    """CPU contract probe for nonstream responses. Streaming retries are intentionally unsupported."""
    if body.get("stream"):
        raise ValueError("never replay a potentially accepted stream")
    started = time.perf_counter()
    attempts = []
    for attempt in (1, 2):
        remaining = 2.5 - (time.perf_counter() - started)
        if remaining <= 0:
            raise TimeoutError("retry deadline")
        async with asyncio.timeout(remaining):
            response = await client.post(url, json=body)
        delay = retry_delay(response.status_code, response.headers.get("Retry-After"), attempt,
                            time.perf_counter() - started, jitter=jitter)
        attempts.append({"attempt": attempt, "status": response.status_code, "delay": delay})
        if delay is None:
            return response, attempts
        await asyncio.sleep(delay)


class HeaderCapture:
    def __init__(self, client):
        self.client, self.retry_after = client, None

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        async with self.client.stream(*args, **kwargs) as response:
            self.retry_after = response.headers.get("Retry-After")
            yield response


async def stream_with_budget(send_one, client, url, body, priority, *, jitter=.05):
    """Retry only an explicit pre-admission429. Never replay200/partial streams or transport errors."""
    started = time.perf_counter()
    attempts = []
    for attempt in (1, 2):
        before = time.perf_counter() - started
        capture = HeaderCapture(client)
        try:
            async with asyncio.timeout(max(.001, 2.5 - before)):
                result = await send_one(capture, url, body, priority, max(.001, 2.5 - before))
        except TimeoutError:
            result = {"status_code": None, "category": "timeout", "ttft_s": None,
                      "e2e_s": time.perf_counter() - started, "chunks": 0,
                      "completion_tokens": None, "tpot_s": None}
        elapsed = time.perf_counter() - started
        delay = retry_delay(result["status_code"], capture.retry_after, attempt, elapsed, jitter=jitter)
        attempts.append({"attempt": attempt, "status": result["status_code"], "category": result["category"],
                         "started_after_s": before, "finished_after_s": elapsed,
                         "retry_after": capture.retry_after, "sleep_s": delay})
        if delay is None:
            if result["ttft_s"] is not None:
                result["ttft_s"] += before
            result["e2e_s"] = elapsed
            result["attempts"] = attempts
            return result
        await asyncio.sleep(delay)
