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
import release_common
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
    handlers=(logging.StreamHandler(sys.stdout),),
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


def inspect_image(root: Path, image: str) -> subprocess.CompletedProcess[str] | None:
    """Treat only an explicitly missing registry manifest as absent."""
    result = run(root, "docker", "buildx", "imagetools", "inspect", image, check=False)
    if result.returncode:
        if re.search(
            r"manifest unknown|no such manifest|name unknown|404 Not Found",
            result.stderr,
            re.I,
        ):
            return None

        result.check_returncode()

    return result


def verify_platform(
    root: Path, ref: str, version: str, sha: str, arch: str | None = None
) -> str:
    """Validate a digest-addressed image's architecture and release provenance."""
    if not DIGEST.fullmatch(ref.rsplit("@", 1)[-1]):
        raise ValueError("Invalid platform image digest")

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

    actual = metadata.get("architecture")
    if (
        metadata.get("os") != "linux"
        or actual not in PLATFORMS
        or (arch is not None and actual != arch)
    ):
        raise ValueError("Image platform differs from the supported Linux architecture")

    labels = json_object(json_object(metadata.get("config")).get("Labels"))
    if (
        labels.get("org.opencontainers.image.revision") != sha
        or labels.get("org.opencontainers.image.version") != version
    ):
        raise ValueError(
            "Existing image belongs to another commit or version; bump the image version"
        )

    assert isinstance(actual, str)

    return actual


def verify_image(
    root: Path,
    image: str,
    inspection: subprocess.CompletedProcess[str],
    version: str,
    sha: str,
) -> tuple[str, dict[str, str]]:
    """Validate an immutable manifest and both of its platform images."""
    match = re.search(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", inspection.stdout, re.M)
    if match is None:
        raise ValueError("Registry image has no valid manifest digest")

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
    registry = image.rsplit(":", 1)[0]

    for arch, digest in digests.items():
        verify_platform(root, f"{registry}@{digest}", version, sha, arch)

    return match[1], digests


def verify_inputs(
    root: Path, registry: str, inputs: list[str], version: str, sha: str
) -> dict[str, str]:
    """Require two distinct digests covering exactly the intended architectures."""
    if (
        len(inputs) != 2
        or len(set(inputs)) != 2
        or any(not DIGEST.fullmatch(value) for value in inputs)
    ):
        raise ValueError("Supply exactly two distinct sha256 platform digests")

    result: dict[str, str] = {}
    for digest in inputs:
        arch = verify_platform(root, f"{registry}@{digest}", version, sha)
        if arch in result:
            raise ValueError("Duplicate platform architecture")

        result[arch] = digest

    if set(result) != set(PLATFORMS):
        raise ValueError("Image must support exactly linux/amd64 and linux/arm64")

    return result


def publish_image(
    root: Path,
    repository: str,
    image: str,
    version: str,
    source_sha: str,
    platform_inputs: list[str] | None = None,
) -> None:
    """Publish or resume a registry image and its GitHub release description."""
    tag = f"image-v{version}"
    verify_tag(root, repository, tag, source_sha)
    body = release_body(root, repository, tag)

    registry = image.rsplit(":", 1)[0]
    supplied = (
        verify_inputs(root, registry, platform_inputs, version, source_sha)
        if platform_inputs is not None
        else None
    )

    inspect = inspect_image(root, image)
    if inspect is None:
        if supplied is not None:
            run(
                root,
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                image,
                *(f"{registry}@{supplied[arch]}" for arch in PLATFORMS),
            )
        else:
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

    digest, digests = verify_image(root, image, inspect, version, source_sha)
    if supplied is not None and supplied != digests:
        raise ValueError(
            "Existing versioned image contains different platform digests; bump the image version"
        )

    section = image_section(image, digest)

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
        help="image repository (defaults to NEBIUS_REGISTRY_HOST/NEBIUS_REGISTRY_NAMESPACE/nfsd)",
    )
    parser.add_argument(
        "--platform-digest",
        action="append",
        help="digest of a prebuilt platform image; supply once per architecture",
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

        registry = release_common.image_repository(args.registry_repository)

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
        image = f"{registry}:{version}"

        logger.info(f"Publishing {image} from commit {source_sha}")

        if args.platform_digest is None:
            publish_image(root, args.github_repository, image, version, source_sha)
        else:
            publish_image(
                root,
                args.github_repository,
                image,
                version,
                source_sha,
                args.platform_digest,
            )

        logger.info(
            f"Published {image} and https://github.com/{args.github_repository}/releases/tag/image-v{version}"
        )

    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            logger.error(error.stderr)

        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
