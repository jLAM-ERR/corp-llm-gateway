# Task 3b prune audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 9, local only),
Task 3b: prune within the integration and boundary layers. Phase 1 decides keep / delete /
fold for every prunable id and deletes nothing; phase 2 (after review) does the deletions,
the [deleted-tests.md](deleted-tests.md) rows, the fault injections and the manifest
regeneration. The gates are in [must-keep.md](must-keep.md). Tree: `release/1.0.x` at `5fda500`.

## The prunable universe

Task 2 moved 249 tests out of `tests/test_litellm_hook.py` and
`tests/test_litellm_hook_adversarial.py` into `tests/litellm_hook/` and `tests/route_gate/`
(`tests/_manifests/moves.json` `ids`, current → baseline). The candidates are the `ids` whose
baseline id has no line in `tests/_manifests/must_keep/*.txt` (the `[param]` suffix stripped):
**163**, recomputed for this audit with the same per-file counts as the brief. Every other test
in those two directories is must-keep and only appears here as a survivor.

All 163 passed in both environments at the baseline (`expected_outcomes.{minimal,full}.json`;
`test_pre_call_wrapper_preserves_named_error_codes` with its 6 cases). `python -m
tests._gates.must_keep --check` is clean: no candidate is must-keep by the rules and missing
from `must_keep/` (no gate gap).

## How the criteria were applied

- **(1)** A hook test is a candidate only when every assert / raises reads the sanitised content
  and nothing the hook adds. It is **not** a candidate when an assert touches request state,
  the audit sink or metrics, an `E_*` code or status, headers / auth, a litellm signature, or
  **dispatch** — a field or call_type that `litellm_hook.py` decides to walk or to skip (the
  `messages` / `input` / `system` / `instructions` choice, the skip of empty or non-dict items,
  the content-less tool-call and Responses items, `_NON_CHAT_INPUT_CALL_TYPES`). A field that
  `sanitizer/content_blocks.py` decides to walk (a tool_result's `content`, a document's
  `title`) is algorithm-owned. The survivor must be under `tests/sanitizer`, `tests/detectors`,
  `tests/rules` or `tests/payload`, on the **same input** (same block / text / pairs), with
  checks at least as strong. A survivor on similar but different data is a near-miss and the
  id is kept; the near-misses are named in the notes.
- **(2)** A response-side test in `route_gate/test_desanitize_stream.py` is a candidate when its
  checks are a strict subset of a case in `route_gate/test_desanitize_middleware.py` or
  `tests/sanitizer/test_streaming*.py` on the same wire input and mapping, with no audit /
  ticket / status check. `test_desanitize_middleware.py` collection-skips in minimal
  (`importorskip("litellm.proxy.proxy_server")`) while every candidate passed there, so none of
  its cases can be a survivor. A `tests/sanitizer` survivor feeds `SseStreamDesanitizer`
  directly, so a deletion also needs the middleware path on the same wire input held by a
  must-keep test of the same file, and is refused where the middleware has its own branch for
  that input (`[DONE]`, the no-mapping pass-through).
- **The plan's kept list** (Task 3b item 1) is applied by its baseline line ranges:
  `:331`, `:1749-1780` and `:729` (transport-vs-internal), `:420-506` (restoration failure),
  `:4399` (cache-hit collision), allocator wiring, the ticket hand-over, stream
  reconstruction, the object-shape tests, the scalar pass-through.
- **Fold-ups (rev 9):** only a group of ≥ 3 functions in one file whose assert / raises
  statements are textually identical modulo data literals, with the same builder and helpers.

Columns: `checks` is the baseline line in `tests/_manifests/baseline_checks/_root__test_litellm_hook*.json`
(asserts / raises / fail, delegated helper checks, parametrize cases, loops). `(:N)` is the
current line. For `keep`, the note names the hook-specific check or why no survivor is as
strong; for `delete`, it is the ledger row's semantic note; `fault injection needed` names
the `src/` line phase 2 mutates and the survivor's failing assert.

## Totals

| file | ids | keep | delete | fold |
|---|---|---|---|---|
| `litellm_hook/test_pre_call_shapes.py` | 47 | 42 | 1 | 4 |
| `litellm_hook/test_fail_policy.py` | 17 | 17 | 0 | 0 |
| `litellm_hook/test_tool_calls.py` | 17 | 17 | 0 | 0 |
| `litellm_hook/test_pre_call_headers.py` | 16 | 16 | 0 | 0 |
| `litellm_hook/test_audit_facts.py` | 14 | 14 | 0 | 0 |
| `litellm_hook/test_payload_limits.py` | 12 | 12 | 0 | 0 |
| `litellm_hook/test_stage0_classifier.py` | 10 | 7 | 0 | 3 |
| `litellm_hook/test_stage5_dlp.py` | 8 | 8 | 0 | 0 |
| `litellm_hook/test_log_hygiene.py` | 6 | 6 | 0 | 0 |
| `litellm_hook/test_litellm_entrypoints.py` | 6 | 6 | 0 | 0 |
| `route_gate/test_desanitize_stream.py` | 10 | 7 | 3 | 0 |
| **total** | **163** | **152** | **4** | **7** |

## Audit table

### `tests/litellm_hook/test_pre_call_shapes.py` (47 ids: keep 42, delete 1, fold 4)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_sanitizes_responses_input_instructions_and_tool_output` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_sanitizes_responses_input_instructions_and_tool_output` (:27) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_request_items` picks `input` (src/corp_llm_gateway/litellm_hook.py:2456), `_sanitize_prompt_field` walks `instructions`, and `"messages" not in out` is a hook decision. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_new_anthropic_and_chat_completion_block_types_sanitize_and_pass` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_new_anthropic_and_chat_completion_block_types_sanitize_and_pass` (:54) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `sanitizer/test_content_blocks.py` has each block alone (`test_sanitize_server_tool_use_input_is_scanned` … `test_sanitize_container_upload_passes_through_unchanged`, `test_sanitize_opaque_openai_block_types_pass_through_unchanged[block2,block3]`), but `web_search_tool_result` (`title: "acme title"`) and `code_execution_tool_result` (extra `stderr`) differ, and none checks nine blocks of one assistant + user request with one `json.dumps(out)` negative check. Task 3a: not a twin group. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_newer_anthropic_block_types_sanitize_and_pass` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_newer_anthropic_block_types_sanitize_and_pass` (:121) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor: no algorithm test feeds `web_fetch_tool_result`, `bash_code_execution_tool_result`, `text_editor_code_execution_tool_result` or `code_execution_output` (only the comment at `test_content_blocks.py:1007` names them); the type-less dict is in `test_unrecognized_block_type_value_preserved_and_dict_without_type_key_is_scanned` with another value. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_responses_input_image_and_input_file_blocks_pass_without_raising` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_responses_input_image_and_input_file_blocks_pass_without_raising` (:164) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: a Responses `input` message item goes to `sanitize_responses_item` (hook, `request_shape`); `test_sanitize_opaque_openai_block_types_pass_through_unchanged[block0,block1]` calls `sanitize_content` on each block alone, without the `input_text` sibling. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_genuinely_unknown_block_type_no_longer_fails_closed` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_genuinely_unknown_block_type_no_longer_fails_closed` (:185) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_genuinely_unknown_type_no_longer_fails_closed` uses `{"type": "some_future_block", "payload": "raw"}` (a scannable field); this block has only `type`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_responses_bare_string_input_element_is_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_responses_bare_string_input_element_is_sanitized` (:202) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: the hook's bare-string branch (src/corp_llm_gateway/litellm_hook.py:996-998) and `_item_text`'s str case. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_embeddings_input_string_passes_through_untouched` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_embeddings_input_string_passes_through_untouched` (:218) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F1 | the new `test_pre_call_unmanaged_call_type_input_passes_through_untouched[embedding]` | fold group F1 (below): 4 functions → 1 parametrised test, checks per id unchanged. Dispatch (`_NON_CHAT_INPUT_CALL_TYPES`) stays checked per case. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_moderations_input_list_passes_through_untouched` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_moderations_input_list_passes_through_untouched` (:233) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F1 | the new `…[moderation]` | fold group F1. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_amoderation_call_type_also_passes_through` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_amoderation_call_type_also_passes_through` (:248) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_NON_CHAT_INPUT_CALL_TYPES` membership of `amoderation`; one assert, so not in fold F1 (its body differs from F1's two asserts). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_responses_call_type_still_sanitizes_input` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_responses_call_type_still_sanitizes_input` (:259) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `responses` call_type keeps the `input` walk. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_completion_call_type_still_sanitizes_messages` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_completion_call_type_still_sanitizes_messages` (:272) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `completion` call_type keeps the `messages` walk. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_no_call_type_defaults_to_todays_behavior` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_no_call_type_defaults_to_todays_behavior` (:280) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `call_type=None` walks `input`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_explicit_null_input_passes_through_like_release` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_explicit_null_input_passes_through_like_release` (:294) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_request_items` on `input: null` (no 400). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_unrecognized_call_type_still_sanitizes_input` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_unrecognized_call_type_still_sanitizes_input` (:307) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: an unknown call_type is not unmanaged. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_speech_input_passes_through_untouched` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_speech_input_passes_through_untouched` (:324) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F1 | the new `…[aspeech]` | fold group F1. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_speech_call_type_also_passes_through` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_speech_call_type_also_passes_through` (:340) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `speech` call_type is unmanaged; one assert, not in F1. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_pass_through_endpoint_input_passes_through_untouched` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_pass_through_endpoint_input_passes_through_untouched` (:351) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F1 | the new `…[pass_through_endpoint]` | fold group F1. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_codex_profile_oracle_disabled_applies_rules_directly` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_codex_profile_oracle_disabled_applies_rules_directly` (:367) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: Responses `input` with `forward_chatgpt_auth=True`; and no survivor on the same orchestrator: `test_oracle_trigger.py::test_oracle_disabled_local_pass_branch_applies_replace_md_rules` adds an email and a static-finding detector instead of `RegexChecksumDetector`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_replaces_message_content_with_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_replaces_message_content_with_sanitized` (:414) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: the hook loop walks `messages[0].content` (a string). ⚠️ Intra-layer twin, outside both criteria: the first block of `test_pre_call_single_key_shapes_unaffected_by_hybrid_guard` has the same builder, pairs, data and assert. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_same_email_two_segments_reuses_one_token` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_same_email_two_segments_reuses_one_token` (:421) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: allocator wiring across fields (system + message). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_collision_across_blocks_in_one_message` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_collision_across_blocks_in_one_message` (:436) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: allocator wiring across blocks. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_collision_message_vs_tool_result` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_collision_message_vs_tool_result` (:454) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: allocator wiring (text block vs tool_result). Also holds the hook path `messages` → `tool_result` with str content for the `test_pre_call_tool_result_block_sanitized` deletion. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_rejects_non_list_messages` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_rejects_non_list_messages` (:472) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: `E_BAD_REQUEST` (src/corp_llm_gateway/litellm_hook.py:721-733). ⚠️ Near-twin outside the criteria: `test_audit_facts.py::test_pre_call_bad_request_audits_inline` (adds `Authorization`, content `hi`, and the audit asserts). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_skips_unwrapped_literal_scan_when_codex_flag_off` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_skips_unwrapped_literal_scan_when_codex_flag_off` (:480) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned: the `find_unwrapped_placeholder_literals` gate (src/corp_llm_gateway/litellm_hook.py:844-850). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_runs_unwrapped_literal_scan_when_codex_flag_on` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_runs_unwrapped_literal_scan_when_codex_flag_on` (:504) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned: positive control of the same gate. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_block_list_message_content_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_block_list_message_content_sanitized` (:531) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_list_text_block` has no image block, `test_sanitize_list_non_text_blocks_pass_through` other text and blocks. Holds the hook path `messages` → block list for the deletion below. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_result_block_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_tool_result_block_sanitized` (:548) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **delete** | (1) | `tests/sanitizer/test_content_blocks.py::test_sanitize_tool_result_str_content`; hook path: `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_collision_message_vs_tool_result` (kept) | Same content `[{"type": "tool_result", "content": "the secret is revealed"}]` and the same expected `"the [SECRET_001] is revealed"` and `type == "tool_result"`. The survivor calls `sanitize_content` directly with a `str.replace("secret", "[SECRET_001]")` sanitize_one; the deleted test reached the same `sanitize_content` → `_sanitize_block` tool_result branch through `pre_call` → `sanitize_message`, with the oracle-only orchestrator over `_corp_llm_returning([("secret", "[SECRET_001]")])` and the request allocator (one pair, so the remap is the identity). The survivor also asserts one block and one result. The hook's walk of `messages[0].content` into a tool_result with str content stays checked by `test_pre_call_collision_message_vs_tool_result`. Normalised bodies differ (other module, other builder); compared by hand. | yes — `src/corp_llm_gateway/sanitizer/content_blocks.py:425` `return {**block, "content": new_sub}, sub_results` → `return dict(block), sub_results`; the survivor fails `assert new_content[0]["content"] == "the [SECRET_001] is revealed"` (and the hook-path holder fails at `re.search(...).group(0)` on the raw tool_result text) |
| `tests/test_litellm_hook.py::test_pre_call_system_str_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_system_str_sanitized` (:564) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_sanitize_prompt_field` walks `system` (str). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_system_list_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_system_list_sanitized` (:573) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_sanitize_prompt_field` walks `system` (block list). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_no_system_no_op` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_no_system_no_op` (:584) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: no `system` key is added. ⚠️ Its second assert twins `test_pre_call_str_message_regression` (same pairs) — outside both criteria. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_sanitizes_both_system_and_instructions_when_both_present` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_sanitizes_both_system_and_instructions_when_both_present` (:594) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: both prompt fields walked. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_str_message_regression` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_str_message_regression` (:615) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `messages[0].content` string. ⚠️ Intra-layer twin outside both criteria: `test_pre_call_no_system_no_op`'s content assert (same builder and pairs, `hi alice` vs `hello alice`). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_hybrid_messages_empty_and_input_rejected` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_hybrid_messages_empty_and_input_rejected` (:624) | a3 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class (422 `E_POLICY_BLOCKED`) + audit negative check (`original not in json.dumps(sink.records)`). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_hybrid_messages_none_and_input_rejected` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_hybrid_messages_none_and_input_rejected` (:643) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: `messages: None` + `input` is refused. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_hybrid_payload_audit_matches_stage0_convention` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_hybrid_payload_audit_matches_stage0_convention` (:660) | a7 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: `block_reason` / status / user / team of the record. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_single_key_shapes_unaffected_by_hybrid_guard` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_single_key_shapes_unaffected_by_hybrid_guard` (:682) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: messages-only, `input` str and `input` list each walked. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_document_block_title_and_text_source_redacted` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_document_block_title_and_text_source_redacted` (:708) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_document_title_and_text_source` adds `context` and uses `See bob@corp.example for details`; `test_no_original_in_sanitized_tool_use_and_document` has bob in both fields. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_document_block_base64_source_untouched` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_document_block_base64_source_untouched` (:732) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_document_base64_source_untouched` has title `A title` and an identity mock, so it never checks that the title is redacted. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_empty_list_content_no_crash` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_empty_list_content_no_crash` (:751) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: the hook's `content_empty` test (src/corp_llm_gateway/litellm_hook.py:946) decides whether `[]` reaches the walker, and the assert reads that field. Near-miss: `test_content_blocks.py::test_sanitize_empty_list` has the same `[]` but starts below that decision. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_content_with_only_non_text_blocks` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_content_with_only_non_text_blocks` (:761) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_list_non_text_blocks_pass_through` adds a text and a document block; here the unreachable corp LLM proves no oracle call through `pre_call`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_missing_content_field_no_crash` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_missing_content_field_no_crash` (:778) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: the hook's skip branch (src/corp_llm_gateway/litellm_hook.py:960-967). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_system_empty_str` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_system_empty_str` (:792) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `_sanitize_prompt_field` skips an empty `system`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_system_empty_list` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_system_empty_list` (:802) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: same skip for `[]`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_multiple_text_blocks_in_content_all_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_multiple_text_blocks_in_content_all_sanitized` (:812) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_multiple_text_blocks` has three text blocks with one label and no image block. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_deeply_nested_tool_result_blocks_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_deeply_nested_tool_result_blocks_sanitized` (:828) | a7 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor: no algorithm test nests a tool_result with bare-dict content inside a tool_result list. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_non_dict_message_items_skipped` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_non_dict_message_items_skipped` (:862) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: the hook loop skips non-dict, non-str items (src/corp_llm_gateway/litellm_hook.py:940-942). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_result_bare_dict_content_sanitized` | `tests/litellm_hook/test_pre_call_shapes.py::test_pre_call_tool_result_bare_dict_content_sanitized` (:882) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor: a bare-dict tool_result content is only in `test_collect_text_bare_dict_tool_result` (read-only collect, other shape). | n/a |

### `tests/litellm_hook/test_fail_policy.py` (17 ids: keep 17, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_unexpected_error_records_metrics_failure` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_unexpected_error_records_metrics_failure` (:74) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | metrics + error class: `gateway_failure{internal}` on the F8 path. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_wrapper_does_not_swallow_cancelled_error` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_wrapper_does_not_swallow_cancelled_error` (:91) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class (`CancelledError` not caught) + audit (no record). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_non_dict_data_returns_opaque_500` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_non_dict_data_returns_opaque_500` (:110) | a4 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: opaque 500 `E_INTERNAL`, no `__cause__`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_wrapper_preserves_named_error_codes` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_wrapper_preserves_named_error_codes` (:145) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; parametrize 6 cases | keep | (1) excluded | — | error class, per case (6): the wrapper passes classified errors through. | n/a |
| `tests/test_litellm_hook.py::test_auth_error_message_lookup_falls_back_safely_for_unmapped_code` | `tests/litellm_hook/test_fail_policy.py::test_auth_error_message_lookup_falls_back_safely_for_unmapped_code` (:164) | a4 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + auth: static message for an unmapped code. | n/a |
| `tests/test_litellm_hook.py::test_auth_error_messages_keys_match_classify_auth_error_return_values` | `tests/litellm_hook/test_fail_policy.py::test_auth_error_messages_keys_match_classify_auth_error_return_values` (:203) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: transport-vs-internal classification (`:729` at the baseline). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_stale_span_in_message_loop_maps_to_fail_policy_matrix` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_stale_span_in_message_loop_maps_to_fail_policy_matrix` (:223) | a7 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class (`E_SPAN_INVALID`) + audit negative check. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_stale_span_in_prompt_field_maps_to_fail_policy_matrix` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_stale_span_in_prompt_field_maps_to_fail_policy_matrix` (:260) | a7 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + audit, prompt-field path. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_corp_llm_down_fails_closed_503` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_corp_llm_down_fails_closed_503` (:292) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: `:1749-1780` at the baseline. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_corp_llm_down_does_not_forward_content` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_corp_llm_down_does_not_forward_content` (:309) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: `:1766`, inside `:1749-1780`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oracle_disabled_gazetteer_hit_sanitizes_without_oracle` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_oracle_disabled_gazetteer_hit_sanitizes_without_oracle` (:322) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | kept list: its baseline def is `:1779`, inside `:1749-1780` ("oracle off is not down"). Near-miss, not used: `tests/sanitizer/test_oracle_trigger.py::test_oracle_disabled_gazetteer_hit_skips_oracle_local_findings_applied` (same text and gazetteer; a counting client where this test passes none). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_ner_required_but_absent_returns_503_not_500` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_ner_required_but_absent_returns_503_not_500` (:336) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: `E_NER_UNAVAILABLE` 503. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_ner_required_but_absent_on_system_returns_503` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_ner_required_but_absent_on_system_returns_503` (:349) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class on the system path. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_ner_required_but_absent_does_not_forward_content` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_ner_required_but_absent_does_not_forward_content` (:360) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: fail-closed, content not forwarded. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_ner_not_required_stays_on_dev_graceful_path` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_ner_not_required_stays_on_dev_graceful_path` (:370) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: the M4 F2 fail-open row (no 503). No survivor on the same input: `detectors/test_dual_ner_require.py::test_require_ner_off_both_absent_returns_empty_fail_open` runs the detector alone on other text. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_corp_llm_down_on_system_fails_closed_503` | `tests/litellm_hook/test_fail_policy.py::test_pre_call_corp_llm_down_on_system_fails_closed_503` (:381) | a3 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + log presence on the system path. | n/a |
| `tests/test_litellm_hook.py::test_corp_llm_fails_on_second_segment_fails_closed_503` | `tests/litellm_hook/test_fail_policy.py::test_corp_llm_fails_on_second_segment_fails_closed_503` (:397) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: partial success still 503. | n/a |

### `tests/litellm_hook/test_tool_calls.py` (17 ids: keep 17, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_responses_input_item_tool_calls_field_is_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_responses_input_item_tool_calls_field_is_sanitized` (:19) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: a content-less Responses item is walked because of `any(_item_text(msg))` (src/corp_llm_gateway/litellm_hook.py:959). `test_sanitize_responses_item_covers_tool_calls_field` uses other values. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_responses_custom_tool_call_dict_input_is_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_responses_custom_tool_call_dict_input_is_sanitized` (:47) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch (same branch). `test_sanitize_responses_item_dict_shaped_input_is_scanned` uses other values and no `name`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_openai_tool_calls_arguments_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_openai_tool_calls_arguments_sanitized` (:69) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: `content: None` + `message_has_tool_calls` (src/corp_llm_gateway/litellm_hook.py:955) keeps the message. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_legacy_function_call_arguments_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_legacy_function_call_arguments_sanitized` (:81) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: same branch for `function_call`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_calls_secret_in_arguments_caught_by_dlp` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_tool_calls_secret_in_arguments_caught_by_dlp` (:98) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: Stage 5 `E_DLP_BLOCKED` over `collect_tool_call_text`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_calls_invalid_json_arguments_sanitized_whole` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_tool_calls_invalid_json_arguments_sanitized_whole` (:113) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: tool-call-only message; no algorithm test on the same input. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_anthropic_tool_use_still_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_anthropic_tool_use_still_sanitized` (:123) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_tool_use_flat_input` is `{"to": "a@x.com"}` without a text sibling; the `"tool_calls" not in` negative check has no twin. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_calls_dict_arguments_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_tool_calls_dict_arguments_sanitized` (:137) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: tool-call-only message, dict arguments. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_calls_dict_arguments_secret_caught_by_dlp` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_tool_calls_dict_arguments_secret_caught_by_dlp` (:150) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: Stage 5 on dict arguments. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_tool_calls_unrecognized_scalar_arguments_fails_closed` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_tool_calls_unrecognized_scalar_arguments_fails_closed` (:166) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: `UnsanitizableToolArgumentsError` → 400 (src/corp_llm_gateway/litellm_hook.py:1012-1021). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_deep_nesting_returns_400_e_bad_request` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_deep_nesting_returns_400_e_bad_request` (:178) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (1) excluded | — | error class: `ContentTooDeepError` → 400 (src/corp_llm_gateway/litellm_hook.py:1003-1011); `test_sanitize_json_depth_limit_raises` checks only the exception. | n/a |
| `tests/test_litellm_hook.py::test_tool_use_input_nested_dict_in_dict_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_tool_use_input_nested_dict_in_dict_sanitized` (:193) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_tool_use_nested_dict` and `test_sanitize_tool_use_non_str_scalars_unchanged` split this dict; neither has both parts. | n/a |
| `tests/test_litellm_hook.py::test_tool_use_input_list_of_strings_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_tool_use_input_list_of_strings_sanitized` (:219) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_tool_use_list_input` is `{"cc": [two emails]}`, no unmatched element. | n/a |
| `tests/test_litellm_hook.py::test_tool_use_input_dict_keys_not_altered` | `tests/litellm_hook/test_tool_calls.py::test_tool_use_input_dict_keys_not_altered` (:240) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_tool_use_dict_key_preserved` (key `email`, upper-case mock) and `test_sanitize_tool_use_flat_input` (`a@x.com`, replace mock) differ from `addr@corp.example` through the per-segment corp LLM. | n/a |
| `tests/test_litellm_hook.py::test_tool_use_input_no_pii_passes_through` | `tests/litellm_hook/test_tool_calls.py::test_tool_use_input_no_pii_passes_through` (:265) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input. | n/a |
| `tests/test_litellm_hook.py::test_tool_use_input_image_block_still_passes_through` | `tests/litellm_hook/test_tool_calls.py::test_tool_use_input_image_block_still_passes_through` (:284) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input: `test_sanitize_image_still_passes_through_unchanged` has the image block alone. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_local_shell_call_action_command_is_sanitized` | `tests/litellm_hook/test_tool_calls.py::test_pre_call_local_shell_call_action_command_is_sanitized` (:304) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch: content-less Responses item; `test_sanitize_local_shell_call_action_command_and_env_is_scanned` uses `acme`, not `acme-corp-secret`. | n/a |

### `tests/litellm_hook/test_pre_call_headers.py` (16 ids: keep 16, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_missing_token_rejected` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_missing_token_rejected` (:12) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | auth + error class: 401 `E_MISSING_TOKEN`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_chatgpt_auth_bridge_forwards_only_required_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_chatgpt_auth_bridge_forwards_only_required_headers` (:20) | a14 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (1) excluded | — | headers: the ChatGPT bridge allow-list, `api_key`, `metadata` drop. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_chatgpt_auth_bridge_merges_litellm_header_buckets` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_chatgpt_auth_bridge_merges_litellm_header_buckets` (:60) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (1) excluded | — | headers: bucket merge. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_chatgpt_auth_bridge_reads_litellm_secret_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_chatgpt_auth_bridge_reads_litellm_secret_headers` (:83) | a5 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (1) excluded | — | headers: `secret_fields.raw_headers`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_chatgpt_auth_bridge_scrubs_litellm_logging_object_metadata` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_chatgpt_auth_bridge_scrubs_litellm_logging_object_metadata` (:108) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: logging-object scrub. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_chatgpt_auth_bridge_requires_bearer` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_chatgpt_auth_bridge_requires_bearer` (:144) | a9 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | auth + error class + audit (`E_PROVIDER_AUTH`). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_invalid_token_rejected` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_invalid_token_rejected` (:169) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | auth + error class: `E_TOKEN_INVALID`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_from_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_from_headers` (:178) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: corp token stripped, BYOK kept (invariants 3, 4). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_secret_fields_raw_headers_not_declassified_into_data_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_secret_fields_raw_headers_not_declassified_into_data_headers` (:186) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: no declassification. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_from_litellm_params_proxy_server_request` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_from_litellm_params_proxy_server_request` (:210) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: `litellm_params.proxy_server_request`. | n/a |
| `tests/test_litellm_hook.py::test_extract_headers_write_path_matches_release_semantics` | `tests/litellm_hook/test_pre_call_headers.py::test_extract_headers_write_path_matches_release_semantics` (:232) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: `_extract_headers` (hook code). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_handles_proxy_server_request_headers_shape` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_handles_proxy_server_request_headers_shape` (:262) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: `proxy_server_request` shape. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_from_all_header_locations` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_from_all_header_locations` (:277) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | keep | (1) excluded | — | headers: every location. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_from_proxy_server_request_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_from_proxy_server_request_headers` (:293) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers: `proxy_server_request.headers`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_case_insensitive_all_locations` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_case_insensitive_all_locations` (:308) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | keep | (1) excluded | — | headers: case-insensitive strip. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_strips_corp_token_from_litellm_params_metadata_headers` | `tests/litellm_hook/test_pre_call_headers.py::test_pre_call_strips_corp_token_from_litellm_params_metadata_headers` (:331) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (1) excluded | — | headers: `litellm_params.metadata.headers`. | n/a |

### `tests/litellm_hook/test_audit_facts.py` (14 ids: keep 14, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_reentrant_audit_failure_does_not_double_count_component_failure` | `tests/litellm_hook/test_audit_facts.py::test_reentrant_audit_failure_does_not_double_count_component_failure` (:26) | a3 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + metrics + error class. | n/a |
| `tests/test_litellm_hook.py::test_reentrant_audit_ambiguous_delivery_does_not_duplicate_and_keeps_state` | `tests/litellm_hook/test_audit_facts.py::test_reentrant_audit_ambiguous_delivery_does_not_duplicate_and_keeps_state` (:68) | a5 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + state. | n/a |
| `tests/test_litellm_hook.py::test_audit_emits_full_event_after_pre_and_post` | `tests/litellm_hook/test_audit_facts.py::test_audit_emits_full_event_after_pre_and_post` (:111) | a9 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: the full record. | n/a |
| `tests/test_litellm_hook.py::test_audit_after_failed_pre_call_uses_unknown_user` | `tests/litellm_hook/test_audit_facts.py::test_audit_after_failed_pre_call_uses_unknown_user` (:134) | a6 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + error class. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_bad_request_audits_inline` | `tests/litellm_hook/test_audit_facts.py::test_pre_call_bad_request_audits_inline` (:157) | a4 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + error class. | n/a |
| `tests/test_litellm_hook.py::test_audit_provider_detection_anthropic_claude` | `tests/litellm_hook/test_audit_facts.py::test_audit_provider_detection_anthropic_claude` (:170) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: `provider` from `_detect_provider`. | n/a |
| `tests/test_litellm_hook.py::test_audit_provider_detection_openai_default` | `tests/litellm_hook/test_audit_facts.py::test_audit_provider_detection_openai_default` (:179) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: same, OpenAI default. (Two functions only, so not a fold group.) | n/a |
| `tests/test_litellm_hook.py::test_audit_extracts_tokens_from_response_object_usage` | `tests/litellm_hook/test_audit_facts.py::test_audit_extracts_tokens_from_response_object_usage` (:188) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: token counts from an object. | n/a |
| `tests/test_litellm_hook.py::test_audit_extracts_tokens_anthropic_usage_shape` | `tests/litellm_hook/test_audit_facts.py::test_audit_extracts_tokens_anthropic_usage_shape` (:211) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: Anthropic usage names. | n/a |
| `tests/test_litellm_hook.py::test_audit_same_email_two_segments_redaction_count_one` | `tests/litellm_hook/test_audit_facts.py::test_audit_same_email_two_segments_redaction_count_one` (:224) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: distinct-secret count. | n/a |
| `tests/test_litellm_hook.py::test_audit_two_different_emails_redaction_count_two` | `tests/litellm_hook/test_audit_facts.py::test_audit_two_different_emails_redaction_count_two` (:242) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: distinct-secret count. | n/a |
| `tests/test_litellm_hook.py::test_audit_mixed_families_label_counts` | `tests/litellm_hook/test_audit_facts.py::test_audit_mixed_families_label_counts` (:261) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: `finding_label_counts`. | n/a |
| `tests/test_litellm_hook.py::test_audit_invariant_sum_equals_redaction_count_equals_placeholder_len` | `tests/litellm_hook/test_audit_facts.py::test_audit_invariant_sum_equals_redaction_count_equals_placeholder_len` (:274) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: count invariant. | n/a |
| `tests/test_litellm_hook.py::test_audit_finding_label_counts_keys_never_contain_originals` | `tests/litellm_hook/test_audit_facts.py::test_audit_finding_label_counts_keys_never_contain_originals` (:291) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: M1-14 on label keys. | n/a |

### `tests/litellm_hook/test_payload_limits.py` (12 ids: keep 12, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_oversize_on_instructions_second_field_fails_closed` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_oversize_on_instructions_second_field_fails_closed` (:21) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class (422 `E_OVERSIZE_BLOCKED`) on the second prompt field. | n/a |
| `tests/test_litellm_hook.py::test_oversize_message_leaf_fails_closed_not_leaked` | `tests/litellm_hook/test_payload_limits.py::test_oversize_message_leaf_fails_closed_not_leaked` (:40) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oversize_message_leaf_repro_blocked` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_oversize_message_leaf_repro_blocked` (:60) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class. Twin of the next test (same builder and asserts) — a group of two, not a fold. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oversize_document_source_data_repro_blocked` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_oversize_document_source_data_repro_blocked` (:72) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oversize_unmanaged_input_fails_closed_before_scanning` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_oversize_unmanaged_input_fails_closed_before_scanning` (:85) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + dispatch (`_guard_unmanaged_input_size`). | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oversize_message_chunk_policy_sanitizes` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_oversize_message_chunk_policy_sanitizes` (:105) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) not met | — | no survivor on the same input (`chunky@corp.example` through the chunk policy); not found under `tests/sanitizer` or `tests/payload`. | n/a |
| `tests/test_litellm_hook.py::test_oversize_deliver_flag_marks_audit_block_reason` | `tests/litellm_hook/test_payload_limits.py::test_oversize_deliver_flag_marks_audit_block_reason` (:119) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: `oversize:delivered`. | n/a |
| `tests/test_litellm_hook.py::test_normal_request_audit_has_no_block_reason` | `tests/litellm_hook/test_payload_limits.py::test_normal_request_audit_has_no_block_reason` (:140) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit: no marker. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_max_tokens_clamped_when_over_cap` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_max_tokens_clamped_when_over_cap` (:156) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned: the `max_output_tokens_cap` clamp (src/corp_llm_gateway/litellm_hook.py:495-504) + log presence. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_max_tokens_not_clamped_when_at_or_under_cap` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_max_tokens_not_clamped_when_at_or_under_cap` (:174) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned clamp. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_max_tokens_cap_none_by_default` | `tests/litellm_hook/test_payload_limits.py::test_pre_call_max_tokens_cap_none_by_default` (:185) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned clamp default. | n/a |
| `tests/test_litellm_hook.py::test_system_oversize_deliver_flag_logs_system_oversize_delivered` | `tests/litellm_hook/test_payload_limits.py::test_system_oversize_deliver_flag_logs_system_oversize_delivered` (:196) | a6 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + log presence + `system` path. | n/a |

### `tests/litellm_hook/test_stage0_classifier.py` (10 ids: keep 7, delete 0, fold 3)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_stage0_blocks_env_dump_in_responses_custom_tool_call_input` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_blocks_env_dump_in_responses_custom_tool_call_input` (:11) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + dispatch (Stage 0 reads `_item_text` of a Responses item). | n/a |
| `tests/test_litellm_hook.py::test_stage0_env_payload_raises_policy_blocked` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_env_payload_raises_policy_blocked` (:31) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F2 | the new `test_stage0_refuses_config_and_log_payloads[env]` | fold group F2 (below): 3 functions → 1 parametrised test, checks per id unchanged; the error class stays checked per case. | n/a |
| `tests/test_litellm_hook.py::test_stage0_kube_payload_raises_policy_blocked` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_kube_payload_raises_policy_blocked` (:48) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F2 | the new `…[kube]` | fold group F2. | n/a |
| `tests/test_litellm_hook.py::test_stage0_log_dump_raises_policy_blocked` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_log_dump_raises_policy_blocked` (:70) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | fold F2 | the new `…[log]` | fold group F2. | n/a |
| `tests/test_litellm_hook.py::test_stage0_upstream_not_called_for_blocked_request` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_upstream_not_called_for_blocked_request` (:92) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: Stage 0 runs before the oracle (`E_POLICY_BLOCKED`, not `E_CORP_LLM_DOWN`). One assert, so not in F2. | n/a |
| `tests/test_litellm_hook.py::test_stage0_audit_record_emitted_with_block_reason` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_audit_record_emitted_with_block_reason` (:114) | a8 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + idempotency. | n/a |
| `tests/test_litellm_hook.py::test_stage0_clean_request_passes_through` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_clean_request_passes_through` (:144) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned Stage-0 gate on prose (no raise). No survivor on the same text in `tests/payload/test_classifier.py`. | n/a |
| `tests/test_litellm_hook.py::test_stage0_exception_message_is_generic` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_exception_message_is_generic` (:153) | a3 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class: content-free message. | n/a |
| `tests/test_litellm_hook.py::test_stage0_disabled_by_flag_allows_through` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_disabled_by_flag_allows_through` (:168) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned: `CORP_LLM_BLOCK_PAYLOADS` gate (src/corp_llm_gateway/litellm_hook.py:863). | n/a |
| `tests/test_litellm_hook.py::test_stage0_scans_unmanaged_call_type_input_too` | `tests/litellm_hook/test_stage0_classifier.py::test_stage0_scans_unmanaged_call_type_input_too` (:191) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | dispatch + error class (unmanaged `input`). | n/a |

### `tests/litellm_hook/test_stage5_dlp.py` (8 ids: keep 8, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_stage5_dlp_blocks_canary_survivor` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_blocks_canary_survivor` (:14) | a4 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + log presence. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_clean_request_passes_through` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_clean_request_passes_through` (:31) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned Stage-5 gate (no raise); no survivor on the same text. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_audit_has_block_reason_dlp_canary` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_audit_has_block_reason_dlp_canary` (:42) | a6 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | audit + negative check. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_raw_secret_blocked_by_default_guard` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_raw_secret_blocked_by_default_guard` (:66) | a2 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_blocks_canary_in_responses_custom_tool_call_input` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_blocks_canary_in_responses_custom_tool_call_input` (:78) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + dispatch. Twin of the next one (2 functions; the third canary test adds `call_type`), not a fold. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_blocks_canary_in_local_shell_call_action_command` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_blocks_canary_in_local_shell_call_action_command` (:103) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + dispatch. | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_scans_unmanaged_call_type_input_without_rewriting` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_scans_unmanaged_call_type_input_without_rewriting` (:134) | a1 r1 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | error class + dispatch (unmanaged `input`). | n/a |
| `tests/test_litellm_hook.py::test_stage5_dlp_disabled_by_flag_passes_through` | `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_disabled_by_flag_passes_through` (:152) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned `CORP_LLM_DLP_GUARD` gate. | n/a |

### `tests/litellm_hook/test_log_hygiene.py` (6 ids: keep 6, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_pre_call_request_id_stable_across_calls_on_same_data` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_request_id_stable_across_calls_on_same_data` (:13) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state (request id). ⚠️ Vacuous: one `pre_call`, one `isinstance(rid1, str) and rid1` assert — the name promises stability across calls. No stronger survivor on the same input → Task 7. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_logs_sanitize_done_per_block` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_logs_sanitize_done_per_block` (:62) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | log presence of hook lines. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_known_role_logs_verbatim` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_known_role_logs_verbatim` (:124) | a5 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | hook-owned `_safe_role_for_log`. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_newline_bearing_litellm_call_id_falls_back_to_uuid` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_newline_bearing_litellm_call_id_falls_back_to_uuid` (:135) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state: request id validation. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_oversize_litellm_call_id_falls_back_to_uuid` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_oversize_litellm_call_id_falls_back_to_uuid` (:150) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state: request id validation. | n/a |
| `tests/test_litellm_hook.py::test_pre_call_wellformed_litellm_call_id_used_verbatim` | `tests/litellm_hook/test_log_hygiene.py::test_pre_call_wellformed_litellm_call_id_used_verbatim` (:163) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state: request id. | n/a |

### `tests/litellm_hook/test_litellm_entrypoints.py` (6 ids: keep 6, delete 0, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook.py::test_async_pre_call_hook_threads_call_type_to_embeddings_gate` | `tests/litellm_hook/test_litellm_entrypoints.py::test_async_pre_call_hook_threads_call_type_to_embeddings_gate` (:9) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | signature: litellm's `async_pre_call_hook` passes `call_type`. | n/a |
| `tests/test_litellm_hook.py::test_async_pre_call_hook_delegates` | `tests/litellm_hook/test_litellm_entrypoints.py::test_async_pre_call_hook_delegates` (:22) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | signature. | n/a |
| `tests/test_litellm_hook.py::test_async_log_success_event_emits_audit` | `tests/litellm_hook/test_litellm_entrypoints.py::test_async_log_success_event_emits_audit` (:29) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | signature + audit. | n/a |
| `tests/test_litellm_hook.py::test_audit_preserves_user_and_team_across_pre_post_handoff` | `tests/litellm_hook/test_litellm_entrypoints.py::test_audit_preserves_user_and_team_across_pre_post_handoff` (:44) | a6 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state + audit. | n/a |
| `tests/test_litellm_hook.py::test_audit_recovers_state_via_litellm_call_id` | `tests/litellm_hook/test_litellm_entrypoints.py::test_audit_recovers_state_via_litellm_call_id` (:96) | a8 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | state + audit. | n/a |
| `tests/test_litellm_hook.py::test_codex_path_metadata_pop_does_not_break_audit_attribution` | `tests/litellm_hook/test_litellm_entrypoints.py::test_codex_path_metadata_pop_does_not_break_audit_attribution` (:140) | a6 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (1) excluded | — | headers + audit. | n/a |

### `tests/route_gate/test_desanitize_stream.py` (10 ids: keep 7, delete 3, fold 0)

| baseline id | current id | checks | decision | criterion | survivor node id(s) | semantic note | fault injection needed |
|---|---|---|---|---|---|---|---|
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_sse_bytes_framing_intact_all_json` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_sse_bytes_framing_intact_all_json` (:1169) | a1 r0 f1; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | **delete** | (2) | `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json`; middleware path: `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_anthropic_sse_bytes_placeholder_restored` (must-keep) | Same wire input (the eleven events are `ANTHROPIC_SSE_FIXTURE` byte for byte: `_cb_start(0)` == `_CB_START`), same mapping `user@example.com` → `[EMAIL_001]`, same predicate (every non-`[DONE]` `data:` line `json.loads`, `pytest.fail` otherwise). The survivor feeds `SseStreamDesanitizer` directly; the deleted test got the mapping from `pre_call` on `send to user@example.com` (the file's own `_build`, oracle-only over `_corp_llm_returning`) and drove `DesanitizeMiddleware`, whose `_SseRestorer` forwards each restored event as text and writes no JSON of its own (src/corp_llm_gateway/route_gate/desanitize_middleware.py:477-496). The deleted test's `isinstance(chunk, bytes)` assert is made by the must-keep sibling on the same events and mapping through the middleware. No audit / ticket / status check. | yes — `src/corp_llm_gateway/sanitizer/streaming.py:789` `json.dumps(new_obj, ensure_ascii=False)` → `json.dumps(new_obj, ensure_ascii=False)[:-1]`; the survivor hits `pytest.fail("data: line is not valid JSON …")` on the first rewritten delta |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_sse_bytes_placeholder_restored_and_no_leak` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_sse_bytes_placeholder_restored_and_no_leak` (:1203) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | **delete** | (2) | `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_original_reconstructed_after_split`, `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled`; middleware path: `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_anthropic_sse_bytes_placeholder_restored` (must-keep) | Same wire input (`ANTHROPIC_SSE_FIXTURE`), same mapping, the same two asserts (`"user@example.com" in full`, `"[EMAIL_001]" not in full` over the joined `text_delta` texts). The two survivors feed `SseStreamDesanitizer` directly; the must-keep sibling makes both asserts on the same events and mapping through `DesanitizeMiddleware` (its request text is `email is user@example.com` and its builder `_build_guardrail`, where the deleted test used `send to user@example.com` and the file's `_build`: both pre-calls hand over the one pair `user@example.com` → `[EMAIL_001]`, the only request input the reversal reads, `litellm_hook.py:2677-2678`). No audit / ticket / status check. | yes — `src/corp_llm_gateway/sanitizer/streaming.py:600` `rewritten = ds.feed(text_in)` → `rewritten = text_in`; the survivors fail `assert "user@example.com" in full…` (the sibling fails the same assert) |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_sse_bytes_message_stop_present` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_sse_bytes_message_stop_present` (:1241) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | keep | (2) not met | — | no survivor in `test_desanitize_middleware.py` or `tests/sanitizer/test_streaming*.py` on this input and mapping: `test_sse_passthrough_events_byte_identical` maps `alice` → `[NAME_001]` and does not check `content_block_stop`. ⚠️ A same-file must-keep superset exists (`test_post_call_stream_anthropic_sse_bytes_placeholder_restored`, same events and mapping, same two type checks) — outside criterion (2)'s survivor set as written. | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_empty_mapping_sse_bytes_passthrough` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_empty_mapping_sse_bytes_passthrough` (:1263) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | keep | (2) not met | — | no survivor on the same production path: with no pairs `_hand_over` registers no mapping (src/corp_llm_gateway/litellm_hook.py:1241), so the middleware passes the body through without an SSE restorer. `test_streaming_adversarial.py::test_empty_mapping_bytes_pass_through_byte_identical` (same events, same predicate) runs `SseStreamDesanitizer` with an empty mapping, which production never builds; the middleware's own pass-through test collection-skips in minimal. | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_malformed_data_line_does_not_raise` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_malformed_data_line_does_not_raise` (:1275) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (2) not met | — | ⚠️ Vacuous: only `isinstance(out, list)` (and no raise). `test_streaming_adversarial.py::test_malformed_data_line_passes_through_unchanged` is stronger but on `NOT JSON AT ALL`, not `NOT JSON`, and skips the middleware's own `_json_or_none` read → Task 7. | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_done_sentinel_passes_through` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_done_sentinel_passes_through` (:1287) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (2) not met | — | the middleware has its own `[DONE]` branch (src/corp_llm_gateway/route_gate/desanitize_middleware.py:484-486). `test_streaming_adversarial.py::test_done_sentinel_passes_through_unchanged` (same event, same mapping, stronger predicate) reaches only `SseStreamDesanitizer`, and no minimal-run test asserts that `[DONE]` leaves the middleware (the must-keep chat-SSE tests send it but skip it when reading). | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_str_chunks_return_str` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_str_chunks_return_str` (:1299) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | **delete** | (2) | `tests/sanitizer/test_streaming_adversarial.py::test_str_input_returns_str_output`; middleware path: `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_anthropic_sse_bytes_placeholder_restored` (must-keep) | Same events as str (`_cb_start()`, `_delta("[N1]")`, `_cb_stop()` decoded), same mapping `alice` → `[N1]`, same predicate (`isinstance(chunk, str)` per chunk). The deleted test's assert is vacuous for production: `tests/response_restore.restore_stream` sends every chunk as bytes (`_wire`) and decodes the bodies itself when the first chunk was str, so the str type came from the test helper. The middleware decodes the bytes and feeds `SseStreamDesanitizer` str events, which is exactly what the survivor checks. The wire path (bytes in, restored events out) is the sibling's. No audit / ticket / status check. | yes — `src/corp_llm_gateway/sanitizer/streaming.py:744` `return text` → `return text.encode("utf-8")`; the survivor fails `assert isinstance(chunk, str)`. The deleted test does not fail under it (its str type is the helper's), which is the vacuity above |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_str_chunks_placeholder_restored` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_str_chunks_placeholder_restored` (:1315) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | keep | (2) not met | — | `test_streaming_adversarial.py::test_str_input_placeholder_restored` has the same events and mapping but no `"[N1]" not in` negative check — weaker. | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_mixed_bytes_and_dict_chunks` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_mixed_bytes_and_dict_chunks` (:1349) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (2) not met | — | ⚠️ Vacuous: only `isinstance(out, list)`. No survivor on the same input → Task 7. | n/a |
| `tests/test_litellm_hook_adversarial.py::test_post_call_stream_no_pre_call_passthrough` | `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_no_pre_call_passthrough` (:1382) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | keep | (2) not met | — | no survivor in the two named modules (an unknown request registers nothing; the middleware pass-through test collection-skips in minimal). ⚠️ Same-file must-keep `test_post_call_stream_unknown_request_passes_through` takes the same branch with a chat chunk — outside criterion (2)'s survivor set. | n/a |

## The delete list

| deleted id (current) | criterion | survivor(s) | `src/` line to mutate in phase 2 |
|---|---|---|---|
| `litellm_hook/test_pre_call_shapes.py::test_pre_call_tool_result_block_sanitized` | (1) | `sanitizer/test_content_blocks.py::test_sanitize_tool_result_str_content`; hook path `litellm_hook/test_pre_call_shapes.py::test_pre_call_collision_message_vs_tool_result` | `sanitizer/content_blocks.py:425` |
| `route_gate/test_desanitize_stream.py::test_post_call_stream_sse_bytes_framing_intact_all_json` | (2) | `sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json`; middleware path `route_gate/test_desanitize_stream.py::test_post_call_stream_anthropic_sse_bytes_placeholder_restored` | `sanitizer/streaming.py:789` |
| `route_gate/test_desanitize_stream.py::test_post_call_stream_sse_bytes_placeholder_restored_and_no_leak` | (2) | `sanitizer/test_streaming_adversarial.py::test_framing_integrity_original_reconstructed_after_split`, `sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled`; middleware path as above | `sanitizer/streaming.py:600` |
| `route_gate/test_desanitize_stream.py::test_post_call_stream_str_chunks_return_str` | (2) | `sanitizer/test_streaming_adversarial.py::test_str_input_returns_str_output`; middleware path as above | `sanitizer/streaming.py:744` |

Every survivor is in another file than the deleted test, so all four need a fault injection.
The survivors' modules are `tests/sanitizer/test_content_blocks.py`,
`tests/sanitizer/test_streaming.py`, `tests/sanitizer/test_streaming_adversarial.py`; the
path holders are `tests/litellm_hook/test_pre_call_shapes.py` (a `keep` of this audit) and
`tests/route_gate/test_desanitize_stream.py` (must-keep).

## Fold groups

Two groups qualify. Each is "N functions → 1 parametrised test, checks per id unchanged": the
old ids go to the ledger as removed, the param ids are new, the old `moves.json` keys go and the
new test gets no map entry.

**F1** — `tests/litellm_hook/test_pre_call_shapes.py`, 4 functions (`:218`, `:233`, `:324`,
`:351`), each a2 r0 f0, builder `_build_guardrail(<pairs>)`, no helper. The statements, identical
modulo the literals:

```python
g, _ = _build_guardrail(<pairs>)
data = {"model": <model>, "input": <input>,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}}
out = await g.pre_call(data, call_type=<call_type>)
assert out["input"] == <input>
assert "messages" not in out
```

| id | pairs | model | input | call_type |
|---|---|---|---|---|
| `test_pre_call_embeddings_input_string_passes_through_untouched` | `[("alice", "[NAME_001]")]` | `text-embedding-3-small` | `"alice@corp.example"` | `embedding` |
| `test_pre_call_moderations_input_list_passes_through_untouched` | `[("alice", "[NAME_001]"), ("bob", "[NAME_002]")]` | `omni-moderation-latest` | `["alice", "bob"]` | `moderation` |
| `test_pre_call_speech_input_passes_through_untouched` | `[("Alice Smith", "[NAME_001]")]` | `tts-1` | `"Please welcome Alice Smith to the stage"` | `aspeech` |
| `test_pre_call_pass_through_endpoint_input_passes_through_untouched` | `[("alice", "[NAME_001]")]` | `voyage-3` | `["alice@corp.example"]` | `pass_through_endpoint` |

`test_pre_call_amoderation_call_type_also_passes_through` and
`test_pre_call_speech_call_type_also_passes_through` have only the first assert, so they are
not in F1 (two functions, not a group).

**F2** — `tests/litellm_hook/test_stage0_classifier.py`, 3 functions (`:31`, `:48`, `:70`), each
a2 r1 f0, builder `_build_guardrail(corp_llm=_corp_llm_unreachable())`, helper `_data_with_token`:

```python
g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
data = _data_with_token("tok-1", content=<payload>)
with pytest.raises(GuardrailHttpException) as ei:
    await g.pre_call(data)
assert ei.value.status_code == 422
assert ei.value.error_code == "E_POLICY_BLOCKED"
```

Cases `env` (`test_stage0_env_payload_raises_policy_blocked`), `kube`
(`test_stage0_kube_payload_raises_policy_blocked`), `log`
(`test_stage0_log_dump_raises_policy_blocked`). The log payload is built by `"\n".join([...])`;
the case carries the same joined string. `test_stage0_upstream_not_called_for_blocked_request`
has the same setup but only the `error_code` assert, so it is not in F2.

Not groups: `test_tool_calls.py`'s nine edge cases and the four block-type tests (Task 3a); the
two oversize `_data_with_token` repros of `test_payload_limits.py` (the instructions case builds
its data as a dict); the two `custom_tool_call` / `local_shell_call` canary tests of
`test_stage5_dlp.py` (the third adds `call_type`); the two provider-detection tests of
`test_audit_facts.py`.

## Smells from the Task 2 review

- **Vacuous checks — confirmed, four.** `test_log_hygiene.py::test_pre_call_request_id_stable_across_calls_on_same_data`
  (one `pre_call`, `assert isinstance(rid1, str) and rid1`; the name promises stability across
  calls), `test_desanitize_stream.py::test_post_call_stream_malformed_data_line_does_not_raise`
  and `::test_post_call_stream_mixed_bytes_and_dict_chunks` (only `isinstance(out, list)`):
  kept, no stronger survivor on the same input → **Task 7**.
  `::test_post_call_stream_str_chunks_return_str` is vacuous for production (the str type is
  made by `tests/response_restore.restore_stream`) and has a stronger survivor on the same
  input → deleted above.
- **Direct `_restore_object` caller — confirmed.** `tests/route_gate/test_desanitize_unary.py:289`
  `test_post_call_unary_non_model_response_passes_through_unchanged_like_release` (the scalar
  pass-through, must-keep) calls `hook_fixtures._restore_object` instead of `restore_unary`. A
  rewrite through the wire path is a body change: out of scope, noted for the object-shapes
  follow-up.
- **Adversarial helpers twinning `hook_fixtures` — confirmed.** `test_desanitize_stream.py:1084-1166`:
  `_StaticRules` (always `Rules(rules=())`) ≈ `hook_fixtures._StaticRules()`,
  `_corp_llm_returning` ≈ `hook_fixtures._corp_llm_returning`, `_build` ≈ `_build_guardrail`
  without the sink and the keyword arguments, `_request` ≈ `_data_with_token`, `_iter` ≈
  `_async_iter`, `_collect` ≈ a `restore_stream` loop. Helper consolidation is its own PR
  (Task 1b-style notes); the seven adversarial tests this audit keeps still use them.

## Intra-layer twins outside both criteria

Neither criterion covers a hook test whose survivor is another hook test, nor a stream test
whose only survivor is a must-keep test of its own file. These were kept and are listed for
review; widening the criteria is a plan decision, not this audit's:

- `test_pre_call_shapes.py::test_pre_call_replaces_message_content_with_sanitized` — same
  builder, pairs, data and assert as the first block of `::test_pre_call_single_key_shapes_unaffected_by_hybrid_guard`.
- `test_pre_call_shapes.py::test_pre_call_str_message_regression` — `::test_pre_call_no_system_no_op`'s
  content assert on `hello alice` instead of `hi alice`, same pairs.
- `test_pre_call_shapes.py::test_pre_call_rejects_non_list_messages` — `test_audit_facts.py::test_pre_call_bad_request_audits_inline`
  (adds the audit asserts) and `test_fail_policy.py::test_pre_call_wrapper_preserves_named_error_codes[bad_request]`.
- `test_desanitize_stream.py::test_post_call_stream_sse_bytes_message_stop_present` — the
  must-keep `::test_post_call_stream_anthropic_sse_bytes_placeholder_restored` makes both type
  checks on the same events and mapping.
- `test_desanitize_stream.py::test_post_call_stream_no_pre_call_passthrough` — the must-keep
  `::test_post_call_stream_unknown_request_passes_through` takes the same pass-through branch
  with a chat chunk.

## Negative-log and name-pinned checks

- `tests/_manifests/negative_log_checks.json`: no `owner` and no `security_node_ids` entry is
  any of the 163 ids (baseline or current). The 14 sites owned by former hook-file tests all
  belong to must-keep ids. Phase 2's `negative_logs --write` must therefore drop no row.
- `tests/_manifests/name_pinned.json`: none of the 163 ids (baseline or current) is in `ids`.
  The four deleted names appear only in `docs/testing/task2-classification.md` (a record of
  Task 2, not a citation source).

## Survivor sanity run (2026-10-05)

The survivor and path-holder files, run whole: `tests/sanitizer/test_content_blocks.py`,
`test_streaming.py`, `test_streaming_adversarial.py`, `test_oracle_trigger.py`,
`tests/route_gate/test_desanitize_stream.py`, `tests/litellm_hook/test_pre_call_shapes.py`.
Full venv (`CI=true`, Postgres up): 354 passed. Minimal venv: 353 passed, 1 skipped
(`test_desanitize_stream.py::test_post_call_stream_responses_events_restore_split_placeholder`,
litellm absent — its ledger skip, not a survivor). Every named survivor passed in both, where
the deleted ids passed.

## Phase 2 notes

- Ledger rows: 4 deletions + 7 fold rows (old ids removed), `PR` = `Task 3b`, reviewer
  `auto-review (pending)`.
- `moves.json`: remove the 11 keys (4 deleted, 7 folded); the two new parametrised tests get no
  entry.
- Expected manifest diff: `expected_outcomes.*` lose the 11 ids and gain the 7 param ids;
  `baseline_checks/_root__test_litellm_hook.json` loses 8 lines and
  `_root__test_litellm_hook_adversarial.json` 3, `baseline_checks/litellm_hook.json` gains the
  two new functions;
  `negative_log_checks.json` no row lost; `must_keep/`, `coverage.*.json`,
  `not_applicable.json`, `name_pinned.json` unchanged; `external_deps.json` hash-only. A
  coverage drop restores the test.

## Open questions

Decided here by the plan's rule:

- *Algorithm-layer survivors bypass the hook / the middleware.* Criteria (1) and (2) name the
  survivor directories, so a survivor there is accepted for the check it makes; the hook or
  middleware route of the same input must still be held by a test that stays (named in each
  row), and an input with its own hook or middleware branch is kept.
- *"Stream reconstruction" in the kept list* is read as the must-keep stream tests that restore
  split placeholders through the middleware (they all stay); the (2) deletions each leave such a
  test on the same wire input. Read as every stream test that reconstructs text, the two SSE
  deletions above would be kept instead.
- *`test_pre_call_oracle_disabled_gazetteer_hit_sanitizes_without_oracle`* starts at baseline
  `:1779`, inside the kept range `:1749-1780`, so it is kept although
  `test_oracle_trigger.py::test_oracle_disabled_gazetteer_hit_skips_oracle_local_findings_applied`
  would otherwise be a survivor.
- *`test_pre_call_empty_list_content_no_crash`*: its assert reads the field whose walk the hook
  decides (`content_empty`), so it is kept, although its assert cannot tell a skip from a walk.

Not decided (for review):

- Whether the intra-layer twins above may be pruned under a widened criterion.
- Whether to fold F1 and F2 at all: both are optional; each loses one docstring per case (the
  review repro it records) unless the parametrize ids or a comment carry it.
