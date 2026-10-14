"""What litellm itself does with the shipped configs' settings: the ``supported_db_objects``
pin (plan 20260926 hazard 14a), what its config-table overlay can still add (hazard 14c),
and why its own guardrails are not adopted (plan 20260926 Task 6).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"

LITELLM_CONFIGS = {
    "compose-anthropic": COMPOSE / "litellm" / "config.anthropic.yaml",
    "compose-codex": COMPOSE / "litellm" / "config.codex.yaml",
    "compose-corp": COMPOSE / "litellm" / "config.corp.yaml",
}


def test_litellm_reads_the_pin_as_models_only() -> None:
    """litellm's own reader of the key (what its DB reconcile consults)."""
    litellm_proxy = pytest.importorskip("litellm.proxy.proxy_server")
    config = yaml.safe_load(LITELLM_CONFIGS["compose-corp"].read_text())
    patched = pytest.MonkeyPatch()
    try:
        patched.setattr(litellm_proxy, "general_settings", config["general_settings"])
        loads = {
            name: litellm_proxy.should_load_db_object(object_type=name)
            for name in ("models", "policies", "guardrails", "config_overrides")
        }
    finally:
        patched.undo()
    assert loads == {
        "models": True,
        "policies": False,
        "guardrails": False,
        "config_overrides": False,
    }


# ── hazard 14c: litellm's config-table overlay ──────────────────────────────
# With a database and ``store_model_in_db`` (env ``STORE_MODEL_IN_DB``, or a DB
# ``general_settings`` row saying so), litellm's reconcile job re-reads the
# ``LiteLLM_Config`` table every 30 s and applies two rows: ``litellm_settings``
# (``_update_config_fields`` + ``_add_callbacks_from_db_config``) and ``general_settings``
# (``_update_general_settings``); ``supported_db_objects`` gates neither. Since 1.1.0
# neither compose nor the Helm chart gives litellm a database, so the overlay is closed in
# the shipped setups; the tests below pin what it would do if an operator added one. The
# served-stack side (what such a callback sees, the spend-log row, a pass-through route)
# is ``tests/test_desanitize_served_stack.py``.


def test_a_db_litellm_settings_row_appends_callbacks_and_starts_no_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the reconcile does with a DB ``litellm_settings`` row: a ``callbacks`` name
    litellm does not know is appended to ``litellm.callbacks`` as a string that resolves to
    nothing (a known integration's name is instantiated into the success/failure lists
    instead, log events only); the arm check never sees it, and ``guardrails`` starts
    nothing."""
    litellm = pytest.importorskip("litellm")
    proxy = pytest.importorskip("litellm.proxy.proxy_server")
    litellm_logging = pytest.importorskip("litellm.litellm_core_utils.litellm_logging")
    from corp_llm_gateway.route_gate.arm_checks import guardrail_problems

    ours = object()
    monkeypatch.setattr(litellm, "callbacks", [ours])
    config = yaml.safe_load(LITELLM_CONFIGS["compose-corp"].read_text())
    row = {
        "callbacks": ["db-row-callback"],
        "guardrails": [{"guardrail_name": "db-row", "litellm_params": {"guardrail": "x"}}],
    }

    merged = proxy.proxy_config._update_config_fields(
        current_config=config, param_name="litellm_settings", db_param_value=row
    )
    proxy.proxy_config._add_callbacks_from_db_config(merged)

    assert merged["litellm_settings"]["callbacks"] == ["db-row-callback"]
    assert litellm.callbacks == [ours, "db-row-callback"]
    assert "db-row-callback" not in litellm._known_custom_logger_compatible_callbacks
    assert litellm_logging.get_custom_logger_compatible_class("db-row-callback") is None
    assert guardrail_problems(litellm.callbacks, is_ours=lambda cb: cb is ours) == []


@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
async def test_a_db_general_settings_row_turns_on_prompts_in_spend_logs(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The shipped configs do not set ``store_prompts_in_spend_logs``, so a DB row's value
    is the one litellm uses (YAML wins only for a key it sets)."""
    proxy = pytest.importorskip("litellm.proxy.proxy_server")
    general = dict(yaml.safe_load(LITELLM_CONFIGS[name].read_text())["general_settings"])
    monkeypatch.setattr(proxy, "general_settings", general)
    monkeypatch.setattr(proxy.proxy_config, "_yaml_general_settings_keys", set(general))

    await proxy.proxy_config._update_general_settings({"store_prompts_in_spend_logs": True})

    assert proxy.general_settings["store_prompts_in_spend_logs"] is True


def test_the_pinned_names_are_litellms_own() -> None:
    """The guardrail names litellm 1.101.0 registers and reads, and both guardrails run
    through its unified dispatch."""
    pytest.importorskip("litellm.proxy.proxy_server")
    from litellm.integrations.custom_guardrail import CustomGuardrail
    from litellm.proxy.guardrails.guardrail_hooks.custom_code.custom_code_guardrail import (
        CustomCodeGuardrail,
    )
    from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import (
        ContentFilterGuardrail,
    )
    from litellm.proxy.guardrails.guardrail_registry import guardrail_initializer_registry
    from litellm.types.guardrails import LitellmParams

    assert {"litellm_content_filter", "custom_code"} <= set(guardrail_initializer_registry)
    assert {"on_sensitive_data", "sensitive_data_route_to_model"} <= set(LitellmParams.model_fields)
    # Both run through litellm's unified_guardrail dispatch (hazards 7, 8, 12).
    for cls in (ContentFilterGuardrail, CustomCodeGuardrail):
        assert issubclass(cls, CustomGuardrail)
        assert "apply_guardrail" in vars(cls)


class _LitellmRecords(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def test_litellm_content_filter_masks_one_way_and_logs_its_matches() -> None:
    """Why it is not adopted: two different emails come out as the same tag (no mapping
    to undo, unlike our placeholders), and a configured term it masks goes to litellm's
    INFO log and ``guardrail_information`` (every ``StandardLoggingPayload`` sink); a
    blocked one goes into the 400 body."""
    pytest.importorskip("litellm.proxy.proxy_server")
    from fastapi import HTTPException
    from litellm._logging import verbose_proxy_logger
    from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import (
        ContentFilterGuardrail,
    )

    term = "falcon-ledger"
    email = {"pattern_type": "prebuilt", "pattern_name": "email", "action": "MASK"}
    masking = ContentFilterGuardrail(
        guardrail_name="content-filter",
        patterns=[email],
        blocked_words=[{"keyword": term, "action": "MASK"}],
    )
    blocking = ContentFilterGuardrail(
        guardrail_name="content-filter",
        blocked_words=[{"keyword": term, "action": "BLOCK"}],
    )
    data: dict[str, Any] = {"metadata": {}}
    records = _LitellmRecords()
    level = verbose_proxy_logger.level
    verbose_proxy_logger.addHandler(records)
    verbose_proxy_logger.setLevel(logging.INFO)
    try:
        out = await masking.apply_guardrail(
            inputs={"texts": [f"ask alice@corp.example and bob@corp.example about {term}"]},
            request_data=data,
            input_type="request",
        )
        with pytest.raises(HTTPException) as blocked:
            await blocking.apply_guardrail(
                inputs={"texts": [f"about {term}"]},
                request_data={"metadata": {}},
                input_type="request",
            )
    finally:
        verbose_proxy_logger.removeHandler(records)
        verbose_proxy_logger.setLevel(level)

    assert out["texts"] == ["ask [EMAIL_REDACTED] and [EMAIL_REDACTED] about [KEYWORD_REDACTED]"]
    assert any(term in message for message in records.messages)
    (entry,) = data["metadata"]["standard_logging_guardrail_information"]
    assert term in str(entry["match_details"])
    assert term in str(blocked.value.detail)
