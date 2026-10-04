"""Check inventory of the suite, from the AST alone (runs the same in every venv).

Per test function (``path::name`` / ``path::Class::name``; parametrised ids are the
ledgers' job): its asserts, ``pytest.raises`` / ``pytest.fail`` sites, the helpers it
reaches that themselves check something, its case data (parametrize tables, loop
iterables), its fixtures and helpers, and one hash over the normalised executable body
of the test plus every helper, fixture, class and module constant it reaches
transitively (docstrings and comments dropped, repo paths canonicalised, ``tests.*``
import paths folded, approved renames applied).

Beside it, the external-dependency manifest: every repo file a test reaches outside the
Python call graph (a script run by ``subprocess``, a template, a config it reads, a
module chosen at run time), hashed whole. A process launch or a dynamic lookup the walker
cannot resolve is listed as unresolved and fails the gate until
``external_deps_overrides.json`` resolves it.

``python -m tests._gates.inventory --write|--check``
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
MANIFESTS = TESTS / "_manifests"
CHECKS_DIR = MANIFESTS / "baseline_checks"
EXTERNAL_PATH = MANIFESTS / "external_deps.json"
OVERRIDES_PATH = MANIFESTS / "external_deps_overrides.json"
RENAMES_PATH = MANIFESTS / "renames.json"
BASELINE = "807831a"

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

PROCESS_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.system",
    "os.execv",
    "os.execvp",
    "os.execve",
    "os.spawnv",
}
DYNAMIC_CALLS = {"importlib.import_module", "runpy.run_path", "runpy.run_module", "__import__"}
PATH_CTORS = {"Path", "pathlib.Path", "PurePath", "PosixPath"}
# Never a dependency: the gate's own output, and bare bases every path is built from.
NOT_DEPENDENCIES = {"tests/_manifests"}
BASES = {".", "tests", "src", "src/corp_llm_gateway"}


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


def _index(name: str, path: Path, text: str | None = None) -> Module:
    source = path.read_text() if text is None else text
    tree = ast.parse(source, filename=str(path))
    module = Module(name, path, tree)
    package = name.rsplit(".", 1)[0] if path.name != "__init__.py" else name
    for stmt in _top_level(tree.body):
        if isinstance(stmt, DEFS):
            module.defs[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for target_name in _targets(target):
                    module.consts[target_name] = stmt
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign) and isinstance(stmt.target, ast.Name):
            module.consts.setdefault(stmt.target.id, stmt)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    module.imports[alias.asname] = (alias.name, None)
                else:
                    top = alias.name.split(".", 1)[0]
                    module.imports[top] = (top, None)
        elif isinstance(stmt, ast.ImportFrom):
            source = stmt.module or ""
            if stmt.level:
                base = package.rsplit(".", stmt.level - 1)[0] if stmt.level > 1 else package
                source = f"{base}.{source}" if source else base
            for alias in stmt.names:
                module.imports[alias.asname or alias.name] = (source, alias.name)
    return module


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


# ── path evaluation (repo paths canonicalised; external dependencies) ───────────


class _UnknownError(Exception):
    pass


def _eval(module: Module, node: ast.AST, depth: int = 0) -> Any:
    """Statically evaluate a path-building expression to a Path or str."""
    if depth > 40:
        raise _UnknownError
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return module.path
        target = _resolve_name(module, node.id)
        if target and target[0] == "const":
            owner, stmt = target[1], target[2]
            value = getattr(stmt, "value", None)
            if value is None or not _single_target(stmt, node.id):
                raise _UnknownError
            return _eval(owner, value, depth + 1)
        raise _UnknownError
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval(module, node.left, depth + 1)
        right = _eval(module, node.right, depth + 1)
        if isinstance(left, Path) and isinstance(right, str | Path):
            return left / right
        raise _UnknownError
    if isinstance(node, ast.Attribute):
        base = _eval(module, node.value, depth + 1)
        if isinstance(base, Path) and node.attr == "parent":
            return base.parent
        raise _UnknownError
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr == "parents" and isinstance(node.slice, ast.Constant):
            base = _eval(module, node.value.value, depth + 1)
            if isinstance(base, Path):
                return base.parents[node.slice.value]
        raise _UnknownError
    if isinstance(node, ast.Call):
        func = ast.unparse(node.func)
        if func in PATH_CTORS and node.args and not node.keywords:
            parts = [_eval(module, arg, depth + 1) for arg in node.args]
            first = parts[0]
            result = first if isinstance(first, Path) else Path(first)
            for part in parts[1:]:
                result = result / part
            return result
        if func == "str" and len(node.args) == 1:
            return _eval(module, node.args[0], depth + 1)
        if func in {"os.path.join"} and node.args:
            parts = [_eval(module, arg, depth + 1) for arg in node.args]
            return Path(os.path.join(*map(str, parts)))
        if func in {"os.path.dirname"} and len(node.args) == 1:
            return Path(os.path.dirname(str(_eval(module, node.args[0], depth + 1))))
        if func in {"os.path.abspath", "os.path.realpath"} and len(node.args) == 1:
            return Path(str(_eval(module, node.args[0], depth + 1)))
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in {"resolve", "absolute", "expanduser"} and not node.args:
                base = _eval(module, node.func.value, depth + 1)
                if isinstance(base, Path):
                    return base
            if method == "joinpath":
                base = _eval(module, node.func.value, depth + 1)
                if isinstance(base, Path):
                    for arg in node.args:
                        base = base / _eval(module, arg, depth + 1)
                    return base
            if method == "with_name" and len(node.args) == 1:
                base = _eval(module, node.func.value, depth + 1)
                if isinstance(base, Path):
                    return base.with_name(_eval(module, node.args[0], depth + 1))
            if method == "with_suffix" and len(node.args) == 1:
                base = _eval(module, node.func.value, depth + 1)
                if isinstance(base, Path):
                    return base.with_suffix(_eval(module, node.args[0], depth + 1))
        raise _UnknownError
    raise _UnknownError


def _single_target(stmt: ast.AST, name: str) -> bool:
    if isinstance(stmt, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets)
    return isinstance(stmt, ast.AnnAssign)


def eval_path(module: Module, node: ast.AST) -> Path | None:
    try:
        value = _eval(module, node)
    except (_UnknownError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(value, Path):
        return None
    if not value.is_absolute():
        value = ROOT / value
    try:
        rel = value.resolve().relative_to(ROOT)
    except ValueError:
        return None
    return rel


# ── name resolution ─────────────────────────────────────────────────────────────


def _resolve_name(module: Module, name: str, seen: frozenset[str] = frozenset()):
    """(kind, owner module, node, name) for a tests-local symbol, else None."""
    if name in module.defs:
        return ("def", module, module.defs[name], name)
    if name in module.consts:
        return ("const", module, module.consts[name], name)
    if name in module.imports:
        source, attr = module.imports[name]
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


_FIXTURES: dict[str, dict[str, tuple[Module, ast.AST]]] = {}
_CHAINS: dict[str, list[Module]] = {}


def _module_fixtures(module: Module) -> dict[str, tuple[Module, ast.AST]]:
    if module.name not in _FIXTURES:
        _FIXTURES[module.name] = _scan_fixtures(module)
    return _FIXTURES[module.name]


def _scan_fixtures(module: Module) -> dict[str, tuple[Module, ast.AST]]:
    fixtures: dict[str, tuple[Module, ast.AST]] = {}
    for local in [*module.defs, *module.imports]:
        target = _resolve_name(module, local)
        is_def = target is not None and target[0] == "def" and isinstance(target[2], FUNCS)
        if is_def and _is_fixture(target[2]):
            fixtures[_fixture_spec(target[2])[0]] = (target[1], target[2])
    return fixtures


def _conftest_chain(module: Module) -> list[Module]:
    if module.name not in _CHAINS:
        _CHAINS[module.name] = _scan_chain(module)
    return _CHAINS[module.name]


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


# ── normalisation ───────────────────────────────────────────────────────────────


@cache
def renames() -> dict[str, str]:
    """``renames.json`` ``{"names": {new: old}}``: approved renames of module-level helpers,
    fixtures, classes and constants, applied to the current tree so a move PR that renames
    one reproduces the baseline hashes. Only module-level names are rewritten (a ``def`` /
    ``class`` statement of the module and a load of a name the module defines or imports),
    never locals, attributes or arguments that happen to share the name."""
    if RENAMES_PATH.exists():
        return json.loads(RENAMES_PATH.read_text()).get("names", {})
    return {}


class _Normaliser(ast.NodeTransformer):
    def __init__(self, module: Module, root: ast.AST, root_is_module_level: bool) -> None:
        self.module = module
        self.root = root
        self.root_is_module_level = root_is_module_level
        self.top_level = {*module.defs, *module.consts, *module.imports}

    def _rename(self, name: str) -> str:
        return renames().get(name, name) if name in self.top_level else name

    def _strip_docstring(self, node: Any) -> Any:
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if isinstance(node, ast.expr) and not isinstance(node, ast.Constant | ast.Name):
            rel = eval_path(self.module, node)
            if rel is not None:
                return ast.Constant(f"<repo>/{rel.as_posix()}")
        if isinstance(node, (*DEFS, ast.Module)):
            self._strip_docstring(node)
        return super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == "__file__":
            return ast.Constant(f"<repo>/{self.module.rel}")
        node.id = self._rename(node.id)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.module and (node.module == "tests" or node.module.startswith("tests.")):
            node.module = "tests"
            node.level = 0
        return node

    def _visit_def(self, node: Any) -> ast.AST:
        if node is self.root and self.root_is_module_level:
            node.name = self._rename(node.name)
        return self.generic_visit(node)

    visit_FunctionDef = _visit_def  # noqa: N815
    visit_AsyncFunctionDef = _visit_def  # noqa: N815
    visit_ClassDef = _visit_def  # noqa: N815


_DUMPS: dict[tuple[str, int, bool], str] = {}


def normalised_dump(module: Module, node: ast.AST, *, drop_name: bool = False) -> str:
    key = (module.name, id(node), drop_name)
    if key not in _DUMPS:
        _DUMPS[key] = _normalise(module, node, drop_name)
    return _DUMPS[key]


def _normalise(module: Module, node: ast.AST, drop_name: bool) -> str:
    clone = copy.deepcopy(node)
    if drop_name and hasattr(clone, "name"):
        clone.name = "_"
    module_level = module.defs.get(getattr(node, "name", None)) is node
    clone = _Normaliser(module, clone, module_level).visit(clone)
    return ast.dump(clone, annotate_fields=False, include_attributes=False)


# ── per-test inventory ──────────────────────────────────────────────────────────


def _counts(node: ast.AST) -> dict[str, int]:
    asserts = raises = fails = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            asserts += 1
        elif isinstance(sub, ast.Call):
            func = ast.unparse(sub.func)
            if func in {"pytest.raises", "raises"}:
                raises += 1
            elif func in {"pytest.fail", "fail"}:
                fails += 1
    return {"asserts": asserts, "raises": raises, "fail": fails}


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
    fixtures: set[str] = field(default_factory=set)
    external_fixtures: set[str] = field(default_factory=set)
    module_refs: set[str] = field(default_factory=set)


def _walk_closure(ctx: Context, roots: list[tuple[Module, ast.AST, str, str]]) -> Closure:
    closure = Closure()
    queue = list(roots)
    while queue:
        module, node, qual, kind = queue.pop()
        key = (module.name, qual, id(node))
        if key in closure.items:
            continue
        closure.items[key] = (module, node, kind)
        if kind == "fixture":
            closure.fixtures.add(_fixture_spec(node)[0])
        if isinstance(node, FUNCS) and kind in {"fixture", "test"}:
            skip = _parametrized_names(node.decorator_list)
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if arg.arg in {"self", "cls"} or arg.arg in skip:
                    continue
                found = ctx.fixture(arg.arg)
                if found:
                    queue.append((found[0], found[1], f"fixture:{arg.arg}", "fixture"))
                else:
                    closure.external_fixtures.add(arg.arg)
        for sub in ast.walk(node):
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
                target = _resolve_name(module, sub.id)
                if target is None:
                    continue
                kind_, owner, target_node, name = target
                if kind_ == "module":
                    closure.module_refs.add(owner.name)
                    continue
                queue.append((owner, target_node, name, "def" if kind_ == "def" else "const"))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                target = _resolve_name(module, sub.value.id)
                if target and target[0] == "module":
                    owner = target[1]
                    inner = _resolve_name(owner, sub.attr)
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


def _case_data(module: Module, node: ast.AST, cls: ast.ClassDef | None) -> dict[str, Any]:
    decorators = [*_pytestmark(module.tree.body)]
    if cls is not None:
        decorators += [*cls.decorator_list, *_pytestmark(cls.body)]
    decorators += node.decorator_list
    tables = []
    for decorator in decorators:
        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith(
            "mark.parametrize"
        ):
            values = decorator.args[1] if len(decorator.args) > 1 else None
            if isinstance(values, ast.Name):
                target = _resolve_name(module, values.id)
                if target and target[0] == "const":
                    values = getattr(target[2], "value", None)
            size = len(values.elts) if isinstance(values, ast.List | ast.Tuple | ast.Set) else None
            if isinstance(values, ast.Dict):
                size = len(values.keys)
            tables.append(
                {
                    "argnames": ast.unparse(decorator.args[0]) if decorator.args else "",
                    "cases": size,
                    "values": _digest(normalised_dump(module, decorator)),
                }
            )
    loops = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.For | ast.AsyncFor | ast.comprehension):
            loops.append(_short(ast.unparse(sub.iter)))
    return {"parametrize": tables, "loops": loops}


def _short(text: str) -> str:
    return text if len(text) <= 100 else f"{text[:60]}…#{_digest(text)[:12]}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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


def inventory_entry(module: Module, node: ast.AST, cls: ast.ClassDef | None) -> dict[str, Any]:
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

    delegated: dict[str, dict[str, int]] = {}
    helpers: set[str] = set()
    constants: set[str] = set()
    parts = [normalised_dump(module, node, drop_name=True)]
    for (_, qual, _), (owner, item, kind) in sorted(closure.items.items(), key=lambda kv: kv[0][1]):
        if kind == "test":
            continue
        name = qual.split(":", 1)[1] if qual.startswith("fixture:") else qual
        name = renames().get(name, name)
        parts.append(f"{kind}:{name}:{normalised_dump(owner, item)}")
        if kind == "const":
            constants.add(name)
        elif kind == "def" and not name.startswith("<") and ".<member" not in name:
            helpers.add(name)
        if kind in {"def", "fixture"} and isinstance(item, (*FUNCS, ast.ClassDef)):
            counts = _counts(item)
            if any(counts.values()):
                delegated[name] = counts
        elif kind == "def" and ".<member" in qual:
            counts = _counts(item)
            if any(counts.values()) and hasattr(item, "name"):
                delegated[f"{cls.name}.{item.name}"] = counts
    return {
        **_counts(node),
        "delegated": dict(sorted(delegated.items())),
        "case_data": _case_data(module, node, cls),
        "fixtures": sorted(closure.fixtures | {f"ext:{n}" for n in closure.external_fixtures}),
        "helpers": sorted(helpers),
        "constants": sorted(constants),
        "body_hash": _digest("\n".join(sorted(parts[1:])) + "\n" + parts[0])[:32],
    }, closure


# ── external dependencies ───────────────────────────────────────────────────────


@cache
def tracked_files() -> tuple[str, ...]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=60
        ).stdout
        return tuple(sorted(p for p in out.decode().split("\0") if p))
    except (OSError, subprocess.SubprocessError):
        return tuple(
            sorted(
                p.relative_to(ROOT).as_posix()
                for p in ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
            )
        )


@cache
def file_hash(rel: str) -> str | None:
    path = ROOT / rel
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    if path.is_dir():
        prefix = rel.rstrip("/") + "/"
        members = [p for p in tracked_files() if p.startswith(prefix)]
        digest = hashlib.sha256()
        for member in members:
            digest.update(member.encode() + b"\0" + (file_hash(member) or "-").encode() + b"\n")
        return "dir:" + digest.hexdigest()[:28]
    return None


def _excluded(rel: str) -> bool:
    return rel in BASES or any(rel == n or rel.startswith(n + "/") for n in NOT_DEPENDENCIES)


def _maximal_paths(module: Module, node: ast.AST) -> Iterator[tuple[ast.AST, Path]]:
    if isinstance(node, ast.expr) and not isinstance(node, ast.Constant | ast.Name):
        rel = eval_path(module, node)
        if rel is not None:
            yield node, rel
            return
    if isinstance(node, ast.Name):
        rel = eval_path(module, node)
        if rel is not None:
            yield node, rel
            return
    for child in ast.iter_child_nodes(node):
        yield from _maximal_paths(module, child)


def _strings(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _local_value(scope: ast.AST, name: str) -> ast.expr | None:
    for sub in ast.walk(scope):
        if isinstance(sub, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in sub.targets
        ):
            return sub.value
    return None


def _program(module: Module, call: ast.Call, scope: ast.AST) -> str | None:
    """The command name a process launch starts, when it is a literal or sys.executable."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Name) and _resolve_name(module, first.id) is None:
        first = _local_value(scope, first.id) or first
    head = first.elts[0] if isinstance(first, ast.List | ast.Tuple) and first.elts else first
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return head.value.split()[0] if head.value.strip() else None
    if ast.unparse(head) == "sys.executable":
        return "python"
    if isinstance(head, ast.Name):
        target = _resolve_name(module, head.id)
        if target and target[0] == "const":
            value = getattr(target[2], "value", None)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.split()[0]
            if isinstance(value, ast.List | ast.Tuple) and value.elts:
                inner = value.elts[0]
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    return inner.value
                if ast.unparse(inner) == "sys.executable":
                    return "python"
    return None


@dataclass
class ExternalScan:
    deps: set[str] = field(default_factory=set)
    programs: set[str] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)


def _site(module: Module, qual: str, index: int, what: str) -> str:
    return f"{module.rel}::{qual}#{what}{index}"


@cache
def overrides() -> dict[str, dict[str, Any]]:
    if OVERRIDES_PATH.exists():
        return json.loads(OVERRIDES_PATH.read_text())["sites"]
    return {}


def _scan_item(module: Module, node: ast.AST, qual: str) -> ExternalScan:
    scan = ExternalScan()
    for _, rel in _maximal_paths(module, node):
        posix = rel.as_posix()
        if (ROOT / rel).exists() and not _excluded(posix):
            scan.deps.add(posix)
    process_index = dynamic_index = 0
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = ast.unparse(sub.func)
        if func in PROCESS_CALLS:
            site = _site(module, qual, process_index, "process")
            process_index += 1
            resolved = False
            for arg in [*sub.args, *(k.value for k in sub.keywords)]:
                for _, rel in _maximal_paths(module, arg):
                    if (ROOT / rel).exists():
                        resolved = True
                for text in _strings(arg):
                    candidate = text.split()[0] if text.strip() else ""
                    if candidate and "/" in candidate and (ROOT / candidate).is_file():
                        scan.deps.add(candidate)
                        resolved = True
            program = _program(module, sub, node)
            if program:
                scan.programs.add(program)
                resolved = True
            if site in overrides():
                scan.deps |= set(overrides()[site].get("deps", []))
                scan.programs |= set(overrides()[site].get("programs", []))
            elif not resolved:
                scan.unresolved.add(site)
        elif func in DYNAMIC_CALLS:
            site = _site(module, qual, dynamic_index, "dynamic")
            dynamic_index += 1
            first = sub.args[0] if sub.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                name = first.value
                mods = modules()
                if name in mods:
                    scan.deps.add(mods[name].rel)
                else:
                    scan.programs.add(f"module:{name}")
            elif site in overrides():
                scan.deps |= set(overrides()[site].get("deps", []))
                scan.programs |= set(overrides()[site].get("programs", []))
            else:
                scan.unresolved.add(site)
    return scan


def _python_imports_of(rel: str, seen: set[str]) -> set[str]:
    """Test-tree modules a subprocess-run Python file imports, whole files too."""
    if rel in seen or not rel.endswith(".py"):
        return set()
    seen.add(rel)
    found = set()
    mods = modules()
    by_rel = {m.rel: m for m in mods.values()}
    module = by_rel.get(rel)
    if module is None:
        return set()
    for source, attr in module.imports.values():
        for candidate in (source, f"{source}.{attr}" if attr else None):
            if candidate in mods:
                found.add(mods[candidate].rel)
                found |= _python_imports_of(mods[candidate].rel, seen)
    return found


# ── whole-suite build ───────────────────────────────────────────────────────────


TestItem = tuple[Module, str, ast.AST, ast.ClassDef | None]


@cache
def build() -> tuple[dict[str, Any], dict[str, Any]]:
    return collect(
        (module, qual, node, cls)
        for module in suite_modules()
        for qual, node, cls in _tests_in(module)
    )


def collect(items: Iterable[TestItem]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(checks, external) for the given tests; the scan cache is shared across them."""
    checks: dict[str, Any] = {}
    external: dict[str, Any] = {}
    unresolved: set[str] = set()
    scans: dict[tuple[str, str, int], ExternalScan] = {}
    for module, qual, node, cls in items:
        node_id = f"{module.rel}::{qual}"
        entry, closure = inventory_entry(module, node, cls)
        checks[node_id] = entry
        deps: set[str] = set()
        programs: set[str] = set()
        for key, (owner, item, _) in closure.items.items():
            if key not in scans:
                scans[key] = _scan_item(owner, item, key[1] if key[1] != "<test>" else qual)
            scan = scans[key]
            deps |= scan.deps
            programs |= scan.programs
            unresolved |= {f"{s} (via {node_id})" for s in scan.unresolved}
        for dep in list(deps):
            if dep.startswith("tests/") and dep.endswith(".py"):
                deps |= _python_imports_of(dep, set())
        deps.discard(module.rel)
        if deps or programs:
            external[node_id] = {
                "files": {dep: file_hash(dep) for dep in sorted(deps)},
                "programs": sorted(programs),
            }
    unresolved_sites = sorted({u.split(" (via ", 1)[0] for u in unresolved})
    return checks, {"unresolved": unresolved_sites, "tests": external}


def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def cases_from_ledgers() -> dict[str, dict[str, int]]:
    """function-level id -> {env: number of collected ids}, from the expected outcomes."""
    from tests._gates import ledger

    counted: dict[str, dict[str, int]] = {}
    for env in ledger.ENVS:
        if not ledger.expected_path(env).exists():
            continue
        for node_id in ledger.ids_with_outcome(env):
            if "::" not in node_id:
                continue
            path, test, _ = ledger.split_id(node_id)
            slot = counted.setdefault(ledger.join_id(path, test, ""), {})
            slot[env] = slot.get(env, 0) + 1
    return counted


def _area(node_id: str) -> str:
    """``tests/<dir>/…`` -> ``<dir>``; ``tests/test_x.py`` -> ``_root__test_x`` (one file per
    root module: together they are over the 500 KB commit limit)."""
    parts = node_id.split("::", 1)[0].split("/")
    return parts[1] if len(parts) > 2 else f"_root__{parts[-1].removesuffix('.py')}"


def _one_per_line(entries: dict[str, Any]) -> str:
    def line(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    lines = [f"  {json.dumps(k)}: {line(v)}" for k, v in sorted(entries.items())]
    return "{\n" + ",\n".join(lines) + "\n }"


def write_checks(checks: dict[str, Any]) -> None:
    """One file per test directory (per module for tests/*.py), one line per test, so a
    PR's manifest diff reads test by test and no file nears the 500 KB commit limit."""
    cases = cases_from_ledgers()
    CHECKS_DIR.mkdir(exist_ok=True)
    areas: dict[str, dict[str, Any]] = {}
    for node_id, entry in checks.items():
        areas.setdefault(_area(node_id), {})[node_id] = entry
    for stale in CHECKS_DIR.glob("*.json"):
        if stale.stem not in areas:
            stale.unlink()
    for area, entries in areas.items():
        area_cases = {k: v for k, v in cases.items() if k in entries}
        text = (
            f'{{\n "baseline": "{BASELINE}",\n "tests": {_one_per_line(entries)},\n'
            f' "cases": {_one_per_line(area_cases)}\n}}\n'
        )
        (CHECKS_DIR / f"{area}.json").write_text(text)


def read_checks() -> tuple[dict[str, Any], dict[str, Any]]:
    """(tests, cases) across every area file."""
    tests: dict[str, Any] = {}
    cases: dict[str, Any] = {}
    for path in sorted(CHECKS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        tests.update(data["tests"])
        cases.update(data["cases"])
    return tests, cases


def diff_checks(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems = []
    for node_id in sorted(set(recorded) - set(current)):
        problems.append(f"missing test: {node_id}")
    for node_id in sorted(set(current) - set(recorded)):
        problems.append(f"new test not in the inventory: {node_id}")
    for node_id in sorted(set(recorded) & set(current)):
        before, after = recorded[node_id], current[node_id]
        for column in sorted(set(before) | set(after)):
            if before.get(column) != after.get(column):
                old, new = _brief(before.get(column)), _brief(after.get(column))
                problems.append(f"{node_id}: {column} {old} -> {new}")
    return problems


def diff_external(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems = [f"unresolved external dependency: {site}" for site in current["unresolved"]]
    before, after = recorded["tests"], current["tests"]
    for node_id in sorted(set(before) | set(after)):
        old = before.get(node_id, {"files": {}, "programs": []})
        new = after.get(node_id, {"files": {}, "programs": []})
        for path in sorted(set(old["files"]) | set(new["files"])):
            was, now = old["files"].get(path), new["files"].get(path)
            if was != now:
                problems.append(f"{node_id}: external {path} {was} -> {now}")
        if old["programs"] != new["programs"]:
            problems.append(f"{node_id}: programs {old['programs']} -> {new['programs']}")
    return problems


def _brief(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 160 else text[:150] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    checks, external = build()
    if args.write:
        write_checks(checks)
        EXTERNAL_PATH.write_text(_dump(external))
        for site in external["unresolved"]:
            print(f"UNRESOLVED {site}", file=sys.stderr)
        return 1 if external["unresolved"] else 0
    recorded_checks, _ = read_checks()
    recorded_external = json.loads(EXTERNAL_PATH.read_text())
    problems = diff_checks(recorded_checks, checks) + diff_external(recorded_external, external)
    for line in problems:
        print(f"INVENTORY: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
