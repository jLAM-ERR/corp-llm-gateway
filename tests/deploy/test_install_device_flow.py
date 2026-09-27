"""End-to-end runs of scripts/install.sh against a stub Keycloak and a stub gateway.

The stub speaks RFC 8628 (device authorization grant) the way Keycloak does and
records every request with a timestamp, so polling cadence is measured, not
inferred. `curl` is wrapped by a PATH shim that logs its argv, so the "secrets
never reach argv" claims are checked against what was actually executed.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

REQUIRED_COMMANDS = ("bash", "curl", "jq")

pytestmark = pytest.mark.skipif(
    any(shutil.which(cmd) is None for cmd in REQUIRED_COMMANDS),
    reason=f"install.sh needs {', '.join(REQUIRED_COMMANDS)} on PATH",
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install.sh"

REALM_PATH = "/realms/dev"
DEVICE_PATH = f"{REALM_PATH}/protocol/openid-connect/auth/device"
TOKEN_PATH = f"{REALM_PATH}/protocol/openid-connect/token"
ISSUE_PATH = "/internal/issue-token"
MESSAGES_PATH = "/v1/messages"
CLIENT_ID = "corp-gateway-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CANARY = "CANARY_BODY_7f3a"
VERIFICATION_URI = "https://kc.example.test/device"
USER_CODE = "ABCD-EFGH"
ISSUE_ERRORS = {
    401: "E_ISSUE_UNAUTHORIZED",
    403: "E_ISSUE_FORBIDDEN",
    429: "E_ISSUE_RATE",
    503: "E_ISSUE_UNAVAILABLE",
}


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    at: float

    def form(self) -> dict[str, list[str]]:
        return parse_qs(self.body.decode())


@dataclass
class StubState:
    base_url: str = ""
    device_code: str = field(default_factory=lambda: "dc-" + secrets.token_urlsafe(16))
    access_token: str = field(default_factory=lambda: "kc-access-" + secrets.token_urlsafe(24))
    corp_token: str = field(default_factory=lambda: "ct_" + secrets.token_urlsafe(32))
    expires_at: str = "2026-10-27T00:00:00Z"
    interval: int | None = 1
    expires_in: int = 120
    device_status: int = 200
    device_omit: tuple[str, ...] = ()
    device_extra: dict[str, Any] = field(default_factory=dict)
    # Per poll: "ok", "drop" (close without a reply), "502" (HTML 502),
    # "html" (HTML 200), anything else is an RFC 8628 error code (JSON 400).
    token_script: list[str] = field(default_factory=lambda: ["ok"])
    issue_status: int = 200
    requests: list[Recorded] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def of(self, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.path == path]


def _make_handler(state: StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _html(self, status: int) -> None:
            self._send(status, f"<html><body>{CANARY}</body></html>".encode(), "text/html")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with state.lock:
                state.requests.append(
                    Recorded(
                        method=self.command,
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                        at=time.monotonic(),
                    )
                )
                token_round = len(state.of(TOKEN_PATH)) - 1

            if self.path == DEVICE_PATH:
                if state.device_status != 200:
                    self._reply(
                        state.device_status,
                        {"error": "unauthorized_client", "error_description": CANARY},
                    )
                    return
                payload: dict[str, Any] = {
                    "device_code": state.device_code,
                    "user_code": USER_CODE,
                    "verification_uri": VERIFICATION_URI,
                    "verification_uri_complete": f"{VERIFICATION_URI}?user_code={USER_CODE}",
                    "expires_in": state.expires_in,
                }
                if state.interval is not None:
                    payload["interval"] = state.interval
                for key in state.device_omit:
                    payload.pop(key, None)
                payload.update(state.device_extra)
                self._reply(200, payload)
            elif self.path == TOKEN_PATH:
                step = state.token_script[min(token_round, len(state.token_script) - 1)]
                if step == "ok":
                    self._reply(
                        200,
                        {
                            "access_token": state.access_token,
                            "token_type": "Bearer",
                            "expires_in": 300,
                        },
                    )
                elif step == "drop":
                    self.close_connection = True
                elif step == "502":
                    self._html(502)
                elif step == "html":
                    self._html(200)
                else:
                    self._reply(400, {"error": step, "error_description": CANARY})
            elif self.path == ISSUE_PATH:
                if state.issue_status == 200:
                    self._reply(
                        200,
                        {"corp_token": state.corp_token, "expires_at": state.expires_at},
                    )
                else:
                    self._reply(
                        state.issue_status,
                        {
                            "error": ISSUE_ERRORS.get(state.issue_status, "E_ISSUE"),
                            "message": CANARY,
                            "detail": CANARY,
                        },
                    )
            elif self.path == MESSAGES_PATH:
                self._reply(200, {"content": [{"type": "text", "text": "ok"}]})
            else:
                self._reply(404, {"error": "not found", "detail": CANARY})

        def do_PUT(self) -> None:
            self.do_POST()

    return Handler


@pytest.fixture
def stub() -> Any:
    state = StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Run:
    proc: subprocess.CompletedProcess[str]
    home: Path
    curl_argv: str
    elapsed: float

    @property
    def token_file(self) -> Path:
        return self.home / ".corp-llm-gateway" / "token"

    @property
    def rc_file(self) -> Path:
        return self.home / ".bashrc"

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr


def _curl_shim(bin_dir: Path, log: Path) -> None:
    real_curl = shutil.which("curl")
    assert real_curl is not None, "curl is required to run install.sh"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "curl"
    shim.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\nexec "{real_curl}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path,
    stub: StubState,
    *,
    extra_env: dict[str, str] | None = None,
    drop: tuple[str, ...] = (),
) -> Run:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    argv_log = tmp_path / "curl-argv.log"
    bin_dir = tmp_path / "bin"
    _curl_shim(bin_dir, argv_log)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "SHELL": "/bin/bash",
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
        "CORP_GATEWAY_URL": stub.base_url,
        "KEYCLOAK_ISSUER": stub.base_url + REALM_PATH,
        "KEYCLOAK_CLIENT_ID": CLIENT_ID,
    }
    env.update(extra_env or {})
    for key in drop:
        env.pop(key, None)
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    elapsed = time.monotonic() - started
    curl_argv = argv_log.read_text() if argv_log.exists() else ""
    return Run(proc=proc, home=home, curl_argv=curl_argv, elapsed=elapsed)


def _assert_no_secrets_leaked(run: Run, stub: StubState) -> None:
    for secret in (stub.access_token, stub.corp_token, stub.device_code, CANARY):
        assert secret not in run.output
        assert secret not in run.curl_argv


def _assert_aborted_without_a_token(run: Run, stub: StubState) -> None:
    assert run.proc.returncode != 0
    assert "missing required command" not in run.proc.stderr
    assert stub.of(ISSUE_PATH) == []
    assert not run.token_file.exists()
    _assert_no_secrets_leaked(run, stub)


def test_happy_path_polls_through_pending_and_writes_token(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["authorization_pending", "authorization_pending", "ok"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    device = stub.of(DEVICE_PATH)
    assert len(device) == 1
    assert device[0].form() == {"client_id": [CLIENT_ID]}

    polls = stub.of(TOKEN_PATH)
    assert len(polls) == 3
    for poll in polls:
        assert poll.form() == {
            "grant_type": [DEVICE_GRANT],
            "device_code": [stub.device_code],
            "client_id": [CLIENT_ID],
        }
    gaps = [b.at - a.at for a, b in zip([device[0], *polls], polls, strict=False)]
    assert all(gap >= 0.8 for gap in gaps), gaps

    assert f"{VERIFICATION_URI}?user_code={USER_CODE}" in run.proc.stdout
    assert f"expires_at: {stub.expires_at}" in run.output

    assert run.token_file.read_text().strip() == stub.corp_token
    assert stat.S_IMODE(run.token_file.stat().st_mode) == 0o600
    _assert_no_secrets_leaked(run, stub)


def test_issue_token_request_carries_exact_bearer_and_no_body(
    tmp_path: Path, stub: StubState
) -> None:
    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    (issue,) = stub.of(ISSUE_PATH)
    assert issue.method == "POST"
    assert issue.headers["authorization"] == f"Bearer {stub.access_token}"
    assert issue.body == b""
    assert "content-type" not in issue.headers
    assert issue.headers["content-length"] == "0"


def test_slow_down_adds_five_seconds_to_the_interval(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["slow_down", "ok"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    first, second = stub.of(TOKEN_PATH)
    (device,) = stub.of(DEVICE_PATH)
    normal_gap = first.at - device.at
    slowed_gap = second.at - first.at
    assert slowed_gap - normal_gap >= 4.8, (normal_gap, slowed_gap)
    assert run.token_file.read_text().strip() == stub.corp_token
    _assert_no_secrets_leaked(run, stub)


@pytest.mark.parametrize(
    ("error", "phrase"),
    [("expired_token", "expired"), ("access_denied", "denied")],
)
def test_terminal_device_errors_abort_without_a_token(
    tmp_path: Path, stub: StubState, error: str, phrase: str
) -> None:
    stub.token_script = ["authorization_pending", error]

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert phrase in run.proc.stderr.lower()
    assert len(stub.of(TOKEN_PATH)) == 2


def test_unknown_token_error_aborts(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["invalid_client"]

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "invalid_client" in run.proc.stderr
    assert len(stub.of(TOKEN_PATH)) == 1


@pytest.mark.parametrize("transient", ["502", "drop", "html"])
def test_a_transient_poll_failure_is_retried(
    tmp_path: Path, stub: StubState, transient: str
) -> None:
    stub.token_script = [transient, "authorization_pending", "ok"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert len(stub.of(TOKEN_PATH)) == 3
    assert run.token_file.read_text().strip() == stub.corp_token
    _assert_no_secrets_leaked(run, stub)


def test_five_transient_failures_in_a_row_are_tolerated_and_the_count_resets(
    tmp_path: Path, stub: StubState
) -> None:
    stub.token_script = ["502"] * 5 + ["authorization_pending"] + ["502"] * 5 + ["ok"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert len(stub.of(TOKEN_PATH)) == 12
    assert run.token_file.read_text().strip() == stub.corp_token
    _assert_no_secrets_leaked(run, stub)


def test_six_transient_failures_in_a_row_abort(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["502"] * 6 + ["ok"]

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "HTTP 502" in run.proc.stderr
    assert len(stub.of(TOKEN_PATH)) == 6


def test_the_device_code_deadline_ends_the_wait(tmp_path: Path, stub: StubState) -> None:
    stub.expires_in = 2
    stub.token_script = ["authorization_pending"]

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "expired" in run.proc.stderr.lower()
    assert run.elapsed < 5, run.elapsed


def test_device_request_refused_aborts(tmp_path: Path, stub: StubState) -> None:
    stub.device_status = 401

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "HTTP 401" in run.proc.stderr
    assert "unauthorized_client" in run.proc.stderr
    assert stub.of(TOKEN_PATH) == []


@pytest.mark.parametrize(
    "omit",
    [("device_code",), ("verification_uri_complete", "verification_uri")],
)
def test_incomplete_device_response_aborts(
    tmp_path: Path, stub: StubState, omit: tuple[str, ...]
) -> None:
    stub.device_omit = omit

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "incomplete" in run.proc.stderr
    assert stub.of(TOKEN_PATH) == []


def test_verification_uri_and_user_code_are_shown_without_a_complete_uri(
    tmp_path: Path, stub: StubState
) -> None:
    stub.device_omit = ("verification_uri_complete",)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert VERIFICATION_URI in run.proc.stdout
    assert f"enter the code: {USER_CODE}" in run.proc.stdout
    assert run.token_file.read_text().strip() == stub.corp_token


def test_a_trailing_slash_on_the_issuer_is_dropped(tmp_path: Path, stub: StubState) -> None:
    run = _run(tmp_path, stub, extra_env={"KEYCLOAK_ISSUER": stub.base_url + REALM_PATH + "/"})

    assert run.proc.returncode == 0, run.output
    assert len(stub.of(DEVICE_PATH)) == 1
    assert len(stub.of(TOKEN_PATH)) == 1


@pytest.mark.parametrize(
    "issuer",
    [
        "http://keycloak.corp.lan/realms/dev",
        "http://127.0.0.1.evil.test/realms/dev",
        "http://localhost@evil.test/realms/dev",
        "ftp://keycloak.corp.lan/realms/dev",
    ],
)
def test_a_plain_http_issuer_is_refused_before_any_request(
    tmp_path: Path, stub: StubState, issuer: str
) -> None:
    run = _run(tmp_path, stub, extra_env={"KEYCLOAK_ISSUER": issuer})

    assert run.proc.returncode == 1
    assert "https://" in run.proc.stderr
    assert stub.requests == []
    assert run.curl_argv == ""
    assert not run.token_file.exists()


def test_an_access_token_of_the_wrong_shape_is_never_sent(tmp_path: Path, stub: StubState) -> None:
    stub.access_token = "kc-" + secrets.token_urlsafe(8) + '"\nurl = "http://evil.test'

    run = _run(tmp_path, stub)

    _assert_aborted_without_a_token(run, stub)
    assert "access token" in run.proc.stderr


def test_a_corp_token_of_the_wrong_shape_is_not_written(tmp_path: Path, stub: StubState) -> None:
    stub.corp_token = "ct_" + secrets.token_urlsafe(8) + " rm -rf ~"

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert "usable corp token" in run.proc.stderr
    assert len(stub.of(ISSUE_PATH)) == 1
    assert not run.token_file.exists()
    _assert_no_secrets_leaked(run, stub)


def test_an_unparseable_expires_at_is_not_echoed(tmp_path: Path, stub: StubState) -> None:
    stub.expires_at = f"{CANARY} \x1b[31m"

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert "expires_at: (unparseable)" in run.output
    assert "\x1b[31m" not in run.output
    assert run.token_file.read_text().strip() == stub.corp_token
    _assert_no_secrets_leaked(run, stub)


@pytest.mark.parametrize("status", sorted(ISSUE_ERRORS))
def test_issue_token_refusal_aborts_without_a_token(
    tmp_path: Path, stub: StubState, status: int
) -> None:
    stub.issue_status = status

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert f"HTTP {status}, {ISSUE_ERRORS[status]}" in run.proc.stderr
    assert not run.token_file.exists()
    _assert_no_secrets_leaked(run, stub)


def test_failed_issuance_keeps_the_existing_token(tmp_path: Path, stub: StubState) -> None:
    token_dir = tmp_path / "home" / ".corp-llm-gateway"
    token_dir.mkdir(parents=True)
    (token_dir / "token").write_text("ct_previous\n")
    stub.issue_status = 403

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert "the gateway refused to issue a corp token (HTTP 403" in run.proc.stderr
    assert "missing required command" not in run.proc.stderr
    assert run.token_file.read_text() == "ct_previous\n"


def test_the_token_file_location_comes_from_the_environment(
    tmp_path: Path, stub: StubState
) -> None:
    custom = tmp_path / "elsewhere" / "corp-token"

    run = _run(tmp_path, stub, extra_env={"CORP_GATEWAY_TOKEN_FILE": str(custom)})

    assert run.proc.returncode == 0, run.output
    assert custom.read_text().strip() == stub.corp_token
    assert stat.S_IMODE(custom.stat().st_mode) == 0o600
    assert not run.token_file.exists()
    assert f"CORP_GATEWAY_TOKEN_FILE='{custom}'" in run.rc_file.read_text()


def test_a_token_file_path_with_a_quote_is_refused(tmp_path: Path, stub: StubState) -> None:
    run = _run(
        tmp_path, stub, extra_env={"CORP_GATEWAY_TOKEN_FILE": str(tmp_path / "it's" / "token")}
    )

    assert run.proc.returncode == 1
    assert "CORP_GATEWAY_TOKEN_FILE" in run.proc.stderr
    assert stub.requests == []


@pytest.mark.parametrize("relative", [True, False], ids=["relative-link", "absolute-link"])
def test_a_symlinked_token_file_is_written_through(
    tmp_path: Path, stub: StubState, relative: bool
) -> None:
    token_dir = tmp_path / "home" / ".corp-llm-gateway"
    token_dir.mkdir(parents=True)
    vault = tmp_path / "home" / "vault"
    vault.mkdir()
    target = vault / "corp-token"
    target.write_text("ct_previous\n")
    link = token_dir / "token"
    link.symlink_to(Path("..") / "vault" / "corp-token" if relative else target)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert link.is_symlink()
    assert os.readlink(link) == str(Path("..") / "vault" / "corp-token" if relative else target)
    assert target.read_text().strip() == stub.corp_token
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in vault.iterdir()) == ["corp-token"]


def test_missing_interval_defaults_to_five_seconds(tmp_path: Path, stub: StubState) -> None:
    stub.interval = None

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    (device,) = stub.of(DEVICE_PATH)
    (poll,) = stub.of(TOKEN_PATH)
    assert poll.at - device.at >= 4.8


def test_smoke_is_skipped_with_a_message_when_auth_token_unset(
    tmp_path: Path, stub: StubState
) -> None:
    run = _run(tmp_path, stub, drop=("ANTHROPIC_AUTH_TOKEN",))

    assert run.proc.returncode == 0, run.output
    assert "skipping smoke test" in run.output
    assert "ANTHROPIC_AUTH_TOKEN" in run.output
    assert stub.of(MESSAGES_PATH) == []


def test_smoke_uses_the_subscription_token_and_corp_token(tmp_path: Path, stub: StubState) -> None:
    subscription = "sk-ant-oat-" + secrets.token_urlsafe(16)

    run = _run(tmp_path, stub, extra_env={"ANTHROPIC_AUTH_TOKEN": subscription})

    assert run.proc.returncode == 0, run.output
    (smoke,) = stub.of(MESSAGES_PATH)
    assert smoke.headers["authorization"] == f"Bearer {subscription}"
    assert smoke.headers["x-corp-auth"] == stub.corp_token
    json.loads(smoke.body)
    assert subscription not in run.output
    assert subscription not in run.curl_argv
    _assert_no_secrets_leaked(run, stub)


def test_client_id_is_required_with_an_issuer(tmp_path: Path, stub: StubState) -> None:
    run = _run(tmp_path, stub, drop=("KEYCLOAK_CLIENT_ID",))

    assert run.proc.returncode != 0
    assert "KEYCLOAK_CLIENT_ID" in run.proc.stderr
    assert stub.requests == []
    assert not run.token_file.exists()


def test_without_an_issuer_no_login_runs_and_no_token_is_invented(
    tmp_path: Path, stub: StubState
) -> None:
    run = _run(tmp_path, stub, drop=("KEYCLOAK_ISSUER", "KEYCLOAK_CLIENT_ID"))

    assert run.proc.returncode == 0, run.output
    assert "KEYCLOAK_ISSUER" in run.output
    assert stub.requests == []
    assert not run.token_file.exists()
    assert "corp-llm-gateway" in run.rc_file.read_text()


def test_the_old_device_url_variable_is_gone() -> None:
    assert "KEYCLOAK_DEVICE_URL" not in SCRIPT.read_text()


def _sleep_shim(tmp_path: Path, *, real_seconds: float = 0.0) -> Path:
    """Replace `sleep` for install.sh: record each requested interval, sleep
    ``real_seconds`` instead. Returns the log of requested intervals."""
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "sleep.log"
    shim = bin_dir / "sleep"
    shim.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$1" >> "{log}"\nexec "{real_sleep}" {real_seconds}\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return log


def _sleeps(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def test_device_response_text_is_printed_never_evaluated(tmp_path: Path, stub: StubState) -> None:
    marker = tmp_path / "evaluated"
    hostile = f"$(touch {marker})`touch {marker}`;touch {marker}'\"%s%n\\x41"
    uri = f"https://kc.example.test/device?user_code={hostile}"
    code = f"WX%sYZ$(touch {marker})"
    stub.device_extra = {"verification_uri_complete": uri, "user_code": code}
    _sleep_shim(tmp_path)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert not marker.exists()
    assert uri in run.proc.stdout
    assert code in run.proc.stdout
    assert run.token_file.read_text().strip() == stub.corp_token


@pytest.mark.parametrize(
    ("interval", "slept"),
    [("3", "3"), ("abc", "5"), (0, "5"), (-3, "5"), (1.5, "5"), ("", "5"), ([2], "5")],
    ids=["numeric-string", "word", "zero", "negative", "fraction", "empty", "list"],
)
def test_the_device_interval_is_used_only_when_it_is_a_positive_integer(
    tmp_path: Path, stub: StubState, interval: Any, slept: str
) -> None:
    stub.device_extra = {"interval": interval}
    log = _sleep_shim(tmp_path)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert _sleeps(log) == [slept]


def test_repeated_slow_down_adds_five_seconds_each_time(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["slow_down", "authorization_pending", "slow_down", "slow_down", "ok"]
    log = _sleep_shim(tmp_path)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert _sleeps(log) == ["1", "6", "6", "11", "16"]
    assert run.token_file.read_text().strip() == stub.corp_token
    _assert_no_secrets_leaked(run, stub)


def test_slow_down_forever_still_ends_at_the_device_code_deadline(
    tmp_path: Path, stub: StubState
) -> None:
    stub.expires_in = 2
    stub.token_script = ["slow_down"]
    log = _sleep_shim(tmp_path, real_seconds=0.7)

    run = _run(tmp_path, stub)

    assert "expired" in run.proc.stderr
    _assert_aborted_without_a_token(run, stub)
    polls = len(stub.of(TOKEN_PATH))
    assert 1 <= polls <= 5
    assert _sleeps(log) == [str(1 + 5 * i) for i in range(polls)]


def test_a_device_code_carrying_curl_config_lines_injects_nothing(
    tmp_path: Path, stub: StubState
) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE-KEY-CANARY-51d0\n")
    stub.device_code = (
        f"dc-x\nupload-file = {secret}\nurl = {stub.base_url}/exfil\n"
        f'header = "X-Injected: yes"\noutput = {tmp_path / "written"}'
    )
    _sleep_shim(tmp_path)

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert [r.path for r in stub.requests if not r.path.startswith(REALM_PATH)] == []
    assert all("x-injected" not in r.headers for r in stub.requests)
    assert all(b"PRIVATE-KEY-CANARY" not in r.body for r in stub.requests)
    assert not (tmp_path / "written").exists()
    assert not run.token_file.exists()


def test_a_home_with_spaces_installs_and_the_rc_file_loads_the_token(
    tmp_path: Path, stub: StubState
) -> None:
    home = tmp_path / "my home" / "dev user"
    home.mkdir(parents=True)

    run = _run(tmp_path, stub, extra_env={"HOME": str(home)})

    assert run.proc.returncode == 0, run.output
    token_file = home / ".corp-llm-gateway" / "token"
    assert token_file.read_text().strip() == stub.corp_token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    loaded = subprocess.run(
        ["bash", "-c", 'source "$HOME/.bashrc"; printf %s "$ANTHROPIC_CUSTOM_HEADERS"'],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout == f"X-Corp-Auth: {stub.corp_token}"


def test_a_preexisting_install_dir_is_made_private(tmp_path: Path, stub: StubState) -> None:
    install_dir = tmp_path / "home" / ".corp-llm-gateway"
    install_dir.mkdir(parents=True)
    install_dir.chmod(0o755)

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    assert stat.S_IMODE(install_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(run.token_file.stat().st_mode) == 0o600
