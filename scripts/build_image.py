#!/usr/bin/env python3
"""Build and push one release platform by digest, without assigning a version tag."""

import argparse
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import prepare_release
import publish_image
import release_common

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=(logging.StreamHandler(sys.stdout),),
)
logger: logging.Logger = logging.getLogger(__name__)


def build(root: Path, registry: str, repository: str, platform: str) -> str:
    """Reuse a validated release image or push one platform with release labels."""
    if platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("Platform must be linux/amd64 or linux/arm64")

    manifest = release_common.validate_manifest(root)
    if not manifest["image_changed"]:
        raise ValueError("Chart-only release; no image build needed")

    version = manifest["versions"]["image"]
    sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
    release_common.verify_tag(root, repository, f"image-v{version}", sha)
    image = f"{registry}:{version}"

    existing = publish_image.inspect_image(root, image)
    if existing is not None:
        _, digests = publish_image.verify_image(root, image, existing, version, sha)
        return digests[platform.removeprefix("linux/")]

    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "metadata.json"
        release_common.run(
            root,
            "docker",
            "buildx",
            "build",
            "docker",
            "--platform",
            platform,
            "--output",
            f"type=image,name={registry},push-by-digest=true,name-canonical=true,push=true",
            "--metadata-file",
            str(metadata),
            "--provenance=false",
            "--sbom=false",
            "--label",
            f"org.opencontainers.image.revision={sha}",
            "--label",
            f"org.opencontainers.image.version={version}",
            "--label",
            f"org.opencontainers.image.source=https://github.com/{repository}",
        )
        digest = release_common.json_object(json.loads(metadata.read_text())).get(
            "containerimage.digest"
        )
        if not isinstance(digest, str) or not publish_image.DIGEST.fullmatch(digest):
            raise ValueError("Buildx returned no valid platform digest")

    publish_image.verify_platform(
        root, f"{registry}@{digest}", version, sha, platform.removeprefix("linux/")
    )

    return digest


def main() -> None:
    """Write the verified digest for a local build or a native CI runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="prepared repository checkout",
    )
    parser.add_argument(
        "--platform", choices=("linux/amd64", "linux/arm64"), required=True
    )
    parser.add_argument(
        "--digest-file",
        type=Path,
        required=True,
        help="output digest file; use a path outside the checkout or under ignored build/",
    )
    parser.add_argument(
        "--registry-repository",
        help="defaults to NEBIUS_REGISTRY_HOST/NEBIUS_REGISTRY_NAMESPACE/nfsd",
    )
    parser.add_argument("--github-repository", default="nebius/nfs-server")
    args = parser.parse_args()

    try:
        root = args.root.resolve()
        registry = release_common.image_repository(args.registry_repository)

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.github_repository):
            raise ValueError("--github-repository must be owner/repository")

        release_common.run(root, "docker", "info")
        release_common.run(root, "docker", "buildx", "version")

        digest = build(root, registry, args.github_repository, args.platform)

        args.digest_file.parent.mkdir(parents=True, exist_ok=True)
        args.digest_file.write_text(digest + "\n")

        logger.info(f"Platform image: {registry}@{digest}")

    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            parser.exit(1, f"{error}\n{error.stderr}\n")

        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
