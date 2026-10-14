"""Acceptance check of the litellm guardrail adoption plan (20260926): the policy and
guardrail route families are refused."""

from __future__ import annotations

import re

from corp_llm_gateway.route_gate.table import LITELLM_REGEX_TABLE, LITELLM_ROUTE_TABLE, Verdict


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
