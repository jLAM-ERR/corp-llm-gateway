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

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not found on PATH")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install.sh"

REALM_PATH = "/realms/dev"
DEVICE_PATH = f"{REALM_PATH}/protocol/openid-connect/auth/device"
TOKEN_PATH = f"{REALM_PATH}/protocol/openid-connect/token"
ISSUE_PATH = "/internal/issue-token"
MESSAGES_PATH = "/v1/messages"
CLIENT_ID = "corp-gateway-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


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
    device_code: str = field(default_factory=lambda: "dc-" + secrets.token_urlsafe(16))
    access_token: str = field(default_factory=lambda: "kc-access-" + secrets.token_urlsafe(24))
    corp_token: str = field(default_factory=lambda: "ct_" + secrets.token_urlsafe(32))
    interval: int | None = 1
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

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with state.lock:
                state.requests.append(
                    Recorded(
                        method="POST",
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                        at=time.monotonic(),
                    )
                )
                token_round = len(state.of(TOKEN_PATH)) - 1

            if self.path == DEVICE_PATH:
                payload: dict[str, Any] = {
                    "device_code": state.device_code,
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://kc.example.test/device",
                    "verification_uri_complete": "https://kc.example.test/device?user_code=ABCD-EFGH",
                    "expires_in": 120,
                }
                if state.interval is not None:
                    payload["interval"] = state.interval
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
                else:
                    self._reply(400, {"error": step, "error_description": "stub"})
            elif self.path == ISSUE_PATH:
                if state.issue_status == 200:
                    self._reply(
                        200,
                        {"corp_token": state.corp_token, "expires_at": "2026-10-27T00:00:00Z"},
                    )
                else:
                    self._reply(state.issue_status, {"error": "E_ISSUE_UNAUTHORIZED"})
            elif self.path == MESSAGES_PATH:
                self._reply(200, {"content": [{"type": "text", "text": "ok"}]})
            else:
                self._reply(404, {"error": "not found"})

    return Handler


@pytest.fixture
def stub() -> Any:
    state = StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
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

    @property
    def token_file(self) -> Path:
        return self.home / ".corp-llm-gateway" / "token"

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
        "CORP_GATEWAY_URL": stub.base_url,  # type: ignore[attr-defined]
        "KEYCLOAK_ISSUER": stub.base_url + REALM_PATH,  # type: ignore[attr-defined]
        "KEYCLOAK_CLIENT_ID": CLIENT_ID,
    }
    env.update(extra_env or {})
    for key in drop:
        env.pop(key, None)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    curl_argv = argv_log.read_text() if argv_log.exists() else ""
    return Run(proc=proc, home=home, curl_argv=curl_argv)


def _assert_no_secrets_leaked(run: Run, stub: StubState) -> None:
    for secret in (stub.access_token, stub.corp_token, stub.device_code):
        assert secret not in run.output
        assert secret not in run.curl_argv


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

    assert "https://kc.example.test/device?user_code=ABCD-EFGH" in run.proc.stdout

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
    assert issue.headers.get("content-length", "0") == "0"


def test_slow_down_adds_five_seconds_to_the_interval(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["slow_down", "ok"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode == 0, run.output
    first, second = stub.of(TOKEN_PATH)
    (device,) = stub.of(DEVICE_PATH)
    assert first.at - device.at < 4
    assert second.at - first.at >= 5.8
    assert run.token_file.read_text().strip() == stub.corp_token


@pytest.mark.parametrize(
    ("error", "phrase"),
    [("expired_token", "expired"), ("access_denied", "denied")],
)
def test_terminal_device_errors_abort_without_a_token(
    tmp_path: Path, stub: StubState, error: str, phrase: str
) -> None:
    stub.token_script = ["authorization_pending", error]

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert phrase in run.proc.stderr.lower()
    assert len(stub.of(TOKEN_PATH)) == 2
    assert stub.of(ISSUE_PATH) == []
    assert not run.token_file.exists()
    _assert_no_secrets_leaked(run, stub)


def test_unknown_token_error_aborts(tmp_path: Path, stub: StubState) -> None:
    stub.token_script = ["invalid_client"]

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert "invalid_client" in run.proc.stderr
    assert not run.token_file.exists()


def test_issue_token_401_aborts_without_a_token(tmp_path: Path, stub: StubState) -> None:
    stub.issue_status = 401

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert "401" in run.proc.stderr
    assert not run.token_file.exists()
    _assert_no_secrets_leaked(run, stub)


def test_failed_issuance_keeps_the_existing_token(tmp_path: Path, stub: StubState) -> None:
    token_dir = tmp_path / "home" / ".corp-llm-gateway"
    token_dir.mkdir(parents=True)
    (token_dir / "token").write_text("ct_previous\n")
    stub.issue_status = 403

    run = _run(tmp_path, stub)

    assert run.proc.returncode != 0
    assert run.token_file.read_text() == "ct_previous\n"


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


def test_the_old_device_url_variable_is_gone() -> None:
    assert "KEYCLOAK_DEVICE_URL" not in SCRIPT.read_text()
