"""Read litellm's installed proxy source with ``ast`` and report every route it
registers, so the route table can be regenerated and re-checked on every bump.

Source, not ``app.routes``: 33 of litellm's routers are imported lazily on the
first request that matches their prefix (``proxy/_lazy_features.py``), and
importing litellm's app needs a dependency set neither venv has. The parser
therefore resolves what the interpreter would: the router a module defines, the
prefix it carries, the prefix of the ``include_router`` site, and the module
constant a path may be spelled with.

A registration whose path this cannot resolve to a literal is reported as a
``DynamicSite``; ``KNOWN_DYNAMIC_REGISTRATIONS`` pins the ones that exist in the
pinned release, keyed by ``(file, line)``, so a bump that adds or moves one
fails the guard until someone classifies it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Decorator names that register a route. ``api_route`` and ``add_api_route``
# carry their verbs in ``methods=``; ``websocket`` has no HTTP verb.
_VERB_DECORATORS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)
_ROUTE_DECORATORS: frozenset[str] = _VERB_DECORATORS | {"api_route", "websocket"}
_ROUTE_CALLS: frozenset[str] = frozenset({"add_api_route", "add_websocket_route"})

WEBSOCKET = "WEBSOCKET"

# The hook is reached through litellm's shared request processor; a handler that
# calls neither of these never runs CorpLlmGuardrail.async_pre_call_hook.
_HOOK_MARKERS: frozenset[str] = frozenset(
    {"base_process_llm_request", "pre_call_hook", "async_pre_call_hook"}
)

# Calls that can put a request body on the wire to a provider. `router.a*` is
# litellm's Router (acompletion / aresponses / aembedding / …) — matched by
# shape, not by name, so a new async Router method is caught too.
_PROVIDER_CALLS: frozenset[str] = frozenset(
    {
        "acompletion",
        "atext_completion",
        "aresponses",
        "aembedding",
        "amoderation",
        "aspeech",
        "atranscription",
        "arerank",
        "aimage_generation",
        "anthropic_messages",
        "token_counter",
        "create_pass_through_route",
        "pass_through_request",
        "llm_passthrough_factory_proxy_route",
    }
)
_ROUTER_OBJECTS: frozenset[str] = frozenset({"router", "llm_router", "litellm_router"})

# The keyword whose literal names litellm's own call type for the request.
_CALL_TYPE_KEYWORDS: frozenset[str] = frozenset({"route_type", "call_type"})

# Registration sites whose path is not a literal, keyed by (file, line), with
# the policy that stands for each. The guard fails on any unresolved site that
# is not listed here, and on any listed site that has disappeared — either way
# a litellm bump gets reviewed before its routes can be served.
#
# Nine, not the eleven the plan predicted: `@app.api_route(BASE_MCP_ROUTE, …)`
# (proxy_server.py:18488) and `app.mount(METRICS_PATH, …)`
# (prometheus_metrics_server.py:78) both spell their path with a module
# constant, and constant resolution reads them.
KNOWN_DYNAMIC_REGISTRATIONS: dict[tuple[str, int], str] = {
    ("proxy/proxy_server.py", 2053): (
        "static UI asset mount under a configured asset prefix; serves files, no "
        "user text and no provider call. Unresolved -> refused by default-deny."
    ),
    ("proxy/_lazy_features.py", 33): (
        "_mount_app(prefix): mounts a lazy feature's own ASGI app. The only "
        "caller is the mcp_app feature at /mcp (LAZY_FEATURES), and the MCP "
        "tree is REFUSE, so the unresolved prefix costs nothing."
    ),
    ("proxy/pass_through_endpoints/pass_through_endpoints.py", 2781): (
        "SafeRouteAdder: paths come from the `pass_through_endpoints` block of "
        "the litellm config. This deployment configures none, default-deny "
        "refuses whatever such a route would add, and `config check` rejects a "
        "config that sets the block."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2482): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2494): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2513): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2533): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2602): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
    ("proxy/_experimental/mcp_server/discoverable_endpoints.py", 2617): (
        "MCP OAuth discovery path built from an f-string; the MCP tree is REFUSE."
    ),
}


# Lazy routers are included on the first request that matches their prefix, so
# their routes land after every eagerly included one however early their
# `include_router` line sits.
LAZY_ORDER = 10**9


@dataclass(frozen=True, order=True)
class Route:
    """One registered route. ``module`` is the path relative to litellm's
    package root, so a guard failure names the file to read.

    ``order`` is the chain of ``include_router`` line numbers from the app down
    to this route's own registration line. Starlette matches in registration
    order, so comparing two routes' ``order`` says which one answers a path both
    patterns match."""

    method: str
    path: str
    module: str
    handler: str
    reaches_hook: bool
    registration_kind: str
    call_type: str | None = None
    provider_calls: tuple[str, ...] = ()
    order: tuple[int, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


@dataclass(frozen=True, order=True)
class DynamicSite:
    """A registration whose path the parser could not resolve to a literal."""

    module: str
    line: int
    kind: str
    source: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.module, self.line)


@dataclass(frozen=True, order=True)
class Mount:
    """An ASGI sub-app mounted at a prefix. Its inner routes are not visible to
    ``ast``; everything under the prefix is refused by default-deny."""

    module: str
    line: int
    path: str | None


@dataclass(frozen=True)
class Collection:
    routes: frozenset[Route]
    dynamic_sites: tuple[DynamicSite, ...]
    mounts: tuple[Mount, ...]
    unresolved_includes: tuple[DynamicSite, ...]

    def route_keys(self) -> set[tuple[str, str]]:
        return {route.key for route in self.routes}

    def by_key(self, method: str, path: str) -> list[Route]:
        return sorted(r for r in self.routes if r.method == method and r.path == path)


def collect(litellm_root: Path) -> Collection:
    """Parse ``<litellm_root>/proxy/**/*.py`` and return every route it registers."""
    universe = _Universe(litellm_root)
    modules = [universe.load(path) for path in sorted(_iter_sources(litellm_root / "proxy"))]

    prefixes = _resolve_prefixes(universe, modules)

    routes: set[Route] = set()
    dynamic: list[DynamicSite] = []
    mounts: list[Mount] = []
    unresolved_includes: list[DynamicSite] = []

    for module in modules:
        dynamic.extend(module.dynamic_sites)
        mounts.extend(module.mounts)
        unresolved_includes.extend(module.unresolved_includes)
        for raw in module.raw_routes:
            own = module.routers.get(raw.router, "")
            for outer, chain in sorted(prefixes.get((module.dotted, raw.router), {("", ())})):
                for method in raw.methods:
                    routes.add(
                        Route(
                            method=method,
                            path=outer + own + raw.path,
                            module=module.rel,
                            handler=raw.handler,
                            reaches_hook=module.reaches_hook(raw.handler),
                            registration_kind=raw.kind,
                            call_type=module.call_type(raw.handler),
                            provider_calls=module.provider_calls(raw.handler),
                            order=(*chain, raw.line),
                        )
                    )

    return Collection(
        routes=frozenset(_with_head(routes)),
        dynamic_sites=tuple(sorted(dynamic)),
        mounts=tuple(sorted(mounts)),
        unresolved_includes=tuple(sorted(unresolved_includes)),
    )


def unknown_dynamic_sites(collection: Collection) -> list[DynamicSite]:
    return [
        site for site in collection.dynamic_sites if site.key not in KNOWN_DYNAMIC_REGISTRATIONS
    ]


def missing_dynamic_sites(collection: Collection) -> list[tuple[str, int]]:
    found = {site.key for site in collection.dynamic_sites}
    return sorted(key for key in KNOWN_DYNAMIC_REGISTRATIONS if key not in found)


def _with_head(routes: Iterable[Route]) -> Iterator[Route]:
    # Starlette registers HEAD for every route that declares GET.
    for route in routes:
        yield route
        if route.method == "GET":
            yield Route(
                method="HEAD",
                path=route.path,
                module=route.module,
                handler=route.handler,
                reaches_hook=route.reaches_hook,
                registration_kind=route.registration_kind,
                call_type=route.call_type,
                provider_calls=route.provider_calls,
                order=route.order,
            )


def _iter_sources(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


@dataclass(frozen=True)
class _RawRoute:
    router: str
    methods: tuple[str, ...]
    path: str
    handler: str
    line: int
    kind: str


@dataclass(frozen=True)
class _Include:
    host: str
    target_module: str
    target_name: str
    prefix: str
    line: int


class _Universe:
    """Parsed modules, keyed by dotted name. Modules outside ``proxy/`` are
    parsed on demand — the one import hop a constant may take."""

    def __init__(self, litellm_root: Path) -> None:
        self.root = litellm_root
        self.package = litellm_root.name
        self._by_dotted: dict[str, _Module] = {}

    def load(self, path: Path) -> _Module:
        dotted = self._dotted(path)
        module = self._by_dotted.get(dotted)
        if module is None:
            module = _Module(path, dotted, path.relative_to(self.root).as_posix(), self)
            self._by_dotted[dotted] = module
        return module

    def get(self, dotted: str) -> _Module | None:
        if dotted in self._by_dotted:
            return self._by_dotted[dotted]
        parts = dotted.split(".")
        if not parts or parts[0] != self.package:
            return None
        base = self.root.joinpath(*parts[1:])
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return self.load(candidate)
        return None

    def _dotted(self, path: Path) -> str:
        rel = path.relative_to(self.root)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][: -len(".py")]
        return ".".join([self.package, *parts])


class _Module:
    def __init__(self, path: Path, dotted: str, rel: str, universe: _Universe) -> None:
        self.path = path
        self.dotted = dotted
        self.rel = rel
        self._universe = universe
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.source = path.read_text(encoding="utf-8")

        self.constants: dict[str, str] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        self.routers: dict[str, str] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.raw_routes: list[_RawRoute] = []
        self.includes: list[_Include] = []
        self.mounts: list[Mount] = []
        self.dynamic_sites: list[DynamicSite] = []
        self.unresolved_includes: list[DynamicSite] = []

        self._scan_names()
        self._scan_registrations()

    # ── names ───────────────────────────────────────────────────────────────

    def _scan_names(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                self._bind(node.targets, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._bind([node.target], node.value)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)
        # Module level only: an `ast.walk` would index a nested def or a class
        # method under its bare name, and the first one found would answer for
        # the module-level handler of the same name. `GET /get/config/callbacks`
        # was read against `ProxyConfig.get_config` that way.
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions[node.name] = node

    def _bind(self, targets: list[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.constants.setdefault(target.id, value.value)
            elif _is_api_router(value):
                self.routers[target.id] = self._router_prefix(value)

    def _router_prefix(self, call: ast.Call) -> str:
        for keyword in call.keywords:
            if keyword.arg == "prefix":
                return self.resolve_str(keyword.value) or ""
        return ""

    def resolve_str(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.constants:
                return self.constants[node.id]
            target = self.imports.get(node.id)
            if target is not None:
                other = self._universe.get(target[0])
                if other is not None and other is not self:
                    return other.constants.get(target[1])
        return None

    # ── registrations ───────────────────────────────────────────────────────

    def _is_router_object(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and (node.id in self.routers or node.id == "app")

    def _scan_registrations(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for decorator in node.decorator_list:
                    self._scan_decorator(decorator, node.name)
            elif isinstance(node, ast.Call):
                self._scan_call(node)

    def _scan_decorator(self, decorator: ast.expr, handler: str) -> None:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            return
        attr = decorator.func.attr
        if attr not in _ROUTE_DECORATORS or not self._is_router_object(decorator.func.value):
            return
        router = decorator.func.value.id  # type: ignore[union-attr]
        paths = self._paths(decorator, "decorator", attr)
        if not paths:
            return
        methods = self._methods(decorator, "decorator", attr)
        for path in paths:
            self.raw_routes.append(
                _RawRoute(router, methods, path, handler, decorator.lineno, "decorator")
            )

    def _scan_call(self, call: ast.Call) -> None:
        if not isinstance(call.func, ast.Attribute):
            return
        attr = call.func.attr
        if attr == "mount" and self._is_router_object(call.func.value):
            path = self._argument(call, 0, "path")
            resolved = self.resolve_str(path) if path is not None else None
            self.mounts.append(Mount(self.rel, call.lineno, resolved))
            if resolved is None:
                self.dynamic_sites.append(
                    DynamicSite(self.rel, call.lineno, "mount", self._segment(call))
                )
            return
        if attr == "include_router" and self._is_router_object(call.func.value):
            self._scan_include(call)
            return
        if attr not in _ROUTE_CALLS or not self._is_router_object(call.func.value):
            return
        router = call.func.value.id  # type: ignore[union-attr]
        kind = "add_websocket_route" if attr == "add_websocket_route" else "add_api_route"
        paths = self._paths(call, kind, attr)
        if not paths:
            return
        handler = self._endpoint_name(call)
        methods = self._methods(call, kind, attr)
        for path in paths:
            self.raw_routes.append(_RawRoute(router, methods, path, handler, call.lineno, kind))

    def _scan_include(self, call: ast.Call) -> None:
        host = call.func.value.id  # type: ignore[union-attr]
        prefix_node = next((k.value for k in call.keywords if k.arg == "prefix"), None)
        prefix = self.resolve_str(prefix_node) if prefix_node is not None else ""
        target = call.args[0] if call.args else None
        if isinstance(target, ast.Name) and target.id in self.routers:
            self.includes.append(_Include(host, self.dotted, target.id, prefix or "", call.lineno))
            return
        if isinstance(target, ast.Name) and target.id in self.imports:
            module, name = self.imports[target.id]
            self.includes.append(_Include(host, module, name, prefix or "", call.lineno))
            return
        # An include whose router this cannot name only loses a prefix — unless
        # the site sets one, in which case the paths would be wrong.
        if prefix_node is not None or prefix:
            self.unresolved_includes.append(
                DynamicSite(self.rel, call.lineno, "include_router", self._segment(call))
            )

    def _paths(self, call: ast.Call, kind: str, attr: str) -> list[str]:
        node = self._argument(call, 0, "path")
        if node is None:
            return []
        if isinstance(node, ast.List | ast.Tuple):
            items = [self.resolve_str(element) for element in node.elts]
        else:
            items = [self.resolve_str(node)]
        if any(item is None for item in items):
            self.dynamic_sites.append(
                DynamicSite(self.rel, call.lineno, f"{kind}:{attr}", self._segment(call))
            )
        return [item for item in items if item is not None]

    def _methods(self, call: ast.Call, kind: str, attr: str) -> tuple[str, ...]:
        if attr == "websocket" or attr == "add_websocket_route":
            return (WEBSOCKET,)
        if attr in _VERB_DECORATORS:
            return (attr.upper(),)
        node = next((k.value for k in call.keywords if k.arg == "methods"), None)
        if node is None:
            # api_route without methods= is GET, as FastAPI documents.
            return ("GET",)
        verbs = (
            [self.resolve_str(element) for element in node.elts]
            if isinstance(node, ast.List | ast.Tuple)
            else [None]
        )
        if any(verb is None for verb in verbs):
            # A computed methods= would otherwise fall through to GET, and the
            # POST spelling of the same path would be served and never listed.
            self.dynamic_sites.append(
                DynamicSite(self.rel, call.lineno, f"{kind}:{attr}:methods", self._segment(call))
            )
            return ()
        return tuple(sorted(verb.upper() for verb in verbs if verb))

    def _endpoint_name(self, call: ast.Call) -> str:
        node = self._argument(call, 1, "endpoint")
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return "<unknown>"

    @staticmethod
    def _argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
        if len(call.args) > index:
            return call.args[index]
        return next((k.value for k in call.keywords if k.arg == name), None)

    def _segment(self, node: ast.AST) -> str:
        return " ".join((ast.get_source_segment(self.source, node) or "").split())[:120]

    # ── handler analysis ────────────────────────────────────────────────────

    @cache  # noqa: B019 — per-instance cache; a _Module lives as long as the parse
    def _names_called(self, handler: str) -> frozenset[str]:
        function = self.functions.get(handler)
        if function is None:
            return frozenset()
        names = _called_names(function)
        # One level deep: a handler that delegates to a same-module helper.
        for name in sorted(names):
            helper = self.functions.get(name)
            if helper is not None and helper is not function:
                names |= _called_names(helper)
        return frozenset(names)

    def reaches_hook(self, handler: str) -> bool:
        return bool(self._names_called(handler) & _HOOK_MARKERS)

    def provider_calls(self, handler: str) -> tuple[str, ...]:
        called = self._names_called(handler)
        hits = {name for name in called if name in _PROVIDER_CALLS}
        hits |= {
            name
            for name in called
            if "." in name
            and name.split(".", 1)[0] in _ROUTER_OBJECTS
            and name.split(".", 1)[1].startswith("a")
        }
        return tuple(sorted(hits))

    def call_type(self, handler: str) -> str | None:
        function = self.functions.get(handler)
        if function is None:
            return None
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg in _CALL_TYPE_KEYWORDS:
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        return value.value
        return None


def _called_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    # `function.body`, not the whole node: `ast.walk` would also read the
    # decorator calls, whose `Depends(...)` arguments are not what the handler
    # does when it runs.
    for statement in function.body:
        for node in ast.walk(statement):
            names |= _call_name(node)
    return names


def _call_name(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Call):
        return set()
    func = node.func
    if isinstance(func, ast.Name):
        return {func.id}
    if isinstance(func, ast.Attribute):
        names = {func.attr}
        if isinstance(func.value, ast.Name):
            names.add(f"{func.value.id}.{func.attr}")
        return names
    return set()


def _is_api_router(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "APIRouter"
    return isinstance(func, ast.Attribute) and func.attr == "APIRouter"


# ── include graph ───────────────────────────────────────────────────────────


_Node = tuple[str, str, str, tuple[int, ...]]


def _resolve_prefixes(
    universe: _Universe, modules: list[_Module]
) -> dict[tuple[str, str], set[tuple[str, tuple[int, ...]]]]:
    """Accumulated ``(prefix, include-line chain)`` per ``(module, router variable)``.

    FastAPI bakes a router's own ``prefix=`` into its routes at decoration
    time and prepends the ``include_router(prefix=…)`` of every site above it,
    so a node's value is what sits in front of ``own prefix + decorated path``.
    The chain records the line of every ``include_router`` on the way down,
    because Starlette matches routes in registration order and the gate needs to
    know which of two matching patterns litellm reaches first.
    """
    roots: list[_Node] = []
    server = universe.get(f"{universe.package}.proxy.proxy_server")
    if server is not None:
        roots.append((server.dotted, "app", "", ()))
    roots.extend(_lazy_roots(universe))

    prefixes: dict[tuple[str, str], set[tuple[str, tuple[int, ...]]]] = {}
    by_dotted = {module.dotted: module for module in modules}
    queue = list(roots)
    seen: set[_Node] = set()
    while queue:
        dotted, var, accum, chain = queue.pop()
        if (dotted, var, accum, chain) in seen:
            continue
        seen.add((dotted, var, accum, chain))
        module = by_dotted.get(dotted) or universe.get(dotted)
        if module is None:
            continue
        own = module.routers.get(var, "")
        prefixes.setdefault((dotted, var), set()).add((accum, chain))
        for include in module.includes:
            if include.host != var:
                continue
            target = universe.get(include.target_module)
            if target is None:
                continue
            name = include.target_name
            if name not in target.routers:
                # `from x import router as y`: the alias is the name in x.
                name = "router" if "router" in target.routers else name
            queue.append(
                (target.dotted, name, accum + own + include.prefix, (*chain, include.line))
            )
    return prefixes


def _lazy_roots(universe: _Universe) -> list[_Node]:
    """``LAZY_FEATURES`` entries: module + router attribute, mounted at no
    prefix (``_include_router``) or as their own ASGI app (``_mount_app``)."""
    lazy = universe.get(f"{universe.package}.proxy._lazy_features")
    if lazy is None:
        return []
    roots: list[_Node] = []
    for node in ast.walk(lazy.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "LazyFeature":
            continue
        keywords = {k.arg: k.value for k in node.keywords}
        module_path = (
            lazy.resolve_str(keywords["module_path"]) if "module_path" in keywords else None
        )
        if module_path is None:
            continue
        attr = "router"
        register = keywords.get("register_fn")
        if isinstance(register, ast.Call) and isinstance(register.func, ast.Name):
            if register.func.id == "_mount_app":
                continue  # a mounted sub-app, not a router
            if register.func.id == "_include_router":
                named = _Module._argument(register, 0, "attr_name")
                attr = (lazy.resolve_str(named) if named is not None else None) or "router"
        roots.append((module_path, attr, "", (LAZY_ORDER, node.lineno)))
    return roots
