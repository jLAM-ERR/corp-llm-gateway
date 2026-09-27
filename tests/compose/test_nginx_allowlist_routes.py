"""The gateway allow-list against the route tables it fronts.

Two guards, one invariant:

* **litellm's source** (``tests/route_gate/litellm_routes.py``, the ``ast``
  collector the route-gate guard uses): every admitted pair that is not
  gateway-owned is a route litellm registers; every pair litellm registers at an
  admitted path is admitted or in ``BLOCKED_AT_NGINX``; litellm registers
  nothing at a gateway-owned path. Skips only without litellm (``.venv``); runs
  on ``.venv-bench``. The warmed table from the pinned image is checked by
  ``tests/integration/test_nginx_allowlist_image.py``.
* **the gateway's own table** (``corp_llm_gateway.route_gate``): nginx admits no
  pair the gateway refuses. Needs no litellm, so it runs everywhere.
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.route_gate import GATEWAY_ROUTE_TABLE, Verdict, classify, lookup
from tests.compose.nginx_allowlist import (
    BLOCKED_AT_NGINX,
    WEBSOCKET,
    declared_pairs,
    gateway_owned,
    gateway_paths_litellm_registers,
    gateway_snippet,
    installed_litellm_root,
    missing_from_litellm,
    passing_pairs,
    stale_blocked_entries,
    unaccounted_at_admitted_paths,
)
from tests.route_gate.litellm_routes import Collection, collect

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# "The security constraint that shapes this design": the admitted set.
ADMITTED = {
    ("POST", "/v1/messages"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
    ("GET", "/healthz/live"),
    ("POST", "/internal/issue-token"),
}
GATEWAY_OWNED = {("GET", "/healthz/live"), ("POST", "/internal/issue-token")}


def test_the_snippet_declares_exactly_the_admitted_set() -> None:
    declared = declared_pairs(gateway_snippet())

    assert declared == ADMITTED
    assert gateway_owned(declared) == GATEWAY_OWNED
    assert set(GATEWAY_ROUTE_TABLE) >= GATEWAY_OWNED


# --------------------------------------------------------------------------- #
# nginx admits nothing the gateway refuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), sorted(declared_pairs(gateway_snippet())))
def test_the_gateway_table_admits_every_pair_nginx_admits(method: str, path: str) -> None:
    entry = lookup(method, path)

    assert entry is not None, f"{method} {path}: the gateway has no row, so it refuses it"
    if (method, path) in GATEWAY_OWNED:
        # Terminates in the HealthRouter; issuance refuses any body itself.
        assert entry.verdict is Verdict.PASSTHROUGH, (method, path, entry)
    elif method in BODY_METHODS:
        # table.py rule 1, pinned against litellm's source by the route guard.
        assert entry.verdict is Verdict.REWRITTEN, (method, path, entry)
    else:
        assert entry.verdict is Verdict.PASSTHROUGH, (method, path, entry)


@pytest.mark.parametrize(
    ("method", "path"), sorted(passing_pairs(declared_pairs(gateway_snippet())))
)
def test_the_gateway_classifies_every_pair_that_passes_nginx_as_served(
    method: str, path: str
) -> None:
    # HEAD rides along with GET at nginx; the gateway's classifier decides it
    # (HEAD inherits the GET verdict, and is never REWRITTEN).
    decision = classify(method, path, path.encode())

    assert decision.verdict is not Verdict.REFUSE, (method, path, decision)


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/messages/count_tokens"), ("POST", "/v1/responses/input_tokens")],
)
def test_the_token_counters_are_refused_by_the_gateway_too(method: str, path: str) -> None:
    # The control: the same lookup does return REFUSE, so the assertion above
    # cannot pass vacuously.
    entry = lookup(method, path)

    assert entry is not None and entry.verdict is Verdict.REFUSE
    assert (method, path) not in passing_pairs(declared_pairs(gateway_snippet()))


def test_embeddings_is_not_rewritten_and_not_admitted() -> None:
    entry = lookup("POST", "/v1/embeddings")

    assert entry is None or entry.verdict is not Verdict.REWRITTEN
    assert ("POST", "/v1/embeddings") not in declared_pairs(gateway_snippet())


# --------------------------------------------------------------------------- #
# litellm's installed source
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def registered() -> set[tuple[str, str]]:
    collection: Collection = collect(installed_litellm_root())
    return collection.route_keys()


def test_every_admitted_litellm_pair_is_a_route_litellm_registers(
    registered: set[tuple[str, str]],
) -> None:
    missing = missing_from_litellm(declared_pairs(gateway_snippet()), registered)

    assert missing == [], f"nginx admits pairs litellm does not serve: {missing}"


def test_litellm_registers_nothing_at_a_gateway_owned_path(
    registered: set[tuple[str, str]],
) -> None:
    # A litellm release that serves /healthz/live or /internal/issue-token is a
    # decision, not a surprise.
    clash = gateway_paths_litellm_registers(declared_pairs(gateway_snippet()), registered)

    assert clash == []


def test_every_litellm_pair_at_an_admitted_path_is_admitted_or_blocked(
    registered: set[tuple[str, str]],
) -> None:
    unaccounted = unaccounted_at_admitted_paths(declared_pairs(gateway_snippet()), registered)

    assert unaccounted == [], (
        "litellm registers pairs at an admitted path that nginx neither admits nor "
        "lists in BLOCKED_AT_NGINX. Decide each one — an entry in one of the two "
        f"lists, not an nginx change unless the pair is genuinely reachable: {unaccounted}"
    )


def test_every_blocked_entry_is_a_registered_pair_nginx_does_not_admit(
    registered: set[tuple[str, str]],
) -> None:
    assert (WEBSOCKET, "/v1/responses") in registered, "the collector sees no WebSocket here"
    assert stale_blocked_entries(declared_pairs(gateway_snippet()), registered) == []
    assert set(BLOCKED_AT_NGINX) == {(WEBSOCKET, "/v1/responses")}


def test_a_new_pair_at_an_admitted_path_fails_the_guard(
    registered: set[tuple[str, str]],
) -> None:
    # A bump that adds PUT /v1/messages must go red until someone decides it.
    drifted = registered | {("PUT", "/v1/messages")}

    assert unaccounted_at_admitted_paths(declared_pairs(gateway_snippet()), drifted) == [
        "PUT /v1/messages"
    ]
