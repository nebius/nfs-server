"""Shared validation and GitHub checks for local release publishers."""

import json
import re
import subprocess
from pathlib import Path

import prepare_release


def run(
    root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an external tool without a shell, retaining diagnostics on failure."""
    return subprocess.run(
        args,
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
        env=env,
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


def add_release_section(body: str, section: str, kind: str = "container-image") -> str:
    """Preserve existing notes and reject malformed or conflicting marked sections."""
    start, end = f"<!-- {kind}:start -->", f"<!-- {kind}:end -->"

    if start not in body and end not in body:
        return body + ("\n\n" if body else "") + section + "\n"

    if body.count(start) != 1 or body.count(end) != 1:
        raise ValueError(f"Malformed {kind} section in release notes")

    first, last = body.index(start), body.index(end)

    if first > last:
        raise ValueError(f"Malformed {kind} section in release notes")

    if body[first : last + len(end)] != section:
        raise ValueError(f"Existing release describes a different {kind}")

    return body


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
        raise ValueError("Publish from a clean checkout; commit prepared changes first")

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
