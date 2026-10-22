# Follow-up to the six-request streaming pilot (prepared only)

Execution update 2026-10-21: user approved and this plan was executed once.
Same offsets/priorities/phases confirmed; 6 HTTP 200 successes, no client drops,
first-chunk count 6→12, GPU maximum 59 C. No capacity/SLO conclusion.
[Actual comparison and limits](../capacity/20261021-streaming-pilot-02-cap4/README.md).
The plan text below is retained as its pre-execution record.

The [actual first pilot](../capacity/20261021-streaming-pilot-01/README.md)
had one dropped_client_cap request. This was a client-side limit, not a GPU
capacity failure: arrivals 1 and 2 were about 51 ms apart with max-in-flight=1.

Next proposal: repeat the exact six scheduled arrivals with max-in-flight=4.
Keep rate, seed, duration, model, prompt length and token cap unchanged. This
isolates the client concurrency cap; it does not guarantee no drops, demonstrate
GPU overload or determine capacity C. It is not executed and needs approval of
the changed in-flight cap before using --execute.

Dry-run performed 2026-10-21, exit 0; scheduled 6, high 3 / low 3, peak 0.5 req/s:

```powershell
.\.venv\Scripts\python.exe -B scripts/load-test/open_loop.py `
  --base-url http://127.0.0.1:18003 --model Qwen/Qwen2.5-0.5B-Instruct `
  --rate 0.5 --duration-s 10 --seed 42 --high-ratio 0.2 --max-in-flight 4 `
  --prompt-words 8 --max-tokens 16 --timeout 10 --warmup-s 2
```

After approval, use the running streaming candidate Gateway, verify GPU headroom
and monitoring health, and append --execute plus a new --output-dir. Do not
restart, rebuild or replace the model or services for this run. Monitor GPU
temperature and Gateway health/5xx; stop on >=85 C, timeout/5xx, memory pressure
or unrelated workload interference. Do not increase rate or retry automatically.
Save all six rows, categories, send lag, GPU samples and before/after metrics.
Compare categories with the first pilot; report success-only latency sample
sizes separately. No SLO was selected, so do not calculate SLO attainment yet.

If both runs complete, plan the capacity sweep separately with explicit rates,
durations, client cap, failure-inclusive SLO criteria and total request budget.
Do not treat either six-request pilot as that sweep or silently widen approval.
