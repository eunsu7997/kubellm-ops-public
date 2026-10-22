from scripts.ops.scale_preview import propose


def test_one_physical_gpu_does_not_become_two_by_increasing_replicas():
    r = propose(backlog=4, kv_fraction=.9, age_s=0)
    assert r["desired"] == 2
    assert r["status"] == "CAPACITY_BLOCKED"
    assert r["deployable_without_new_gpu"] == 1
    assert not r["apply_allowed"]


def test_stale_nan_or_percent_instead_of_fraction_blocks_scaling():
    for fraction, age in [(float("nan"), 0), (85, 0), (.1, 5), (.1, -1)]:
        assert propose(backlog=0, kv_fraction=fraction, age_s=age)["desired"] is None


def test_steady_or_hypothetical_capacity_is_still_preview_only():
    assert propose(backlog=0, kv_fraction=0, age_s=0)["status"] == "STEADY"
    r = propose(backlog=4, kv_fraction=.9, age_s=0, physical_gpus=2)
    assert r["status"] == "PROPOSAL_ONLY"
    assert not r["apply_allowed"]
