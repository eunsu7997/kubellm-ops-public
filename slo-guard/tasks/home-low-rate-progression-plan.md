# Low-rate progression proposal — not executed

Execution/status update 2026-10-21: A executed once, all 15 succeeded (4 warmup,
11 measurement). B was SKIPPED because free VRAM had not returned to the initial
snapshot under the conservative gate (2632→2622 MiB), not because of OOM or
server error. [Actual run](../capacity/20261021-low-rate-progression-01/README.md).
User subsequently paused further SLO work until the hiring portfolio is finalized.
The remaining text preserves the original plan; it does not authorize a retry.

Purpose: after the [cap=4 pilot](../capacity/20261021-streaming-pilot-02-cap4/README.md),
observe the streaming pipeline at two slightly higher arrival rates. This is
not the full capacity curve, policy evaluation, benchmark or SLO experiment.
User approval must cover the two stages and request budget before --execute.

## Fixed setup

Current streaming candidate Gateway http://127.0.0.1:18003, existing Qwen/Qwen2.5-0.5B-Instruct,
same private vLLM digest and memory settings. No service restart/build/recreation,
model download, network change, driver update or public port. Do not change
streaming/model settings, prompt length, token cap or max-in-flight between stages.

| Stage | Mean rate | Duration | Seed | Scheduled by actual dry-run | Priority assignment |
|---|---|---|---|---|---|
| A | 1 req/s | 10 s | 42 | 15 | high 4 / low 11 |
| B | 2 req/s | 10 s | 42 | 27 | high 3 / low 24 |

Both dry-runs completed on 2026-10-21, exit 0, execution=DRY RUN; NOT VERIFIED.
Total budget: at most 42 scheduled requests and 672 requested output tokens.
The arrival process is Poisson; realized high ratio is not exactly 0.2.

## Commands for review (no traffic as written)

```powershell
.\.venv\Scripts\python.exe -B scripts/load-test/open_loop.py --base-url http://127.0.0.1:18003 --model Qwen/Qwen2.5-0.5B-Instruct --rate 1 --duration-s 10 --seed 42 --high-ratio 0.2 --max-in-flight 4 --prompt-words 8 --max-tokens 16 --timeout 10 --warmup-s 2
.\.venv\Scripts\python.exe -B scripts/load-test/open_loop.py --base-url http://127.0.0.1:18003 --model Qwen/Qwen2.5-0.5B-Instruct --rate 2 --duration-s 10 --seed 42 --high-ratio 0.2 --max-in-flight 4 --prompt-words 8 --max-tokens 16 --timeout 10 --warmup-s 2
```

After explicit approval, append --execute and a different new --output-dir for
each stage. No large-plan override, burst, retry or additional rate escalation.
Approve both stages once with the gate below; do not continue when the gate fails.

## Monitoring and stage gate

Before each stage: model ready; Gateway health/ready 200; both Prometheus targets
UP; GPU below 85 C, free memory at least 512 MiB; no known unrelated GPU work.
Record pre/post metrics and GPU snapshots. Monitor temperature, free memory,
Gateway health and error-counter deltas approximately once a second. Monitor
unavailability, >=85 C, free <512 MiB, new 5xx or timeout stops the client.
Request timeout is per HTTP phase; supervise each process with a 45-second
deadline and preserve all rows on stop. Submitted server work can finish after
client cancellation. Do not restart or alter any service to salvage a run.

Run B only if A completes with no failed/dropped request, no monitor warning,
max send lag <=50 ms, and queue/KV observations and GPU headroom return to idle
baseline. Wait at least 5 seconds between stages and inspect the results;
skip B otherwise, reporting why. No automatic repetition is authorized.

Keep failures and dropped_client_cap in CSV and the comparison denominator.
If the client cap clips arrivals or send lag grows, mark that stage client-limited
and do not infer GPU capacity. Success-only latency percentiles have tiny and
different populations; no significance or improvement claim. Report actual
warmup counts, metric sample coverage, reset boundaries and concurrent traffic.
TPOT is a client approximation; no model-side TTFT inference from Gateway chunks.

SLO threshold and capacity C remain unselected. Do not derive an SLO attainment
rate or label the highest successful stage as C. A subsequent repeatable sweep
needs a separate reviewed plan and failure-inclusive criteria.
