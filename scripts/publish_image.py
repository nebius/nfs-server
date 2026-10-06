#!/usr/bin/env python3
"""Push prepared multi-platform images and publish registry references on GitHub."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import prepare_release

PLATFORMS = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an external tool without a shell, retaining diagnostics on failure."""
    return subprocess.run(
        args,
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
    )


def release_body(root: Path, repository: str, tag: str) -> str | None:
    """Read existing release notes; only an explicit HTTP 404 means absent."""
    result = run(
        root, "gh", "api", f"repos/{repository}/releases/tags/{tag}", check=False
    )
    if result.returncode == 0:
        body = json_object(json.loads(result.stdout)).get("body")
        if body is None:
            return ""
        if not isinstance(body, str):
            raise ValueError("GitHub release body must be a string or null")
        return body
    if "(HTTP 404)" in result.stderr:
        return None
    result.check_returncode()
    return None


def verify_tag(root: Path, repository: str, tag: str, source_sha: str) -> None:
    """Reject existing GitHub tags from another commit, including annotated tags."""
    ref = f"refs/tags/{tag}"
    result = run(
        root,
        "git",
        "ls-remote",
        "--exit-code",
        "--tags",
        f"https://github.com/{repository}.git",
        ref,
        ref + "^{}",
        check=False,
    )
    if result.returncode == 2:
        return

    result.check_returncode()

    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or fields[1] not in (ref, ref + "^{}")
            or not re.fullmatch(r"[0-9a-f]{40}", fields[0])
        ):
            raise ValueError("Malformed remote tag response")

        refs[fields[1]] = fields[0]

    if refs.get(ref + "^{}", refs.get(ref)) != source_sha:
        raise ValueError(f"Tag {tag} belongs to a different commit")


def json_object(value: object) -> dict[str, object]:
    """Validate a decoded JSON object before accessing its fields."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object with string keys")

    return {key: item for key, item in value.items()}


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
    """Preserve existing notes and reject malformed or conflicting image sections."""
    start, end = "<!-- container-image:start -->", "<!-- container-image:end -->"
    if start not in body and end not in body:
        return body + ("\n\n" if body else "") + section + "\n"
    if body.count(start) != 1 or body.count(end) != 1:
        raise ValueError("Malformed container image section in release notes")
    first, last = body.index(start), body.index(end)
    if first > last:
        raise ValueError("Malformed container image section in release notes")
    if body[first : last + len(end)] != section:
        raise ValueError("Existing release describes a different container image")
    return body


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
        print(f"Reusing existing registry image {image}")

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


SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def manifest_versions(value: object) -> dict[str, str]:
    """Validate image and chart versions from an external JSON object."""
    versions = json_object(value)
    result: dict[str, str] = {}
    for project in ("image", "chart"):
        version = versions.get(project)
        if not isinstance(version, str) or not SEMVER.fullmatch(version):
            raise ValueError(f"Invalid {project} version in release manifest")

        result[project] = version

    return result


def validate_manifest(root: Path) -> prepare_release.ReleaseManifest:
    """Require committed preparation metadata and unchanged image build sources."""
    if prepare_release.run(root, "git", "status", "--porcelain"):
        raise ValueError(
            "Publish images from a clean checkout; commit prepared changes first"
        )

    path = root / ".changes/release.json"
    if not path.is_file():
        raise ValueError(
            "No release manifest; run just release prepare and commit the generated files"
        )

    text = path.read_text()
    value: object = json.loads(text)
    data = json_object(value)
    source = data.get("source_sha")
    changed = data.get("image_changed")

    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Invalid preparation source SHA")

    if not isinstance(changed, bool):
        raise ValueError("image_changed must be a JSON boolean")

    manifest: prepare_release.ReleaseManifest = {
        "source_sha": source,
        "image_changed": changed,
        "previous": manifest_versions(data.get("previous")),
        "versions": manifest_versions(data.get("versions")),
    }

    if text.strip() != prepare_release.run(
        root, "git", "show", "HEAD:.changes/release.json"
    ):
        raise ValueError("Release manifest differs from committed metadata")

    if manifest["versions"] != prepare_release.versions(root):
        raise ValueError("Chart versions differ from the release manifest")

    prepare_release.run(root, "git", "merge-base", "--is-ancestor", source, "HEAD")

    before = prepare_release.versions(
        root, prepare_release.run(root, "git", "show", f"{source}:helm/Chart.yaml")
    )
    if before != manifest["previous"]:
        raise ValueError("Release baseline differs from the preparation source")

    for project in ("image", "chart"):
        old = tuple(map(int, before[project].split(".")))
        new = tuple(map(int, manifest["versions"][project].split(".")))
        expected_change = project == "chart" or changed

        if new < old or (new != old) != expected_change:
            raise ValueError(f"Invalid {project} version change")

        if (
            new != old
            and not (
                root / f'.changes/{project}/{manifest["versions"][project]}.md'
            ).is_file()
        ):
            raise ValueError(f"Missing {project} release notes")

    files = prepare_release.run(
        root, "git", "diff", "--name-only", source, "HEAD"
    ).splitlines()
    if any(
        path.startswith("docker/")
        and path not in ("docker/README.md", "docker/CHANGELOG.md")
        for path in files
    ):
        raise ValueError("Image sources changed after release preparation")

    return manifest


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
            print("Chart-only release; no image publication needed")
            return
        for tool in ("docker", "gh"):
            if shutil.which(tool) is None:
                raise ValueError(f"{tool} is missing; install it before publishing")
        run(root, "docker", "info")
        run(root, "docker", "buildx", "version")
        source_sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
        version = manifest["versions"]["image"]
        image = f"{args.registry_repository}:{version}"
        print(f"Publishing {image} from commit {source_sha}", flush=True)
        publish_image(root, args.github_repository, image, version, source_sha)
        print(
            f"Published {image} and https://github.com/{args.github_repository}/releases/tag/image-v{version}"
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
