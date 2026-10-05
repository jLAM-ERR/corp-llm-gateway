# Task 4 streaming audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 11, local only),
Task 4: `tests/sanitizer/test_streaming.py` absorbs `tests/sanitizer/test_streaming_adversarial.py`.
Phase 1 decides move / fold / delete for each of the 44 adversarial ids and changes no test.
Phase 2 (after review) does the absorption, the [deleted-tests.md](deleted-tests.md) rows,
`moves.json`, the self-test edit and the manifest regeneration. The gates are in
[must-keep.md](must-keep.md). Tree: `release/1.0.x` at `2560566`.

**Decision: 32 move / 11 fold (F1 8, F2 3) / 1 delete.** After phase 2 the merged file holds
46 + 32 + 2 functions, 89 collected ids (90 today).

## Rules applied

- **move** (default): the function goes into `test_streaming.py` with its body, docstring and
  the helpers it calls unchanged; a `moves.json` `ids` entry; no ledger row.
- **fold** (plan rev 9): ≥ 3 functions whose assert / raises / `pytest.fail` statements are
  textually identical modulo data literals, with the same delegated helpers and the same builder
  (`SseStreamDesanitizer(_mapping(...))` + the same `_collect*`). One parametrised test; each old
  id is a prune-style ledger row ("N functions → 1 parametrised test, checks per id unchanged");
  no `moves.json` entry for the new test. A difference in the operation (`flush()` vs `feed(…)`)
  is not a data literal.
- **delete** (plan rev 10, criterion (3)): a survivor in the base file or the adversarial file on
  the same wire input, the same mapping and the same builder, with equal-or-stronger asserts. Same
  file after the merge and same builder, so no fault injection.
- Groups made only of base-file tests are out of scope; no base-file test is deleted or folded.
- Candidates were found by reading the bodies, and cross-checked by a script that groups all 90
  functions by their check statements with literals (then names) normalised. Its groups of
  ≥ 3 with an adversarial member: F1 (with names normalised, also the Cyrillic test, whose
  `assert cyrillic in combined` reads a decoded join), F2, the `assert result == []` trio (not a
  fold, see below; with names normalised also base `test_stream_empty_iterator`, a
  `StreamingDesanitizer` test), and with names normalised an `assert isinstance(…)` trio
  (`test_feed_after_complete_stream_is_safe` + two base tests on other objects and arguments).

Columns: `checks` is the test's line in `tests/_manifests/baseline_checks/sanitizer.json`
(asserts / raises / fail, delegated helper checks, loops; no test here is parametrised).
`(:N)` is the current line. The baseline id is the current id for all 44 (neither file has
moved since `807831a`). For `move`, the note says "unchanged" and lists the adversarial helpers
the test drags along (base-file helpers are already there); for `fold`, the case data and the
branch it reaches; for `delete`, the ledger row's semantic note, ready to copy.

## Audit table

| node id | checks | decision | fold group or survivor | semantic note | fault injection |
|---|---|---|---|---|---|
| `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json` (:68) | a0 r0 f1; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_bytes`. The plan's `json.loads` + `pytest.fail` framing check over the eleven `ANTHROPIC_SSE_FIXTURE` events; the self-test (b) mutation site (body byte-identical). Task 3b survivor (`deleted-tests.md` row 48). Near: `::test_each_emitted_bytes_chunk_is_self_contained_sse` has the same predicate on another mapping (`user@corp.com`) and without `_PING` / `_MSG_DELTA`; no base test parses every `data:` line. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_original_reconstructed_after_split` (:85) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | **delete** | `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled` (`:414`) | Criterion (3), a same-file twin after the merge. The survivor feeds the same eleven `ANTHROPIC_SSE_FIXTURE` events through the same builder `SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))` and the same collector (`_collect`, whose body is `_collect_bytes`'s), joins the same `text_delta` texts and makes the same two asserts (`"user@example.com" in`, `"[EMAIL_001]" not in`). Its parser `_data_of` is stricter than `_data_obj`: a `data:` line that is not JSON raises `JSONDecodeError` there, where `_data_obj` returned `None` and the line was skipped, and it decodes with `chunk.decode()`, where `_data_obj` used `errors="replace"`; a chunk without a `data:` line is skipped by both. The deleted test's assert messages are diagnostics only. | n/a (same production path — same builder, same file after the merge). Task 3b evidence: `streaming.py:600` `rewritten = ds.feed(text_in)` → `rewritten = text_in` failed both this test (`:97`) and the survivor (`test_streaming.py:429`) |
| `tests/sanitizer/test_streaming_adversarial.py::test_malformed_data_line_passes_through_unchanged` (:106) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[malformed_data_line]` | Data `b"event: content_block_delta\ndata: NOT JSON AT ALL\n\n"`; branch `streaming.py:560-563` (`JSONDecodeError` → pass-through). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_done_sentinel_passes_through_unchanged` (:114) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[done_sentinel]` | Data `b"data: [DONE]\n\n"`; branch `streaming.py:557-559` with nothing held. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_done_sentinel_after_openai_content_flushes_desanitizer` (:122) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 4 | move | — | unchanged; helpers `_collect_bytes`. The only test that sends an OpenAI content delta then `[DONE]` and checks that the held tail goes out first in a `choices` envelope (`streaming.py:557-559` → `_held_tails` → `_chat_tail`), with `[DONE]` kept and `alice` restored. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_empty_data_line_passes_through` (:168) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F2 `[empty_data_line]` | Data: pairs `(("alice", "[N1]"),)`, events `[b"data: \n\n"]`. ⚠ vacuous (`len(out) >= 1`) → Task 7. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_event_with_no_data_line_passes_through` (:177) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[no_data_line]` | Data `b"event: ping\n\n"`; branch `streaming.py:553-556` (no `data:` line). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_input_json_delta_passes_through_byte_identical` (:217) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`, `_input_json_delta_event`. Drags `_input_json_delta_event`. An `input_json_delta` with no `content_block_start` (`ds is None`, `streaming.py:613-615`). ⚠ docstring stale (tool_use blocks are desanitised when a start event opens them) and the asserts are conditional (`if obj … else ev in out or ev in combined`) → Task 7. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_thinking_delta_passes_through_byte_identical` (:234) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_thinking_delta_event`. Drags `_thinking_delta_event`. A `thinking_delta` with no block start; `::test_thinking_delta_with_placeholder_passes_through_verbatim` has a thinking block start, a placeholder and another mapping. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_input_json_delta_after_tool_use_block_start_unchanged` (:244) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_input_json_delta_event`, `_tool_use_block_start`. Drags `_tool_use_block_start`, `_input_json_delta_event`. Near: `::test_tool_use_input_json_delta_no_placeholders_passes_through` (other `partial_json`, mapping `[NAME_001]`, a `_cb_stop(1)`) — not the same input. ⚠ `'"key": "value"' in combined or "key" in combined` is met by any `key` → Task 7. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_non_text_content_block_start_no_desanitizer_created` (:257) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F2 `[tool_use_block]` | Data: pairs `(("alice", "[N1]"),)`, events `[_tool_use_block_start(1), _input_json_delta_event(1, partial_json="{}"), _cb_stop(1)]`. ⚠ vacuous, and the docstring is stale: a tool_use start does create a desanitizer (`streaming.py:579-584`) → Task 7. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_placeholder_held_until_block_stop_then_flushed` (:279) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 3 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. The only test of the order: the tail delta comes before `content_block_stop` (`streaming.py:625-641`). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_content_block_delta_with_no_preceding_start_passes_through` (:308) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`. A `text_delta` at index 99 with no start (`ds is None`, `streaming.py:596-599`); no twin. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_two_text_blocks_each_placeholder_restored_independently` (:322) | a4 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. Near: base `::test_sse_two_text_blocks_no_runtime_error` (`:562`) has the same mapping and the same four asserts, but whole `[N1]` / `[N2]` deltas where this test splits each across two deltas (`[N` + `1]`); other wire input. Two functions, other extraction (`_data_obj` vs `_data_of`): not a fold. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_two_text_blocks_no_runtime_error_on_second_block` (:351) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F2 `[second_text_block]` | Data: pairs `(("x", "[X]"), ("y", "[Y]"))`, events `[_cb_start(0), _delta("[X]", 0), _cb_stop(0), _cb_start(1), _delta("[Y]", 1), _cb_stop(1)]`. Not a delete: base `:562` has the same event shape but another mapping (`alice`/`bob`). ⚠ vacuous → Task 7. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_str_input_returns_str_output` (:372) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_str`. Drags `_collect_str`. Task 3b survivor (`deleted-tests.md` row 50). Near: base `::test_sse_str_input_returns_str_output` (`:611`) feeds `hello` with no placeholder (mapping `[NAME_001]`); this one feeds `[N1]` through the rewrite path (`streaming.py:600-607`). `::test_str_input_placeholder_restored` has the same input but checks the type only by a `TypeError` on a non-empty bytes chunk. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_str_input_placeholder_restored` (:383) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_str`. Drags `_collect_str`. The only str-mode restoration check. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_crlf_event_separators_accepted_no_exception` (:401) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. Near: base `::test_sse_crlf_separators_accepted` (`:516`) has the same CRLF events with `[NAME_001]` and only the positive assert; this one uses `[N1]` and adds `"[N1]" not in`. Other input. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_sse_event_split_across_two_feeds_correct_output` (:434) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. Near: base `::test_sse_event_split_across_feeds` (`:500`) splits `_delta("[NAME_001]")` at its midpoint; this one `_delta("[N1]", 0)` (another offset, another mapping). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_sse_event_split_at_first_byte_across_feeds` (:451) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. The only split at byte 1; no twin. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_multibyte_cyrillic_original_not_corrupted` (:470) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`. Empty mapping, Cyrillic text in one delta split at `len // 2`. ⚠ the comment says the split falls inside the UTF-8 bytes; it is at byte 67 and the Cyrillic bytes are 118-129, so no character is split. The in-character split is held by base `::test_sse_multibyte_utf8_split_across_chunks_not_corrupted` (`:539`, emoji). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_non_ascii_original_emitted_as_utf8_not_escaped` (:492) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`. Same input as `::test_non_ascii_original_data_line_parses_as_json`, other asserts (raw UTF-8 bytes and no `\u0410` vs JSON parse and equality); neither is a superset. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_non_ascii_original_data_line_parses_as_json` (:508) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_bytes`. See the previous row. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_message_delta_usage_passes_byte_identical` (:531) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[message_delta_usage]` | Data `_MSG_DELTA`; branch `streaming.py:646-648`. Near: base `::test_sse_passthrough_events_byte_identical` (`:452`) checks `message_delta` inside the whole fixture with mapping `[NAME_001]` — other input. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_message_delta_with_custom_usage_byte_identical` (:538) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[message_delta_custom_usage]` | Data: the `max_tokens` / 99 / 42 `message_delta` literal; branch `streaming.py:646-648`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_flush_with_nothing_fed_returns_empty_list` (:555) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged. `assert result == []` like the two empty-feed tests, but it calls `flush()` where they call `feed(…)`: the operation is not a data literal, so not a fold (two feeds alone are below 3). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_empty_mapping_bytes_pass_through_byte_identical` (:562) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`. No twin (Task 3b near-miss for the middleware's empty-mapping test). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_flush_called_twice_does_not_raise` (:571) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`. ⚠ vacuous (`isinstance(…, list)` twice; the comment says the second is empty, nothing asserts it) → Task 7. Base `::test_flush_idempotent` is `StreamingDesanitizer`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_feed_after_complete_stream_is_safe` (:582) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`. ⚠ only `isinstance(extra, list)` → Task 7. Base `::test_feed_after_flush_raises` is `StreamingDesanitizer` (which raises). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_empty_bytes_feed_returns_empty_list` (:593) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged. See `::test_flush_with_nothing_fed_returns_empty_list`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_empty_str_feed_returns_empty_list` (:600) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged. See `::test_flush_with_nothing_fed_returns_empty_list`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_ping_passes_through_byte_identical` (:607) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[ping]` | Data `_PING`; branch `streaming.py:646-648`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_message_stop_passes_through_byte_identical` (:614) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[message_stop]` | Data `_MSG_STOP`; branch `streaming.py:646-648`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_each_emitted_bytes_chunk_is_self_contained_sse` (:626) | a0 r0 f1; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_bytes`. Same predicate as the framing test (`:68`) on other data (`user@corp.com`, no `_PING` / `_MSG_DELTA`): not a delete, and two functions are not a fold (the framing test is also the self-test's site). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_openai_sse_bytes_placeholder_not_emitted_in_choices_chunks` (:661) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 2 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`. Defines its own nested `_openai_delta`; drags `_data_obj`. ⚠ its docstring names a `[DONE]`-flush bug that is fixed; restoration is asserted by `::test_done_sentinel_after_openai_content_flushes_desanitizer`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_openai_sse_non_text_choice_passes_through` (:692) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | **fold** | F1 `[openai_finish_chunk]` | Data `b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'`; branch `_process_chat` (`streaming.py:643-644`). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_anthropic_truncated_flush_tail_emitted_as_valid_content_block_delta` (:705) | a1 r0 f1; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 4 | move | — | unchanged; helpers `_data_obj`. Drags `_data_obj`. Near: base `::test_sse_flush_emits_held_text_on_truncated_stream` (`:587`) has the same events, mapping and `alice` assert; it swallows `JSONDecodeError` (this test `pytest.fail`s on it) but decodes strictly (`UnicodeDecodeError` not caught) where this test uses `errors="replace"`. Neither is a superset. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_openai_truncated_flush_tail_emitted_as_valid_choices_event` (:744) | a2 r0 f1; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 4 | move | — | unchanged. Defines its own nested `_openai_delta` (same body as the one above, function-local). The only OpenAI truncated-tail check. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_tool_use_input_json_delta_placeholder_split_across_two_deltas` (:845) | a3 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_collect_partial_jsons`, `_data_obj`, `_input_json_delta`, `_tool_use_start`. Drags `_tool_use_start`, `_input_json_delta`, `_collect_partial_jsons`, `_data_obj`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_tool_use_input_json_delta_json_escape_correctness` (:864) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_collect_partial_jsons`, `_data_obj`, `_input_json_delta`, `_tool_use_start`. Same helpers. Other asserts than the split test (no original-in check; a `"` in the original). | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_tool_use_input_json_delta_no_placeholders_passes_through` (:881) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_collect_partial_jsons`, `_data_obj`, `_input_json_delta`, `_tool_use_start`. Same helpers. One equality assert. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_thinking_delta_with_placeholder_passes_through_verbatim` (:897) | a2 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_thinking_block_start`, `_thinking_delta`. Drags `_thinking_block_start`, `_thinking_delta`. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_signature_delta_passes_through_byte_identical` (:909) | a1 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1 | move | — | unchanged; helpers `_collect_bytes`, `_signature_delta`, `_thinking_block_start`. Drags `_thinking_block_start`, `_signature_delta`. Not in F1: three events, the asserted one in the middle, and an assert message. | n/a |
| `tests/sanitizer/test_streaming_adversarial.py::test_mixed_text_and_tool_use_blocks_both_desanitize` (:917) | a5 r0 f0; delegated `_no_logger_state_left_behind` a0 r0 f1; loops 1 | move | — | unchanged; helpers `_collect_bytes`, `_data_obj`, `_input_json_delta`, `_tool_use_start`. Drags `_tool_use_start`, `_input_json_delta`, `_data_obj`. | n/a |

## Totals

| decision | ids |
|---|---|
| move | 32 |
| fold | 11 (F1: 8, F2: 3) → 2 new parametrised tests, 11 param ids |
| delete | 1 |
| **total** | **44** |

Ledger rows in phase 2: 12 (8 + 3 fold members, 1 deletion), `PR` = `Task 4`, reviewer
`auto-review (pending)`. `moves.json`: 32 `ids` entries.

## Fold groups

No base-file test is a member of either group.

**F1** — 8 adversarial functions (`:106`, `:114`, `:177`, `:531`, `:538`, `:607`, `:614`,
`:692`), each a1 r0 f0, delegated `_no_logger_state_left_behind` only, builder
`SseStreamDesanitizer(_mapping(("alice", "[N1]")))`, collector `_collect_bytes`. The statements,
identical modulo the event (a local literal or a module constant):

```python
sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
out = _collect_bytes(sse, [<event>])
assert <event> in out
```

| old id | param id | event | branch in `src/corp_llm_gateway/sanitizer/streaming.py` |
|---|---|---|---|
| `test_malformed_data_line_passes_through_unchanged` | `malformed_data_line` | `b"event: content_block_delta\ndata: NOT JSON AT ALL\n\n"` | `:560-563` |
| `test_done_sentinel_passes_through_unchanged` | `done_sentinel` | `b"data: [DONE]\n\n"` | `:557-559` |
| `test_event_with_no_data_line_passes_through` | `no_data_line` | `b"event: ping\n\n"` | `:553-556` |
| `test_message_delta_usage_passes_byte_identical` | `message_delta_usage` | `_MSG_DELTA` | `:646-648` |
| `test_message_delta_with_custom_usage_byte_identical` | `message_delta_custom_usage` | the `max_tokens` / 99 / 42 `message_delta` literal | `:646-648` |
| `test_ping_passes_through_byte_identical` | `ping` | `_PING` | `:646-648` |
| `test_message_stop_passes_through_byte_identical` | `message_stop` | `_MSG_STOP` | `:646-648` |
| `test_openai_sse_non_text_choice_passes_through` | `openai_finish_chunk` | `b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'` | `:643-644` (`_process_chat`) |

New test: `tests/sanitizer/test_streaming.py::test_single_event_passes_through_byte_identical`,
`@pytest.mark.parametrize("event", [pytest.param(<event>, id=<param id>), …])`, each old docstring
a `#` comment above its case. Ledger semantic note, ready to copy: *"Fold F1: 8 functions → 1
parametrised test, checks per id unchanged. `…::test_single_event_passes_through_byte_identical[<param id>]`
runs the same three statements on this test's event, byte for byte (`SseStreamDesanitizer(_mapping(("alice", "[N1]")))`,
`_collect_bytes(sse, [event])`, `assert event in out`); this test's docstring is the comment
above its case."*

Not in F1: `test_signature_delta_passes_through_byte_identical` (three events, the asserted one
in the middle, an assert message), `test_empty_mapping_bytes_pass_through_byte_identical`
(empty mapping, five events, a loop), `test_multibyte_cyrillic_original_not_corrupted` (asserts
on the decoded join).

**F2** — 3 adversarial functions (`:168`, `:257`, `:351`), each a1 r0 f0, delegated
`_no_logger_state_left_behind` only, the same builder and `_collect_bytes`:

```python
sse = SseStreamDesanitizer(_mapping(<pairs>))
out = _collect_bytes(sse, <events>)
assert len(out) >= 1
```

| old id | param id | pairs | events |
|---|---|---|---|
| `test_empty_data_line_passes_through` | `empty_data_line` | `(("alice", "[N1]"),)` | `[b"data: \n\n"]` |
| `test_non_text_content_block_start_no_desanitizer_created` | `tool_use_block` | `(("alice", "[N1]"),)` | `[_tool_use_block_start(1), _input_json_delta_event(1, partial_json="{}"), _cb_stop(1)]` |
| `test_two_text_blocks_no_runtime_error_on_second_block` | `second_text_block` | `(("x", "[X]"), ("y", "[Y]"))` | `[_cb_start(0), _delta("[X]", 0), _cb_stop(0), _cb_start(1), _delta("[Y]", 1), _cb_stop(1)]` |

New test: `tests/sanitizer/test_streaming.py::test_stream_does_not_raise_and_emits_output`,
parametrised over `("pairs", "events")`, each old docstring a `#` comment above its case. Its
parametrize list calls `_tool_use_block_start` and `_input_json_delta_event` at import time, so
it sits after them (below). Ledger semantic note, ready to copy: *"Fold F2: 3 functions → 1
parametrised test, checks per id unchanged. `…::test_stream_does_not_raise_and_emits_output[<param id>]`
builds `SseStreamDesanitizer(_mapping(*pairs))` from this test's pairs, feeds this test's events
through `_collect_bytes` and asserts `len(out) >= 1`; this test's docstring is the comment above
its case."* All three checks are vacuous (no raise, one chunk out): Task 7 may still prune cases.

Considered and not folded:

- `test_flush_with_nothing_fed_returns_empty_list`, `test_empty_bytes_feed_returns_empty_list`,
  `test_empty_str_feed_returns_empty_list`: the same `assert result == []`, but `flush()` vs
  `feed(…)` is an operation, not a data literal; the two feeds alone are 2.
- `test_framing_integrity_every_data_line_is_valid_json` + `test_each_emitted_bytes_chunk_is_self_contained_sse`
  (same `pytest.fail` loop): 2 functions, and the first is the self-test's site.
- Adversarial + base pairs with the same asserts (`test_two_text_blocks_each_placeholder_restored_independently`
  / `test_sse_two_text_blocks_no_runtime_error`, `test_sse_event_split_across_two_feeds_correct_output`
  / `test_sse_event_split_across_feeds`, `test_sse_event_split_at_first_byte_across_feeds` /
  `test_sse_crlf_separators_accepted`): 2 each, and their extraction differs (`_data_obj` vs `_data_of`).

## The delete

| deleted id | survivor | why | fault injection |
|---|---|---|---|
| `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_original_reconstructed_after_split` | `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled` | criterion (3): same fixture, mapping, builder, collector body and two asserts; the survivor's `_data_of` is stricter (errors on a non-JSON `data:` line and on bad UTF-8) | none (same file after the merge, same builder); Task 3b's `streaming.py:600` injection already failed both |

It is a survivor of Task 3b's `deleted-tests.md` row 49; that row also names the base-file
survivor, which stays.

Near-twins checked and kept as `move` (other wire input or other mapping, so criterion (3) is
not met): `test_two_text_blocks_no_runtime_error_on_second_block` vs base `:562` (mapping
`x`/`y` vs `alice`/`bob`; it is in F2), `test_each_emitted_bytes_chunk_is_self_contained_sse` vs
the framing test (`user@corp.com`, fewer events), `test_crlf_event_separators_accepted_no_exception`
vs base `:516` and `test_sse_event_split_across_two_feeds_correct_output` vs base `:500` (`[N1]` vs
`[NAME_001]`), `test_str_input_returns_str_output` vs base `:611` (`[N1]` vs `hello`),
`test_anthropic_truncated_flush_tail_emitted_as_valid_content_block_delta` vs base `:587` (same
input, but each test is stricter than the other in one respect). In the last pair the base test
is not a strict subset either, so no base-file deletion is proposed.

## Helpers

| adversarial helper | phase 2 | base-file counterpart |
|---|---|---|
| `_mapping` (`:31`) | dropped: byte-identical to base `_mapping` (`:15`) | same body; the only name clash |
| `_collect_bytes` (`:35`) | moves under its name | twin of `_collect` (`:386`), another name |
| `_collect_str` (`:43`) | moves | none (base `test_sse_str_input_returns_str_output` inlines the loop) |
| `_data_obj` (`:51`) | moves | not a twin: `_data_of` (`:394`) raises where it returns `None`, and decodes strictly |
| `_input_json_delta_event` (`:190`) | moves | twin of adversarial `_input_json_delta` (`:795`), other defaults (`index=0, partial_json='{"k":'` vs `1, ""`) |
| `_thinking_delta_event` (`:199`) | moves | twin of adversarial `_thinking_delta` (`:813`), other default `thinking` |
| `_tool_use_block_start` (`:208`) | moves | twin of adversarial `_tool_use_start` (`:786`) at `name="bash"` |
| `_tool_use_start`, `_input_json_delta`, `_thinking_block_start`, `_thinking_delta`, `_signature_delta`, `_collect_partial_jsons` (`:786-842`) | move | none in the base file |
| nested `_openai_delta` in `:661` and `:744` | stay inside their tests | each other's twin (function-local, no clash) |

No module-level name clash other than `_mapping`; no base-file builder (`_delta`, `_cb_start`,
`_cb_stop`, `_MSG_*`, `_PING`, `ANTHROPIC_SSE_FIXTURE`) has an adversarial redefinition — the
adversarial file imports them. No helper loses its last caller in phase 2. The twins
(`_collect_bytes`/`_collect`, the three event-builder pairs, the two nested `_openai_delta`) are
for a Task 6a-style helper-consolidation PR. No `renames.json`.

`from __future__ import annotations` (adversarial `:8`) is not carried over: the moved
annotations (`list[bytes]`, `dict | None`, `StrategyResult`) all evaluate on 3.12+.

## Dry runs (in memory and in the scratch directory; no tracked file changed)

- **Pure move of all 44** (the adversarial body appended after base `:797`, imports and `_mapping`
  dropped, a 44-entry `ids` map, the adversarial module removed from the tree): `inventory.build()`
  against the committed `baseline_checks/` — 0 diff lines over the whole tree; external
  dependencies — 0; negative-log discovery — 148 sites, none new, none gone; `must_keep.problems()`
  — none; `moves.problems()` — none. `ruff check` and `ruff format --check` clean; 90 passed.
- **Phase-2 preview** (F1, F2 and the delete applied to that file): 89 passed (minimal);
  inventory diff exactly 12 `missing test` (the 11 fold members and the deletion, all at their
  `test_streaming_adversarial.py` ids) and 2 `new test` (the two fold tests); the 32 moved ids
  unchanged; `moves.problems()` with the 32-entry map — none; must-keep and negative logs
  unchanged.

## The self-test

`tests/_gates/inventory.py` keys every entry by `moves.claim` → `moves.to_baseline`, and
`diff_checks` prints those keys. So the gate names a moved test by its **baseline** id. Dry run of
mutation (b) on the merged text: the gate printed
`tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json: fail 1 -> 0`
(and a `body_hash` line), which matches the current expectation.

Phase 2 therefore changes **one** string in `tests/_gates/selftest.py`, not two:

| constant | today | after the merge |
|---|---|---|
| mutation (b) `path` (`:114`) | `"tests/sanitizer/test_streaming_adversarial.py"` | `"tests/sanitizer/test_streaming.py"` |
| `FRAMING` (`:74-77`) | the adversarial id | **unchanged** — it is the expected gate output, keyed by baseline id; changing it to the new id would make (b) fail ("not reported") |

The mutation's `old` text occurs exactly once in the merged file (`_apply` requires that): the
self-contained and truncated-tail tests have other `pytest.fail` messages. `docs/testing/must-keep.md:349`
(self-test row b) names the edit site `sanitizer/test_streaming_adversarial.py::…`; phase 2 updates
the path there. Not a stop condition.

## `moves.json`

The `ids` section: 32 entries `tests/sanitizer/test_streaming.py::<name>` →
`tests/sanitizer/test_streaming_adversarial.py::<name>`; nothing for the two fold tests or the
deleted id. No `files` entry:

- `files: {"tests/sanitizer/test_streaming_adversarial.py": …}` is refused by `moves.problems()`
  ("the key is not in the current tree");
- `files: {"tests/sanitizer/test_streaming.py": "tests/sanitizer/test_streaming_adversarial.py"}`
  passes `problems()` but sends the 46 base-file tests to baseline ids that do not exist.

The map lands in the commit that `git rm`s the adversarial file; while the file exists every
entry is refused ("the value is still in the current tree").

## Where the manifests put the moved ids

The ledger keys by baseline id too (`ledger._translated` → `nest`), so in
`expected_outcomes.{minimal,full}.json` the 32 moved ids **stay under the
`tests/sanitizer/test_streaming_adversarial.py` key** (44 → 32 entries, all `passed`) — as after
Task 2, where `tests/test_litellm_hook.py` still holds 227 entries. `tests/sanitizer/test_streaming.py`
gains the two fold functions with 8 and 3 cases (`passed` in both). `baseline_checks/sanitizer.json`:
−24 lines (12 tests + their 12 `cases` lines), +4 (2 tests + 2 `cases`); the 32 moved lines do
not change. `negative_log_checks.json`, `name_pinned.json`, `must_keep/`, `coverage.*.json`,
`not_applicable.json`, `external_deps.json`: unchanged.

## Negative-log and name-pinned checks

- `negative_log_checks.json`: no site and no `security_node_ids` entry names the adversarial
  file. The one `test_streaming.py` site (`:715`, owner the must-keep
  `::test_responses_stream_event_reconstruct_failure_log_has_no_original`) keeps its line because
  everything is appended after `:797`.
- `name_pinned.json`: no adversarial id and no `test_streaming.py` id (it cites only
  `test_streaming_chat_sse.py`). No doc under `CLAUDE.md`, `README.md`, `docs/*.md` or
  `docs/ops/*.md` cites the adversarial file.

## Task 3b citations to annotate in phase 2

In `deleted-tests.md` (no gate reads the survivor column; this is for navigation):

| row | cited | annotation |
|---|---|---|
| 48 `…framing_intact_all_json` | survivor `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json`; fault-injection `test_streaming_adversarial.py:82` | `(now: tests/sanitizer/test_streaming.py::test_framing_integrity_every_data_line_is_valid_json)`, `(now: test_streaming.py:852)` |
| 49 `…placeholder_restored_and_no_leak` | survivor `::test_framing_integrity_original_reconstructed_after_split`; `test_streaming_adversarial.py:97` | `(now: deleted in Task 4; the row's other survivor test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled holds it)` |
| 50 `…str_chunks_return_str` | survivor `::test_str_input_returns_str_output`; `test_streaming_adversarial.py:380` | `(now: tests/sanitizer/test_streaming.py::test_str_input_returns_str_output)`, `(now: test_streaming.py:1141)` |

The new lines are those of the phase-2 preview layout below; phase 2 re-reads them.
`docs/testing/task3b-prune-audit.md` also cites adversarial ids (`:293-300`, `:309-311`, `:322`,
`:445`); it is a record of Task 3b and is left as it is.

## The merged file, in order

1. `test_streaming.py` `:1-797` unchanged (so `:715` and every base line stay).
2. The adversarial body in its own order, after its docstring, `from __future__`, imports and
   `_mapping` are dropped: the helper banner with `_collect_bytes`, `_collect_str`, `_data_obj`,
   then sections 1-12 with their helpers where they are. The adversarial docstring may become a
   `#` banner (comments are not hashed).
3. F1 in section 2, where `test_malformed_data_line_passes_through_unchanged` was; F2 in
   section 3, where `test_non_text_content_block_start_no_desanitizer_created` was (after the
   section's helpers); the deleted test out of section 1; the other 9 members out.

## Sanity run (2026-10-05)

`pytest tests/sanitizer/test_streaming.py tests/sanitizer/test_streaming_adversarial.py -q -rs`:
minimal (`.venv-test-minimal`, no `CI`, `CORP_REQUIRE_PROXY_CAPTURE=1`) **90 passed**; full
(`.venv-test-full`, `CI=true`, Postgres `pg-test` up on 55432) **90 passed**. No skips. Both
fingerprints match (`python -m tests._gates.fingerprint <env> --check` → 0). Expected after
phase 2: 89 in each.

## Open questions / decisions by rule

Decided by the plan's rules:

- **Must-keep.** `tests/sanitizer/test_streaming.py` is in neither the step-1 lists nor
  `STEP2_GLOBS` of `tests/_gates/must_keep.py`, and the adversarial file is in neither either.
  So (rev 11) the two fold tests are **not** must-keep and `must_keep/` stays byte-identical
  (dry run: `must_keep.function_ids()` gives only the one existing id). That id,
  `test_streaming.py::test_responses_stream_event_reconstruct_failure_log_has_no_original`
  (`:697`, its negative check at `:715`), is no fold member and no deletion.
- Folds by the rev 9 rule (F1, F2); F2's checks are vacuous but identical, so it is folded, and
  Task 7 decides on the cases.
- The `assert result == []` trio is not a fold (the operation differs).
- One deletion by criterion (3); near-twins on other data stay moves.
- The Task 3b audit document is left as a record; only `deleted-tests.md` rows are annotated.

Against the brief (findings, not choices):

- `FRAMING` does not change; only mutation (b)'s `path` does (see "The self-test").
- The moved ids' outcomes stay under the adversarial file's key in `expected_outcomes.*`
  (baseline-keyed), not under the base file's.

Left for review:

- The plan's Development Approach says a move half and a prune half never share a PR, and Task 4
  is titled a move PR; rev 9 makes each fold a prune-style row and the brief adds criterion-(3)
  deletions to this PR. This audit follows the brief. If the split is wanted, the one deletion
  becomes a move (33 / 11 / 0) and waits for a prune PR.
- The fold test names and param ids are proposals.
- Smells for Task 7 (flagged ⚠ in the table): vacuous checks in F2,
  `test_flush_called_twice_does_not_raise`, `test_feed_after_complete_stream_is_safe`; weak or
  conditional asserts in `test_input_json_delta_after_tool_use_block_start_unchanged` and
  `test_input_json_delta_passes_through_byte_identical`; stale docstrings (`:217`, `:257`,
  `:661`); the Cyrillic test that never splits a character.
