"""Bounded, opt-in non-streaming experiment; no network activity on import."""
from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


@dataclass
class RequestResult:
    request_id: int
    status_code: int | None
    category: str
    latency_seconds: float
    completion_tokens: int | None = None


def endpoint(base_url: str, allow_non_loopback: bool = False) -> str:
    """Validate without DNS resolution. Reject URL fields that could leak secrets."""
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", "/v1", "/v1/"}):
        raise ValueError("supply a base URL with no credentials, query, fragment or custom path")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    # Require numeric loopback: no DNS lookup or hostname rebinding.
    if not loopback and not allow_non_loopback:
        raise ValueError("numeric loopback endpoint required; remote targets require --allow-non-loopback")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}/v1/chat/completions"


async def send_one(client, url, model, max_tokens, timeout, request_id):
    start = time.perf_counter()
    status, category, tokens = None, "transport_error", None
    try:
        response = await client.post(url, json={
            "model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": max_tokens, "stream": False,
        }, timeout=timeout)
        status = response.status_code
        if 200 <= status < 300:
            category = "invalid_response"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("choices"), list) and body["choices"]:
                    category = "success"
                    usage = body.get("usage")
                    value = usage.get("completion_tokens") if isinstance(usage, dict) else None
                    if type(value) is int and value >= 0:
                        tokens = value
            except ValueError:
                pass
        else:
            category = {502: "http_502", 504: "http_504", 429: "rate_limited"}.get(
                status, "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "redirect")
    except httpx.TimeoutException:
        category = "timeout"
    except httpx.ConnectError:
        category = "connection_error"
    except httpx.HTTPError:
        pass
    return RequestResult(request_id, status, category, time.perf_counter() - start, tokens)


async def run(plan, transport=None):
    """Caller supplies a validated plan. Fixed workers bound in-flight requests."""
    next_id = iter(range(1, plan["requests"] + 1))
    results = []
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=plan["concurrency"])) as client:
        async def worker():
            for request_id in next_id:
                results.append(await send_one(client, plan["endpoint"], plan["model"],
                                              plan["max_tokens"], plan["timeout"], request_id))
        start = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(min(plan["concurrency"], plan["requests"]))))
        wall = time.perf_counter() - start
    return {
        "schema_version": 1, "started_at_utc": plan["started_at_utc"], "plan": plan,
        "measurement": "non-streaming full-response latency; excludes worker queue wait",
        "ttft": "NOT MEASURED", "tpot": "NOT MEASURED",
        "wall_seconds": wall, "completed_requests_per_second": len(results) / wall if wall else None,
        "categories": dict(Counter(r.category for r in results)),
        "requests": [asdict(r) for r in sorted(results, key=lambda r: r.request_id)],
    }


def write_results(directory, report):
    # Exclusive new directory: never overwrite an earlier run or evidence.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    with (directory / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    with (directory / "requests.csv").open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RequestResult.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(report["requests"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="explicit base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--target-kind", choices=["gateway", "direct-vllm"], default="gateway",
                        help="operator declaration, not automatic detection")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output-dir", help="new directory for JSON and CSV; must not already exist")
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--allow-large-plan", action="store_true",
                        help="explicit override for more than 4 workers or 50 requests; requires separate approval")
    parser.add_argument("--execute", action="store_true", help="send requests only after human approval")
    args = parser.parse_args(argv)
    if any(value <= 0 for value in [args.concurrency, args.requests, args.max_tokens, args.timeout]):
        parser.error("all numeric limits must be positive")
    if not math.isfinite(args.timeout):
        parser.error("timeout must be finite")
    if (args.concurrency > 4 or args.requests > 50) and not args.allow_large_plan:
        parser.error("plans above 4 workers or 50 requests require --allow-large-plan")
    if args.execute and not args.base_url:
        parser.error("--execute requires an explicit --base-url")
    try:
        url = endpoint(args.base_url, args.allow_non_loopback) if args.base_url else None
    except ValueError as exc:
        parser.error(str(exc))
    plan = {
        "endpoint": url, "model": args.model, "target_kind": args.target_kind,
        "concurrency": args.concurrency, "requests": args.requests, "max_tokens": args.max_tokens,
        "timeout": args.timeout, "started_at_utc": datetime.now(UTC).isoformat(),
        "allow_non_loopback": args.allow_non_loopback,
        "allow_large_plan": args.allow_large_plan,
        "execution": "EXECUTE" if args.execute else "DRY RUN; NOT VERIFIED",
        "request_timeout_note": "per HTTP phase; not a total experiment deadline",
    }
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    directory = Path(args.output_dir) if args.output_dir else Path("results") / (
        "experiment_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    if directory.exists():
        parser.error("output directory already exists; choose a new directory")
    report = asyncio.run(run(plan))
    write_results(directory, report)
    print(json.dumps({"categories": report["categories"], "output_dir": str(directory)}))
    return 0 if report["categories"].get("success", 0) == args.requests else 1


if __name__ == "__main__":
    raise SystemExit(main())
