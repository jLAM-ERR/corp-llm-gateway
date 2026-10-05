"""Acceptance matrix of the litellm guardrail adoption plan (20260926): each hazard, and
each acceptance item, names the tests that fail if it regresses. The checks read the
test files' source (``ast`` / ``tokenize``), so the matrix runs in every venv and cannot
point at a test that was renamed, deleted, skipped or marked xfail."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

from corp_llm_gateway.route_gate.table import LITELLM_REGEX_TABLE, LITELLM_ROUTE_TABLE, Verdict

ROOT = Path(__file__).resolve().parents[2]

SERVED = "tests/test_desanitize_served_stack.py"
DISPATCH = "tests/litellm_hook/test_proxy_dispatch.py"
SURFACES = "tests/litellm_hook/test_logging_surfaces.py"
PROBES = "tests/litellm_hook/test_fail_open_probes.py"
INFO = "tests/litellm_hook/test_guardrail_information.py"
GUARDS = "tests/test_litellm_config_guards.py"
ARM = "tests/route_gate/test_arm_checks.py"
ENTRY = "tests/test_asgi_entrypoint.py"
TABLE = "tests/route_gate/test_table.py"
BODY = "tests/route_gate/test_body_policies.py"
H17 = "tests/litellm_hook/test_logging_snapshot_is_content_free.py"
H17B = "tests/litellm_hook/test_failure_hook_request_data_on_refusal.py"
H18 = "tests/litellm_hook/test_corp_token_never_reaches_logging_surfaces.py"
H19 = "tests/litellm_hook/test_bearer_is_hashed_before_logging.py"
LEAK = "tests/invariants/test_no_originals_leak.py"
GONLY = "tests/litellm_hook/test_guardrails_only_dispatch.py"
GUARD = "tests/route_gate/test_litellm_route_guard.py"
MATRIX = "tests/litellm_hook/test_acceptance_matrix.py"

# Rows 3 (latent), 5 (design) and 6 (registration) are guard tests: nothing egresses
# on 1.101.0, the guard fails the moment the precondition goes.
HAZARDS: dict[str, tuple[str, ...]] = {
    "1": (
        f"{SURFACES}::test_standard_logging_payload_is_content_free",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
        f"{INFO}::test_the_payload_carries_exactly_our_entry",
    ),
    "2": (f"{PROBES}::test_opt_out_metadata_overwritten",),
    "3": (
        f"{GONLY}::test_the_guardrail_declares_it_enforces_request_content",
        f"{GONLY}::test_a_guardrails_only_scan_runs_our_pre_call",
        f"{GONLY}::test_without_the_attribute_the_scan_skips_our_pre_call",
    ),
    "4": (f"{PROBES}::test_m4_status_codes_through_proxy",),
    "5": (
        f"{INFO}::test_the_writer_is_never_a_registered_callback",
        f"{MATRIX}::test_the_guardrail_stays_a_plain_custom_logger",
        f"{GUARDS}::test_compose_litellm_config_starts_no_litellm_guardrail",
        f"{GUARDS}::test_helm_litellm_config_starts_no_litellm_guardrail",
    ),
    "6": (
        f"{ENTRY}::test_the_process_exits_70_when_the_guardrail_is_not_registered",
        f"{ENTRY}::test_a_config_listing_a_different_callback_exits_70",
        f"{ARM}::test_no_guardrail_of_ours_is_absent",
        f"{ARM}::test_the_real_guardrail_class_is_recognised_by_type",
    ),
    "7": (
        f"{DISPATCH}::test_apply_guardrail_flips_dispatch",
        f"{ARM}::test_apply_guardrail_anywhere_in_the_mro_is_refused",
        f"{ENTRY}::test_a_bypassable_guardrail_exits_70_with_its_own_line",
        f"{INFO}::test_the_writer_is_never_a_registered_callback",
    ),
    "8": (
        f"{SURFACES}::test_decorated_apply_guardrail_logs_exception_text",
        f"{INFO}::test_a_raising_writer_is_logged_by_type_and_leaves_no_entry",
        f"{INFO}::test_debug_lines_carrying_our_entry_are_content_free",
        f"{INFO}::test_the_otel_span_is_built_from_our_entry_only",
    ),
    "9": (
        f"{SURFACES}::test_native_guardrail_endpoints_stay_refused",
        f"{TABLE}::test_the_bypass_routes_are_refused",
        f"{ENTRY}::test_a_bypass_route_is_refused_on_the_real_app",
    ),
    "10": (
        f"{DISPATCH}::test_capture_positions_unary_and_streaming",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
    ),
    "11": (
        f"{PROBES}::test_scan_raw_request_and_run_in_parallel_refused",
        f"{PROBES}::test_sentinel_does_not_catch_scan_raw_request",
        f"{ARM}::test_an_unsafe_flag_is_refused",
        f"{ENTRY}::test_a_bypassable_guardrail_exits_70_with_its_own_line",
        f"{ENTRY}::test_the_test_only_key_never_lets_a_bypassable_guardrail_arm",
    ),
    "12": (
        f"{SURFACES}::test_debug_chunk_log_and_per_chunk_hooks",
        f"{SURFACES}::test_a_custom_guardrail_turns_per_chunk_hooks_on",
        f"{SURFACES}::test_debug_and_set_verbose_are_refused_at_arm",
        f"{ARM}::test_a_logger_at_debug_is_refused",
        f"{ARM}::test_set_verbose_is_refused",
        f"{ENTRY}::test_litellm_debug_exits_70_and_config_check_reports_it",
        f"{ENTRY}::test_the_test_only_key_exits_78_in_prod_before_litellm_is_imported",
        f"{GUARDS}::test_compose_gateway_env_turns_no_litellm_debug_on",
        f"{GUARDS}::test_helm_gateway_env_turns_no_litellm_debug_on",
        f"{SERVED}::test_litellms_debug_output_after_the_pre_call_holds_no_original",
        f"{INFO}::test_the_writer_is_never_a_registered_callback",
        f"{MATRIX}::test_the_guardrail_stays_a_plain_custom_logger",
    ),
    "13": (
        f"{DISPATCH}::test_capture_positions_unary_and_streaming",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
    ),
    # 14 and 15b on the migrated fixture: they pin why the base class stays.
    "14": (
        f"{DISPATCH}::test_today_plain_callback_runs_regardless_of_policies",
        f"{DISPATCH}::test_migrated_pipeline_skip_egresses_originals",
        f"{INFO}::test_the_writer_is_never_a_registered_callback",
        f"{MATRIX}::test_the_guardrail_stays_a_plain_custom_logger",
        f"{TABLE}::test_the_management_routes_are_refused",
    ),
    "14a": (
        f"{PROBES}::test_migrated_pipeline_skip_egresses_originals_via_db_row",
        f"{GUARDS}::test_compose_litellm_config_loads_no_policies_from_the_db",
        f"{GUARDS}::test_helm_litellm_config_loads_no_policies_from_the_db",
        f"{GUARDS}::test_litellm_reads_the_pin_as_models_only",
    ),
    "14b": (
        f"{PROBES}::test_migrated_pipeline_skip_egresses_originals_via_body_version_id",
        f"{BODY}::test_a_top_level_policies_key_is_refused_before_litellm_or_a_slot",
        f"{BODY}::test_an_escaped_key_is_the_same_key",
        f"{BODY}::test_a_key_split_across_body_chunks_is_seen",
        f"{BODY}::test_a_body_that_is_not_json_is_refused_before_litellm_or_a_slot",
        f"{SERVED}::test_a_body_naming_policies_is_refused_at_the_gate",
        f"{SERVED}::test_a_form_body_is_refused_at_the_gate",
    ),
    # Not closed (DRI options in the plan); these pin what the overlay can and cannot do.
    "14c": (
        f"{GUARDS}::test_compose_litellm_reads_its_config_table",
        f"{GUARDS}::test_helm_litellm_has_no_database_to_read_a_config_table_from",
        f"{GUARDS}::test_a_db_litellm_settings_row_appends_callbacks_and_starts_no_guardrail",
        f"{GUARDS}::test_a_db_general_settings_row_turns_on_prompts_in_spend_logs",
        f"{GUARDS}::test_only_litellms_startup_config_load_starts_guardrails",
        f"{SERVED}::test_the_db_row_adds_both_capture_kinds_prompt_logging_and_a_route",
        f"{SERVED}::test_a_db_added_callback_sees_placeholders_only",
        f"{SERVED}::test_a_spend_log_row_with_prompts_on_holds_placeholders_only",
        f"{SERVED}::test_a_db_added_callbacks_failure_log_holds_placeholders_only",
        f"{SERVED}::test_a_pass_through_route_added_at_runtime_is_404_at_the_gate",
    ),
    "15": (
        f"{DISPATCH}::test_capture_positions_unary_and_streaming",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
    ),
    "15b": (
        f"{DISPATCH}::test_duplicate_guardrail_name_substitutes_callback",
        f"{DISPATCH}::test_a_stand_in_answering_to_our_name_is_not_our_guardrail",
        f"{ARM}::test_the_real_guardrail_class_is_recognised_by_type",
        f"{INFO}::test_the_writer_is_never_a_registered_callback",
        f"{MATRIX}::test_the_guardrail_stays_a_plain_custom_logger",
    ),
    "16": (
        f"{SURFACES}::test_standard_logging_payload_is_content_free",
        f"{INFO}::test_the_payload_carries_exactly_our_entry",
        f"{INFO}::test_the_entry_lands_without_the_sync_too",
        f"{INFO}::test_an_entry_written_after_ours_still_reaches_a_shared_list",
    ),
    "17": (
        f"{H17}::test_responses_logging_payload_holds_placeholders_only",
        f"{H17}::test_chat_and_messages_logging_payload_stays_content_free",
        f"{H17}::test_a_failure_before_the_provider_call_logs_the_sanitised_input",
        f"{SURFACES}::test_standard_logging_payload_is_content_free",
        f"{SURFACES}::test_responses_list_input_reaches_the_success_log_payload",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
    ),
    "17b": (
        f"{H17B}::test_a_dlp_rejection_hands_the_failure_hook_the_sanitised_request",
        f"{H17B}::test_a_stage0_rejection_hands_the_failure_hook_the_original",
        f"{H17B}::test_the_body_snapshot_takes_the_rewritten_content_keys",
        f"{H17B}::test_the_body_snapshot_gains_no_key_the_request_added",
        f"{H17B}::test_no_callback_of_ours_overrides_the_failure_hook",
        f"{GUARDS}::test_compose_litellm_config_logs_no_request_content",
        f"{GUARDS}::test_helm_litellm_config_logs_no_request_content",
    ),
    "18": (
        f"{H18}::test_no_logging_surface_holds_the_corp_token",
        f"{H18}::test_the_byok_authorization_survives_where_the_corp_token_is_dropped",
        f"{H18}::test_a_refused_request_hands_no_failure_hook_the_corp_token",
        f"{LEAK}::test_corp_token_never_reaches_litellms_logging_snapshot",
        f"{LEAK}::test_a_refused_corp_token_is_stripped_before_the_refusal",
        f"{SERVED}::test_no_logging_surface_holds_the_corp_token",
        f"{SERVED}::test_a_provider_error_log_holds_no_corp_token",
    ),
    # A precondition on litellm, not a fix of ours: fails if litellm stops hashing.
    "19": (
        f"{H19}::test_a_shipped_bearer_shape_is_logged_only_hashed",
        f"{H19}::test_an_anthropic_x_api_key_is_logged_only_hashed",
        f"{H19}::test_a_bearer_of_any_other_shape_is_logged_raw",
    ),
    # Open (follow-up): characterised at litellm's exact sites, not fixed.
    "19b": (
        f"{H19}::test_the_chatgpt_bridge_bearer_sits_raw_in_litellms_log_kwargs",
        f"{H19}::test_the_anthropic_bridge_token_sits_raw_in_litellms_log_kwargs",
        f"{H19}::test_the_sites_are_where_litellm_keeps_any_provider_key",
    ),
}

ACCEPTANCE: dict[str, tuple[str, ...]] = {
    # Option A: exactly one reversal, outside litellm; production assertions only.
    "no_original_inside_litellm": (
        f"{SERVED}::test_the_provider_gets_placeholders_and_the_client_its_originals",
        f"{SERVED}::test_no_litellm_capture_holds_an_original",
        f"{SERVED}::test_litellms_debug_output_after_the_pre_call_holds_no_original",
        f"{SERVED}::test_the_openai_sdk_chat_stream_gets_the_original",
        f"{SERVED}::test_exactly_one_terminal_record_with_todays_fields",
        f"{DISPATCH}::test_client_gets_originals_back_on_every_flow",
        f"{DISPATCH}::test_capture_positions_unary_and_streaming",
    ),
    "healthz_sanitization_without_the_oracle": (
        "tests/test_bootstrap.py::test_the_sanitization_probe_is_healthy_with_the_oracle_off",
        "tests/test_bootstrap.py::test_the_sanitization_probe_round_trips_through_the_live_guardrail",
    ),
    "route_refusals": (
        f"{TABLE}::test_the_bypass_routes_are_refused",
        f"{TABLE}::test_the_management_routes_are_refused",
        "tests/route_gate/test_middleware.py::test_the_management_surface_answers_403",
        f"{GUARD}::test_every_collected_route_is_listed",
        f"{GUARD}::test_every_hookless_body_route_is_refused_or_justified",
        "tests/integration/test_route_gate_container.py::test_the_litellm_admin_surface_is_refused",
        f"{BODY}::test_a_top_level_policies_key_is_refused_before_litellm_or_a_slot",
        f"{BODY}::test_a_body_that_is_not_json_is_refused_before_litellm_or_a_slot",
        f"{BODY}::test_two_content_types_are_not_json",
        f"{MATRIX}::test_every_policy_and_guardrail_route_is_refused",
    ),
}

HAZARD_IDS = {str(n) for n in range(1, 20)} | {"14a", "14b", "14c", "15b", "17b", "19b"}

_ALL = sorted({node for rows in (HAZARDS, ACCEPTANCE) for ids in rows.values() for node in ids})


def _module(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(), filename=path)


def _find(tree: ast.Module, names: list[str]) -> ast.AST | None:
    body: list[ast.stmt] = tree.body
    found: ast.AST | None = None
    for name in names:
        found = next(
            (
                node
                for node in body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name == name
            ),
            None,
        )
        if found is None:
            return None
        body = getattr(found, "body", [])
    return found


def _disabling_marks(decorators: list[ast.expr]) -> list[str]:
    """``pytest.mark.skip`` / ``xfail`` in any spelling, and a ``skipif`` whose condition is
    a literal or missing; ``skipif(<expression>)`` is a condition, not a disablement
    (``requires_litellm`` and friends)."""
    marks = []
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        text = ast.unparse(target)
        if re.search(r"\bmark\.(skip|xfail)$", text):
            marks.append(text)
        elif re.search(r"\bmark\.skipif$", text) and _always_skips(decorator):
            marks.append(ast.unparse(decorator))
    return marks


def _always_skips(skipif: ast.expr) -> bool:
    # pytest skips unconditionally when a skipif carries no condition at all.
    if not isinstance(skipif, ast.Call):
        return True
    conditions = [*skipif.args, *(kw.value for kw in skipif.keywords if kw.arg == "condition")]
    return not conditions or any(isinstance(c, ast.Constant) for c in conditions)


def _module_marks(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            value = node.value
            values = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
            return _disabling_marks(list(values))
    return []


def test_every_hazard_has_a_row() -> None:
    assert set(HAZARDS) == HAZARD_IDS
    assert all(HAZARDS.values())
    assert all(ACCEPTANCE.values())


@pytest.mark.parametrize("node", _ALL)
def test_every_listed_test_exists_and_runs(node: str) -> None:
    path, *names = node.split("::")
    assert (ROOT / path).is_file(), node
    tree = _module(path)
    found = _find(tree, names)
    assert isinstance(found, ast.FunctionDef | ast.AsyncFunctionDef), node
    assert found.name.startswith("test_"), node
    assert not _disablements(found), node
    assert not _module_marks(tree), node


def test_the_existence_check_catches_a_missing_test() -> None:
    tree = _module(SERVED)
    assert _find(tree, ["test_no_litellm_capture_holds_an_original"]) is not None
    assert _find(tree, ["test_no_such_test"]) is None
    marked = ast.parse("@pytest.mark.xfail(reason='x')\ndef test_a(): pass\n").body[0]
    assert _disabling_marks(marked.decorator_list) == ["pytest.mark.xfail"]


def _disablements(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return _disabling_marks(fn.decorator_list) + _body_skips(fn.body)


def _body_skips(body: list[ast.stmt]) -> list[str]:
    """A bare ``pytest.skip(...)`` / ``pytest.xfail(...)`` statement always runs; one under
    an ``if`` (or a loop, or a handler) is a condition."""
    skips = []
    for stmt in body:
        if isinstance(stmt, ast.With | ast.AsyncWith):
            skips += _body_skips(stmt.body)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            text = ast.unparse(stmt.value.func)
            if re.search(r"(^|\.)(skip|xfail)$", text):
                skips.append(text)
    return skips


def _test_function(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    (found,) = ast.parse(source).body
    assert isinstance(found, ast.FunctionDef | ast.AsyncFunctionDef)
    return found


@pytest.mark.parametrize(
    "source",
    [
        "@pytest.mark.skipif(True, reason='x')\ndef test_a(): pass\n",
        "@pytest.mark.skipif(condition=1, reason='x')\ndef test_a(): pass\n",
        "@pytest.mark.skipif(reason='x')\ndef test_a(): pass\n",
        "@pytest.mark.skipif\ndef test_a(): pass\n",
        "def test_a():\n    pytest.skip('x')\n",
        "async def test_a():\n    'Doc.'\n    pytest.skip('x')\n    assert False\n",
        "async def test_a():\n    with open('f'):\n        pytest.skip('x')\n",
        "def test_a():\n    pytest.xfail('x')\n",
    ],
)
def test_an_unconditional_skip_disables_a_listed_test(source: str) -> None:
    assert _disablements(_test_function(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        "@pytest.mark.skipif(not HAVE_LITELLM, reason='x')\ndef test_a(): pass\n",
        "@requires_litellm\ndef test_a(): pass\n",
        "def test_a():\n    if not HAVE_LITELLM:\n        pytest.skip('x')\n",
        "def test_a():\n    pytest.importorskip('litellm')\n",
    ],
)
def test_a_conditional_skip_does_not(source: str) -> None:
    assert _disablements(_test_function(source)) == []


def _names(source: str) -> set[str]:
    """Identifiers only: a comment or a docstring saying "leaking" is characterisation."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return {tok.string for tok in tokens if tok.type == tokenize.NAME}


def test_no_leaking_set_survives_in_the_tests() -> None:
    """Production acceptance assertions carry no expected-leak allowance (the Task 0
    ``leaking = {"messages"}`` rows became content-free assertions)."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if "leaking" in _names(path.read_text())
    ]
    assert offenders == []
    assert "leaking" in _names('leaking = {"messages"}\n')
    assert "leaking" not in _names('# leaking == {"messages"}\n"""an original leaking"""\n')


CUSTOM_LOGGER = "litellm.integrations.custom_logger.CustomLogger"


def _import_fallback(node: ast.ExceptHandler, name: str) -> ast.Name | None:
    """``except ImportError: <name> = object``: the venv without litellm."""
    if not (
        isinstance(node.type, ast.Name) and node.type.id in {"ImportError", "ModuleNotFoundError"}
    ):
        return None
    for stmt in node.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == name
            and isinstance(stmt.value, ast.Name)
            and stmt.value.id == "object"
        ):
            return stmt.targets[0]
    return None


def _bindings(tree: ast.Module, name: str) -> list[tuple[ast.AST, str | None]]:
    """Every binding of ``name`` anywhere in the module, in source order, with what an
    import binds it to (None for anything else); the ``ImportError`` fallback excepted."""
    fallbacks = {
        id(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and (target := _import_fallback(node, name))
    }
    found: list[tuple[ast.AST, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found += [
                (node, f"{node.module}.{a.name}")
                for a in node.names
                if (a.asname or a.name) == name
            ]
        elif isinstance(node, ast.Import):
            found += [
                (node, a.name) for a in node.names if (a.asname or a.name.split(".")[0]) == name
            ]
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name:
            if id(node) not in fallbacks:
                found.append((node, None))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == name:
                found.append((node, None))
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append((node, None))
    return sorted(found, key=lambda item: (item[0].lineno, item[0].col_offset))


def _base_binding(tree: ast.Module) -> tuple[int, str | None]:
    """How many times ``CorpLlmGuardrail``'s base name is bound, and what its LAST binding
    imports: a later rebinding is what the class actually derives from."""
    ours = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CorpLlmGuardrail"
    )
    if len(ours.bases) != 1 or not isinstance(ours.bases[0], ast.Name):
        return 0, None
    bindings = _bindings(tree, ours.bases[0].id)
    return len(bindings), bindings[-1][1] if bindings else None


_BASE_IMPORT = """\
try:
    from litellm.integrations.custom_logger import (
        CustomLogger as _LitellmCustomLogger,
    )
except ImportError:
    _LitellmCustomLogger = object
"""
_OURS = "\n\nclass CorpLlmGuardrail(_LitellmCustomLogger):\n    pass\n"


def test_the_base_pin_reads_the_shipped_shape() -> None:
    assert _base_binding(ast.parse(_BASE_IMPORT + _OURS)) == (1, CUSTOM_LOGGER)


@pytest.mark.parametrize(
    "rebinding",
    [
        "from litellm.integrations.custom_guardrail import "
        "CustomGuardrail as _LitellmCustomLogger\n",
        "if True:\n"
        "    from litellm.integrations.custom_guardrail import (\n"
        "        CustomGuardrail as _LitellmCustomLogger,\n"
        "    )\n",
        "from litellm.integrations.custom_guardrail import CustomGuardrail\n"
        "_LitellmCustomLogger = CustomGuardrail\n",
        "try:\n    pass\nexcept ImportError:\n    _LitellmCustomLogger = CustomGuardrail\n",
    ],
)
def test_a_later_rebinding_of_the_base_fails_the_pin(rebinding: str) -> None:
    assert _base_binding(ast.parse(_BASE_IMPORT + rebinding + _OURS)) != (1, CUSTOM_LOGGER)


def test_the_guardrail_stays_a_plain_custom_logger() -> None:
    """Option 0: a ``CustomGuardrail`` base re-opens the pipeline skip (14), the name
    substitution (15b) and per-chunk dispatch for every callback (12). Read from the
    source, so the pin holds in a venv without litellm too."""
    tree = _module("src/corp_llm_gateway/litellm_hook.py")
    ours = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CorpLlmGuardrail"
    )
    assert _base_binding(tree) == (1, CUSTOM_LOGGER)
    assert "apply_guardrail" not in {
        node.name for node in ours.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_policy_and_guardrail_route_is_refused() -> None:
    """Every litellm route of the policy and guardrail families, mutations included, is
    REFUSE (hazards 9, 14): the table is the only way in over HTTP."""
    rows = [(method, path, entry) for (method, path), entry in LITELLM_ROUTE_TABLE.items()]
    rows += [(row.method, row.template, row.entry) for row in LITELLM_REGEX_TABLE]
    family = [
        (method, path, entry.verdict)
        for method, path, entry in rows
        if re.search(r"polic|guardrail", path)
    ]
    assert len(family) >= 50
    assert {method for method, _, _ in family} >= {"POST", "PUT", "PATCH", "DELETE"}
    assert [row for row in family if row[2] is not Verdict.REFUSE] == []
