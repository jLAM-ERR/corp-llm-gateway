"""profile.toml parsing + extends resolution + bundle integrity (D6).

``resolve_extends`` flattens the extends DAG to an ordered ``[core, ...,
most-specific]`` layer list, guarded against cycles (``ProfileCycleError``) and
runaway depth (``ProfileDepthError``, mirroring ``content_blocks``). Hash-
integrity ships fail-closed: a profile may declare ``content_hash`` — an order-
independent SHA-256 over the bundle's other files (``compute_content_hash``;
``profile.toml`` itself excluded to avoid self-reference) — which
``verify_integrity`` recomputes at load and refuses on mismatch, catching a
bundle tampered vs its own manifest with no external PKI. Detached-signature
verify (``verify_signature``) stays a gated no-op behind
``CORP_PROFILE_REQUIRE_SIGNATURE`` until the offline cosign/PKI decision lands.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from corp_llm_gateway.profiles.base import PolicyKnobs, ProfileParseError
from corp_llm_gateway.team_config.models import FailPolicyOverrides

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from typing import Any

# Agreed extends ceiling (plan Post-Completion). A chain deeper than this is
# rejected before any I/O; a cycle trips the ancestor guard first with a clearer
# error.
MAX_EXTENDS_DEPTH = 8

_VALID_FAIL_POLICY = ("fail-closed", "continue")


class ProfileCycleError(Exception):
    pass


class ProfileDepthError(Exception):
    pass


class ProfileIntegrityError(Exception):
    pass


class ProfileSignatureError(Exception):
    pass


@dataclass(frozen=True)
class ProfileManifest:
    name: str
    extends: tuple[str, ...] = ()
    detectors: tuple[str, ...] = ()
    gazetteer_dirs: tuple[str, ...] = ()
    policy: PolicyKnobs = field(default_factory=PolicyKnobs)
    content_hash: str | None = None
    signature: str | None = None


def parse_manifest(text: str) -> ProfileManifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileParseError(f"invalid profile.toml: {exc}") from exc

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileParseError("profile.toml: 'name' is required and must be a non-empty string")

    content_hash = _opt_str(data.get("content_hash"), "content_hash")
    signature = _opt_str(data.get("signature"), "signature")
    return ProfileManifest(
        name=name,
        extends=_as_str_tuple(data.get("extends", ()), "extends"),
        detectors=_as_str_tuple(data.get("detectors", ()), "detectors"),
        gazetteer_dirs=_as_str_tuple(data.get("gazetteer_dirs", ()), "gazetteer_dirs"),
        policy=_parse_policy(data.get("policy", {})),
        content_hash=content_hash,
        signature=signature,
    )


async def resolve_extends(
    roots: Sequence[str],
    read: Callable[[str], Awaitable[ProfileManifest]],
    *,
    max_depth: int = MAX_EXTENDS_DEPTH,
) -> tuple[str, ...]:
    """Expand ``extends`` to an ordered ``[core, ..., most-specific]`` layer list.

    Post-order DFS: a profile appears after all profiles it extends. Duplicate
    layers (diamonds) collapse to their first (deepest) position. Raises
    ``ProfileCycleError`` on a back-edge and ``ProfileDepthError`` past
    ``max_depth``.
    """
    order: list[str] = []
    done: set[str] = set()

    async def visit(profile_id: str, ancestors: tuple[str, ...], depth: int) -> None:
        if depth > max_depth:
            chain = " -> ".join((*ancestors, profile_id))
            raise ProfileDepthError(
                f"extends chain for {profile_id!r} exceeds max depth {max_depth}: {chain}"
            )
        if profile_id in ancestors:
            chain = " -> ".join((*ancestors, profile_id))
            raise ProfileCycleError(f"extends cycle: {chain}")
        if profile_id in done:
            return
        manifest = await read(profile_id)
        for parent in manifest.extends:
            await visit(parent, (*ancestors, profile_id), depth + 1)
        done.add(profile_id)
        order.append(profile_id)

    for root in roots:
        await visit(root, (), 0)
    return tuple(order)


def compute_content_hash(parts: Iterable[tuple[str, bytes]]) -> str:
    """Order-independent SHA-256 over (name, bytes) data-file parts."""
    digest = hashlib.sha256()
    for name, data in sorted(parts, key=lambda part: part[0]):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _require_signature() -> bool:
    from corp_llm_gateway import config

    value = config.get("CORP_PROFILE_REQUIRE_SIGNATURE", "0") or "0"
    return value.strip().lower() in _TRUTHY


def verify_signature(manifest: ProfileManifest, *, require: bool | None = None) -> None:
    """Detached-signature seam — a gated no-op until offline PKI lands.

    ``require`` defaults to the ``CORP_PROFILE_REQUIRE_SIGNATURE`` flag. Off (the
    default) → inert; on → fail closed rather than silently pass.
    """
    if require is None:
        require = _require_signature()
    if not require:
        return None
    # BLOCKER: offline cosign/PKI decision (plan Post-Completion) — no in-tree
    # verifier and no air-gapped home for verification keys yet. Enforcing by
    # default would brick startup, so this is opt-in; opting in fails closed
    # (no ad-hoc fail-open — CLAUDE.md invariant 6) until keys exist.
    raise ProfileSignatureError(
        "CORP_PROFILE_REQUIRE_SIGNATURE is set but detached-signature "
        "verification is not implemented — blocked on the offline cosign/PKI "
        "decision; unset the flag until verification keys exist"
    )


def verify_integrity(manifest: ProfileManifest, computed_hash: str) -> None:
    """Fail closed on a content-hash mismatch, then run the signature seam.

    No-op on the hash when the manifest declares none; the signature seam is
    itself a gated no-op (see :func:`verify_signature`).
    """
    if manifest.content_hash is not None and manifest.content_hash != computed_hash:
        raise ProfileIntegrityError(
            f"profile {manifest.name!r} content-hash mismatch: "
            f"manifest={manifest.content_hash!r} computed={computed_hash!r}"
        )
    verify_signature(manifest)


def _parse_policy(raw: Any) -> PolicyKnobs:
    if not isinstance(raw, dict):
        raise ProfileParseError("profile.toml: [policy] must be a table")
    kwargs: dict[str, Any] = {}
    if "size_threshold_bytes" in raw:
        kwargs["size_threshold_bytes"] = _as_int(
            raw["size_threshold_bytes"], "size_threshold_bytes"
        )
    if "block_payloads" in raw:
        kwargs["block_payloads"] = _as_bool(raw["block_payloads"], "block_payloads")
    if "dlp_guard" in raw:
        kwargs["dlp_guard"] = _as_bool(raw["dlp_guard"], "dlp_guard")
    if "oracle_mode" in raw:
        kwargs["oracle_mode"] = _as_str(raw["oracle_mode"], "oracle_mode")
    if "allowed_providers" in raw:
        kwargs["allowed_providers"] = frozenset(
            _as_str_tuple(raw["allowed_providers"], "allowed_providers")
        )
    if "canary_patterns" in raw:
        kwargs["canary_patterns"] = _as_str_tuple(raw["canary_patterns"], "canary_patterns")
    if "retention_hot_days" in raw:
        kwargs["retention_hot_days"] = _as_int(raw["retention_hot_days"], "retention_hot_days")
    if "retention_cold_years" in raw:
        kwargs["retention_cold_years"] = _as_int(
            raw["retention_cold_years"], "retention_cold_years"
        )
    if "fail_policy" in raw:
        kwargs["fail_policy"] = _parse_fail_policy(raw["fail_policy"])
    return PolicyKnobs(**kwargs)


def _parse_fail_policy(raw: Any) -> FailPolicyOverrides:
    if not isinstance(raw, dict):
        raise ProfileParseError("profile.toml: [policy.fail_policy] must be a table")
    base = FailPolicyOverrides()

    def pick(key: str, default: str) -> str:
        if key not in raw:
            return default
        value = raw[key]
        if value not in _VALID_FAIL_POLICY:
            raise ProfileParseError(
                f"fail_policy.{key}={value!r} must be one of {_VALID_FAIL_POLICY}"
            )
        return value

    return FailPolicyOverrides(
        pre_pass_down=pick("pre_pass_down", base.pre_pass_down),  # type: ignore[arg-type]
        audit_sink_down=pick("audit_sink_down", base.audit_sink_down),  # type: ignore[arg-type]
        audit_buffer_full=pick("audit_buffer_full", base.audit_buffer_full),  # type: ignore[arg-type]
    )


def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ProfileParseError(
                    f"{field_name}: expected strings, got {type(item).__name__}"
                )
            out.append(item)
        return tuple(out)
    raise ProfileParseError(f"{field_name}: expected a string or list of strings")


def _as_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProfileParseError(f"{field_name}: expected a string, got {type(value).__name__}")
    return value


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileParseError(f"{field_name}: expected an integer, got {type(value).__name__}")
    return value


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileParseError(f"{field_name}: expected a boolean, got {type(value).__name__}")
    return value


def _opt_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, field_name)
