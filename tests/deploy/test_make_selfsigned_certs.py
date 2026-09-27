"""scripts/deploy/make-selfsigned-certs.sh: a throwaway CA plus a leaf signed by
it, with the SANs clients actually verify (plan 20260806 ⛔8).

Runs the real script with the host's openssl into a tmp dir and reads the result
back with ``openssl x509 -text`` and ``openssl verify``. Skips without openssl
or shellcheck on a laptop and FAILS on CI.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.compose.nginx_support import skip_or_fail

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy" / "make-selfsigned-certs.sh"
OUTPUTS = {"gateway.crt", "gateway.key", "selfsigned-ca.crt"}


@pytest.fixture(autouse=True)
def _openssl() -> None:
    if shutil.which("openssl") is None:
        skip_or_fail("openssl not on PATH")


def _make(*args: str | bytes, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, timeout=60, check=False
    )


def _openssl_run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["openssl", *args], capture_output=True, text=True, check=False)


def _text(cert: Path) -> str:
    result = _openssl_run("x509", "-in", str(cert), "-noout", "-text")
    assert result.returncode == 0, result.stderr
    return result.stdout


def _sans(cert: Path) -> list[str]:
    text = _text(cert)
    line = re.search(r"Subject Alternative Name:.*?\n\s*(.+)", text)
    assert line, text
    return [entry.strip().lower() for entry in line.group(1).split(",")]


def _verify(out: Path, *check: str) -> subprocess.CompletedProcess[str]:
    return _openssl_run(
        "verify", "-CAfile", str(out / "selfsigned-ca.crt"), *check, str(out / "gateway.crt")
    )


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, subprocess.CompletedProcess[str]]:
    if shutil.which("openssl") is None:
        skip_or_fail("openssl not on PATH")
    out = tmp_path_factory.mktemp("certs")
    result = _make("--domain", "Corp.Example", "--out", str(out), "10.1.2.3", "fd00::1", "gw-host")
    assert result.returncode == 0, result.stderr
    return out, result


def test_the_leaf_carries_both_origins_and_every_extra_dns_and_ip_san(made) -> None:
    out, _ = made

    assert _sans(out / "gateway.crt") == [
        "dns:gateway.corp.example",
        "dns:langfuse.corp.example",
        "ip address:10.1.2.3",
        "ip address:fd00:0:0:0:0:0:0:1",
        "dns:gw-host",
    ]


def test_it_writes_exactly_the_leaf_its_key_and_the_ca_never_the_ca_key(made) -> None:
    out, _ = made

    assert {p.name for p in out.iterdir()} == OUTPUTS
    assert stat.S_IMODE((out / "gateway.key").stat().st_mode) == 0o600
    for name in ("gateway.crt", "selfsigned-ca.crt"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o644, name


def test_stdout_is_the_ca_path_and_stderr_says_not_for_production(made) -> None:
    out, result = made

    assert Path(result.stdout.strip()).samefile(out / "selfsigned-ca.crt")
    assert result.stdout.count("\n") == 1
    assert "NOT FOR PRODUCTION" in result.stderr


def test_the_leaf_chains_to_the_ca_and_matches_its_key(made) -> None:
    out, _ = made

    assert _verify(out).returncode == 0
    leaf_key = _openssl_run("pkey", "-in", str(out / "gateway.key"), "-pubout").stdout
    leaf_cert = _openssl_run("x509", "-in", str(out / "gateway.crt"), "-noout", "-pubkey").stdout
    assert leaf_key and leaf_key == leaf_cert


def test_the_ca_can_sign_nothing_below_the_leaf_and_the_leaf_is_a_server_cert(made) -> None:
    out, _ = made
    ca = _text(out / "selfsigned-ca.crt")
    leaf = _text(out / "gateway.crt")

    assert re.search(r"Basic Constraints: critical\s+CA:TRUE, pathlen:0", ca)
    assert re.search(r"Key Usage: critical\s+Certificate Sign, CRL Sign", ca)
    assert re.search(r"Basic Constraints: critical\s+CA:FALSE", leaf)
    assert re.search(r"Extended Key Usage:\s*\n\s*TLS Web Server Authentication\s*\n", leaf)
    assert "throwaway CA (not for production)" in ca


@pytest.mark.parametrize(
    ("check", "ok"),
    [
        (("-verify_hostname", "gateway.corp.example"), True),
        (("-verify_hostname", "langfuse.corp.example"), True),
        (("-verify_hostname", "gw-host"), True),
        (("-verify_ip", "10.1.2.3"), True),
        (("-verify_ip", "fd00::1"), True),
        (("-verify_hostname", "corp.example"), False),
        (("-verify_hostname", "other.corp.example"), False),
        (("-verify_ip", "10.1.2.4"), False),
    ],
    ids=lambda v: str(v) if isinstance(v, bool) else v[1],
)
def test_a_client_verifies_exactly_the_sans(made, check: tuple[str, str], ok: bool) -> None:
    out, _ = made

    result = _verify(out, *check)

    assert (result.returncode == 0) is ok, result.stdout + result.stderr


def test_the_default_out_dir_is_compose_nginx_certs(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "deploy" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)

    result = _make("10.1.2.3", script=script)

    assert result.returncode == 0, result.stderr
    certs = tmp_path / "compose" / "nginx" / "certs"
    assert {p.name for p in certs.iterdir()} == OUTPUTS
    assert Path(result.stdout.strip()).samefile(certs / "selfsigned-ca.crt")


def test_an_existing_output_is_kept_unless_forced(tmp_path: Path) -> None:
    existing = tmp_path / "gateway.key"
    existing.write_text("the operator's real key\n")

    refused = _make("--out", str(tmp_path), "10.1.2.3")

    assert refused.returncode == 1
    assert "--force" in refused.stderr
    assert existing.read_text() == "the operator's real key\n"
    assert {p.name for p in tmp_path.iterdir()} == {"gateway.key"}

    forced = _make("--force", "--out", str(tmp_path), "10.1.2.3")

    assert forced.returncode == 0, forced.stderr
    assert {p.name for p in tmp_path.iterdir()} == OUTPUTS
    assert "PRIVATE KEY" in existing.read_text()
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "no SAN"),
        (("--domain", "localhost"), "--domain"),
        (("--domain", "-bad.example"), "--domain"),
        (("--domain", "a.example;x"), "--domain"),
        (("a;b",), "'a;b'"),
        (("gw host",), "'gw host'"),
        (("a..b",), "'a..b'"),
        (("*.corp.example",), "'*.corp.example'"),
        (("10.1.2.300",), "'10.1.2.300'"),
        (("fd00::1\nDNS:evil",), "IPv6"),
        # Shaped like IPv6 to a character class; the script says so, not openssl.
        ((":",), "':' is not an IPv6 address"),
        (("::::",), "'::::' is not an IPv6 address"),
        (("f:",), "'f:' is not an IPv6 address"),
        (("--days", "0", "10.1.2.3"), "--days"),
        (("--days", "826", "10.1.2.3"), "--days"),
        (("--days", "1x", "10.1.2.3"), "--days"),
        (("--wipe", "10.1.2.3"), "--wipe"),
    ],
    ids=lambda v: v if isinstance(v, str) else "-".join(v) or "empty",
)
def test_a_bad_argument_is_refused_before_anything_is_written(
    tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    out = tmp_path / "out"

    result = _make("--out", str(out), *args)

    assert result.returncode == 1
    assert message in result.stderr
    assert result.stdout == ""
    assert not out.exists()


@pytest.mark.parametrize(
    ("san", "shown"),
    [
        ("a\x1b[31m\nb", "a?[31m?b"),
        # Not UTF-8: a multibyte-locale `tr` used to cut the name at this byte and
        # certify what was left.
        (b"a\xffb", "a?b"),
    ],
    ids=["control-chars", "invalid-utf8"],
)
def test_the_refusal_is_one_printable_line_whatever_the_argument_held(
    tmp_path: Path, san: str | bytes, shown: str
) -> None:
    out = tmp_path / "out"

    result = _make("--out", str(out), san)

    assert result.returncode == 1
    assert result.stderr == (
        f"FATAL: SAN '{shown}' is neither an IP address nor a DNS name (a-z 0-9 . - only)\n"
    )
    assert not out.exists()


def test_the_key_is_installed_after_both_certificates(tmp_path: Path) -> None:
    """A failure between the renames never leaves a new key beside an old
    certificate: here the key's rename fails, and the old key is still there."""
    out = tmp_path / "out"
    assert _make("--out", str(out), "10.1.2.3").returncode == 0
    before = {name: (out / name).read_bytes() for name in OUTPUTS}
    real_mv = shutil.which("mv")
    assert real_mv
    shims = tmp_path / "bin"
    shims.mkdir()
    shim = shims / "mv"
    shim.write_text(
        f'#!/bin/sh\ncase "$*" in *.gateway.key.new*) exit 1 ;; esac\nexec {real_mv} "$@"\n'
    )
    shim.chmod(0o755)
    env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}

    failed = subprocess.run(
        [str(SCRIPT), "--force", "--out", str(out), "10.1.2.3"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )

    assert failed.returncode != 0
    assert (out / "gateway.key").read_bytes() == before["gateway.key"]
    # The staged key is a fresh private key: it must not outlive the run.
    assert sorted(path.name for path in out.iterdir()) == sorted(OUTPUTS)
    for name in ("gateway.crt", "selfsigned-ca.crt"):
        assert (out / name).read_bytes() != before[name], name


def test_help_exits_zero_and_writes_nothing(tmp_path: Path) -> None:
    result = _make("--help", "--out", str(tmp_path / "out"))

    assert result.returncode == 0
    assert "NOT FOR PRODUCTION" in result.stderr
    assert not (tmp_path / "out").exists()


def test_the_script_is_executable_and_shellcheck_clean() -> None:
    assert os.access(SCRIPT, os.X_OK)
    if shutil.which("shellcheck") is None:
        skip_or_fail("shellcheck not on PATH")
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
