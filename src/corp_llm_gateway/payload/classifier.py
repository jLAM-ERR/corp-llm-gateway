"""Payload classifier — Stage 0 pre-egress config/log block detector.

classify_block(text) → str | None:
  Returns a block_reason code if the text looks like a config file or log dump
  that should be refused before egress (R10 / R11). Returns None for clean text.

Block reason codes:
  "config:env"   — .env file (fenced or high-density KEY=VALUE assignments)
  "config:kube"  — Kubernetes manifest or kubeconfig YAML
  "config:nginx" — nginx server/location block config
  "config:ini"   — ini/toml config file (multiple [section] headers + KV density)
  "log:dump"     — access log, structured app log, or stack-trace dump

Design constraints:
  - CPU-only regex; no model inference.
  - Conservative: devs send CODE; only structured config/log shapes are blocked.
  - Raw content NEVER appears in the returned reason string (value hygiene).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Fenced code block language-tag patterns (strong single-pass signal)
# ---------------------------------------------------------------------------

_RE_FENCED_ENV = re.compile(
    r"^```[ \t]*(?:env|dotenv|\.env)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_RE_FENCED_NGINX = re.compile(
    r"^```[ \t]*(?:nginx|nginx\.conf)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_RE_FENCED_INI = re.compile(
    r"^```[ \t]*(?:ini|toml|cfg|conf)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_RE_FENCED_LOG = re.compile(
    r"^```[ \t]*(?:log|logs|access_?log|syslog)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Content patterns
# ---------------------------------------------------------------------------

# .env: ALL_CAPS_KEY= at the start of a line (no space before `=` prefix check)
_RE_ENV_LINE = re.compile(r"^[A-Z_][A-Z0-9_]*\s*=", re.MULTILINE)

# Kubernetes: apiVersion with a value + kind with a PascalCase word
# Requiring both prevents false positives from prose mentioning the terms.
_RE_KUBE_API_VERSION = re.compile(r"\bapiVersion\s*:\s*\S+")
_RE_KUBE_KIND = re.compile(r"\bkind\s*:\s*[A-Z][A-Za-z]+\b")
# kubeconfig structural sections
_RE_KUBE_CLUSTERS = re.compile(r"^clusters\s*:", re.MULTILINE)
_RE_KUBE_CONTEXTS = re.compile(r"^contexts\s*:", re.MULTILINE)

# nginx: server block opener + location directive
_RE_NGINX_SERVER = re.compile(r"\bserver\s*\{")
_RE_NGINX_LOCATION = re.compile(r"\blocation\s+[/~\w]")

# ini/toml: bare [SectionName] on its own line (excludes markdown links)
_RE_INI_SECTION = re.compile(r"^\s*\[[A-Za-z0-9_.\-]+\]\s*$", re.MULTILINE)
# key = value or key: value assignment (alpha-start key)
_RE_INI_KV = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_.\-]*\s*[=:]\s*\S")

# Logs — structured: ISO / CLF / bracket timestamps
_RE_LOG_TIMESTAMP = re.compile(
    r"(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"  # ISO-ish: 2024-01-01 10:00:00
    r"|\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}"  # CLF: 01/Jan/2024:10:00:00
    r"|\[\d{2}/[A-Za-z]{3}/\d{4}"  # bracket CLF: [01/Jan/2024
    r")"
)
_RE_LOG_LEVEL = re.compile(r"\b(?:INFO|WARN(?:ING)?|ERROR|DEBUG|TRACE|FATAL|CRITICAL)\b")

# Stack-trace frames (Java / Python / JS)
_RE_FRAME_JAVA = re.compile(r"\bat \w[\w.]+\.\w+\(")
_RE_FRAME_PYTHON = re.compile(r'File "[^"]+", line \d+')
_RE_FRAME_JS = re.compile(r"\s+at \w[\w.]*\s+\(")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_block(text: str) -> str | None:
    """Return a block_reason code if *text* looks like a config/log payload.

    Checks fenced code-block language tags first (single pass), then
    content signatures. Returns the first matching reason, or None.
    """
    # Fast path: fenced language tags are unambiguous signals.
    reason = _check_fenced_tags(text)
    if reason is not None:
        return reason

    lines = text.splitlines()

    if _is_env_file(lines):
        return "config:env"
    if _is_kube(text):
        return "config:kube"
    if _is_nginx(text):
        return "config:nginx"
    if _is_ini(lines):
        return "config:ini"
    if _is_log_dump(lines):
        return "log:dump"

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_fenced_tags(text: str) -> str | None:
    if _RE_FENCED_ENV.search(text):
        return "config:env"
    if _RE_FENCED_LOG.search(text):
        return "log:dump"
    if _RE_FENCED_NGINX.search(text):
        return "config:nginx"
    if _RE_FENCED_INI.search(text):
        return "config:ini"
    return None


def _is_env_file(lines: list[str]) -> bool:
    """High-density ALLCAPS_KEY= assignments (≥3, majority of non-blank lines)."""
    non_blank = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if len(non_blank) < 3:
        return False
    env_count = sum(1 for ln in non_blank if _RE_ENV_LINE.match(ln))
    return env_count >= 3 and env_count / len(non_blank) > 0.5


def _is_kube(text: str) -> bool:
    """Kubernetes manifest (apiVersion + PascalCase kind) or kubeconfig structure."""
    has_api = bool(_RE_KUBE_API_VERSION.search(text))
    has_kind = bool(_RE_KUBE_KIND.search(text))
    if has_api and has_kind:
        return True
    has_clusters = bool(_RE_KUBE_CLUSTERS.search(text))
    has_contexts = bool(_RE_KUBE_CONTEXTS.search(text))
    return has_clusters and has_contexts


def _is_nginx(text: str) -> bool:
    """nginx config: server block + location directive."""
    return bool(_RE_NGINX_SERVER.search(text) and _RE_NGINX_LOCATION.search(text))


def _is_ini(lines: list[str]) -> bool:
    """ini/toml: ≥2 bare [Section] headers + majority key=value lines."""
    section_count = sum(1 for ln in lines if _RE_INI_SECTION.match(ln))
    if section_count < 2:
        return False
    non_blank = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", ";"))]
    if not non_blank:
        return False
    kv_count = sum(1 for ln in non_blank if _RE_INI_KV.match(ln))
    return kv_count / len(non_blank) > 0.4


def _is_log_dump(lines: list[str]) -> bool:
    """Structured logs (timestamp+level density) or stack-trace frames."""
    if len(lines) < 5:
        return False
    ts_count = sum(1 for ln in lines if _RE_LOG_TIMESTAMP.search(ln))
    level_count = sum(1 for ln in lines if _RE_LOG_LEVEL.search(ln))
    # Structured log: many lines carry both timestamp and log level.
    if ts_count >= 5 and level_count >= 4:
        return True
    # Access log: very high timestamp density (no log-level field).
    if ts_count >= 8 and ts_count / len(lines) > 0.6:
        return True
    # Stack trace dump: many frame lines.
    frame_count = sum(
        1
        for ln in lines
        if _RE_FRAME_JAVA.search(ln) or _RE_FRAME_PYTHON.search(ln) or _RE_FRAME_JS.search(ln)
    )
    return frame_count >= 5
