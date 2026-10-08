#!/usr/bin/env python3
"""Generate release versions, changelogs, and a manifest from committed fragments."""

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=(logging.StreamHandler(sys.stdout),),
)
logger: logging.Logger = logging.getLogger(__name__)


class ReleaseManifest(TypedDict):
    """Record the source commit and versions before and after preparation."""

    source_sha: str
    previous: dict[str, str]
    versions: dict[str, str]
    image_changed: bool


def run(root: Path, *args: str) -> str:
    """Run a command in the repository and return its stripped output."""
    return subprocess.check_output(args, cwd=root, text=True).strip()


def versions(root: Path, text: str | None = None) -> dict[str, str]:
    """Read stable image and chart versions from Helm metadata."""
    text = (root / "helm/Chart.yaml").read_text() if text is None else text

    result: dict[str, str] = {}
    for project, key in (("image", "appVersion"), ("chart", "version")):
        match = re.search(rf'^{key}:\s*["\']?(\d+\.\d+\.\d+)["\']?\s*$', text, re.M)
        if match is None:
            raise ValueError(f"{key} must be a stable semantic version")

        result[project] = match[1]

    return result


def prepare(root: Path) -> ReleaseManifest | None:
    """Batch Changie fragments in a clean checkout without committing or publishing."""
    if run(root, "git", "status", "--porcelain"):
        raise ValueError(
            "Prepare releases from a clean checkout; commit pending changes first"
        )

    before = versions(root)
    pending = root / ".changes/unreleased"

    image_changed = any(pending.glob("image-*.yaml"))
    chart_changed = any(pending.glob("chart-*.yaml"))
    if not image_changed and not chart_changed:
        return None

    source_sha = run(root, "git", "rev-parse", "HEAD")

    if image_changed:
        run(root, "changie", "batch", "auto", "-j", "image", "--allow-no-changes=false")
        image = run(root, "changie", "latest", "-j", "image").removeprefix("image-")
        run(
            root,
            "changie",
            "new",
            "-j",
            "chart",
            "-k",
            "Changed",
            "-b",
            f"Update NFS server image to {image}.",
            "--interactive=false",
        )
    run(root, "changie", "batch", "auto", "-j", "chart", "--allow-no-changes=false")
    run(root, "changie", "merge")

    manifest: ReleaseManifest = {
        "source_sha": source_sha,
        "previous": before,
        "versions": versions(root),
        "image_changed": image_changed,
    }
    (root / ".changes/release.json").write_text(json.dumps(manifest, indent=2) + "\n")

    return manifest


def main() -> None:
    """Prepare this repository or an explicitly selected checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout (defaults to the repository containing this script)",
    )
    args = parser.parse_args()

    manifest = None
    try:
        manifest = prepare(args.root.resolve())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")

    logger.info(
        json.dumps(manifest, indent=2)
        if manifest is not None
        else "No releasable changes"
    )


if __name__ == "__main__":
    main()
