from corp_llm_gateway.profiles import PolicyKnobs
from corp_llm_gateway.team_config.models import FailPolicyOverrides


def test_merge_empty_returns_defaults() -> None:
    assert PolicyKnobs.merge([]) == PolicyKnobs()


def test_merge_single_layer_is_identity() -> None:
    layer = PolicyKnobs(size_threshold_bytes=10, block_payloads=True)
    assert PolicyKnobs.merge([layer]) == layer


def test_merge_size_takes_min() -> None:
    a = PolicyKnobs(size_threshold_bytes=100_000)
    b = PolicyKnobs(size_threshold_bytes=64_000)
    assert PolicyKnobs.merge([a, b]).size_threshold_bytes == 64_000


def test_merge_flags_are_or() -> None:
    a = PolicyKnobs(block_payloads=False, dlp_guard=True)
    b = PolicyKnobs(block_payloads=True, dlp_guard=False)
    merged = PolicyKnobs.merge([a, b])
    assert merged.block_payloads is True
    assert merged.dlp_guard is True


def test_merge_providers_intersection() -> None:
    a = PolicyKnobs(allowed_providers=frozenset({"anthropic", "openai"}))
    b = PolicyKnobs(allowed_providers=frozenset({"anthropic"}))
    assert PolicyKnobs.merge([a, b]).allowed_providers == frozenset({"anthropic"})


def test_merge_none_providers_is_unrestricted_identity() -> None:
    restricted = PolicyKnobs(allowed_providers=frozenset({"anthropic"}))
    assert PolicyKnobs.merge([PolicyKnobs(), restricted]).allowed_providers == frozenset(
        {"anthropic"}
    )
    assert PolicyKnobs.merge([PolicyKnobs(), PolicyKnobs()]).allowed_providers is None


def test_merge_fail_policy_most_closed() -> None:
    a = PolicyKnobs(fail_policy=FailPolicyOverrides(pre_pass_down="continue"))
    b = PolicyKnobs(fail_policy=FailPolicyOverrides(pre_pass_down="fail-closed"))
    merged = PolicyKnobs.merge([a, b])
    assert merged.fail_policy.pre_pass_down == "fail-closed"
    # a "continue" layer cannot re-open a closed column
    assert PolicyKnobs.merge([b, a]).fail_policy.pre_pass_down == "fail-closed"


def test_merge_canaries_union_order_preserving() -> None:
    a = PolicyKnobs(canary_patterns=("X1", "X2"))
    b = PolicyKnobs(canary_patterns=("X2", "X3"))
    assert PolicyKnobs.merge([a, b]).canary_patterns == ("X1", "X2", "X3")


def test_merge_oracle_keeps_most_coverage() -> None:
    lo = PolicyKnobs(oracle_mode="gazetteer_hit")
    hi = PolicyKnobs(oracle_mode="always")
    assert PolicyKnobs.merge([lo, hi]).oracle_mode == "always"
    # order-independent: a low-coverage layer never narrows a high one
    assert PolicyKnobs.merge([hi, lo]).oracle_mode == "always"


def test_merge_retention_is_last_writer() -> None:
    a = PolicyKnobs(retention_hot_days=90)
    b = PolicyKnobs(retention_hot_days=30)
    assert PolicyKnobs.merge([a, b]).retention_hot_days == 30
    # an unset (None) layer does not clobber an earlier value
    assert PolicyKnobs.merge([a, PolicyKnobs()]).retention_hot_days == 90


def test_merge_is_monotone_tightening() -> None:
    core = PolicyKnobs(
        size_threshold_bytes=100_000,
        allowed_providers=frozenset({"anthropic", "openai"}),
    )
    add = PolicyKnobs(
        size_threshold_bytes=50_000,
        block_payloads=True,
        dlp_guard=True,
        allowed_providers=frozenset({"anthropic"}),
    )
    base = PolicyKnobs.merge([core])
    tightened = PolicyKnobs.merge([core, add])
    assert tightened.size_threshold_bytes <= base.size_threshold_bytes
    assert tightened.block_payloads >= base.block_payloads
    assert tightened.dlp_guard >= base.dlp_guard
    assert base.allowed_providers is not None
    assert tightened.allowed_providers is not None
    assert tightened.allowed_providers <= base.allowed_providers
