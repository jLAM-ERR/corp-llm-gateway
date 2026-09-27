"""The container suite's image tag tracks its build inputs; no Docker needed.

A fake ``docker`` on PATH records every call and keeps a list of the tags it
"has", so these tests prove when a build happens without building anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.integration import gateway_image
from tests.integration.gateway_image import build_command, ensure_image, image_tag

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = image ] && [ "$2" = inspect ]; then
    grep -qxF "$3" "$FAKE_DOCKER_TAGS" 2>/dev/null && exit 0
    exit 1
fi
if [ "$1" = build ]; then
    if [ -n "$FAKE_DOCKER_BUILD_FAILS" ]; then
        echo "build exploded" >&2
        exit 1
    fi
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -t ]; then
            printf '%s\\n' "$2" >> "$FAKE_DOCKER_TAGS"
        fi
        shift
    done
fi
exit 0
"""

LEGACY_TAG = "corp-llm-gateway:route-gate-test"


class FakeDocker:
    def __init__(self, directory: Path) -> None:
        self.log = directory / "calls.log"
        self.tags = directory / "tags"
        self.log.touch()
        self.tags.touch()

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def builds(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("build ")]

    def has(self, tag: str) -> None:
        with self.tags.open("a") as handle:
            handle.write(f"{tag}\n")


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(FAKE_DOCKER)
    script.chmod(0o755)
    fake = FakeDocker(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_DOCKER_TAGS", str(fake.tags))
    monkeypatch.delenv("FAKE_DOCKER_BUILD_FAILS", raising=False)
    return fake


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src/pkg").mkdir(parents=True)
    (root / "crt").mkdir()
    (root / "docs").mkdir()
    (root / "Dockerfile.gateway").write_text("FROM scratch\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "README.md").write_text("readme\n")
    (root / "src/pkg/mod.py").write_text("VALUE = 1\n")
    (root / "crt/README.md").write_text("drop a CA here\n")
    (root / "docs/notes.md").write_text("notes\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_an_absent_tag_is_built_under_the_input_digest(repo: Path, fake_docker: FakeDocker) -> None:
    result = ensure_image(repo)

    assert result.built is True
    assert result.error is None
    assert result.tag == image_tag(repo)
    assert result.tag.startswith("corp-llm-gateway:route-gate-test-")
    assert fake_docker.builds() == [" ".join(build_command(result.tag, repo)[1:])]


def test_a_present_tag_is_reused_without_a_build(repo: Path, fake_docker: FakeDocker) -> None:
    first = ensure_image(repo)
    second = ensure_image(repo)

    assert second.tag == first.tag
    assert second.built is False
    assert len(fake_docker.builds()) == 1


def test_the_old_fixed_tag_does_not_stand_in_for_current_sources(
    repo: Path, fake_docker: FakeDocker
) -> None:
    fake_docker.has(LEGACY_TAG)

    result = ensure_image(repo)

    assert result.tag != LEGACY_TAG
    assert result.built is True


def test_an_uncommitted_source_edit_rebuilds_under_a_new_tag(
    repo: Path, fake_docker: FakeDocker
) -> None:
    stale = ensure_image(repo)
    (repo / "src/pkg/mod.py").write_text("VALUE = 2\n")

    fresh = ensure_image(repo)

    assert fresh.tag != stale.tag
    assert fresh.built is True
    assert len(fake_docker.builds()) == 2
    assert fake_docker.builds()[-1].split("-t ")[1].startswith(fresh.tag)


@pytest.mark.parametrize(
    "edit",
    ["Dockerfile.gateway", "pyproject.toml", "README.md", "crt/README.md", "src/pkg/new.py"],
)
def test_every_build_input_moves_the_digest(repo: Path, edit: str) -> None:
    before = gateway_image.inputs_digest(repo)
    with (repo / edit).open("a") as handle:
        handle.write("# changed\n")

    assert gateway_image.inputs_digest(repo) != before


@pytest.mark.parametrize("edit", ["docs/notes.md", "src/pkg/__pycache__/mod.cpython-312.pyc"])
def test_files_outside_the_build_inputs_leave_the_digest_alone(repo: Path, edit: str) -> None:
    before = gateway_image.inputs_digest(repo)
    (repo / edit).parent.mkdir(parents=True, exist_ok=True)
    (repo / edit).write_text("noise\n")

    assert gateway_image.inputs_digest(repo) == before


def test_a_file_deleted_from_the_working_tree_moves_the_digest(repo: Path) -> None:
    before = gateway_image.inputs_digest(repo)
    (repo / "src/pkg/mod.py").unlink()

    assert gateway_image.inputs_digest(repo) != before


def test_a_build_arg_change_moves_the_digest(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = gateway_image.inputs_digest(repo)
    monkeypatch.setitem(gateway_image.BUILD_ARGS, "NER_PROFILE", "ru-en")

    assert gateway_image.inputs_digest(repo) != before


def test_a_failed_build_reports_and_claims_no_image(
    repo: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_BUILD_FAILS", "1")

    result = ensure_image(repo)

    assert result.built is False
    assert result.error == "build exploded"
    assert fake_docker.tags.read_text() == ""


def test_the_cli_prints_the_tag_ci_runs_against(
    repo: Path, fake_docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gateway_image.main(repo) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == image_tag(repo)


def test_the_cli_fails_when_the_build_fails(
    repo: Path,
    fake_docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_BUILD_FAILS", "1")

    assert gateway_image.main(repo) == 1
    assert "cannot build corp-llm-gateway:route-gate-test-" in capsys.readouterr().err
