"""Render asserts for the litellm callback shim wiring (docs/plans/20260713-helm-callback-shim.md).

These tests validate SHAPE only: that `helm template` renders a ConfigMap
carrying both `config.yaml` and `bootstrap.py`, and a Deployment volume that
projects both under `/etc/litellm`. The runtime guarantee that k8s actually
lays the projected files out this way is the configMap.items contract, not
something these tests can exercise without a live cluster.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not found on PATH"
)

CHART_DIR = Path(__file__).resolve().parents[2] / "helm" / "corp-llm-gateway"


@pytest.fixture(scope="session")
def rendered_docs() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", "gw", str(CHART_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed (exit {result.returncode}):\n{result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _first_of_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == kind:
            return doc
    pytest.fail(f"no rendered doc with kind={kind!r}")


def _litellm_configmap(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-litellm"):
            return doc
    pytest.fail("no ConfigMap with a name ending '-litellm' in rendered output")


def _litellm_config_volume(deployment: dict[str, Any]) -> dict[str, Any]:
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    for volume in volumes:
        if volume["name"] == "litellm-config":
            return volume
    pytest.fail("no 'litellm-config' volume on the Deployment pod spec")


def test_litellm_configmap_carries_config_and_shim(
    rendered_docs: list[dict[str, Any]],
) -> None:
    configmap = _litellm_configmap(rendered_docs)
    data = configmap["data"]

    assert set(data) == {"config.yaml", "bootstrap.py"}

    config = yaml.safe_load(data["config.yaml"])
    assert config["litellm_settings"]["callbacks"] == ["corp_llm_gateway.bootstrap.guardrail"]

    shim_src = data["bootstrap.py"]
    assert "from corp_llm_gateway.bootstrap import guardrail" in shim_src
    compile(shim_src, "<shim>", "exec")  # raises on indentation/templating artifacts


def test_deployment_projects_both_files_and_mounts_litellm_config(
    rendered_docs: list[dict[str, Any]],
) -> None:
    deployment = _first_of_kind(rendered_docs, "Deployment")
    volume = _litellm_config_volume(deployment)

    items = {item["key"]: item["path"] for item in volume["configMap"]["items"]}
    assert items == {
        "config.yaml": "config.yaml",
        "bootstrap.py": "corp_llm_gateway/bootstrap.py",
    }

    containers = deployment["spec"]["template"]["spec"]["containers"]
    litellm_container = next(c for c in containers if c["name"] == "litellm")
    mount = next(m for m in litellm_container["volumeMounts"] if m["name"] == "litellm-config")
    assert mount["mountPath"] == "/etc/litellm"


def _env_of(deployment: dict[str, Any], container: str) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    found = next(c for c in containers if c["name"] == container)
    return {entry["name"]: entry.get("value") for entry in found["env"]}


def test_the_gateway_strips_inbound_wire_headers_by_default(
    rendered_docs: list[dict[str, Any]],
) -> None:
    # Without it the client's Content-Length is forwarded with litellm's own,
    # longer, sanitized body and the provider truncates the request. The chart
    # never set it, so every Helm deploy ran with it off.
    deployment = _first_of_kind(rendered_docs, "Deployment")

    assert _env_of(deployment, "litellm")["CORP_LLM_STRIP_INBOUND_HEADERS"] == "1"


def test_callback_dotted_path_matches_items_projection_path(
    rendered_docs: list[dict[str, Any]],
) -> None:
    configmap = _litellm_configmap(rendered_docs)
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    dotted = config["litellm_settings"]["callbacks"][0]
    module_path, _, _attr = dotted.rpartition(".")
    expected_shim_path = module_path.replace(".", "/") + ".py"

    deployment = _first_of_kind(rendered_docs, "Deployment")
    volume = _litellm_config_volume(deployment)
    projected_paths = {item["path"] for item in volume["configMap"]["items"]}

    assert expected_shim_path in projected_paths


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "gw", str(CHART_DIR)]
    for item in sets:
        args += ["--set", item]
    return subprocess.run(args, capture_output=True, text=True)


def _egress_rules(*sets: str) -> list[dict[str, Any]]:
    result = _render("networkPolicy.enabled=true", *sets)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    return _first_of_kind(docs, "NetworkPolicy")["spec"]["egress"]


def _ip_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rule for rule in rules if "ipBlock" in rule["to"][0]]


def test_keycloak_egress_is_off_by_default() -> None:
    assert _ip_rules(_egress_rules()) == []


def test_keycloak_egress_renders_one_rule_for_the_configured_cidr_and_port() -> None:
    rules = _egress_rules(
        "networkPolicy.keycloak.enabled=true",
        "networkPolicy.keycloak.cidr=10.20.30.40/32",
        "networkPolicy.keycloak.port=8443",
    )

    assert _ip_rules(rules) == [
        {
            "to": [{"ipBlock": {"cidr": "10.20.30.40/32"}}],
            "ports": [{"protocol": "TCP", "port": 8443}],
        }
    ]


def test_keycloak_egress_port_defaults_to_443() -> None:
    rules = _egress_rules(
        "networkPolicy.keycloak.enabled=true", "networkPolicy.keycloak.cidr=10.20.30.40/32"
    )

    assert _ip_rules(rules)[0]["ports"] == [{"protocol": "TCP", "port": 443}]


def test_keycloak_egress_without_a_cidr_fails_the_render() -> None:
    result = _render("networkPolicy.enabled=true", "networkPolicy.keycloak.enabled=true")

    assert result.returncode != 0
    assert "networkPolicy.keycloak.cidr is required" in result.stderr


def test_keycloak_egress_needs_the_network_policy_enabled() -> None:
    result = _render(
        "networkPolicy.keycloak.enabled=true", "networkPolicy.keycloak.cidr=10.20.30.40/32"
    )

    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert not any(doc.get("kind") == "NetworkPolicy" for doc in docs)


def test_keycloak_egress_knobs_sit_under_network_policy() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())

    assert "keycloak" not in values
    assert values["networkPolicy"]["keycloak"] == {"enabled": False, "cidr": "", "port": 443}


# ── the in-flight cap and litellm's disconnect watch ─────────────────────────


def test_litellm_watches_for_client_disconnects(rendered_docs: list[dict[str, Any]]) -> None:
    config = yaml.safe_load(_litellm_configmap(rendered_docs)["data"]["config.yaml"])

    assert config["general_settings"]["cancel_on_disconnect"] is True


def test_the_in_flight_cap_reaches_the_gateway_and_the_config_check(
    rendered_docs: list[dict[str, Any]],
) -> None:
    deployment = _first_of_kind(rendered_docs, "Deployment")
    init = deployment["spec"]["template"]["spec"]["initContainers"]

    assert _env_of(deployment, "litellm")["CORP_LLM_MAX_INFLIGHT"] == "64"
    assert all(
        {e["name"]: e.get("value") for e in c.get("env", [])}.get("CORP_LLM_MAX_INFLIGHT") == "64"
        for c in init
    )


_CAPACITY_DEFAULTS = {
    "CORP_LLM_MAX_INFLIGHT": "64",
    "CORP_LLM_CANCEL_GRACE_SECONDS": "5",
    "CORP_LLM_BODY_READ_SECONDS": "30",
    # Empty follows the cap: 4 x CORP_LLM_MAX_INFLIGHT.
    "CORP_LLM_MAX_DRAINING": "",
    "CORP_LLM_MAX_DRAINING_BYTES": "536870912",
}


def _init_env(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    init = deployment["spec"]["template"]["spec"]["initContainers"]
    return [{e["name"]: e.get("value") for e in c.get("env", [])} for c in init]


@pytest.mark.parametrize(("key", "value"), sorted(_CAPACITY_DEFAULTS.items()))
def test_every_capacity_key_reaches_the_gateway_and_the_config_check(
    rendered_docs: list[dict[str, Any]], key: str, value: str
) -> None:
    deployment = _first_of_kind(rendered_docs, "Deployment")

    assert _env_of(deployment, "litellm")[key] == value
    assert all(env.get(key) == value for env in _init_env(deployment))


def test_the_rendered_capacity_defaults_pass_the_boot_check(
    rendered_docs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_llm_gateway import settings

    env = _env_of(_first_of_kind(rendered_docs, "Deployment"), "litellm")
    for key in _CAPACITY_DEFAULTS:
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("CORP_ENV", "prod")

    capacity = settings.capacity()

    assert capacity.max_inflight == 64
    assert capacity.max_draining == 4 * 64
    assert capacity.max_draining_bytes == 512 * 1024 * 1024


def test_raising_the_cap_alone_keeps_the_draining_cap_valid() -> None:
    # The draining cap follows the in-flight cap, so an operator who raises only
    # CORP_LLM_MAX_INFLIGHT past 4 x 64 does not trip the boot check.
    result = _render("config.CORP_LLM_MAX_INFLIGHT=300")
    assert result.returncode == 0, result.stderr
    deployment = _first_of_kind(
        [doc for doc in yaml.safe_load_all(result.stdout) if doc], "Deployment"
    )

    env = _env_of(deployment, "litellm")
    assert env["CORP_LLM_MAX_INFLIGHT"] == "300"
    assert env["CORP_LLM_MAX_DRAINING"] == ""


def test_the_render_never_sets_litellms_dead_concurrency_knobs() -> None:
    result = _render()
    assert result.returncode == 0, result.stderr

    assert "global_max_parallel_requests" not in result.stdout
    assert "LEGACY_MULTI_INSTANCE_RATE_LIMITING" not in result.stdout


# ── developer token issuance: issuance.* → /etc/corp-llm-gateway/config.toml ──

CONFIG_TOML_PATH = "/etc/corp-llm-gateway/config.toml"

_ISSUANCE_ON: dict[str, Any] = {
    "enabled": True,
    "issuer": "https://keycloak.corp.lan/realms/dev",
    "audience": "corp-gateway-issuance",
    "clientId": "corp-gateway-cli",
    # Not sorted on purpose: the rendered table must keep this order.
    "teamMap": [
        {"group": "/devs/payments", "team": "payments"},
        {"group": "/devs/core", "team": "core"},
        {"group": "/devs/alpha", "team": "alpha"},
    ],
}

_EXPECTED_TOML = """\
# Rendered by the corp-llm-gateway chart from the issuance.* values.
CORP_GATEWAY_ISSUE_OIDC_ISSUER = "https://keycloak.corp.lan/realms/dev"
CORP_GATEWAY_ISSUE_OIDC_AUDIENCE = "corp-gateway-issuance"
CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID = "corp-gateway-cli"
CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM = "groups"
CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM = "preferred_username"
CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS = "30"
CORP_GATEWAY_ISSUE_MAX_ACTIVE = "2"
CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS = "600"
CORP_GATEWAY_ISSUE_MAX_INFLIGHT = "4"
CORP_GATEWAY_ISSUE_RATE_PER_MINUTE = "30"
CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS = "10"

# Ordered: the first group in this list that the user belongs to wins.
[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]
"/devs/payments" = "payments"
"/devs/core" = "core"
"/devs/alpha" = "alpha"
"""


def _render_values(
    tmp_path: Path, values: dict[str, Any], *files: Path
) -> subprocess.CompletedProcess[str]:
    values_file = tmp_path / "values-test.yaml"
    values_file.write_text(yaml.safe_dump(values))
    args = ["helm", "template", "gw", str(CHART_DIR)]
    for path in (*files, values_file):
        args += ["-f", str(path)]
    return subprocess.run(args, capture_output=True, text=True)


def _docs_of(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _issuance(**overrides: Any) -> dict[str, Any]:
    return {"issuance": {**_ISSUANCE_ON, **overrides}}


def _gateway_configmap(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-gateway-config"):
            return doc
    return None


def _pod_spec(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _first_of_kind(docs, "Deployment")["spec"]["template"]["spec"]


def _gateway_and_check(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spec = _pod_spec(docs)
    litellm = next(c for c in spec["containers"] if c["name"] == "litellm")
    check = next(c for c in spec["initContainers"] if c["name"] == "config-check")
    return [litellm, check]


def _names(entries: list[dict[str, Any]] | None) -> list[str]:
    return [entry["name"] for entry in entries or []]


def test_issuance_is_off_by_default_and_renders_no_config_file(
    rendered_docs: list[dict[str, Any]],
) -> None:
    assert _gateway_configmap(rendered_docs) is None
    assert "gateway-config" not in _names(_pod_spec(rendered_docs)["volumes"])
    for container in _gateway_and_check(rendered_docs):
        assert "gateway-config" not in _names(container.get("volumeMounts"))
        assert "CORP_LLM_GATEWAY_CONFIG_FILE" not in _names(container["env"])


def test_issuance_values_default_to_off_with_the_settings_defaults() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())

    assert values["issuance"] == {
        "enabled": False,
        "issuer": "",
        "audience": "",
        "clientId": "",
        "jwksUrl": "",
        "teamClaim": "groups",
        "userClaim": "preferred_username",
        "ttlDays": 30,
        "maxActive": 2,
        "minIntervalSeconds": 600,
        "maxInflight": 4,
        "ratePerMinute": 30,
        "storeTimeoutSeconds": 10,
        "teamMap": [],
    }


def test_issuance_renders_the_config_toml_with_the_team_map_in_list_order(
    tmp_path: Path,
) -> None:
    configmap = _gateway_configmap(_docs_of(_render_values(tmp_path, _issuance())))

    assert configmap is not None
    assert configmap["data"] == {"config.toml": _EXPECTED_TOML}


def test_issuance_renders_the_jwks_url_only_when_set(tmp_path: Path) -> None:
    url = "https://keycloak.corp.lan/realms/dev/protocol/openid-connect/certs"
    configmap = _gateway_configmap(_docs_of(_render_values(tmp_path, _issuance(jwksUrl=url))))

    assert configmap is not None
    assert f'CORP_GATEWAY_ISSUE_OIDC_JWKS_URL = "{url}"\n' in configmap["data"]["config.toml"]
    assert "JWKS_URL" not in _EXPECTED_TOML


def test_issuance_renders_large_integers_without_an_exponent(tmp_path: Path) -> None:
    # Helm reads YAML numbers as float64; 2592000 would print as 2.592e+06.
    configmap = _gateway_configmap(
        _docs_of(_render_values(tmp_path, _issuance(minIntervalSeconds=2592000)))
    )

    assert configmap is not None
    assert (
        'CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS = "2592000"\n'
        in (configmap["data"]["config.toml"])
    )


def test_issuance_mounts_the_file_read_only_on_the_gateway_and_the_config_check(
    tmp_path: Path,
) -> None:
    docs = _docs_of(_render_values(tmp_path, _issuance()))
    volume = next(v for v in _pod_spec(docs)["volumes"] if v["name"] == "gateway-config")

    assert volume["configMap"]["name"] == "gw-corp-llm-gateway-gateway-config"
    for container in _gateway_and_check(docs):
        mounts = [m for m in container["volumeMounts"] if m["name"] == "gateway-config"]
        assert mounts == [
            {
                "name": "gateway-config",
                "mountPath": CONFIG_TOML_PATH,
                "subPath": "config.toml",
                "readOnly": True,
            }
        ], container["name"]


def test_issuance_points_both_containers_at_the_config_file(tmp_path: Path) -> None:
    for container in _gateway_and_check(_docs_of(_render_values(tmp_path, _issuance()))):
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["CORP_LLM_GATEWAY_CONFIG_FILE"] == CONFIG_TOML_PATH, container["name"]


def test_issuance_rolls_the_pods_when_the_config_file_changes(tmp_path: Path) -> None:
    # A subPath mount never sees a ConfigMap update; the checksum restarts the pods.
    def checksum(team: str) -> str:
        values = _issuance(teamMap=[{"group": "/devs/core", "team": team}])
        docs = _docs_of(_render_values(tmp_path, values))
        annotations = _first_of_kind(docs, "Deployment")["spec"]["template"]["metadata"]
        return annotations["annotations"]["checksum/gateway-config"]

    assert checksum("core") != checksum("payments")


@pytest.mark.parametrize("field", ["issuer", "audience", "clientId"])
def test_issuance_refuses_to_render_without_a_required_scalar(tmp_path: Path, field: str) -> None:
    result = _render_values(tmp_path, _issuance(**{field: ""}))

    assert result.returncode != 0
    assert f"issuance.{field} is required when issuance.enabled is true" in result.stderr


def test_issuance_refuses_to_render_without_a_team_map(tmp_path: Path) -> None:
    result = _render_values(tmp_path, _issuance(teamMap=[]))

    assert result.returncode != 0
    assert "issuance.teamMap is required when issuance.enabled is true" in result.stderr


@pytest.mark.parametrize(
    "entry", [{"group": "/devs/core"}, {"team": "core"}, {"group": "", "team": "core"}]
)
def test_issuance_refuses_a_team_map_entry_without_a_group_or_team(
    tmp_path: Path, entry: dict[str, str]
) -> None:
    result = _render_values(tmp_path, _issuance(teamMap=[entry]))

    assert result.returncode != 0
    assert "issuance.teamMap entries need a non-empty group and team" in result.stderr


def test_issuance_refuses_a_group_listed_twice(tmp_path: Path) -> None:
    # A duplicate key is a TOML parse error, i.e. a gateway that cannot boot.
    result = _render_values(
        tmp_path,
        _issuance(
            teamMap=[
                {"group": "/devs/core", "team": "core"},
                {"group": "/devs/core", "team": "payments"},
            ]
        ),
    )

    assert result.returncode != 0
    assert 'issuance.teamMap lists group "/devs/core" twice' in result.stderr


@pytest.mark.parametrize("key", ["CORP_GATEWAY_ISSUE_OIDC_ISSUER", "CORP_GATEWAY_ISSUE_MAX_ACTIVE"])
def test_issuance_keys_in_the_config_passthrough_are_refused(tmp_path: Path, key: str) -> None:
    # An env var wins over the file, so a config: copy would silently shadow issuance.*.
    result = _render_values(tmp_path, {**_issuance(), "config": {key: "x"}})

    assert result.returncode != 0
    assert f"config.{key}: set developer token issuance through issuance.*" in result.stderr


def test_issuance_keys_in_the_config_passthrough_are_refused_with_issuance_off() -> None:
    # Set alone, the issuer turns issuance on with no team map: a pod that never starts.
    result = _render("config.CORP_GATEWAY_ISSUE_OIDC_ISSUER=https://k/realms/dev")

    assert result.returncode != 0
    assert "config.CORP_GATEWAY_ISSUE_OIDC_ISSUER: set developer token issuance" in result.stderr


def test_issuance_refuses_a_config_file_override_in_the_passthrough(tmp_path: Path) -> None:
    result = _render_values(
        tmp_path, {**_issuance(), "config": {"CORP_LLM_GATEWAY_CONFIG_FILE": "/tmp/x.toml"}}
    )

    assert result.returncode != 0
    assert "config.CORP_LLM_GATEWAY_CONFIG_FILE: the chart sets it" in result.stderr


def test_issuance_under_a_network_policy_needs_the_keycloak_egress_rule(
    tmp_path: Path,
) -> None:
    # Without it the JWKS fetch never leaves the pod and every issuance is a 503.
    result = _render_values(tmp_path, {**_issuance(), "networkPolicy": {"enabled": True}})

    assert result.returncode != 0
    assert "issuance.enabled with networkPolicy.enabled needs networkPolicy.keycloak" in (
        result.stderr
    )


def test_issuance_under_a_network_policy_renders_with_the_keycloak_egress_rule(
    tmp_path: Path,
) -> None:
    values = {
        **_issuance(),
        "networkPolicy": {
            "enabled": True,
            "keycloak": {"enabled": True, "cidr": "10.20.30.40/32"},
        },
    }

    assert _gateway_configmap(_docs_of(_render_values(tmp_path, values))) is not None


def test_issuance_group_names_survive_quoting_and_parse_back(tmp_path: Path) -> None:
    import tomllib

    groups = ['/devs/"quoted"', "/devs/back\\slash", "/разработка/платежи", "a = b"]
    values = _issuance(teamMap=[{"group": g, "team": f"t{i}"} for i, g in enumerate(groups)])
    configmap = _gateway_configmap(_docs_of(_render_values(tmp_path, values)))

    assert configmap is not None
    parsed = tomllib.loads(configmap["data"]["config.toml"])
    assert list(parsed["CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"].items()) == [
        (g, f"t{i}") for i, g in enumerate(groups)
    ]


_ISSUANCE_ENV_KEYS = (
    "CORP_ENV",
    "CORP_GATEWAY_OIDC_AUDIENCE",
    *(
        name
        for name in (
            "CORP_GATEWAY_ISSUE_OIDC_ISSUER",
            "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE",
            "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID",
            "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL",
            "CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM",
            "CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM",
            "CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS",
            "CORP_GATEWAY_ISSUE_MAX_ACTIVE",
            "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS",
            "CORP_GATEWAY_ISSUE_MAX_INFLIGHT",
            "CORP_GATEWAY_ISSUE_RATE_PER_MINUTE",
            "CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS",
        )
    ),
)


@pytest.mark.usefixtures("hermetic_gateway_config")
def test_the_rendered_config_file_resolves_through_the_gateway_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_llm_gateway import config, settings

    values = {
        **_issuance(minIntervalSeconds=2592000),
        "networkPolicy": {"keycloak": {"enabled": True, "cidr": "10.20.30.40/32"}},
    }
    docs = _docs_of(_render_values(tmp_path, values, CHART_DIR / "values-prod.yaml"))
    configmap = _gateway_configmap(docs)
    assert configmap is not None
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(configmap["data"]["config.toml"])
    for name in _ISSUANCE_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    # The pod's own env: CORP_ENV and the operator audience come from values-prod.
    env = {e["name"]: e.get("value") for e in _gateway_and_check(docs)[0]["env"]}
    monkeypatch.setenv("CORP_ENV", env["CORP_ENV"])
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", env["CORP_GATEWAY_OIDC_AUDIENCE"])
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(toml_file))
    config.reset_cache()

    resolved = settings.issuance()

    assert resolved is not None
    assert resolved.issuer == "https://keycloak.corp.lan/realms/dev"
    assert resolved.jwks_url == "https://keycloak.corp.lan/realms/dev/protocol/openid-connect/certs"
    assert resolved.team_map == (
        ("/devs/payments", "payments"),
        ("/devs/core", "core"),
        ("/devs/alpha", "alpha"),
    )
    assert resolved.min_interval_seconds == 2592000
    assert resolved.token_ttl_days == 30
    assert resolved.allow_insecure_http is False


def _lint(*files: Path) -> subprocess.CompletedProcess[str]:
    args = ["helm", "lint", str(CHART_DIR)]
    for path in files:
        args += ["-f", str(path)]
    return subprocess.run(args, capture_output=True, text=True)


def test_the_chart_lints_with_the_default_and_the_prod_values() -> None:
    for files in ((), (CHART_DIR / "values-prod.yaml",)):
        result = _lint(*files)
        assert result.returncode == 0, result.stdout + result.stderr


def test_the_chart_lints_with_issuance_enabled_on_the_prod_values(tmp_path: Path) -> None:
    values_file = tmp_path / "values-issuance.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                **_issuance(),
                "networkPolicy": {"keycloak": {"enabled": True, "cidr": "10.20.30.40/32"}},
            }
        )
    )

    result = _lint(CHART_DIR / "values-prod.yaml", values_file)

    assert result.returncode == 0, result.stdout + result.stderr
