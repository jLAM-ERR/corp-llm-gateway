"""The test image's input digest with symlinked build inputs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.integration import gateway_image
from tests.integration.test_gateway_image import repo  # noqa: F401  (fixture)


def _track(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def test_an_edit_behind_a_symlinked_input_moves_the_digest(repo: Path) -> None:  # noqa: F811
    (repo / "src/pkg/real.py").write_text("A = 1\n")
    (repo / "src/pkg/alias.py").symlink_to("real.py")
    _track(repo)
    before = gateway_image.inputs_digest(repo)

    (repo / "src/pkg/real.py").write_text("A = 2\n")

    assert gateway_image.inputs_digest(repo) != before


def test_a_dangling_symlinked_input_is_skipped_not_fatal(repo: Path) -> None:  # noqa: F811
    before = gateway_image.inputs_digest(repo)
    (repo / "src/pkg/dangling.py").symlink_to("missing.py")
    _track(repo)

    after = gateway_image.inputs_digest(repo)

    assert after == before
    assert gateway_image.image_tag(repo).endswith(after)
