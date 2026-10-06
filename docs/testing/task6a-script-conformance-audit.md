# Task 6a audit — script-conformance fold of `tests/deploy/` (phase 1)

Plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 14), Task 6a. Branch
`chore/test-suite-task6a-script-conformance` from `release/1.0.x` at `c0c4258`. Phase 1 changes no
test and no manifest: the fold below was applied and gated on a scratch clone only. Every line
number is at `c0c4258` unless it says "after the fold".

## Result

- **4 fold pairs, 8 members → 4 tests in `tests/deploy/test_deploy_script.py`**, each parametrised
  over `script` = `[SCRIPT, BOOTSTRAP]`, ids `[deploy]` / `[bootstrap-server]`. 0 deletions, 0 moves,
  no `moves.json` entry (rev 9: a fold is function-level), `src/` untouched.
- The four keep their names. The bootstrap file loses them and keeps a two-line comment.
- The two same-named security pairs (`test_env_file_contents_are_never_read_or_printed`,
  `test_no_secret_or_real_host_is_committed`) are not identical and stay per script.
- No fold member and no new id is must-keep; `must_keep/` stays byte-identical.
- Ids per env: bootstrap 26 → 22, deploy 151 → 155 (81 functions both before and after); the
  ledgers stay at 5,932 ids per env (minimal collects 5,117 → 5,117).

## 1. The universe

107 test functions (26 + 81). Every one of them also delegates the conftest autouse
`_no_logger_state_left_behind` (fail 1); that is left out of the column. Checks are from
`tests/_manifests/baseline_checks/deploy.json`, cases from the same shard (min / full).

#### `tests/deploy/test_bootstrap_server_script.py`

| # | test | def line | checks (asserts / raises / fail; other delegated; cases min / full) | decision |
|---|---|---|---|---|
| B1 | `test_script_is_executable_bash_with_strict_mode` | 42 | a4 r0 f0; —; 1 / 1 | **fold** |
| B2 | `test_defines_repo_standard_helpers_writing_to_stderr` | 49 | a2 r0 f0; `_function_body` a1; 1 / 1 | **fold** |
| B3 | `test_bash_syntax_is_valid` | 56 | a1 r0 f0; —; 1 / 1 | **fold** |
| B4 | `test_shellcheck_clean` | 62 | a1 r0 f0; —; 1 / 1 | **fold** |
| B5 | `test_help_exits_zero_without_touching_the_host` | 67 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B6 | `test_unknown_flag_is_refused` | 73 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B7 | `test_target_dir_defaults_to_opt_corp_llm_gateway` | 79 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B8 | `test_requires_root` | 83 | a2 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B9 | `test_env_is_seeded_from_example_at_0600_then_exits_one` | 89 | a4 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B10 | `test_existing_env_file_is_never_overwritten` | 99 | a2 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B11 | `test_no_destructive_command_anywhere` | 107 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B12 | `test_existing_systemd_unit_needs_an_explicit_force_flag` | 113 | a5 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B13 | `test_systemd_unit_is_opt_in` | 122 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B14 | `test_unsupported_distro_is_refused_with_an_actionable_message` | 127 | a4 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B15 | `test_docker_daemon_wait_mirrors_demo_sh_polling_loop` | 136 | a6 r0 f0; `_function_body` a1; 1 / 1 | keep per script — reads shell function `wait_for_docker_daemon` and asserts one more line (`"fatal" in body`) than D13 |
| B16 | `test_compose_v2_is_verified_not_assumed` | 146 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B17 | `test_env_file_contents_are_never_read_or_printed` | 153 | a1 r0 f0; —; 1 / 1 | keep per script — selects lines by `$ENV_FILE`, reader regex has no `.env` tail (§2); must-keep (`step2:named`) |
| B18 | `test_no_secret_or_real_host_is_committed` | 163 | a3 r0 f0; —; 1 / 1 | keep per script — also scans the systemd unit (`unit_text`, a loop over two texts) (§2) |
| B19 | `test_systemd_unit_runs_compose_from_the_target_dir` | 171 | a9 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B20 | `test_unit_working_directory_follows_a_custom_target_dir` | 183 | a1 r0 f0; `_function_body` a1; 1 / 1 | keep per script — no cross-file twin |
| B21 | `test_seed_env_writes_0600_and_stops_for_editing` | 200 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B22 | `test_seed_env_keeps_an_existing_env_untouched` | 212 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B23 | `test_seed_env_refuses_when_no_example_has_been_synced` | 223 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B24 | `test_target_dir_is_created_0750_and_is_idempotent` | 231 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B25 | `test_relative_target_dir_is_refused` | 244 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| B26 | `test_running_as_non_root_is_refused` | 253 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |

#### `tests/deploy/test_deploy_script.py`

| # | test | def line | checks (asserts / raises / fail; other delegated; cases min / full) | decision |
|---|---|---|---|---|
| D1 | `test_script_is_executable_bash_with_strict_mode` | 222 | a4 r0 f0; —; 1 / 1 | **fold** |
| D2 | `test_defines_repo_standard_helpers_writing_to_stderr` | 229 | a2 r0 f0; `_function_body` a1; 1 / 1 | **fold** |
| D3 | `test_bash_syntax_is_valid` | 236 | a1 r0 f0; —; 1 / 1 | **fold** |
| D4 | `test_shellcheck_clean` | 242 | a1 r0 f0; —; 1 / 1 | **fold** |
| D5 | `test_help_documents_every_subcommand_and_the_log_warning` | 247 | a5 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D6 | `test_missing_host_is_refused` | 259 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D7 | `test_unknown_option_and_unknown_subcommand_are_refused` | 265 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D8 | `test_remote_dir_default_matches_bootstrap_server` | 272 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D9 | `test_only_the_production_compose_entrypoint_is_used` | 288 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D10 | `test_no_destructive_flag_anywhere` | 308 | a5 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D11 | `test_env_file_contents_are_never_read_or_printed` | 319 | a1 r0 f0; —; 1 / 1 | keep per script — selects lines by a literal `.env`, reader regex needs `\s+[^\|]*\.env\b` (§2); must-keep (`step2:named`) |
| D12 | `test_no_secret_or_real_host_is_committed` | 328 | a3 r0 f0; —; 1 / 1 | keep per script — scans the script only, no loop (§2) |
| D13 | `test_healthcheck_polling_mirrors_demo_sh` | 336 | a5 r0 f0; `_function_body` a1; 1 / 1 | keep per script — reads shell function `wait_for_healthcheck`, one assert fewer than B15 |
| D14 | `test_pull_gates_the_up` | 345 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D15 | `test_bad_remote_dir_is_refused` | 366 | a2 r0 f0; —; 5 / 5 | keep per script — no cross-file twin |
| D16 | `test_bad_host_is_refused` | 375 | a2 r0 f0; —; 3 / 3 | keep per script — no cross-file twin |
| D17 | `test_schema_is_staged_from_source_before_sync` | 386 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D18 | `test_missing_schema_source_is_fatal` | 397 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D19 | `test_sync_never_uploads_the_local_env_and_never_clobbers_the_servers` | 414 | a9 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D20 | `test_sync_refuses_if_the_env_exclude_is_ever_dropped` | 439 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D21 | `test_missing_remote_dir_points_at_the_bootstrap_script` | 460 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D22 | `test_missing_remote_env_is_fatal_and_never_uploaded` | 468 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D23 | `test_ready_remote_passes` | 478 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D24 | `test_a_second_concurrent_deploy_is_refused` | 491 | a5 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D25 | `test_lock_owner_tag_cannot_inject_a_remote_command` | 504 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D26 | `test_release_lock_is_a_noop_when_not_held` | 520 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D27 | `test_force_unlock_clears_a_stale_lock` | 530 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D28 | `test_healthy_stack_stops_polling` | 547 | a1 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D29 | `test_unhealthy_stack_fails_after_the_timeout` | 565 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D30 | `test_a_starting_healthcheck_is_not_mistaken_for_healthy` | 578 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D31 | `test_a_service_without_a_healthcheck_is_healthy_when_running` | 595 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D32 | `test_a_failed_one_shot_fails_at_once_and_names_it` | 609 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D33 | `test_a_one_shot_without_an_exit_code_is_not_mistaken_for_done` | 632 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D34 | `test_the_one_shot_list_is_every_restart_no_service` | 653 | a2 r0 f0; `_shell_array` a1; 1 / 1 | keep per script — no cross-file twin |
| D35 | `test_only_a_running_service_or_a_finished_one_shot_is_done` | 688 | a4 r0 f0; —; 7 / 7 | keep per script — no cross-file twin |
| D36 | `test_a_failed_service_that_is_not_a_one_shot_fails_without_blaming_dependents` | 717 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D37 | `test_the_logs_hint_selects_the_same_stack` | 754 | a2 r0 f0; —; 9 / 9 | keep per script — no cross-file twin |
| D38 | `test_empty_ps_output_is_not_mistaken_for_healthy` | 773 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D39 | `test_status_summary_lists_services_without_env_values` | 785 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D40 | `test_up_stages_syncs_pulls_and_reports` | 819 | a7 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D41 | `test_up_on_an_unprepared_host_changes_nothing` | 840 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D42 | `test_down_needs_confirmation_and_never_removes_volumes` | 850 | a6 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D43 | `test_restart_without_a_service_argument_works` | 866 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D44 | `test_a_service_argument_that_could_inject_a_command_is_refused` | 882 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D45 | `test_dry_run_touches_nothing_on_the_remote` | 893 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D46 | `test_oauth_mode_layers_the_overlay_on_every_remote_compose_call` | 916 | a5 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D47 | `test_the_default_mode_is_the_subscription_mode` | 941 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D48 | `test_virtual_keys_mode_keeps_the_base_file_alone` | 958 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D49 | `test_the_usage_names_oauth_as_the_default` | 975 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D50 | `test_the_oauth_overlay_is_synced_unlike_the_dev_only_build_overlay` | 980 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D51 | `test_an_unknown_mode_is_refused_before_anything_remote_happens` | 998 | a3 r0 f0; —; 4 / 4 | keep per script — no cross-file twin |
| D52 | `test_the_default_run_carries_no_issuance_overlay` | 1033 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D53 | `test_issuance_layers_the_third_overlay_after_oauth_on_every_call` | 1053 | a7 r0 f0; —; 3 / 3 | keep per script — no cross-file twin |
| D54 | `test_issuance_keeps_the_file_list_on_read_only_runs` | 1080 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D55 | `test_issuance_is_refused_outside_the_subscription_mode` | 1101 | a4 r0 f0; —; 3 / 3 | keep per script — no cross-file twin |
| D56 | `test_a_bad_deploy_issuance_value_is_refused` | 1119 | a3 r0 f0; —; 4 / 4 | keep per script — no cross-file twin |
| D57 | `test_deploy_issuance_off_values_keep_two_files` | 1136 | a3 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D58 | `test_issuance_up_without_a_server_config_fails_before_anything_changes` | 1154 | a7 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D59 | `test_sync_never_overwrites_the_servers_config_toml` | 1177 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D60 | `test_sync_never_creates_a_server_config_toml_from_the_laptop` | 1196 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D61 | `test_help_documents_issuance` | 1213 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D62 | `test_rsync_keeps_the_five_extension_excludes_and_adds_the_certs_dir` | 1253 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D63 | `test_a_dry_run_would_send_the_nginx_config_and_no_certificate` | 1268 | a6 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D64 | `test_a_sync_keeps_the_servers_certificates_and_sends_none_of_the_laptops` | 1292 | a4 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D65 | `test_the_script_never_sets_a_profile` | 1316 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D66 | `test_no_remote_command_carries_a_profile` | 1334 | a5 r0 f0; —; 5 / 5 | keep per script — no cross-file twin |
| D67 | `test_up_refuses_both_front_doors_before_the_pull` | 1350 | a7 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D68 | `test_up_accepts_one_front_door` | 1374 | a2 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D69 | `test_up_removes_the_front_door_the_env_no_longer_selects` | 1408 | a5 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D70 | `test_up_without_a_profile_removes_a_leftover_nginx` | 1433 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D71 | `test_up_keeps_the_front_door_the_env_selects` | 1453 | a2 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D72 | `test_down_removes_every_front_door_container_compose_down_leaves` | 1469 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D73 | `test_a_container_name_outside_the_charset_is_refused` | 1500 | a5 r0 f0; —; 14 / 14 | keep per script — no cross-file twin |
| D74 | `test_a_dry_run_removes_no_container` | 1524 | a2 r0 f0; —; 2 / 2 | keep per script — no cross-file twin |
| D75 | `test_up_stops_before_the_pull_when_compose_cannot_resolve_the_stack` | 1539 | a3 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D76 | `test_a_dry_run_does_not_check_profiles_against_the_old_files` | 1556 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D77 | `test_up_fails_at_once_on_a_dead_front_door_and_names_it` | 1583 | a5 r0 f0; —; 6 / 6 | keep per script — no cross-file twin |
| D78 | `test_a_live_front_door_is_polled_like_any_service` | 1622 | a4 r0 f0; —; 5 / 5 | keep per script — no cross-file twin |
| D79 | `test_only_the_front_door_fails_fast_on_exited` | 1646 | a2 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D80 | `test_the_boot_time_unit_still_runs_a_bare_compose_up` | 1661 | a1 r0 f0; —; 1 / 1 | keep per script — no cross-file twin |
| D81 | `test_the_reboot_path_starts_what_the_env_file_selects` | 1676 | a3 r0 f0; `skip_or_fail` f1; 9 / 9 | keep per script — no cross-file twin |

Totals: 8 fold, 99 keep. Baseline outcome of every row: `passed` in both envs (26 + 151 ids).

## 2. The sweep, the identity proof and the difference proof

Method: every test function of both files, normalised three ways. (a) The inventory's own
`normalised_dump(module, node, drop_name=True)` of the test body. (b) The inventory's whole
`body_hash` input, rebuilt from `inventory_entry`'s closure (the test body plus every fixture,
helper, constant and mark it reaches). (c) A looser dump, for discovery only, with every literal
and the names `SCRIPT` / `BOOTSTRAP` / `UNIT` erased.

| comparison | cross-file pairs equal |
|---|---|
| same name | 6: the 4 fold pairs + the 2 security pairs |
| (a) test body, literally | 2: B1 = D1, B2 = D2 |
| (a) test body, modulo the script path literal | 4: the fold pairs |
| (b) recorded `body_hash`, modulo the script path | 4: the fold pairs |
| (c) loose, literals erased | 6: the 4 fold pairs, B5 ~ D6, B6 ~ D6 |

**The four fold pairs — equal by the gate's own measure, modulo the script.** No pair has a
literally equal `body_hash`. Each module binds its own script to the module-level name `SCRIPT`,
and the inventory evaluates that path. If the bootstrap member's hash input is rebuilt with
`<repo>/scripts/deploy/bootstrap-server.sh` rewritten to `<repo>/scripts/deploy/deploy.sh`, the
result is **exactly** the deploy member's recorded `body_hash`:

| pair | bootstrap `body_hash` | deploy `body_hash` | bootstrap rehashed with the deploy path | test-body dumps | closure parts (count) | where the path sits |
|---|---|---|---|---|---|---|
| `test_script_is_executable_bash_with_strict_mode` | `4df529d1…` | `b3e06f64…` | `b3e06f64…` = | literally equal | 13 / 13, only `const:SCRIPT` differs | closure only |
| `test_defines_repo_standard_helpers_writing_to_stderr` | `e35bacfb…` | `da7d2241…` | `da7d2241…` = | literally equal | 14 / 14, only `const:SCRIPT` differs | closure only |
| `test_bash_syntax_is_valid` | `00542b53…` | `8126e7de…` | `8126e7de…` = | equal modulo the path | 11 / 11, only `const:SCRIPT` differs | closure + body |
| `test_shellcheck_clean` | `7c4b5ea8…` | `65e29a2a…` | `65e29a2a…` = | equal modulo the path | 12 / 12, only `const:SCRIPT` differs | closure + body |

In the last two, `generic_visit` in `tests/_gates/inventory.py` runs `eval_path` on `str(SCRIPT)`
(a `Call`, not a `Name`) and inlines it into the body dump as
`Constant('<repo>/scripts/deploy/<script>')`. So the path appears in the body too, and that path is
the only difference. Every other inventory column is equal within each pair: asserts, raises,
fail, `delegated`, `case_data`, fixtures, helpers and constants. The shared closure is also
identical across the two modules: `_function_body` and the module-scoped `script_text` fixture
(`return SCRIPT.read_text()`) give equal normalised dumps in both files.

**The two security pairs — not identical; they stay per script, verbatim.**

- `test_env_file_contents_are_never_read_or_printed` (B17 `:153`, D11 `:319`; both must-keep,
  `STEP2_IDS`). Even modulo the script path the body dumps differ (the rehash gives
  `05725253…` ≠ `5ba495b7…`). Two predicates differ:
  - selector — bootstrap `:156` `if "$ENV_FILE" not in stripped or stripped.startswith("#"):`,
    deploy `:322` `if ".env" not in stripped or stripped.startswith("#"):`;
  - reader — bootstrap `:158` `re.match(r"^(source|\.|cat|echo|printf|grep|sed|awk)\s", stripped)`,
    deploy `:324-325` `reader = r"^(source|\.|cat|echo|printf|grep|sed|awk)\s+[^|]*\.env\b"` then
    `re.match(reader, stripped)`. The deploy reader needs a `.env` token after the command and no
    pipe before it; the bootstrap reader refuses any reader command on a `$ENV_FILE` line. The
    messages also differ ("must not read or print" vs "must not print").

  Gate self-test (d) is this exact selector swap. It still rejects on the folded tree (§6).
- `test_no_secret_or_real_host_is_committed` (B18 `:163`, D12 `:328`). The bootstrap member takes
  `script_text, unit_text` and loops over `(("script", script_text), ("unit", unit_text))`, so it
  also scans `scripts/deploy/corp-llm-gateway.service`. The deploy member takes `script_text` only,
  has no loop, and names the credential regex `credential`. Columns differ: `case_data.loops`
  (1 vs 0), fixtures / helpers (`unit_text`), constants (`UNIT`), closure parts 16 vs 13.

**The `mirrors_demo_sh` pair is not a pair.** B15 `:136` reads shell function
`wait_for_docker_daemon` and has 6 asserts (the sixth is `"fatal" in body`). D13 `:336` reads
`wait_for_healthcheck` and has 5. Different names, a different function and a different check
count, so they are equal under none of (a)-(c).

**Loose-only matches (c), not twins:** B5 `test_help_exits_zero_without_touching_the_host` ~
D6 `test_missing_host_is_refused`, and B6 `test_unknown_flag_is_refused` ~ D6. Each runs
`subprocess.run([str(SCRIPT), <argv>])` and asserts the return code and one stderr substring. The
script, the argv (`--help` / `--wipe-everything` / `status`), the expected code (0 / 1 / 1) and the
substring all differ: they share a shape but not an input. This is a finding for the reviewer;
nothing changes.

**Helpers:** `_function_body` (B `:36`, D `:70`) is identical in both files, but it is a helper,
not a test. It stays in both: B8, B9, B10, B12, B14, B15 and B20 use it in the bootstrap file. The
`script_text` fixture stays in both files too.

**The plan's "six functions each"** (`Context`, the `tests/deploy/test_bootstrap_server_script.py:44-64,155-165`
/ `test_deploy_script.py:222-242,319-328` ranges at `807831a`) means the six same-name pairs: the
four conformance functions plus the two security checks. Task 1c (c339ed1) later moved the
bootstrap file by −2 lines, so they now start at `:42` and `:153`.

## 3. The fold as code

**Layout: confirmed in-file**, in `test_deploy_script.py`'s "content asserts" section. That file
already binds `BOOTSTRAP` (`:26`) and `_function_body` (`:70`). The fold therefore adds no helper,
no import and no constant. A new `tests/deploy/test_script_conformance.py` would need a third
`_function_body`, or an import from a test module. That is visible in the inventory as a new
helper owner, and a reader gains nothing from it.

**Names: kept.** Each test name says what is checked, and the parameter id says which script —
the same convention as the rest of the file (`test_up_accepts_one_front_door[nginx]`).
A `git grep` for the old name still finds the test. Inventory result: 4 `missing test` and 4
changed deploy entries. The reviewer reads each deploy entry's old line against its new one in the
same shard. New names would instead give 8 `missing` and 4 `new`, with 4 more names invented.
Must-keep is safe either way (§4).

**Mechanics:**
- One literal `@pytest.mark.parametrize("script", [SCRIPT, BOOTSTRAP], ids=["deploy", "bootstrap-server"])`
  per test, not a shared mark object. `_case_data` reads only `Call` decorators, so a
  `_BOTH = pytest.mark.parametrize(...)` alias used as `@_BOTH` would hide the case table from the
  inventory.
- `script_text = script.read_text()` is the first statement. It stands in for the module-scoped
  `script_text` fixture, which read the file at setup, before the first assert. The read order is
  kept, and the rest of the body is the original's statements with `SCRIPT` → `script`. One message
  text changes (`f"{script} does not exist"`); no assert changes.
- `@pytest.mark.requires_shellcheck` stays directly above `def test_shellcheck_clean`.

Replacement for `tests/deploy/test_deploy_script.py:217-244` (the banner and the four tests):

```python
# --------------------------------------------------------------------------- #
# content asserts — the first four run on both deploy scripts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("script", [SCRIPT, BOOTSTRAP], ids=["deploy", "bootstrap-server"])
def test_script_is_executable_bash_with_strict_mode(script: Path) -> None:
    script_text = script.read_text()
    assert script.exists(), f"{script} does not exist"
    assert script.stat().st_mode & stat.S_IXUSR, "script is not executable"
    assert script_text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script_text


@pytest.mark.parametrize("script", [SCRIPT, BOOTSTRAP], ids=["deploy", "bootstrap-server"])
def test_defines_repo_standard_helpers_writing_to_stderr(script: Path) -> None:
    script_text = script.read_text()
    for name in ("fatal", "warn", "info"):
        body = _function_body(script_text, name)
        assert ">&2" in body, f"{name}() must write to stderr like scripts/demo.sh"
    assert "exit 1" in _function_body(script_text, "fatal")


@pytest.mark.parametrize("script", [SCRIPT, BOOTSTRAP], ids=["deploy", "bootstrap-server"])
def test_bash_syntax_is_valid(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [SCRIPT, BOOTSTRAP], ids=["deploy", "bootstrap-server"])
@pytest.mark.requires_shellcheck
def test_shellcheck_clean(script: Path) -> None:
    result = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
```

Replacement for `tests/deploy/test_bootstrap_server_script.py:42-66` (the four tests and the two
blank lines after them):

```python
# Strict mode, the stderr helpers, `bash -n` and shellcheck: test_deploy_script.py runs
# them on this script, parametrised over both deploy scripts.


```

Module docstrings (comments and docstrings do not reach any hash):

- `test_bootstrap_server_script.py:3-7` becomes: "`main` installs Docker and writes under `/opt` as
  root, so it is never run here. Two layers instead: content asserts on the script text (no
  destructive command, no committed secret), and behaviour tests that source the script and call
  single functions against a tmp_path — the script only runs `main` when executed directly. Strict
  mode, the repo's stderr helpers, `bash -n` and shellcheck run on this script from
  test_deploy_script.py, parametrised over both deploy scripts." (+2 lines)
- `test_deploy_script.py:9` gains one sentence after "…semantic, not textual.": "The strict-mode,
  stderr-helper, `bash -n` and shellcheck asserts run on bootstrap-server.sh too." (+1 line)

After the fold: AST check against `c0c4258`. Every other top-level node of both files is
unchanged; in the deploy file only the four folded functions differ. The patch is +17 / −10
(deploy) and +8 / −27 (bootstrap). `ruff check` and `ruff format --check` (0.15.14) are clean.

## 4. The 8 ledger rows, ready to copy

Must-keep statement for all 8 (rev 11, checked on the clone with `must_keep.function_ids()`):
- `tests/deploy/test_bootstrap_server_script.py` is in no step-1 list. `tests/deploy/` matches no
  `STEP2_GLOBS` entry. That file's only must-keep function is
  `test_env_file_contents_are_never_read_or_printed` (`STEP2_IDS`).
- `tests/deploy/test_deploy_script.py` is a `STEP1_MODIFIED_TOUCHED` file. Its rule reads
  `e9e877f..807831a` against `807831a`'s copy, and `_step1_touched` selects 43 functions there. None
  of the four fold names is among them.
- No fold name is in `STEP2_IDS`, `POLICY_DEFAULTS`, `negative_log_checks.json` `security_node_ids`
  or `name_pinned.json`.
- On the folded clone, `function_ids()` still selects 45 functions in the two files (1 + 44), none
  of the four. `expand()` returns none of the 8 new param ids, and `must_keep --check` is 0. So the
  fold tests are not must-keep, and `must_keep/` stays byte-identical (no `must_keep --write`).

| PR | deleted node id | baseline outcome (minimal / full) | the check it made | survivor node id(s) | semantic note | fault injection | reviewer |
|---|---|---|---|---|---|---|---|
| Task 6a | `tests/deploy/test_bootstrap_server_script.py::test_script_is_executable_bash_with_strict_mode` | passed / passed | asserts 4, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/deploy/test_deploy_script.py::test_script_is_executable_bash_with_strict_mode[bootstrap-server]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[bootstrap-server]`: `script` = `BOOTSTRAP` = `scripts/deploy/bootstrap-server.sh`, the file this module's `SCRIPT` named. The body is the original's statements with the script as the parameter: `assert script.exists()`, `assert script.stat().st_mode & stat.S_IXUSR`, `assert script_text.startswith("#!/usr/bin/env bash\n")`, `assert "set -euo pipefail" in script_text`; the module-scoped `script_text` fixture becomes `script_text = script.read_text()`, the first statement (the fixture read the file at setup, before the first assert), and the `exists()` message names the parameter. Gate identity: this function's `body_hash` `4df529d1…`, rehashed with the bootstrap path rewritten to `deploy.sh`, is the deploy twin's `b3e06f64…`; the only differing part is `const:SCRIPT`. Same name, now in the other module: no `moves.json` entry (rev 9). Not must-keep (rev 11: the bootstrap file is in no step-1 list; `tests/deploy/` matches no step-2 glob; the deploy file's `step1:touched` rule does not select this name at `807831a`). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_bootstrap_server_script.py::test_defines_repo_standard_helpers_writing_to_stderr` | passed / passed | asserts 2, raises 0, fail 0; delegated: `_function_body` asserts 1, `_no_logger_state_left_behind` fail 1; loop `('fatal', 'warn', 'info')` | `tests/deploy/test_deploy_script.py::test_defines_repo_standard_helpers_writing_to_stderr[bootstrap-server]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[bootstrap-server]`: `script` = `BOOTSTRAP` = `scripts/deploy/bootstrap-server.sh`. The body is the original's statements with the script as the parameter: the loop over `("fatal", "warn", "info")`, `_function_body(script_text, name)`, `assert ">&2" in body`, `assert "exit 1" in _function_body(script_text, "fatal")`; `_function_body` is the deploy module's, whose normalised dump equals this module's; the `script_text` fixture becomes `script_text = script.read_text()`, the first statement. Gate identity: `body_hash` `e35bacfb…`, rehashed with the path rewritten, is the deploy twin's `da7d2241…`; only `const:SCRIPT` differs. No `moves.json` entry (rev 9). Not must-keep (rev 11: no step-1 list names the bootstrap file; no step-2 glob; the deploy file's `step1:touched` rule does not select this name at `807831a`). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_bootstrap_server_script.py::test_bash_syntax_is_valid` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/deploy/test_deploy_script.py::test_bash_syntax_is_valid[bootstrap-server]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[bootstrap-server]`: `script` = `BOOTSTRAP` = `scripts/deploy/bootstrap-server.sh`. The body is the original's statements with the script as the parameter: `subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)`, `assert result.returncode == 0, result.stderr`; no fixture. Gate identity: `body_hash` `00542b53…`, rehashed with the path rewritten, is the deploy twin's `8126e7de…`; the inventory inlines `str(SCRIPT)` as a path constant, so the path differs in the body dump and in `const:SCRIPT`, nowhere else. External dependency: `scripts/deploy/bootstrap-server.sh` is now recorded on the survivor's function (`external_deps.json`). No `moves.json` entry (rev 9). Not must-keep (rev 11: no step-1 list names the bootstrap file; no step-2 glob; the deploy file's `step1:touched` rule does not select this name at `807831a`). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_bootstrap_server_script.py::test_shellcheck_clean` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1; marker `requires_shellcheck` | `tests/deploy/test_deploy_script.py::test_shellcheck_clean[bootstrap-server]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[bootstrap-server]`: `script` = `BOOTSTRAP` = `scripts/deploy/bootstrap-server.sh`. The body is the original's statements with the script as the parameter: `subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)`, `assert result.returncode == 0, result.stdout + result.stderr`; `@pytest.mark.requires_shellcheck` kept on the test, so a missing shellcheck still skips both cases with the same reason. Gate identity: `body_hash` `7c4b5ea8…`, rehashed with the path rewritten, is the deploy twin's `65e29a2a…`; the path differs in the inlined `str(SCRIPT)` and in `const:SCRIPT` only. External dependency: `scripts/deploy/bootstrap-server.sh` now recorded on the survivor's function. No `moves.json` entry (rev 9). Not must-keep (rev 11: no step-1 list names the bootstrap file; no step-2 glob; the deploy file's `step1:touched` rule does not select this name at `807831a`). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_deploy_script.py::test_script_is_executable_bash_with_strict_mode` | passed / passed | asserts 4, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/deploy/test_deploy_script.py::test_script_is_executable_bash_with_strict_mode[deploy]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[deploy]`: `script` = `SCRIPT` = `scripts/deploy/deploy.sh`, the same module constant the original read. The body is the original's statements with the script as the parameter (`SCRIPT` → `script`); the module-scoped `script_text` fixture becomes `script_text = script.read_text()`, the first statement, and the `exists()` message names the parameter. Same function name in the same module, so the inventory reports it as changed, not new: `body_hash`, `case_data` (+ the 2-case table), constants (+ `BOOTSTRAP`), fixtures (− `script_text`); asserts, raises, fail and `delegated` unchanged. No `moves.json` entry (rev 9). Not must-keep (rev 11: a step-1 touched file whose rule reads `807831a`, where this function is outside every hunk of `e9e877f..807831a`; no step-2 glob). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_deploy_script.py::test_defines_repo_standard_helpers_writing_to_stderr` | passed / passed | asserts 2, raises 0, fail 0; delegated: `_function_body` asserts 1, `_no_logger_state_left_behind` fail 1; loop `('fatal', 'warn', 'info')` | `tests/deploy/test_deploy_script.py::test_defines_repo_standard_helpers_writing_to_stderr[deploy]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[deploy]`: `script` = `SCRIPT` = `scripts/deploy/deploy.sh`. The body is the original's statements with the script as the parameter; the `script_text` fixture becomes `script_text = script.read_text()`, the first statement; same `_function_body`, same loop. Inventory: changed, not new — `body_hash`, `case_data` (+ the 2-case table), constants (+ `BOOTSTRAP`), fixtures (− `script_text`); asserts, raises, fail and `delegated` unchanged. No `moves.json` entry (rev 9). Not must-keep (rev 11: a step-1 touched file whose rule reads `807831a`, where this function is outside every hunk of `e9e877f..807831a`; no step-2 glob). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_deploy_script.py::test_bash_syntax_is_valid` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/deploy/test_deploy_script.py::test_bash_syntax_is_valid[deploy]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[deploy]`: `script` = `SCRIPT` = `scripts/deploy/deploy.sh`. The body is the original's statements with the script as the parameter: `subprocess.run(["bash", "-n", str(script)], …)`, `assert result.returncode == 0, result.stderr`. Inventory: changed, not new — `body_hash`, `case_data` (+ the 2-case table), constants (+ `BOOTSTRAP`); asserts, raises, fail, `delegated`, fixtures unchanged; `external_deps.json` adds `scripts/deploy/bootstrap-server.sh` to this function, `programs` stays `['bash']`. No `moves.json` entry (rev 9). Not must-keep (rev 11: a step-1 touched file whose rule reads `807831a`, where this function is outside every hunk of `e9e877f..807831a`; no step-2 glob). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |
| Task 6a | `tests/deploy/test_deploy_script.py::test_shellcheck_clean` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1; marker `requires_shellcheck` | `tests/deploy/test_deploy_script.py::test_shellcheck_clean[deploy]` | Script-conformance fold: 4 same-named pairs (8 functions) → 4 tests in `test_deploy_script.py`, each parametrised over `script` = `[SCRIPT, BOOTSTRAP]` (ids `[deploy, bootstrap-server]`), checks per id unchanged. This id → `[deploy]`: `script` = `SCRIPT` = `scripts/deploy/deploy.sh`. The body is the original's statements with the script as the parameter: `subprocess.run(["shellcheck", str(script)], …)`, `assert result.returncode == 0, result.stdout + result.stderr`; `requires_shellcheck` kept. Inventory: changed, not new — `body_hash`, `case_data` (+ the 2-case table), constants (+ `BOOTSTRAP`); asserts, raises, fail, `delegated`, fixtures unchanged; `external_deps.json` adds `scripts/deploy/bootstrap-server.sh`, `programs` stays `['shellcheck']`. No `moves.json` entry (rev 9). Not must-keep (rev 11: a step-1 touched file whose rule reads `807831a`, where this function is outside every hunk of `e9e877f..807831a`; no step-2 glob). | n/a (fold — same script, same assert; the case runs the same subprocess / regex) | auto-review (pending) |

## 5. Predicted phase-2 manifest diff vs `c0c4258`

Measured on the clone. The fold was applied, and the two files' outcomes from a scoped
outcome-ledger run in each venv were spliced into the expected-outcome ledgers in place of the
recorded ones. Then `inventory --write`, `name_pinned --write` and `negative_logs --write` ran.
Phase 2's whole-suite record runs should give the same result for these two files.

| file | change |
|---|---|
| `expected_outcomes.minimal.json`, `.full.json` | per env 5,932 → 5,932 ids. **Out (8):** `test_bootstrap_server_script.py::{test_script_is_executable_bash_with_strict_mode, test_defines_repo_standard_helpers_writing_to_stderr, test_bash_syntax_is_valid, test_shellcheck_clean}` and the same four bare ids in `test_deploy_script.py`. **In (8):** `test_deploy_script.py::<each of the four>[bootstrap-server]` and `[deploy]`. All `passed` on both sides, 0 changed; 2 lines per file (the two files' lines). Minimal collects 5,117 → 5,117. |
| `baseline_checks/deploy.json` | `tests` 155 → 151: −4 (the bootstrap members), +0, 4 changed. `test_bash_syntax_is_valid` and `test_shellcheck_clean` change `body_hash`, `case_data`, `constants`; `test_script_is_executable_bash_with_strict_mode` and `test_defines_repo_standard_helpers_writing_to_stderr` change `body_hash`, `case_data`, `constants`, `fixtures`. `cases` 155 → 151: −4, 4 changed `{"full":1,"minimal":1}` → `{"full":2,"minimal":2}`. git +8 / −16. |
| `external_deps.json` | 647 → 643 entries: −4 (the bootstrap members). 4 changed: the deploy members gain `scripts/deploy/bootstrap-server.sh: e7e7d85ea4084833e534c6680293e417` beside `deploy.sh`; `programs` unchanged. No file hash changes (the scripts are untouched), no file added to or dropped from the hashed set, `unresolved` stays `[]`. git +4 / −28. |
| `negative_log_checks.json` | unchanged. It has no site under `tests/deploy/` (0 lines mention the directory), so no removal shifts a `site`; `negative_logs --write` writes no diff. |
| `name_pinned.json` | unchanged. No entry under `tests/deploy/`, and no doc, `CLAUDE.md`, `src/` file or other test cites the four names (only the plan and the two files). `--write` writes no diff; it prints only the known `CLAUDE.md:177` placeholder line. |
| `must_keep/`, `moves.json`, `coverage.*.json`, `not_applicable.json`, `env_fingerprint.*.json`, `external_deps_overrides.json`, `tests/_gates/selftest.py` | unchanged. |

Line drift (no manifest records these lines; they matter only for prose line refs):
- bootstrap: the docstring +2 shifts `script_text`, `unit_text` and `_function_body` (`:27/:32/:36`
  → `:29/:34/:38`). The 22 remaining tests and `_call` move −19: `:67` → `:48` … `:253` → `:234`; the must-keep
  `test_env_file_contents_are_never_read_or_printed` goes `:153` → `:134`, and
  `test_no_secret_or_real_host_is_committed` goes `:163` → `:144`.
- deploy: the docstring +1 shifts old `:11-216` (`script_text` `:66` → `:67`, `_remote_dir`
  `:209` → `:210`). The four fold tests are at `def` `:224 / :233 / :242 / :249`
  (decorators `:223 / :232 / :241 / :247`). Everything after them moves +7: the must-keep
  `test_env_file_contents_are_never_read_or_printed` goes `:319` → `:326`,
  `test_no_secret_or_real_host_is_committed` goes `:328` → `:335`, and the last test goes
  `:1676` → `:1683`.

## 6. Dry run on the scratch clone

Clone of the checkout at `c0c4258`, under the session scratch directory. Control on the unmodified
clone: `inventory --check` 0. Then the fold above was applied.

- `ruff check tests/deploy/` and `ruff format --check tests/deploy/`: clean.
- Fingerprint `--check`: 0 in both venvs.
- The two files, run with `-p tests._gates.outcome_ledger`: **177 passed in each venv** (22 + 155
  ids), no skips. shellcheck is on PATH, so `requires_shellcheck` runs, and `not_root` runs because
  the user is not root.
- `ledger check <env> --scope <the two files>` before the splice: exactly 8 `missing id` + 8
  `new id` per env (the ids in §5). After the splice: 0.
- `inventory --check` with the baseline manifests: **exactly these 28 lines, nothing else**:
  - 4 × `missing test: tests/deploy/test_bootstrap_server_script.py::<name>` (the four names);
  - `tests/deploy/test_deploy_script.py::test_bash_syntax_is_valid: body_hash "8126e7de74b20749bb2c6c636c9b9b0f" -> "7d25cd529abf0360ef1a862064ddd999"`, `…: case_data {"loops": [], "parametrize": []} -> {… "argnames": "'script'", "cases": 2 …}`, `…: constants [… "SCRIPT" …] -> ["BOOTSTRAP", …]`;
  - `…::test_defines_repo_standard_helpers_writing_to_stderr: body_hash "da7d2241…" -> "140deba4d673255e0b8e034ac5abb792"`, `case_data` (+ the table, the loop kept), `constants` (+ `BOOTSTRAP`), `fixtures [… "script_text"] -> ["_fresh_metrics_exporter", "_no_logger_state_left_behind"]`;
  - `…::test_script_is_executable_bash_with_strict_mode: body_hash "b3e06f64…" -> "9d37b088ed19fa1054a2f8dd962393a6"`, `case_data`, `constants`, `fixtures` (− `script_text`);
  - `…::test_shellcheck_clean: body_hash "65e29a2a…" -> "b98f888070d8c10f9708bf506dbf34af"`, `case_data`, `constants`;
  - 4 × `tests/deploy/test_bootstrap_server_script.py::<name>: external scripts/deploy/bootstrap-server.sh e7e7d85ea4084833e534c6680293e417 -> None`, plus `…::test_bash_syntax_is_valid: programs ['bash'] -> []` and `…::test_shellcheck_clean: programs ['shellcheck'] -> []`;
  - 4 × `tests/deploy/test_deploy_script.py::<name>: external scripts/deploy/bootstrap-server.sh None -> e7e7d85ea4084833e534c6680293e417`.

  The `helpers` column does not change for the two `script_text` tests. The walker resolves the
  local name `script_text` to the module's `script_text` def by name (only imports shadow; the
  closure over-approximates), so the fixture's one-line body stays in their hash.
- `must_keep --check`, `moves --check`, `name_pinned --check`, `negative_logs --check`: 0 in both
  venvs.
- `python -m pytest tests/_gates -q`: **31 passed / 2 failed** in both venvs. The 2 failures are
  `test_the_check_inventory_matches_the_baseline` and
  `test_every_external_dependency_resolves_and_matches_the_baseline`, the two that need the
  regeneration. `test_the_ledgers_cover_every_test_and_count_its_cases` stays green before the
  regeneration because the names are kept.
- After the preview regeneration (§5): `inventory`, `must_keep`, `moves`, `name_pinned`,
  `negative_logs --check` all 0; `tests/_gates` **33 passed** in both venvs; `selftest inventory`
  rejected every mutation a-s, including (d), the `$ENV_FILE` → `.env` swap on the unchanged
  bootstrap security test.
- Coverage: the 8 members import nothing from `src/corp_llm_gateway`. They run `bash` /
  `shellcheck` and read script files. The only `src/` lines they reach are through the conftest
  autouse fixtures (`_fresh_metrics_exporter`, `_no_logger_state_left_behind`), which every other
  test in the suite runs too. So the line and arc sets cannot change by construction; phase 2's
  record runs prove it.

## 7. Noted for Task 7 (not acted on)

- D7 `test_unknown_option_and_unknown_subcommand_are_refused` (`:265`): the assert is
  `argv[-1] in result.stderr or argv[2] in result.stderr`. For the first argv that is
  `"up" in stderr or "--wipe-everything" in stderr`, so a two-letter substring can satisfy the
  check. (Reading only; the script was not run for this note.)
- B20 `test_unit_working_directory_follows_a_custom_target_dir` (`:183`): one substring check
  (`"WorkingDirectory" in body`) for a substitution claim.
- B5 `test_help_exits_zero_without_touching_the_host` (`:67`): the name promises "without touching
  the host"; the asserts are only the return code 0 and the script name in stderr.
- D38 `test_empty_ps_output_is_not_mistaken_for_healthy` (`:773`): return code only, no reason
  checked.
- Cross-file overlap, not a twin: B19 `:171` asserts `^ExecStart=.*docker compose up -d$` on the
  unit, and D80 `:1661` asserts the exact line `ExecStart=/usr/bin/docker compose up -d`. This is
  one assert out of B19's nine.

## Findings against the brief

1. For `test_bash_syntax_is_valid` and `test_shellcheck_clean`, the test-body dumps are **not**
   literally equal: `eval_path` inlines `str(SCRIPT)`. They are equal modulo the path, and the
   recomputed hash proves the path is the only difference in all four pairs (§2). I did not treat
   this as the stop condition, because the fold's parameter is exactly that path.
2. `external_deps.json` **does change** (−4 entries, 4 entries gain `bootstrap-server.sh`). The
   brief expected it unchanged. No hash changes; the per-test file set follows the fold.
3. `helpers` stays unchanged for the two `script_text` tests (§6), so the line count is 28, not the
   30 a naive count gives.
4. Counts: 81 → 85 is the brief's figure, but 81 is a function count. Ids are 151 → 155 (the
   deploy file has 81 functions before and after).
5. With kept names the ledgers-cover gate test stays green until the regeneration. Phase 2's
   pre-record `tests/_gates` and the record runs should therefore fail exactly the inventory and
   external-dependency tests (Tasks 4 / 5a failed inventory and ledgers-cover).

## Questions for the reviewer

- Q1: keep the four names (chosen; 4 missing + 4 changed) or give new names (8 missing + 4 new)?
- Q2: a literal `parametrize` decorator on each test (chosen; the case table is visible to the
  inventory), or one module fixture with `params=` (no case table in `case_data`)?
- Q3: `script_text = script.read_text()` as the first statement mirrors the fixture's setup-time
  read (chosen). Moving it after `assert script.exists()` would make that assert reachable for a
  missing script — strictly stronger, but a reordering. Keep it?
- Q4: ids `deploy` / `bootstrap-server` (chosen) or `deploy.sh` / `bootstrap-server.sh`?
- Q5: accept the `external_deps.json` change (§5) as the expected part of the phase-2 manifest
  diff?
