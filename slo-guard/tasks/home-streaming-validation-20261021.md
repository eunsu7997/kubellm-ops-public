# Home GPU streaming validation — prepared 2026-10-21

## Execution update (2026-10-21)

User subsequently approved this exact deployment + one smoke + six-request pilot.
Executed results: candidate Gateway running; actual SSE smoke PASS; first-chunk
count 0 -> 1 -> 6; pilot 5 success + 1 dropped_client_cap (not sent). No GPU
overload was demonstrated. GPU maximum observed 64 C. Old Gateway remains stopped
and retained, private vLLM and monitoring unchanged. No rollback was necessary
or tested. See [actual run and limits](../capacity/20261021-streaming-pilot-01/README.md).
The remaining text preserves the pre-execution plan and its approval boundary.

Status: image BUILD VERIFIED; schedule DRY RUN VERIFIED; container replacement,
live SSE, first-chunk counter and six-request run NOT VERIFIED. No source code
was changed and no existing service was stopped while preparing this plan.

## Findings and prepared artifacts

- Existing Gateway image ID:
  `sha256:d19712abf6bac8e7b636fb50d7d2d4614681ed2fee9a77d9f4b593c5805e3bd5`.
- Read-only live `/app/app/main.py` SHA256:
  `2eabea0c46b8d0132e434c9d6f237000bb6345084f007272e5ddd9eba15ed450`.
  `StreamingResponse` and `TIME_TO_FIRST_CHUNK` were absent. Do not use this
  Gateway to characterize the new streaming source.
- Built `kubellm-gateway-streaming:6ce55fa` successfully (exit 0), without starting
  a container. Immutable local image ID:
  `sha256:156b4fff57a3de106fea8c769f54b26e6e8d1e4190b0f2925087dc0200671935`.
  Docker user is `appuser`; healthcheck probes `/healthz`.
- Full current source already passed 109 offline tests and Ruff in the preceding
  publication step. This is not evidence of actual GPU streaming.
- vLLM stays running with the existing model/digest/options and no host port.
  No model download, second GPU server, network creation or driver change is needed.

## Approved-only deployment and rollback

Approval must cover stopping the old Gateway, creating the candidate Gateway on
the existing bridge, a single 16-token stream smoke, and the bounded plan below.
This is a planned Gateway outage. Keep vLLM/Prometheus/Grafana untouched.

Before execution: require old Gateway healthy, vLLM model ready, both scrape jobs
UP, no unrelated workload interference, GPU temperature below 85 C, and no
candidate name collision. Save selected state/image/ports and metrics in a new
evidence directory. Abort rather than adopting an existing candidate container.

Future commands (not executed):

```powershell
docker stop kubellm-gpu-secure-gateway-1
docker run --detach --pull never --name kubellm-gateway-streaming-candidate-20261021 `
  --network kubellm-gpu-secure_backend --network-alias gateway `
  --publish 127.0.0.1:18003:8000 --restart no `
  --env VLLM_BASE_URL=http://vllm:8000 `
  --env MODEL_NAME=Qwen/Qwen2.5-0.5B-Instruct --env REQUEST_TIMEOUT=30 `
  sha256:156b4fff57a3de106fea8c769f54b26e6e8d1e4190b0f2925087dc0200671935
```

The alias keeps Prometheus `gateway:8000` scraping the candidate. It does not
change the existing network configuration. Confirm DNS/scraping actually works;
do not assume it. The retained Compose Gateway remains stopped. Do not run a
full Compose up while the separate candidate owns port 18003.

Require health/ready HTTP 200, one actual `stream=true` response with
`text/event-stream`, content delta and `[DONE]`, and an increase in the
`gateway_time_to_first_chunk_seconds_count` histogram count (inspect actual
metric spelling/labels). Capture baseline/after counters, Content-Type, HTTP
status, first-content delay, usage when present and stream completion. No secret
headers. Avoid saving prompt/response content except the agreed harmless smoke.
The first-chunk metric is not automatically model-side TTFT; document its actual
semantics. A failed smoke is not permission to start a load run.

If candidate startup/readiness/SSE fails: save candidate logs, then:

```powershell
docker stop kubellm-gateway-streaming-candidate-20261021
docker start kubellm-gpu-secure-gateway-1
```

If candidate creation failed before a container exists, skip the candidate stop.
Verify old health/ready, one ordinary chat and Prometheus UP before declaring
rollback success. Preserve both containers and logs; no rm/prune or network
disconnect. Unlike legacy vLLM rollback, this restores only localhost Gateway
18003 and does not revive public vLLM 8002.

## Bounded six-request plan (NOT EXECUTED)

Dry-run command executed successfully, exit 0, with no client or output directory:

```powershell
.\.venv\Scripts\python.exe -B scripts/load-test/open_loop.py `
  --base-url http://127.0.0.1:18003 --model Qwen/Qwen2.5-0.5B-Instruct `
  --rate 0.5 --duration-s 10 --seed 42 --high-ratio 0.2 --max-in-flight 1 `
  --prompt-words 8 --max-tokens 16 --timeout 10 --warmup-s 2
```

Actual dry-run output: scheduled_requests=6, high=3, low=3, peak=0.5 req/s,
execution=`DRY RUN; NOT VERIFIED`. The high ratio is a sampling probability;
six sampled requests are not guaranteed to have a 20% high-priority proportion.

Only after approval and successful SSE smoke, add `--execute` and an explicitly
new `--output-dir slo-guard/capacity/20261021-UNIQUE/streaming-pilot` to that
command. Do not use `--allow-large-plan`, bursts, retries, multiple workers or
rate escalation. Maximum one in-flight request; later scheduled arrivals may
be dropped and must remain in results. Timeout is per HTTP phase, not a total
experiment deadline. Observe metrics/headroom and GPU temperature concurrently.
Stop on unexpected 5xx/timeout, memory pressure, >=85 C, or other workload
interference. Stop scheduling on interrupt; submitted backend work can continue.

This is a small pipeline pilot, not a capacity curve, SLO result or benchmark.
No SLO threshold or capacity C is selected. Report failed/dropped requests and
send lag, not only successful latency percentiles. A command exit 0 does not
mean all requests succeeded. Use a separately reviewed larger plan for C later.

## Approval boundary

Existing approval covered restarting the four existing secure containers.
No Gateway stop/replacement, live stream request or load execution was done in
this preparation. Ask for approval of the concrete deployment + one smoke +
six-request pilot before those commands. No Git commit/push is part of this step.
