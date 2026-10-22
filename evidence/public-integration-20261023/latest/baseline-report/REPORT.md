# Local evidence report — deterministic, no LLM generation

This run is a bounded comparison, not a production incident. Recovery duration/MTTR is not estimable.

| Policy | Success/planned | Success-only client P99 ms | Logical final429 | Output tokens/s |
|---|---:|---:|---:|---:|
| token-budget | 30/40 | 481.5 | 25.0% | 32.44 |

## Cause candidates
Small bounded trace completed; policy efficacy under overload is not established.

## Evidence
RUN/PLAN/GPUQ/RECOVERY/ACCESS refer to evidence.json. All IDs exist; content correctness still needs review.

## Counterevidence
Uncontrolled policy also succeeds; small samples and desktop GPU contention prevent causal conclusions.

## Suggested actions
Keep production serving unchanged; review policy-specific latency and rejection budgets.

## Unknowns
Causal protection benefit, stable population P99 and physical KV reclamation latency are unverified.

No LLM, RAG, Slack or automatic remediation was executed. This reuses the actual Incident Copilot validator.
