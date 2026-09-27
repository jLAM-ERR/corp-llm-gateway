"""The gateway test image, tagged by what went into it.

The tag carries a digest of every build input ``Dockerfile.gateway`` copies (as
they are in the working tree, uncommitted edits included) plus the build args, so
an image built from older sources is never reused under the same name. The
container fixture and CI's ``integration-container`` job both go through
``ensure_image`` — ``python -m tests.integration.gateway_image`` prints the tag.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IMAGE_REPO = "corp-llm-gateway"
TAG_PREFIX = "route-gate-test"
DOCKERFILE = "Dockerfile.gateway"
# Everything Dockerfile.gateway COPYs. crt/'s git-ignored CA only widens trust
# inside the build and is restored before the image closes, so it is not hashed.
BUILD_INPUTS: tuple[str, ...] = (DOCKERFILE, "pyproject.toml", "README.md", "src", "crt")
# LITELLM_VERSION is left to the Dockerfile's own ARG default, which is hashed.
BUILD_ARGS: dict[str, str] = {"NER_PROFILE": "base"}

BUILD_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class ImageResult:
    tag: str
    built: bool
    error: str | None = None


def _input_files(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", *BUILD_INPUTS],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted({name for name in listed.stdout.decode().split("\0") if name})


def inputs_digest(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(BUILD_ARGS.items()):
        digest.update(f"arg\0{name}\0{value}\0".encode())
    for name in _input_files(root):
        path = root / name
        # Listed by the index but deleted in the working tree: absent from the build.
        if not path.is_file():
            continue
        digest.update(f"file\0{name}\0".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def image_tag(root: Path = ROOT) -> str:
    return f"{IMAGE_REPO}:{TAG_PREFIX}-{inputs_digest(root)}"


def build_command(tag: str, root: Path = ROOT) -> list[str]:
    command = ["docker", "build", "-f", str(root / DOCKERFILE)]
    for name, value in BUILD_ARGS.items():
        command += ["--build-arg", f"{name}={value}"]
    return [*command, "-t", tag, str(root)]


def _run(
    command: list[str], timeout: float, *, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=capture, text=True, timeout=timeout, check=False)


def ensure_image(root: Path = ROOT, *, capture: bool = True) -> ImageResult:
    """The digest-tagged image, built only when that tag is not present."""
    tag = image_tag(root)
    if _run(["docker", "image", "inspect", tag], timeout=60).returncode == 0:
        return ImageResult(tag=tag, built=False)
    built = _run(build_command(tag, root), timeout=BUILD_TIMEOUT_SECONDS, capture=capture)
    if built.returncode != 0:
        error = (built.stderr or "").strip()[-400:] or f"docker build exited {built.returncode}"
        return ImageResult(tag=tag, built=False, error=error)
    return ImageResult(tag=tag, built=True)


def main(root: Path = ROOT) -> int:
    # Uncaptured, so CI shows the build log as it runs.
    result = ensure_image(root, capture=False)
    if result.error is not None:
        print(f"cannot build {result.tag}: {result.error}", file=sys.stderr)
        return 1
    print(result.tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
