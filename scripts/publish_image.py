#!/usr/bin/env python3
"""Push prepared multi-platform images and publish registry references on GitHub."""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import prepare_release
from release_common import (
    add_release_section,
    json_object as json_object,
    release_body as release_body,
    run as run,
    validate_manifest as validate_manifest,
    verify_tag as verify_tag,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=(
        logging.StreamHandler(sys.stdout),
        logging.StreamHandler(sys.stderr),
    ),
)
logger: logging.Logger = logging.getLogger(__name__)

PLATFORMS = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def platform_digests(text: str) -> dict[str, str]:
    """Require exactly one valid digest for each supported Linux architecture."""
    value: object = json.loads(text)
    manifests = json_object(value).get("manifests")

    if not isinstance(manifests, list):
        raise ValueError("Image manifest must contain a manifests array")

    result: dict[str, str] = {}
    for entry in manifests:
        manifest = json_object(entry)
        platform = json_object(manifest.get("platform"))
        arch = platform.get("architecture")
        digest = manifest.get("digest")
        if (
            platform.get("os") != "linux"
            or not isinstance(arch, str)
            or arch not in PLATFORMS
            or arch in result
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
        ):
            raise ValueError("Invalid or duplicate image platform/digest")

        result[arch] = digest

    if set(result) != set(PLATFORMS):
        raise ValueError("Image must support exactly linux/amd64 and linux/arm64")

    return result


def image_section(image: str, digest: str) -> str:
    """Describe where to pull the versioned and immutable registry image."""
    registry = image.rsplit(":", 1)[0]
    return (
        "<!-- container-image:start -->\n"
        "## Container image\n\n"
        f"Image: `{image}`\n\n"
        f"```sh\ndocker pull {image}\n```\n\n"
        f"Immutable reference: `{registry}@{digest}`\n"
        "<!-- container-image:end -->"
    )


def add_image_section(body: str, section: str) -> str:
    """Preserve notes using the shared marked-section validation."""
    return add_release_section(body, section)


def publish_image(
    root: Path, repository: str, image: str, version: str, source_sha: str
) -> None:
    """Publish or resume a registry image and its GitHub release description."""
    tag = f"image-v{version}"
    verify_tag(root, repository, tag, source_sha)
    body = release_body(root, repository, tag)

    inspect = run(root, "docker", "buildx", "imagetools", "inspect", image, check=False)
    if inspect.returncode:
        if not re.search(
            r"manifest unknown|no such manifest|name unknown|404 Not Found",
            inspect.stderr,
            re.I,
        ):
            inspect.check_returncode()

        run(
            root,
            "docker",
            "buildx",
            "build",
            "docker",
            "--platform",
            "linux/amd64,linux/arm64",
            "--tag",
            image,
            "--push",
            "--provenance=false",
            "--sbom=false",
            "--label",
            f"org.opencontainers.image.revision={source_sha}",
            "--label",
            f"org.opencontainers.image.version={version}",
            "--label",
            f"org.opencontainers.image.source=https://github.com/{repository}",
        )
        inspect = run(root, "docker", "buildx", "imagetools", "inspect", image)
    else:
        logger.info(f"Reusing existing registry image {image}")

    match = re.search(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", inspect.stdout, re.M)
    if match is None:
        raise ValueError("Registry image has no valid manifest digest")

    registry = image.rsplit(":", 1)[0]
    digests = platform_digests(
        run(
            root,
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            f"{image}@{match[1]}",
            "--raw",
        ).stdout
    )

    for arch in PLATFORMS:
        ref = f"{registry}@{digests[arch]}"
        metadata = json_object(
            json.loads(
                run(
                    root,
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    ref,
                    "--format",
                    "{{json .Image}}",
                ).stdout
            )
        )
        if metadata.get("os") != "linux" or metadata.get("architecture") != arch:
            raise ValueError(f"Image platform differs from linux/{arch}")
        labels = json_object(json_object(metadata.get("config")).get("Labels"))
        if (
            labels.get("org.opencontainers.image.revision") != source_sha
            or labels.get("org.opencontainers.image.version") != version
        ):
            raise ValueError(
                "Existing image belongs to another commit or version; bump the image version"
            )

    section = image_section(image, match[1])
    if body is None:
        notes = add_image_section(
            (root / f".changes/image/{version}.md").read_text(), section
        )
        run(
            root,
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--target",
            source_sha,
            "--latest=false",
            "--title",
            f"NFS server image {version}",
            "--notes",
            notes,
        )
    else:
        notes = add_image_section(body, section)
        if notes != body:
            run(
                root,
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                repository,
                "--notes",
                notes,
            )


def main() -> None:
    """Publish a locally prepared image release using existing CLI credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout (defaults to this script's repository)",
    )
    parser.add_argument(
        "--registry-repository",
        default="cr.eu-north1.nebius.cloud/soperator/nfsd",
        help="registry image repository without a version tag",
    )
    parser.add_argument(
        "--github-repository",
        default="nebius/nfs-server",
        help="GitHub owner/repository for tags and release descriptions",
    )
    args = parser.parse_args()
    try:
        root = args.root.resolve()

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.github_repository):
            raise ValueError("--github-repository must be owner/repository")

        if not re.fullmatch(
            r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+",
            args.registry_repository,
        ):
            raise ValueError(
                "--registry-repository must be a registry/repository without a tag"
            )

        manifest = validate_manifest(root)
        if not manifest["image_changed"]:
            logger.info("Chart-only release; no image publication needed")
            return

        for tool in ("docker", "gh"):
            if shutil.which(tool) is None:
                raise ValueError(f"{tool} is missing; install it before publishing")

        run(root, "docker", "info")
        run(root, "docker", "buildx", "version")

        source_sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
        version = manifest["versions"]["image"]
        image = f"{args.registry_repository}:{version}"

        logger.info(f"Publishing {image} from commit {source_sha}")
        publish_image(root, args.github_repository, image, version, source_sha)
        logger.info(
            f"Published {image} and https://github.com/{args.github_repository}/releases/tag/image-v{version}"
        )

    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            logger.error(error.stderr)

        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
