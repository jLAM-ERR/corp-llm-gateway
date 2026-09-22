"""The guard: litellm's installed source against the route table.

A litellm bump that adds, moves or re-spells a route fails here until someone
classifies it. Skips only when litellm itself is absent (the 3.14 venv); if
litellm is present but its ``proxy/`` source is not, this fails — a skip is
exactly the silence this test exists to prevent.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from importlib.util import find_spec
from pathlib import Path

import pytest

from corp_llm_gateway.litellm_hook import _NON_CHAT_INPUT_CALL_TYPES
from corp_llm_gateway.route_gate import (
    LITELLM_REGEX_TABLE,
    LITELLM_ROUTE_TABLE,
    ROUTE_GATE_UNLISTED,
    ROUTE_GATE_WEBSOCKET,
    Verdict,
    classify,
    lookup,
)
from corp_llm_gateway.route_gate.table import template_matcher

from .litellm_routes import (
    KNOWN_DYNAMIC_REGISTRATIONS,
    Collection,
    Route,
    collect,
    missing_dynamic_sites,
    unknown_dynamic_sites,
)

pytestmark = pytest.mark.skipif(
    find_spec("litellm") is None, reason="litellm is not installed in this interpreter"
)

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _litellm_root() -> Path:
    spec = find_spec("litellm")
    assert spec is not None and spec.origin is not None
    root = Path(spec.origin).parent
    if not (root / "proxy").is_dir():
        pytest.fail(f"litellm is installed at {root} but has no proxy/ source to read")
    return root


@pytest.fixture(scope="module")
def collection() -> Collection:
    return collect(_litellm_root())


def _table_rows() -> Iterator[tuple[str, str, object]]:
    for (method, path), entry in LITELLM_ROUTE_TABLE.items():
        yield method, path, entry
    for row in LITELLM_REGEX_TABLE:
        yield row.method, row.template, row.entry


def _http(collection: Collection) -> list[Route]:
    return sorted(route for route in collection.routes if route.method != "WEBSOCKET")


# ── dynamic registrations ───────────────────────────────────────────────────


def test_every_dynamic_registration_is_known(collection: Collection) -> None:
    unknown = [
        f"{site.module}:{site.line} {site.kind} {site.source}"
        for site in unknown_dynamic_sites(collection)
    ]
    assert unknown == [], (
        "litellm registers a route whose path this cannot resolve. Read each site, "
        "decide its policy and add it to KNOWN_DYNAMIC_REGISTRATIONS:\n" + "\n".join(unknown)
    )


def test_every_known_dynamic_registration_still_exists(collection: Collection) -> None:
    missing = missing_dynamic_sites(collection)
    assert missing == [], (
        "a pinned dynamic registration moved or disappeared; re-read the site and "
        f"re-key it: {missing}"
    )


def test_no_include_site_applies_a_prefix_this_cannot_resolve(collection: Collection) -> None:
    # A prefix the parser cannot read would put every route of that router at
    # the wrong path, and the table would pin a path nothing serves.
    assert [
        f"{site.module}:{site.line} {site.source}" for site in collection.unresolved_includes
    ] == []


# ── (a) every collected route is classified ─────────────────────────────────


def _unlisted(collection: Collection) -> list[str]:
    return sorted(
        f"{route.method} {route.path}  ({route.module}:{route.handler})"
        for route in _http(collection)
        if classify(route.method, route.path, route.path.encode()).block_reason
        == ROUTE_GATE_UNLISTED
    )


def test_every_collected_route_is_listed(collection: Collection) -> None:
    unlisted = _unlisted(collection)
    assert unlisted == [], (
        "litellm serves routes the table does not classify. Decide each one "
        "(REFUSE unless the hook provably rewrites it) and regenerate table.py:\n"
        + "\n".join(unlisted)
    )


# ── (b) a REWRITTEN entry is backed by the hook ─────────────────────────────


def _rewritten_rows() -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for method, path, entry in _table_rows()
        if entry.verdict is Verdict.REWRITTEN  # type: ignore[attr-defined]
    )


def test_every_rewritten_entry_reaches_the_hook(collection: Collection) -> None:
    for method, path in _rewritten_rows():
        routes = collection.by_key(method, path)
        assert routes, f"{method} {path} is REWRITTEN but litellm no longer registers it"
        for route in routes:
            assert route.reaches_hook, (
                f"{method} {path} is REWRITTEN but {route.module}:{route.handler} "
                "never reaches pre_call_hook"
            )
            assert route.call_type not in _NON_CHAT_INPUT_CALL_TYPES, (
                f"{method} {path} is REWRITTEN but its call type {route.call_type!r} "
                "is in the hook's no-rewrite set"
            )


def test_the_rewritten_set_is_the_eight_generation_spellings() -> None:
    # Three more spellings reach the hook (`/openai/v1/responses`,
    # `/openai/v1/responses/compact`, `/openai/deployments/{model:path}/chat/completions`)
    # and are REFUSE anyway — see the ordering test below.
    assert _rewritten_rows() == [
        ("POST", "/chat/completions"),
        ("POST", "/engines/{model:path}/chat/completions"),
        ("POST", "/responses"),
        ("POST", "/responses/compact"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/responses/compact"),
    ]


def _sample(template: str) -> str:
    return re.sub(r"\{[^{}]+\}", "sample", template)


def _hookless_shadows(collection: Collection, method: str, template: str) -> list[Route]:
    """Hook-less routes whose own pattern also matches this template's path."""
    sample = _sample(template)
    return sorted(
        route
        for route in _http(collection)
        if route.method == method
        and route.path != template
        and not route.reaches_hook
        and template_matcher(route.path).match(sample)
    )


def outranked_rewritten_rows(collection: Collection, rows: list[tuple[str, str]]) -> list[str]:
    """REWRITTEN rows a hook-less route would answer first."""
    offenders: list[str] = []
    for method, template in rows:
        winners = collection.by_key(method, template)
        if not winners:
            offenders.append(f"{method} {template}: litellm no longer registers it")
            continue
        first = min(route.order for route in winners)
        offenders += [
            f"{method} {template} (order {first}) is shadowed by hook-less "
            f"{route.path} (order {route.order}, {route.module}:{route.handler})"
            for route in _hookless_shadows(collection, method, template)
            if route.order < first
        ]
    return offenders


def test_no_hookless_route_can_outrank_a_rewritten_one(collection: Collection) -> None:
    """A REWRITTEN verdict promises the hook rewrote the body. litellm registers
    catch-all raw passthroughs (`/openai/{endpoint:path}`) whose pattern also
    matches admitted spellings; they lose only because `include_router` runs
    later. A bump that reorders the includes would send a REWRITTEN path to a
    hook-less handler, so the gate would admit a body nothing sanitized."""
    offenders = outranked_rewritten_rows(collection, _rewritten_rows())
    assert offenders == [], (
        "litellm now reaches a hook-less handler first on a REWRITTEN path; refuse "
        "the spelling or re-read the include order:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "path",
    [
        "/openai/v1/responses",
        "/openai/v1/responses/compact",
        "/openai/deployments/{model:path}/chat/completions",
    ],
)
def test_the_openai_spellings_are_refused_because_a_raw_passthrough_matches_them(
    collection: Collection, path: str
) -> None:
    # The positive control for the test above: these three DO reach the hook, and
    # the only thing keeping the raw `/openai/{endpoint:path}` passthrough off
    # them is that its router is included later. They are REFUSE for that reason.
    sample = re.sub(r"\{[^{}]+\}", "sample", path)
    shadows = [
        route
        for route in _http(collection)
        if route.method == "POST"
        and route.path != path
        and not route.reaches_hook
        and template_matcher(route.path).match(sample)
    ]
    assert [route.path for route in shadows] == ["/openai/{endpoint:path}"]
    assert all(route.reaches_hook for route in collection.by_key("POST", path))
    assert lookup("POST", sample).verdict is Verdict.REFUSE  # type: ignore[union-attr]


# ── (c) a hook-less body route is refused, or justified and provably inert ──


def test_every_hookless_body_route_is_refused_or_justified(collection: Collection) -> None:
    offenders: list[str] = []
    for route in _http(collection):
        if route.method not in BODY_METHODS or route.reaches_hook:
            continue
        entry = lookup(route.method, route.path)
        if entry is None or entry.verdict is Verdict.REFUSE:
            continue
        where = f"{route.method} {route.path} ({route.module}:{route.handler})"
        if entry.verdict is Verdict.REWRITTEN:
            offenders.append(f"{where}: REWRITTEN without a hook")
        elif not (entry.justification or "").strip():
            offenders.append(f"{where}: PASSTHROUGH without a written no-egress justification")
        elif route.provider_calls:
            offenders.append(f"{where}: justified, but its ast shows {route.provider_calls}")
    assert offenders == [], "\n".join(offenders)


# ── (d) websockets are never in the table ───────────────────────────────────


def test_no_websocket_route_is_listed(collection: Collection) -> None:
    websockets = {(route.path) for route in collection.routes if route.method == "WEBSOCKET"}
    assert websockets, "the collector found no websocket route; it used to find eight"
    listed = {path for _, path, _ in _table_rows()} & websockets
    # A websocket path may also exist as an HTTP route (POST /v1/responses does);
    # what matters is that a websocket scope is refused before any lookup.
    for path in sorted(websockets):
        decision = classify("GET", path, path.encode(), scope_type="websocket")
        assert decision.verdict is Verdict.REFUSE
        assert decision.block_reason == ROUTE_GATE_WEBSOCKET
    assert listed <= {route.path for route in _http(collection)}


# ── (e) no dead table entry ─────────────────────────────────────────────────


def test_no_table_entry_is_dead(collection: Collection) -> None:
    keys = collection.route_keys()
    dead = sorted(
        f"{method} {path}" for method, path, _ in _table_rows() if (method, path) not in keys
    )
    assert dead == [], (
        "the table classifies routes litellm no longer registers; regenerate it:\n"
        + "\n".join(dead)
    )


# ── (f) the collector sees lazy and imperative registrations ────────────────


def test_the_collector_sees_a_lazy_router(collection: Collection) -> None:
    routes = collection.by_key("POST", "/v1/messages")
    assert [route.module for route in routes] == ["proxy/anthropic_endpoints/endpoints.py"]


def test_the_collector_sees_an_imperative_registration(collection: Collection) -> None:
    routes = collection.by_key("POST", "/v1/unified_access_group")
    assert routes and all(route.registration_kind == "add_api_route" for route in routes)


# ── (g) the token counters still bypass the hook ────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages/count_tokens",
        "/v1/responses/input_tokens",
        "/openai/v1/responses/input_tokens",
    ],
)
def test_the_token_counters_still_bypass_the_hook(collection: Collection, path: str) -> None:
    routes = collection.by_key("POST", path)
    assert routes, f"POST {path} is gone from litellm; re-read the refusal"
    for route in routes:
        assert not route.reaches_hook, (
            f"POST {path} now reaches the hook; the REFUSE can be reconsidered"
        )
    assert lookup("POST", path).verdict is Verdict.REFUSE  # type: ignore[union-attr]


# ── synthetic drift ─────────────────────────────────────────────────────────


DRIFT_MODULE = """
from fastapi import APIRouter

router = APIRouter()
X = "thing"


@router.post("/model/new_thing")
async def new_thing():
    return {}


@router.post("/v1/messages/extra")
async def messages_extra():
    return {}


@router.post(f"/computed/{X}")
async def computed():
    return {}
"""


@pytest.fixture(scope="module")
def drifted(tmp_path_factory: pytest.TempPathFactory) -> Collection:
    root = _litellm_root()
    fake = tmp_path_factory.mktemp("drift") / "litellm"
    fake.mkdir()
    shutil.copytree(
        root / "proxy", fake / "proxy", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    # The MCP route constant lives outside proxy/; the collector resolves it
    # through one import hop, so the copy needs it too.
    shutil.copy(root / "constants.py", fake / "constants.py")
    (fake / "proxy" / "drift_endpoints.py").write_text(DRIFT_MODULE)
    return collect(fake)


@pytest.mark.parametrize("path", ["/model/new_thing", "/v1/messages/extra"])
def test_drift_fails_the_listed_check(drifted: Collection, path: str) -> None:
    # The same check the real guard runs — an admin tree and an admitted one.
    assert ("POST", path) in drifted.route_keys()
    assert any(line.startswith(f"POST {path} ") for line in _unlisted(drifted))


def test_drift_adds_an_unknown_dynamic_registration(drifted: Collection) -> None:
    unknown = unknown_dynamic_sites(drifted)
    assert [site.module for site in unknown] == ["proxy/drift_endpoints.py"]
    assert all(site.key not in KNOWN_DYNAMIC_REGISTRATIONS for site in unknown)


def _route(method: str, path: str, *, hook: bool, order: tuple[int, ...]) -> Route:
    return Route(
        method=method,
        path=path,
        module="proxy/fake.py",
        handler="fake",
        reaches_hook=hook,
        registration_kind="decorator",
        order=order,
    )


def test_the_include_order_check_fires_when_a_raw_passthrough_is_included_first() -> None:
    # The real guard above is satisfied by litellm 1.101.0's include order, so
    # this fabricates the reorder it exists to catch: same paths, passthrough
    # first.
    shadowed = Collection(
        routes=frozenset(
            {
                _route("POST", "/openai/{endpoint:path}", hook=False, order=(10, 1)),
                _route("POST", "/openai/v1/responses", hook=True, order=(20, 1)),
            }
        ),
        dynamic_sites=(),
        mounts=(),
        unresolved_includes=(),
    )
    rows = [("POST", "/openai/v1/responses")]

    assert len(outranked_rewritten_rows(shadowed, rows)) == 1
    # Flip the two include lines back and the same collection is clean.
    ordered = Collection(
        routes=frozenset(
            {
                _route("POST", "/openai/{endpoint:path}", hook=False, order=(20, 1)),
                _route("POST", "/openai/v1/responses", hook=True, order=(10, 1)),
            }
        ),
        dynamic_sites=(),
        mounts=(),
        unresolved_includes=(),
    )
    assert outranked_rewritten_rows(ordered, rows) == []


def test_drift_does_not_move_the_known_dynamic_registrations(drifted: Collection) -> None:
    # The copy is byte-identical apart from the appended module, so every
    # pinned (file, line) must still resolve — otherwise the keys are fragile.
    assert missing_dynamic_sites(drifted) == []
