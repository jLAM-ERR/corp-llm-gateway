"""The gateway allow-list against the gateway's own route table.

Nginx admits no pair the gateway (``corp_llm_gateway.route_gate``) refuses. Needs
no litellm, so it runs everywhere. The warmed table from the pinned image is
checked by ``tests/integration/test_nginx_allowlist_image.py``.
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.route_gate import Verdict, classify, lookup
from tests.compose.nginx_allowlist import declared_pairs, gateway_snippet, passing_pairs

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

GATEWAY_OWNED = {("GET", "/healthz/live"), ("POST", "/internal/issue-token")}


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
        # table.py rule 1, checked against litellm's source by docs/ops/upgrade.md step 2.
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
