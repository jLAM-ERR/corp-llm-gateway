"""The suite's AST index, read by the permanent gates (docs/testing/must-keep.md).

``modules`` / ``suite_modules`` / ``suite_files`` / ``_tests_in`` list the test tree for
must-keep, name-pinned and moves; ``build`` gives, per test function (keyed by its baseline
id, ``moves.to_baseline``), the module-level helpers it reaches through its body, fixtures,
marks and class members, which negative-logs reads to find the tests that run a security
check through a helper. A function-local, nested-scope or ``global`` import is followed
like a module-level one; an unaliased ``import tests[.x]`` or an import under ``global`` /
``nonlocal`` is refused rather than missed.

The refactor-time check inventory and external-dependency manifest that used to live here
were retired in Task 10 (plan 20260926); ``docs/testing/deleted-tests.md`` is the record.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

from tests._gates import moves

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
RENAMES_PATH = TESTS / "_manifests" / "renames.json"

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# ── module index ────────────────────────────────────────────────────────────────


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    defs: dict[str, ast.AST] = field(default_factory=dict)
    consts: dict[str, ast.AST] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    @property
    def rel(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


def _top_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    for stmt in body:
        if isinstance(stmt, ast.If | ast.Try | ast.With):
            yield from _top_level(stmt.body)
            yield from _top_level(getattr(stmt, "orelse", []))
            for handler in getattr(stmt, "handlers", []):
                yield from _top_level(handler.body)
            yield from _top_level(getattr(stmt, "finalbody", []))
        else:
            yield stmt


def _targets(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Tuple | ast.List):
        for elt in node.elts:
            yield from _targets(elt)


def _package(name: str, path: Path) -> str:
    return name.rsplit(".", 1)[0] if path.name != "__init__.py" else name


class UnaliasedTestsImportError(moves.GateRefusalError):
    pass


class RebindingImportError(moves.GateRefusalError):
    pass


def _import_bindings(
    stmt: ast.Import | ast.ImportFrom, package: str, path: Path
) -> Iterator[tuple[str, tuple[str, str | None]]]:
    """The names ``stmt`` binds, each as ``(name, (source module, attribute or None))``."""
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.asname:
                yield alias.asname, (alias.name, None)
                continue
            if alias.name == "tests" or alias.name.startswith("tests."):
                where = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                raise UnaliasedTestsImportError(
                    f"{where}:{stmt.lineno}: an unaliased `import {alias.name}` binds only "
                    "`tests`, which the suite index cannot follow; write "
                    "`from tests.x import name` or `import tests.x as alias`"
                )
            top = alias.name.split(".", 1)[0]
            yield top, (top, None)
        return
    source = stmt.module or ""
    if stmt.level:
        base = package.rsplit(".", stmt.level - 1)[0] if stmt.level > 1 else package
        source = f"{base}.{source}" if source else base
    for alias in stmt.names:
        yield alias.asname or alias.name, (source, alias.name)


def _index(name: str, path: Path, text: str | None = None) -> Module:
    source = path.read_text() if text is None else text
    tree = ast.parse(source, filename=str(path))
    module = Module(name, path, tree)
    package = _package(name, path)
    for stmt in _top_level(tree.body):
        if isinstance(stmt, DEFS):
            module.defs[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for target_name in _targets(target):
                    module.consts[target_name] = stmt
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign) and isinstance(stmt.target, ast.Name):
            module.consts.setdefault(stmt.target.id, stmt)
        elif isinstance(stmt, ast.Import | ast.ImportFrom):
            for bound, binding in _import_bindings(stmt, package, path):
                _refuse_second_tests_binding(module, bound, binding, stmt)
                module.imports[bound] = binding
    _refuse_rebinding_imports(tree, package, path)
    return module


def _from_tests(binding: tuple[str, str | None]) -> bool:
    return binding[0] == "tests" or binding[0].startswith("tests.")


def _dotted(binding: tuple[str, str | None]) -> str:
    """``import tests.x as x`` and ``from tests import x`` bind the same object."""
    source, attr = binding
    return source if attr is None else f"{source}.{attr}"


def _refuse_second_tests_binding(
    module: Module, name: str, binding: tuple[str, str | None], stmt: ast.stmt
) -> None:
    """Module-level imports are indexed one binding per name; a second import of the same
    name (a try / except fallback) where either side is from tests would hide a helper.
    A star import binds no name the index follows."""
    first = module.imports.get(name)
    if name == "*" or first is None or _dotted(first) == _dotted(binding):
        return
    if not (_from_tests(first) or _from_tests(binding)):
        return
    path = module.path
    where = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    raise RebindingImportError(
        f"{where}:{stmt.lineno}: `{name}` is imported at module level a second time, and "
        "one of the two is from tests; the suite index follows one binding per module-level "
        "name, so import it once"
    )


def _refuse_rebinding_imports(tree: ast.Module, package: str, path: Path) -> None:
    """A scope that imports a ``tests`` name it declares ``global`` / ``nonlocal`` rebinds it
    for code the walker resolves elsewhere; refuse it rather than miss the helper."""
    for scope in ast.walk(tree):
        if not isinstance(scope, DEFS):
            continue
        globals_, nonlocals = _declarations(scope)
        if not globals_ | nonlocals:
            continue
        for name, bindings in _own_imports(scope, package, path).items():
            tests_bound = any(s == "tests" or s.startswith("tests.") for s, _ in bindings)
            if tests_bound and name in globals_ | nonlocals:
                where = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                kind = "global" if name in globals_ else "nonlocal"
                raise RebindingImportError(
                    f"{where}:{scope.lineno}: `{scope.name}` declares `{kind} {name}` and "
                    f"imports `{name}` from tests, rebinding it where the suite index "
                    "cannot follow; import it in the scope that uses it"
                )


def suite_files() -> list[Path]:
    return sorted(
        path
        for path in TESTS.rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith(".")
    )


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def modules() -> dict[str, Module]:
    return {_module_name(path): _index(_module_name(path), path) for path in suite_files()}


# ── name resolution ─────────────────────────────────────────────────────────────


def _resolve_name(module: Module, name: str, seen: frozenset[str] = frozenset()):
    """(kind, owner module, node, name) for a tests-local symbol, else None."""
    if name in module.defs:
        return ("def", module, module.defs[name], name)
    if name in module.consts:
        return ("const", module, module.consts[name], name)
    if name in module.imports:
        return _resolve_import(*module.imports[name], seen)
    return None


def _resolve_import(source: str, attr: str | None, seen: frozenset[str] = frozenset()):
    key = f"{source}:{attr}"
    if key in seen:
        return None
    mods = modules()
    if attr is None:
        if source in mods:
            return ("module", mods[source], None, source)
        return None
    if source in mods:
        found = _resolve_name(mods[source], attr, seen | {key})
        if found is not None:
            return found
    if f"{source}.{attr}" in mods:
        return ("module", mods[f"{source}.{attr}"], None, f"{source}.{attr}")
    return None


# Every import a scope binds a name with: which one is live depends on control flow, so
# all of them are followed.
ImportTable = dict[str, list[tuple[str, str | None]]]


class Scope(NamedTuple):
    is_class: bool
    imports: ImportTable
    globals: set[str]
    nonlocals: set[str]


# Innermost last.
ScopeChain = tuple[Scope, ...]


def _resolve_scoped(module: Module, chain: ScopeChain, name: str) -> list[Any]:
    """Like ``_resolve_name``, but an import of an enclosing scope shadows the module's own
    name; a class body's imports are visible in that body only, not in its methods. Fails
    closed on ``global`` / ``nonlocal``: a ``global`` declaration on the way to the matching
    scope adds the module-level target, and a ``nonlocal`` scope's imports are kept while
    the search goes on outward."""
    found: list[Any] = []
    global_seen = False
    for depth, scope in enumerate(reversed(chain)):
        if scope.is_class and depth:
            continue
        global_seen |= name in scope.globals
        if name in scope.imports:
            found += [t for b in scope.imports[name] if (t := _resolve_import(*b)) is not None]
            if name not in scope.nonlocals:
                break
    else:
        global_seen = True
    if global_seen:
        target = _resolve_name(module, name)
        if target is not None:
            found.append(target)
    return found


def _decorator_names(node: ast.AST) -> list[str]:
    names = []
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        names.append(ast.unparse(target))
    return names


def _is_fixture(node: ast.AST) -> bool:
    return any(name.split(".")[-1] == "fixture" for name in _decorator_names(node))


def _fixture_spec(node: ast.AST) -> tuple[str, bool]:
    name = node.name
    autouse = False
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith("fixture"):
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    name = keyword.value.value
                if keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant):
                    autouse = bool(keyword.value.value)
    return name, autouse


# Keyed by object identity; each value holds its module, so the id cannot be reused by a
# re-parsed module while the entry lives.
_FIXTURES: dict[int, tuple[Module, dict[str, tuple[Module, ast.AST]]]] = {}
_CHAINS: dict[int, tuple[Module, list[Module]]] = {}


def _module_fixtures(module: Module) -> dict[str, tuple[Module, ast.AST]]:
    cached = _FIXTURES.get(id(module))
    if cached is None or cached[0] is not module:
        cached = _FIXTURES[id(module)] = (module, _scan_fixtures(module))
    return cached[1]


def _scan_fixtures(module: Module) -> dict[str, tuple[Module, ast.AST]]:
    fixtures: dict[str, tuple[Module, ast.AST]] = {}
    for local in [*module.defs, *module.imports]:
        target = _resolve_name(module, local)
        is_def = target is not None and target[0] == "def" and isinstance(target[2], FUNCS)
        if is_def and _is_fixture(target[2]):
            fixtures[_fixture_spec(target[2])[0]] = (target[1], target[2])
    return fixtures


def _conftest_chain(module: Module) -> list[Module]:
    cached = _CHAINS.get(id(module))
    if cached is None or cached[0] is not module:
        cached = _CHAINS[id(module)] = (module, _scan_chain(module))
    return cached[1]


def _scan_chain(module: Module) -> list[Module]:
    mods = modules()
    chain = []
    directory = module.path.parent
    while True:
        name = _module_name(directory / "conftest.py")
        if name in mods and mods[name] is not module:
            chain.append(mods[name])
        if directory == TESTS:
            break
        directory = directory.parent
    return chain


def _parametrized_names(decorators: Iterable[ast.expr]) -> set[str]:
    names: set[str] = set()
    for decorator in decorators:
        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith(
            "mark.parametrize"
        ):
            if not decorator.args:
                continue
            first = decorator.args[0]
            indirect = any(
                k.arg == "indirect"
                and not (isinstance(k.value, ast.Constant) and not k.value.value)
                for k in decorator.keywords
            )
            if indirect:
                continue
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names |= {n.strip() for n in first.value.split(",") if n.strip()}
            elif isinstance(first, ast.Tuple | ast.List):
                names |= {e.value for e in first.elts if isinstance(e, ast.Constant)}
    return names


def _pytestmark(node_body: list[ast.stmt]) -> list[ast.expr]:
    for stmt in node_body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            value = stmt.value
            return list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
    return []


def _usefixtures(decorators: Iterable[ast.expr]) -> list[str]:
    names = []
    for decorator in decorators:
        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith(
            "mark.usefixtures"
        ):
            names += [a.value for a in decorator.args if isinstance(a, ast.Constant)]
    return names


# ── scopes ──────────────────────────────────────────────────────────────────────


@cache
def renames() -> dict[str, str]:
    """``renames.json`` ``{"names": {new: old}}``: approved renames of module-level helpers,
    so a renamed helper keeps its baseline name in a test's ``helpers``."""
    if RENAMES_PATH.exists():
        return json.loads(RENAMES_PATH.read_text()).get("names", {})
    return {}


COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
SCOPES = (*DEFS, ast.Lambda, *COMPREHENSIONS)


def _enclosing_parts(scope: ast.AST) -> list[ast.AST]:
    """The parts of a def, class, lambda or comprehension evaluated in the scope around it."""
    if isinstance(scope, COMPREHENSIONS):
        return [scope.generators[0].iter]
    if isinstance(scope, ast.ClassDef):
        return [
            *scope.decorator_list,
            *scope.bases,
            *(k.value for k in scope.keywords),
            *getattr(scope, "type_params", []),
        ]
    a = scope.args
    parts: list[ast.AST] = [*a.defaults, *filter(None, a.kw_defaults)]
    if isinstance(scope, FUNCS):
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None and arg.annotation is not None:
                parts.append(arg.annotation)
        parts += [*scope.decorator_list, *getattr(scope, "type_params", [])]
        if scope.returns is not None:
            parts.append(scope.returns)
    return parts


def _scope_parts(scope: ast.AST) -> list[ast.AST]:
    """The parts of a scope evaluated in that scope itself."""
    if isinstance(scope, COMPREHENSIONS):
        first, *rest = scope.generators
        parts: list[ast.AST] = [first.target, *first.ifs]
        for gen in rest:
            parts += [gen.target, gen.iter, *gen.ifs]
        return parts + [getattr(scope, f) for f in ("elt", "key", "value") if hasattr(scope, f)]
    return list(scope.body) if isinstance(scope.body, list) else [scope.body]


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node whose nearest enclosing scope is ``scope``: a nested scope is yielded
    itself, with its enclosing parts, but nothing inside it."""
    stack = _scope_parts(scope)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPES):
            stack += _enclosing_parts(node)
        else:
            stack += ast.iter_child_nodes(node)


def _declarations(scope: ast.AST) -> tuple[set[str], set[str]]:
    """(``global`` names, ``nonlocal`` names) ``scope`` declares itself."""
    globals_: set[str] = set()
    nonlocals: set[str] = set()
    if not isinstance(scope, COMPREHENSIONS):
        for node in _own_nodes(scope):
            if isinstance(node, ast.Global):
                globals_ |= set(node.names)
            elif isinstance(node, ast.Nonlocal):
                nonlocals |= set(node.names)
    return globals_, nonlocals


def _own_imports(scope: ast.AST, package: str, path: Path) -> ImportTable:
    imports: ImportTable = {}
    if isinstance(scope, DEFS):
        for node in _own_nodes(scope):
            if isinstance(node, ast.Import | ast.ImportFrom):
                for name, binding in _import_bindings(node, package, path):
                    imports.setdefault(name, []).append(binding)
    return imports


def _scoped_walk(module: Module, root: ast.AST) -> Iterator[tuple[ast.AST, ScopeChain]]:
    """Every node under ``root`` (itself included) with the import tables of the scopes it is
    evaluated in; ``root``'s enclosing parts are evaluated at module level."""
    package = _package(module.name, module.path)
    stack: list[tuple[ast.AST, ScopeChain]] = [(root, ())]
    while stack:
        node, chain = stack.pop()
        yield node, chain
        if isinstance(node, SCOPES):
            scope = Scope(
                isinstance(node, ast.ClassDef),
                _own_imports(node, package, module.path),
                *_declarations(node),
            )
            inner = (*chain, scope)
            stack += [(part, chain) for part in _enclosing_parts(node)]
            stack += [(part, inner) for part in _scope_parts(node)]
        else:
            stack += [(child, chain) for child in ast.iter_child_nodes(node)]


@dataclass
class Context:
    """Where a test's fixtures resolve: its module, class and conftest chain."""

    module: Module
    cls: ast.ClassDef | None

    def fixture(self, name: str) -> tuple[Module, ast.AST] | None:
        if self.cls is not None:
            for stmt in self.cls.body:
                if isinstance(stmt, FUNCS) and _is_fixture(stmt) and _fixture_spec(stmt)[0] == name:
                    return (self.module, stmt)
        local = _module_fixtures(self.module)
        if name in local:
            return local[name]
        for conftest in _conftest_chain(self.module):
            found = _module_fixtures(conftest)
            if name in found:
                return found[name]
        return None

    def autouse(self) -> list[tuple[Module, ast.AST]]:
        found = []
        scopes = [*reversed(_conftest_chain(self.module)), self.module]
        for scope in scopes:
            for owner, node in _module_fixtures(scope).values():
                if _fixture_spec(node)[1]:
                    found.append((owner, node))
        if self.cls is not None:
            for stmt in self.cls.body:
                if isinstance(stmt, FUNCS) and _is_fixture(stmt) and _fixture_spec(stmt)[1]:
                    found.append((self.module, stmt))
        return found


@dataclass
class Closure:
    # Keyed by node identity too: every test's root is "<test>", and two marks can share
    # their truncated label.
    items: dict[tuple[str, str, int], tuple[Module, ast.AST, str]] = field(default_factory=dict)


def _walk_closure(ctx: Context, roots: list[tuple[Module, ast.AST, str, str]]) -> Closure:
    closure = Closure()
    queue = list(roots)
    while queue:
        module, node, qual, kind = queue.pop()
        key = (module.name, qual, id(node))
        if key in closure.items:
            continue
        closure.items[key] = (module, node, kind)
        if isinstance(node, FUNCS) and kind in {"fixture", "test"}:
            skip = _parametrized_names(node.decorator_list)
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if arg.arg in {"self", "cls"} or arg.arg in skip:
                    continue
                found = ctx.fixture(arg.arg)
                if found:
                    queue.append((found[0], found[1], f"fixture:{arg.arg}", "fixture"))
        for sub, chain in _scoped_walk(module, node):
            if (
                isinstance(sub, ast.Call)
                and ast.unparse(sub.func).endswith("getfixturevalue")
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
            ):
                name = sub.args[0].value
                found = ctx.fixture(name)
                if found:
                    queue.append((found[0], found[1], f"fixture:{name}", "fixture"))
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                for kind_, owner, target_node, name in _resolve_scoped(module, chain, sub.id):
                    if kind_ == "module":
                        continue
                    queue.append((owner, target_node, name, "def" if kind_ == "def" else "const"))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                for target in _resolve_scoped(module, chain, sub.value.id):
                    if target[0] != "module":
                        continue
                    inner = _resolve_name(target[1], sub.attr)
                    if inner and inner[0] != "module":
                        queue.append(
                            (inner[1], inner[2], inner[3], "def" if inner[0] == "def" else "const")
                        )
    return closure


def _test_class_members(cls: ast.ClassDef) -> list[ast.stmt]:
    """A test class contributes its attributes and non-test methods, not sibling tests."""
    return [
        stmt
        for stmt in cls.body
        if not (isinstance(stmt, FUNCS) and stmt.name.startswith("test"))
        and not (isinstance(stmt, FUNCS) and _is_fixture(stmt))
    ]


def _tests_in(module: Module) -> Iterator[tuple[str, ast.AST, ast.ClassDef | None]]:
    for stmt in module.tree.body:
        if isinstance(stmt, FUNCS) and stmt.name.startswith("test"):
            yield stmt.name, stmt, None
        elif isinstance(stmt, ast.ClassDef) and stmt.name.startswith("Test"):
            for sub in stmt.body:
                if isinstance(sub, FUNCS) and sub.name.startswith("test"):
                    yield f"{stmt.name}::{sub.name}", sub, stmt


def suite_modules() -> list[Module]:
    return [m for m in modules().values() if m.path.name.startswith("test_")]


# ── per-test index ──────────────────────────────────────────────────────────────


def inventory_entry(
    module: Module, node: ast.AST, cls: ast.ClassDef | None
) -> tuple[dict[str, Any], Closure]:
    """``{"helpers": [...]}`` for one test, with the closure it was read from."""
    ctx = Context(module, cls)
    roots: list[tuple[Module, ast.AST, str, str]] = [(module, node, "<test>", "test")]
    marks = [*_pytestmark(module.tree.body), *getattr(node, "decorator_list", [])]
    if cls is not None:
        marks += [*_pytestmark(cls.body), *cls.decorator_list]
        for index, member in enumerate(_test_class_members(cls)):
            roots.append((module, member, f"{cls.name}.<member{index}>", "def"))
    for owner, fixture in ctx.autouse():
        roots.append((owner, fixture, f"fixture:{_fixture_spec(fixture)[0]}", "fixture"))
    for name in _usefixtures(marks):
        found = ctx.fixture(name)
        if found:
            roots.append((found[0], found[1], f"fixture:{name}", "fixture"))
    for decorator in marks:
        roots.append((module, decorator, f"<mark:{ast.unparse(decorator)[:80]}>", "mark"))
    closure = _walk_closure(ctx, roots)
    helpers = {
        renames().get(qual, qual)
        for (_, qual, _), (_, _, kind) in closure.items.items()
        if kind == "def" and not qual.startswith("<") and ".<member" not in qual
    }
    return {"helpers": sorted(helpers)}, closure


TestItem = tuple[Module, str, ast.AST, ast.ClassDef | None]


@cache
def build() -> dict[str, dict[str, Any]]:
    return collect(
        (module, qual, node, cls)
        for module in suite_modules()
        for qual, node, cls in _tests_in(module)
    )


def collect(items: Iterable[TestItem]) -> dict[str, dict[str, Any]]:
    """Baseline node id -> index entry for the given tests; two current tests that translate
    to one baseline id are refused (``moves.claim``)."""
    checks: dict[str, dict[str, Any]] = {}
    seen: dict[str, str] = {}
    for module, qual, node, cls in items:
        node_id = moves.claim(seen, f"{module.rel}::{qual}")
        checks[node_id], _ = inventory_entry(module, node, cls)
    return checks


def _area(node_id: str) -> str:
    """``tests/<dir>/…`` -> ``<dir>``; ``tests/test_x.py`` -> ``_root__test_x`` (one file per
    root module: together they are over the 500 KB commit limit)."""
    parts = node_id.split("::", 1)[0].split("/")
    return parts[1] if len(parts) > 2 else f"_root__{parts[-1].removesuffix('.py')}"
