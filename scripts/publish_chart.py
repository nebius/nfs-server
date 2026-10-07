#!/usr/bin/env python3
"""Publish a prepared Helm chart to Nebius OCI, GitHub Releases, and Pages."""

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import prepare_release
import release_common

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

DIGEST = re.compile(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", re.M)


def package_chart(root: Path, version: str, work: Path) -> Path:
    """Lint and package a temporary copy, leaving the source checkout untouched."""
    chart = work / "chart"
    shutil.copytree(root / "helm", chart)

    (chart / "RELEASE-NOTES.md").write_text(
        (root / f".changes/chart/{version}.md").read_text()
    )

    release_common.run(root, "helm", "lint", str(chart))

    packages = work / "packages"
    packages.mkdir()

    release_common.run(
        root, "helm", "package", str(chart), "--destination", str(packages)
    )

    package = packages / f"nfs-server-{version}.tgz"
    if not package.is_file():
        raise ValueError("Expected a packaged nfs-server chart")

    return package


def archive_contents(path: Path) -> dict[str, tuple[int, bytes]]:
    """Compare regular archive members without extracting potentially unsafe paths."""
    contents: dict[str, tuple[int, bytes]] = {}
    seen: set[str] = set()

    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or name.parts[0] != "nfs-server"
                or str(name) in seen
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(
                    f"Unsafe or duplicate chart archive member: {member.name}"
                )

            seen.add(str(name))

            if member.isfile():
                stream = archive.extractfile(member)

                if stream is None:
                    raise ValueError(f"Unreadable chart archive member: {member.name}")

                with stream:
                    contents[str(name)] = (member.mode, stream.read())

    if "nfs-server/Chart.yaml" not in contents:
        raise ValueError("Chart archive has no Chart.yaml")

    return contents


def compare_charts(expected: Path, existing: Path) -> None:
    """Reject different chart contents while ignoring archive timestamps and owners."""
    if archive_contents(expected) != archive_contents(existing):
        raise ValueError("Existing chart package differs; bump the chart version")


def registry_digest(result: subprocess.CompletedProcess[str]) -> str:
    """Read Helm's confirmed OCI digest from stdout or stderr."""
    match = DIGEST.search(result.stdout + "\n" + result.stderr)

    if match is None:
        raise ValueError("Helm returned no valid chart digest")

    return match[1]


def pull_chart(root: Path, ref: str, version: str, destination: Path) -> str | None:
    """Download an existing chart, treating only explicit registry absence as missing."""
    destination.mkdir()
    result = release_common.run(
        root,
        "helm",
        "pull",
        ref,
        "--version",
        version,
        "--destination",
        str(destination),
        check=False,
    )

    if result.returncode:
        # ORAS (Helm 4) renders a manifest HTTP 404 as "<exact reference>: not found".
        absent_ref = re.search(
            rf"(?:^|: ){re.escape(ref.removeprefix('oci://'))}:{re.escape(version)}: not found\s*$",
            result.stderr,
            re.M,
        )

        if (
            re.search(
                r"404 Not Found|MANIFEST_UNKNOWN|NAME_UNKNOWN|manifest unknown|name unknown",
                result.stderr,
                re.I,
            )
            or absent_ref
        ):
            return None

        result.check_returncode()

    return registry_digest(result)


def release_asset(root: Path, repository: str, tag: str, name: str) -> bool:
    """Validate the asset listing instead of silently assuming a missing package."""
    value: object = json.loads(
        release_common.run(
            root, "gh", "release", "view", tag, "--repo", repository, "--json", "assets"
        ).stdout
    )

    assets = release_common.json_object(value).get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub release assets must be an array")

    names: list[str] = []
    for asset in assets:
        item = release_common.json_object(asset).get("name")

        if not isinstance(item, str):
            raise ValueError("GitHub asset name must be a string")

        names.append(item)

    return name in names


def chart_section(ref: str, version: str, digest: str, repository: str) -> str:
    """Render a marked chart section using the same preservation rules as images."""
    owner, repo = repository.split("/")

    return (
        "<!-- helm-chart:start -->\n"
        "## Helm chart\n\n"
        f"OCI chart: `{ref}` (version `{version}`)\n\n"
        f"```sh\nhelm upgrade --install nfs-server {ref} --version {version}\n```\n\n"
        f"Immutable reference: `{ref}@{digest}`\n\n"
        f"Helm repository: https://{owner}.github.io/{repo}/\n"
        "<!-- helm-chart:end -->"
    )


def publish_chart(root: Path, repository: str, namespace: str, branch: str) -> None:
    """Publish missing artifacts and repair notes/index without touching source Git state."""
    manifest = release_common.validate_manifest(root)
    version = manifest["versions"]["chart"]
    sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
    tag = f"nfs-server-{version}"
    ref = f"oci://{namespace}/nfs-server"

    for tool in ("helm", "git", "gh", "cr"):
        if shutil.which(tool) is None:
            package_name = "chart-releaser" if tool == "cr" else tool
            raise ValueError(
                f"{tool} is missing; install {package_name} before publishing"
            )

    token = release_common.run(
        root, "gh", "auth", "token", "--hostname", "github.com"
    ).stdout.strip()
    if not token:
        raise ValueError("GitHub authentication returned no token")

    author = prepare_release.run(root, "git", "var", "GIT_AUTHOR_IDENT")
    identity = re.fullmatch(r"(.+) <([^<>]+)> \d+ [+-]\d{4}", author)
    if identity is None:
        raise ValueError("Configure Git user.name and user.email before publishing")

    release_common.verify_tag(root, repository, tag, sha)
    body = release_common.release_body(root, repository, tag)
    remote = f"https://github.com/{repository}.git"
    pages = release_common.run(
        root,
        "git",
        "ls-remote",
        "--exit-code",
        "--heads",
        remote,
        f"refs/heads/{branch}",
        check=False,
    )
    if pages.returncode == 2:
        raise ValueError(
            f"""Pages branch {branch} is missing. Create it in a separate checkout:
pages_checkout="$(mktemp -d)"
gh repo clone {repository} "$pages_checkout" -- --depth 1
git -C "$pages_checkout" switch --orphan {shlex.quote(branch)}
printf 'apiVersion: v1\\nentries: {{}}\\n' > "$pages_checkout/index.yaml"
touch "$pages_checkout/.nojekyll"
git -C "$pages_checkout" add index.yaml .nojekyll
git -C "$pages_checkout" commit -m "Initialize Helm repository"
git -C "$pages_checkout" push origin {shlex.quote(branch)}
Then select Settings > Pages > Deploy from a branch > {branch} > /(root)."""
        )

    pages.check_returncode()

    if not re.fullmatch(
        rf"[0-9a-f]{{40}}\s+refs/heads/{re.escape(branch)}\s*", pages.stdout
    ):
        raise ValueError("Malformed Pages branch response")

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        package = package_chart(root, version, work)
        archive_contents(package)
        digest = pull_chart(root, ref, version, work / "oci")

        if digest is not None:
            compare_charts(package, work / "oci" / package.name)

        if body is not None:
            if digest is None and (
                "<!-- helm-chart:start -->" in body or "<!-- helm-chart:end -->" in body
            ):
                raise ValueError(
                    "Release already references an OCI chart that is missing; investigate before republishing"
                )

            if digest is not None:
                release_common.add_release_section(
                    body, chart_section(ref, version, digest, repository), "helm-chart"
                )

        has_asset = body is not None and release_asset(
            root, repository, tag, package.name
        )
        if has_asset:
            existing = work / "github"
            existing.mkdir()
            release_common.run(
                root,
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--pattern",
                package.name,
                "--dir",
                str(existing),
            )
            compare_charts(package, existing / package.name)
            # cr index hashes this local file: it must be byte-identical to the GitHub asset.
            shutil.copyfile(existing / package.name, package)
        elif digest is not None:
            shutil.copyfile(work / "oci" / package.name, package)

        # Clone and configure the index workspace before any remote writes.
        checkout = work / "pages"
        release_common.run(
            root,
            "git",
            "clone",
            "--no-checkout",
            "--single-branch",
            "--branch",
            branch,
            "--config",
            "credential.helper=",
            "--config",
            "credential.helper=!gh auth git-credential",
            remote,
            str(checkout),
        )
        release_common.run(checkout, "git", "config", "user.name", identity[1])
        release_common.run(checkout, "git", "config", "user.email", identity[2])
        release_common.run(checkout, "git", "checkout", "--detach", f"origin/{branch}")

        config = work / "cr.json"
        config.write_text("{}\n")

        env = {
            key: value for key, value in os.environ.items() if not key.startswith("CR_")
        }
        env["CR_TOKEN"] = token

        owner, repo = repository.split("/")

        common = (
            "--config",
            str(config),
            "--owner",
            owner,
            "--git-repo",
            repo,
            "--package-path",
            str(package.parent),
            "--release-name-template",
            "{{ .Name }}-{{ .Version }}",
        )

        if digest is None:
            pushed = release_common.run(
                root, "helm", "push", str(package), f"oci://{namespace}"
            )
            digest = registry_digest(pushed)

        section = chart_section(ref, version, digest, repository)

        if body is None:
            release_common.run(
                root,
                "cr",
                "upload",
                *common,
                "--commit",
                sha,
                "--release-notes-file",
                "RELEASE-NOTES.md",
                "--make-release-latest=false",
                env=env,
            )
            body = release_common.release_body(root, repository, tag)

            if body is None:
                raise ValueError("Chart-releaser did not create the expected release")

            has_asset = release_asset(root, repository, tag, package.name)

        notes = release_common.add_release_section(body, section, "helm-chart")

        if not has_asset:
            release_common.run(
                root, "gh", "release", "upload", tag, str(package), "--repo", repository
            )

        if notes != body:
            release_common.run(
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

        release_common.run(
            checkout,
            "cr",
            "index",
            *common,
            "--pages-branch",
            branch,
            "--pages-index-path",
            "index.yaml",
            "--index-path",
            str(work / "index.yaml"),
            "--remote",
            "origin",
            "--push=false",
            env=env,
        )

        # cr --push embeds its token in Git's URL argument; use the credential helper instead.
        index = work / "index.yaml"
        if index.is_file():
            shutil.copyfile(index, checkout / "index.yaml")
            release_common.run(checkout, "git", "add", "index.yaml")

            diff = release_common.run(
                checkout, "git", "diff", "--cached", "--quiet", check=False
            )

            if diff.returncode not in (0, 1):
                diff.check_returncode()

            if diff.returncode == 1:
                release_common.run(
                    checkout, "git", "commit", "-m", "Update Helm repository index"
                )
                release_common.run(
                    checkout, "git", "push", "origin", f"HEAD:refs/heads/{branch}"
                )

    logger.info(
        f"Published {ref}:{version} and https://github.com/{repository}/releases/tag/{tag}"
    )


def main() -> None:
    """Publish from this checkout or an explicit root using local credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="prepared checkout (defaults to this script's repository)",
    )
    parser.add_argument(
        "--registry-namespace",
        default="cr.eu-north1.nebius.cloud/soperator",
        help="OCI registry/namespace, without oci:// or chart name",
    )
    parser.add_argument(
        "--github-repository",
        default="nebius/nfs-server",
        help="GitHub owner/repository for chart releases and index",
    )
    parser.add_argument(
        "--pages-branch",
        default="gh-pages",
        help="existing branch serving index.yaml from its root",
    )
    args = parser.parse_args()

    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.github_repository):
            raise ValueError("--github-repository must be owner/repository")

        if not re.fullmatch(
            r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+",
            args.registry_namespace,
        ):
            raise ValueError(
                "--registry-namespace must be a registry/namespace without a tag"
            )

        release_common.run(
            args.root.resolve(),
            "git",
            "check-ref-format",
            f"refs/heads/{args.pages_branch}",
        )

        publish_chart(
            args.root.resolve(),
            args.github_repository,
            args.registry_namespace,
            args.pages_branch,
        )

    except (
        OSError,
        ValueError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            logger.error(error.stderr)

        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
