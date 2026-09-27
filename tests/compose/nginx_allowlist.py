"""The gateway allow-list as data, for every test that checks it against a route
table: the static guards (litellm's source via ``tests/route_gate/litellm_routes.py``,
and the gateway's own ``route_gate`` table) and the image dump in
``tests/integration/test_nginx_allowlist_image.py``.

The unit is a (method, path) pair. A pair is **declared** by its location's
``limit_except``; nginx also lets ``HEAD`` through wherever it admits ``GET``, so
the pairs that **pass** nginx are the declared ones plus that ``HEAD``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

import pytest

from corp_llm_gateway.route_gate import GATEWAY_ROUTE_TABLE
from tests.compose.nginx_support import NGINX_DIR

GATEWAY_SNIPPET = NGINX_DIR / "templates" / "snippets" / "gateway-locations.inc.template"

WEBSOCKET = "WEBSOCKET"

# The one named location the gateway snippet may carry: the edge's 429 (its
# body, Retry-After, and HSTS when the connection is TLS).
RATE_LIMITED = "@rate_limited"
# The only placeholders the gateway snippet may carry: the edge limits, which
# the entrypoint renders as bare positive integers (never a path or a method).
SNIPPET_PLACEHOLDERS = frozenset({"NGINX_TOKEN_BURST", "NGINX_TOKEN_CONN"})
LIMIT_DIRECTIVES = frozenset({"limit_req", "limit_conn"})

# Every (method, path) litellm registers at an admitted path that nginx does not
# admit, with what blocks it. Produced by the collector against litellm 1.101.0
# — what that release shows, not a closed set: a bump that adds a pair at an
# admitted path fails the guards until someone puts it on one of the two lists.
BLOCKED_AT_NGINX: dict[tuple[str, str], str] = {
    (WEBSOCKET, "/v1/responses"): (
        "the handshake is a GET, which `limit_except POST` refuses; `Upgrade` and "
        "`Connection` are cleared, so no upgrade is ever forwarded"
    ),
}


# --------------------------------------------------------------------------- #
# a small nginx parser: nested blocks, quoted strings, comments
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Directive:
    name: str
    args: tuple[str, ...]
    block: tuple[Directive, ...] | None = None

    def __str__(self) -> str:
        head = " ".join((self.name, *self.args))
        if self.block is None:
            return head
        return f"{head} {{ {'; '.join(str(child) for child in self.block)} }}"


# Whitespace, a comment, a quoted string, a brace or semicolon, or a word (an
# envsubst `${NAME}` inside a word is not a block).
_TOKEN = re.compile(
    r"\s+|#[^\n]*"
    r"""|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
    r"|[{};]"
    r"""|(?:\$\{[^}]*\}|[^\s{};"'#])+"""
)


def _tokens(text: str) -> Iterator[str]:
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match, f"cannot tokenize at {text[position : position + 40]!r}"
        position = match.end()
        token = match.group(0)
        if not token.isspace() and not token.startswith("#"):
            yield token


def parse(text: str) -> tuple[Directive, ...]:
    """Every top-level directive of ``text``, blocks nested."""
    stack: list[list[Directive]] = [[]]
    heads: list[list[str]] = []
    words: list[str] = []
    for token in _tokens(text):
        if token == ";":
            assert words, "an empty directive"
            stack[-1].append(Directive(words[0], tuple(words[1:])))
            words = []
        elif token == "{":
            assert words, "a block with no directive name"
            heads.append(words)
            stack.append([])
            words = []
        elif token == "}":
            assert not words, f"unterminated directive {' '.join(words)!r}"
            assert heads, "an unbalanced }"
            head, children = heads.pop(), stack.pop()
            stack[-1].append(Directive(head[0], tuple(head[1:]), tuple(children)))
        else:
            words.append(token)
    assert not words and not heads, "unterminated input"
    return tuple(stack[0])


# --------------------------------------------------------------------------- #
# the snippet
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Location:
    modifier: str
    path: str
    directives: tuple[Directive, ...]

    @property
    def key(self) -> str:
        return f"{self.modifier} {self.path}".strip()

    def find(self, name: str) -> list[Directive]:
        return [d for d in self.directives if d.name == name]

    @property
    def methods(self) -> tuple[str, ...]:
        limits = self.find("limit_except")
        if len(limits) != 1:
            raise AssertionError(f"{self.key}: expected one limit_except, found {len(limits)}")
        return limits[0].args


@dataclass(frozen=True)
class Snippet:
    server: tuple[Directive, ...]
    locations: tuple[Location, ...]

    def exact(self) -> list[Location]:
        return [location for location in self.locations if location.modifier == "="]

    def named(self) -> list[Location]:
        return [location for location in self.locations if location.path.startswith("@")]


def parse_snippet(text: str) -> Snippet:
    server: list[Directive] = []
    locations: list[Location] = []
    for directive in parse(text):
        if directive.name != "location":
            assert directive.block is None, f"unexpected block {directive}"
            server.append(directive)
            continue
        assert directive.block is not None, directive
        *modifier, path = directive.args
        assert len(modifier) <= 1, directive
        if path.startswith("@"):
            # A named location is reachable only by an internal redirect; one
            # the tests do not know is a route nobody reviewed.
            assert not modifier and path == RATE_LIMITED, f"unexpected named location {directive}"
        locations.append(Location("".join(modifier), path, directive.block))
    return Snippet(tuple(server), tuple(locations))


def gateway_snippet() -> Snippet:
    text = GATEWAY_SNIPPET.read_text()
    # The rendered snippet is the template with the edge limits filled in.
    assert set(re.findall(r"\$\{([^}]*)\}", text)) == SNIPPET_PLACEHOLDERS
    return parse_snippet(text)


def limits_off_the_allow_list(snippet: Snippet, admitted: Iterable[tuple[str, str]]) -> list[str]:
    """Every ``limit_req`` / ``limit_conn`` that is not a direct child of an exact
    location for an admitted pair: at server level, on the catch-all, on a named
    location, inside a nested block, or on a location nginx should not have. A
    limit there says one thing and does another."""
    admitted_paths = {path for _, path in admitted}
    found = [f"server: {d}" for d in snippet.server if d.name in LIMIT_DIRECTIVES]
    for location in snippet.locations:
        on_allow_list = location.modifier == "=" and location.path in admitted_paths
        for directive in location.directives:
            if directive.name in LIMIT_DIRECTIVES and not on_allow_list:
                found.append(f"{location.key}: {directive}")
            nested = [d for d in directive.block or () if d.name in LIMIT_DIRECTIVES]
            found += [f"{location.key}: {directive.name} {{ {d} }}" for d in nested]
    return found


def declared_pairs(snippet: Snippet) -> set[tuple[str, str]]:
    return {(method, location.path) for location in snippet.exact() for method in location.methods}


def passing_pairs(declared: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """What passes nginx: ``limit_except GET`` always admits ``HEAD`` too."""
    declared = set(declared)
    return declared | {("HEAD", path) for method, path in declared if method == "GET"}


def gateway_owned(declared: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    return {pair for pair in declared if pair in GATEWAY_ROUTE_TABLE}


# --------------------------------------------------------------------------- #
# the two properties, against any route table
# --------------------------------------------------------------------------- #


def missing_from_litellm(
    declared: set[tuple[str, str]], registered: set[tuple[str, str]]
) -> list[str]:
    """Admitted pairs that are not gateway-owned and litellm does not register."""
    owned = gateway_owned(declared)
    return sorted(f"{m} {p}" for m, p in declared - owned if (m, p) not in registered)


def gateway_paths_litellm_registers(
    declared: set[tuple[str, str]], registered: set[tuple[str, str]]
) -> list[str]:
    owned_paths = {path for _, path in gateway_owned(declared)}
    return sorted(f"{m} {p}" for m, p in registered if p in owned_paths)


def unaccounted_at_admitted_paths(
    declared: set[tuple[str, str]], registered: set[tuple[str, str]]
) -> list[str]:
    """litellm pairs at an admitted path that neither pass nginx nor are listed
    in ``BLOCKED_AT_NGINX``."""
    passing = passing_pairs(declared)
    paths = {path for _, path in declared}
    return sorted(
        f"{m} {p}"
        for m, p in registered
        if p in paths and (m, p) not in passing and (m, p) not in BLOCKED_AT_NGINX
    )


def stale_blocked_entries(
    declared: set[tuple[str, str]], registered: set[tuple[str, str]]
) -> list[str]:
    """``BLOCKED_AT_NGINX`` entries litellm no longer registers, or that nginx admits."""
    passing = passing_pairs(declared)
    return sorted(
        f"{m} {p}" for m, p in BLOCKED_AT_NGINX if (m, p) not in registered or (m, p) in passing
    )


def installed_litellm_root() -> Path:
    """litellm's package root. Skips only when litellm is absent; installed
    without its ``proxy/`` source is a failure, never a skip."""
    spec = find_spec("litellm")
    if spec is None:
        pytest.skip("litellm is not installed in this interpreter")
    assert spec.origin is not None
    root = Path(spec.origin).parent
    if not (root / "proxy").is_dir():
        pytest.fail(f"litellm is installed at {root} but has no proxy/ source to read")
    return root
