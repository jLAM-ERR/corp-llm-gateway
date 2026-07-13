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
