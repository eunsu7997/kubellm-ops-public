"""Offline external-metric scale proposal. Never creates HPA/KEDA objects or changes replicas."""
import math


def propose(*, backlog, kv_fraction, age_s, current=1, physical_gpus=1, max_replicas=2):
    result = {"mode": "offline preview only", "apply_allowed": False, "current": current,
              "physical_gpus": physical_gpus, "controller_path": "single KEDA-managed HPA (design)"}
    if (not all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
                for v in [backlog, kv_fraction, age_s, current, physical_gpus, max_replicas]) or
            not 0 <= kv_fraction <= 1 or not 0 <= age_s <= 4 or backlog < 0 or
            any(type(v) is not int for v in [backlog, current, physical_gpus, max_replicas]) or
            current < 1 or physical_gpus < 0 or max_replicas < current):
        return {**result, "status": "TELEMETRY_OR_CAPACITY_INVALID", "desired": None}
    desired = min(max_replicas, current + 1) if backlog > 0 or kv_fraction >= .85 else current
    status = "CAPACITY_BLOCKED" if desired > physical_gpus else "PROPOSAL_ONLY" if desired > current else "STEADY"
    return {**result, "status": status, "desired": desired,
            "deployable_without_new_gpu": min(desired, physical_gpus),
            "warning": "Kueue/time-slicing logical slots are not extra physical GPUs; loading/warmup not estimated"}
