#!/usr/bin/env python3
"""Test chart publication with focused mocks and one local index integration."""

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import prepare_release
from prepare_release_test import ROOT
from publish_image_test import prepared_repository
import publish_chart
import release_common


@contextmanager
def chart_repository() -> Iterator[Path]:
    """Add the real chart to the shared prepared-release Git fixture."""
    with prepared_repository(False) as root:
        metadata = (root / "helm/Chart.yaml").read_text()
        shutil.copytree(ROOT / "helm", root / "helm", dirs_exist_ok=True)
        (root / "helm/Chart.yaml").write_text(metadata)
        prepare_release.run(root, "git", "add", ".")
        prepare_release.run(root, "git", "commit", "-qm", "Chart fixture")
        yield root


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
REF = "oci://registry.example/ns/nfs-server"


def command_result(
    root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Supply fixed preflight responses, without modeling remote service state."""
    output = ""
    if args[:3] == ("gh", "auth", "token"):
        output = "secret-token"
    elif args[:2] == ("git", "ls-remote"):
        output = f"{SHA}\trefs/heads/gh-pages\n"
    elif args[:2] == ("helm", "push"):
        output = "Digest: " + DIGEST
    return subprocess.CompletedProcess(args, 0, output, "")


@contextmanager
def publication(
    body: str | None = None, exists: bool = False, has_asset: bool = True
) -> Iterator[tuple[Path, MagicMock, MagicMock]]:
    """Mock independently-tested helpers and inspect publication commands."""
    manifest: prepare_release.ReleaseManifest = {
        "source_sha": SHA,
        "previous": {"image": "1.0.1", "chart": "1.0.1"},
        "versions": {"image": "1.0.1", "chart": "1.0.2"},
        "image_changed": False,
    }
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(release_common, "validate_manifest", return_value=manifest),
        patch.object(
            prepare_release,
            "run",
            side_effect=[SHA, "Tests <test@example.invalid> 1 +0000"],
        ),
        patch("publish_chart.shutil.which", return_value="/tools/tool"),
        patch.object(release_common, "verify_tag"),
        patch.object(
            release_common, "release_body", side_effect=[body, "Custom notes\n"]
        ),
        patch.object(publish_chart, "release_asset", return_value=has_asset),
        patch.object(
            publish_chart, "pull_chart", return_value=DIGEST if exists else None
        ),
        patch.object(publish_chart, "archive_contents"),
        patch.object(publish_chart, "compare_charts") as compare,
        patch("publish_chart.shutil.copyfile"),
        patch.object(release_common, "run", side_effect=command_result) as command,
    ):
        root = Path(directory)
        with patch.object(
            publish_chart, "package_chart", return_value=root / "nfs-server-1.0.2.tgz"
        ):
            yield (root, command, compare)


def writes(command: MagicMock) -> list[tuple[str, ...]]:
    """Select publishing commands from a mocked command trace."""
    return [
        call.args[1:3] if call.args[1] in ("helm", "cr") else call.args[1:4]
        for call in command.call_args_list
        if call.args[1:3] in (("helm", "push"), ("cr", "upload"), ("cr", "index"))
        or call.args[1:4]
        in (
            ("gh", "release", "upload"),
            ("gh", "release", "edit"),
            ("git", "push", "origin"),
        )
    ]


class ChartPublicationTests(unittest.TestCase):
    """Check immutable packages, resumable writes, and index preservation."""

    def test_package_and_archive_validation(self) -> None:
        """Use real Helm packages; reject unsafe, duplicate, or different contents."""
        with chart_repository() as root, tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            package = publish_chart.package_chart(root, "1.0.2", work)
            contents = publish_chart.archive_contents(package)
            assert (
                contents["nfs-server/RELEASE-NOTES.md"][1]
                == (root / ".changes/chart/1.0.2.md").read_bytes()
            )
            assert not (root / "helm/RELEASE-NOTES.md").exists()
            assert not prepare_release.run(root, "git", "status", "--porcelain")
            modified = work / "modified.tgz"
            with tarfile.open(modified, "w:gz") as archive:
                for name, (mode, data) in contents.items():
                    item = tarfile.TarInfo(name)
                    item.mode, item.size, item.mtime, item.uid = (
                        mode,
                        len(data),
                        12345,
                        123,
                    )
                    archive.addfile(item, io.BytesIO(data))
            publish_chart.compare_charts(package, modified)
            assert package.read_bytes() != modified.read_bytes()
            for unsafe in (
                "../bad",
                "/bad",
                "other/Chart.yaml",
                "nfs-server/link",
                "duplicate",
                "different",
            ):
                with tarfile.open(modified, "w:gz") as archive:
                    item = tarfile.TarInfo("nfs-server/Chart.yaml")
                    item.size = 1
                    archive.addfile(item, io.BytesIO(b"x"))
                    if unsafe != "different":
                        item = tarfile.TarInfo(
                            "nfs-server/Chart.yaml" if unsafe == "duplicate" else unsafe
                        )
                        if unsafe == "nfs-server/link":
                            item.type, item.linkname = (
                                tarfile.SYMTYPE,
                                "../../outside",
                            )
                        archive.addfile(item)
                with self.assertRaises(ValueError):
                    publish_chart.compare_charts(package, modified)

    def test_pull_errors_and_digests(self) -> None:
        """Fail closed on auth/network errors and malformed or missing OCI digests."""
        for status, output, stderr in (
            (0, "", "Digest: sha256:" + "a" * 64),
            (1, "", "404 Not Found"),
            (
                1,
                "",
                'Error: failed to perform "FetchReference" on source: example/chart:1.0.2: not found',
            ),
            (1, "", "Error: another/chart:1.0.2: not found"),
            (1, "", "unauthorized"),
            (1, "", "network error"),
            (0, "Digest: broken", ""),
        ):
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(
                    release_common,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["helm"], status, output, stderr
                    ),
                ):
                    missing = (
                        "404" in stderr or "example/chart:1.0.2: not found" in stderr
                    )
                    if status and (not missing) or output == "Digest: broken":
                        with self.assertRaises(
                            (ValueError, subprocess.CalledProcessError)
                        ):
                            publish_chart.pull_chart(
                                Path("."),
                                "oci://example/chart",
                                "1.0.2",
                                Path(directory) / "pull",
                            )
                    else:
                        digest = publish_chart.pull_chart(
                            Path("."),
                            "oci://example/chart",
                            "1.0.2",
                            Path(directory) / "pull",
                        )
                        assert digest == (None if status else "sha256:" + "a" * 64)

    def test_notes_and_asset_shapes(self) -> None:
        """Keep custom release text, reject conflicts, and validate asset JSON."""
        section = publish_chart.chart_section(
            "oci://registry.example/ns/nfs-server",
            "1.0.2",
            "sha256:" + "a" * 64,
            "test/repo",
        )
        notes = release_common.add_release_section(
            "Custom notes\n", section, "helm-chart"
        )
        assert notes.startswith("Custom notes\n")
        assert release_common.add_release_section(notes, section, "helm-chart") == notes
        for body in ("<!-- helm-chart:start -->", notes.replace("1.0.2", "1.0.3")):
            with self.assertRaises(ValueError):
                release_common.add_release_section(body, section, "helm-chart")
        for value in ({}, {"assets": [None]}, {"assets": [{"name": 4}]}):
            with (
                patch.object(
                    release_common,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["gh"], 0, json.dumps(value), ""
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                publish_chart.release_asset(Path("."), "test/repo", "tag", "chart.tgz")

    def test_publication_scenarios(self) -> None:
        """Publish missing artifacts and avoid rewriting already-linked releases."""
        linked = release_common.add_release_section(
            "Custom notes\n",
            publish_chart.chart_section(REF, "1.0.2", DIGEST, "test/repo"),
            "helm-chart",
        )
        for body, exists, has_asset in (
            (None, False, True),
            ("Custom notes\n", True, False),
            (linked, True, True),
        ):
            with publication(body, exists, has_asset) as (root, command, _):
                publish_chart.publish_chart(
                    root, "test/repo", "registry.example/ns", "gh-pages"
                )
                assert writes(command) == [
                    *([("helm", "push")] if not exists else []),
                    *([("cr", "upload")] if body is None else []),
                    *([("gh", "release", "upload")] if not has_asset else []),
                    *([("gh", "release", "edit")] if body != linked else []),
                    ("cr", "index"),
                ]
                for call in command.call_args_list:
                    args = call.args[1:]
                    assert "--clobber" not in args
                    assert "--force" not in args
                    assert "secret-token" not in args
                    if args[0] == "cr":
                        assert call.kwargs["env"]["CR_TOKEN"] == "secret-token"
                        if args[1] == "index":
                            assert "--push=false" in args
                        else:
                            assert args[args.index("--commit") + 1] == SHA
                    if args[:3] == ("gh", "release", "edit"):
                        assert linked in args[-1]

    def test_conflicts_and_preflight_failures(self) -> None:
        """Reject conflicting artifacts and failed preflight before publication."""
        for failure in (
            "oci-conflict",
            "asset-conflict",
            "tag",
            "auth",
            "pages",
            "clone",
            "notes",
        ):
            with publication(
                "<!-- helm-chart:start -->" if failure == "notes" else "Custom notes\n",
                True,
                failure == "asset-conflict",
            ) as (root, command, compare):
                if failure.endswith("conflict"):
                    error = ValueError("Different chart contents")
                    compare.side_effect = (
                        [None, error] if failure == "asset-conflict" else error
                    )

                def fail(
                    checkout: Path,
                    *args: str,
                    check: bool = True,
                    env: dict[str, str] | None = None,
                ) -> subprocess.CompletedProcess[str]:
                    """Fail a selected preflight command, leaving all writes mocked."""
                    prefix = {
                        "auth": ("gh", "auth"),
                        "pages": ("git", "ls-remote"),
                        "clone": ("git", "clone"),
                    }.get(failure)
                    if prefix is not None and args[:2] == prefix:
                        raise subprocess.CalledProcessError(1, args, stderr=failure)
                    return command_result(checkout, *args, check=check, env=env)

                command.side_effect = fail
                with patch.object(
                    release_common,
                    "verify_tag",
                    side_effect=(
                        ValueError("Conflicting tag") if failure == "tag" else None
                    ),
                ):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        publish_chart.publish_chart(
                            root, "test/repo", "registry.example/ns", "gh-pages"
                        )
                assert writes(command) == []
                if failure == "asset-conflict":
                    assert compare.call_count == 2

    def test_publication_retries(self) -> None:
        """Resume each interrupted stage without repeating successful writes."""
        linked = release_common.add_release_section(
            "Custom notes\n",
            publish_chart.chart_section(REF, "1.0.2", DIGEST, "test/repo"),
            "helm-chart",
        )
        stages = (
            ("helm", "push"),
            ("cr", "upload"),
            ("gh", "release", "upload"),
            ("gh", "release", "edit"),
            ("cr", "index"),
        )
        for stage in stages:
            with publication(has_asset=stage != ("gh", "release", "upload")) as (
                root,
                command,
                _,
            ):

                def fail(
                    checkout: Path,
                    *args: str,
                    check: bool = True,
                    env: dict[str, str] | None = None,
                ) -> subprocess.CompletedProcess[str]:
                    """Interrupt one publishing command at its call boundary."""
                    if args[: len(stage)] == stage:
                        raise subprocess.CalledProcessError(1, args)
                    return command_result(checkout, *args, check=check, env=env)

                command.side_effect = fail
                with self.assertRaises(subprocess.CalledProcessError):
                    publish_chart.publish_chart(
                        root, "test/repo", "registry.example/ns", "gh-pages"
                    )
                assert command.call_args_list[-1].args[1 : len(stage) + 1] == stage
            exists = stage != ("helm", "push")
            body = (
                None
                if not exists
                else linked if stage == ("cr", "index") else "Custom notes\n"
            )
            has_asset = stage in (
                ("helm", "push"),
                ("gh", "release", "edit"),
                ("cr", "index"),
            )
            with publication(body, exists, has_asset) as (root, command, _):
                publish_chart.publish_chart(
                    root, "test/repo", "registry.example/ns", "gh-pages"
                )
                assert writes(command) == [
                    *([("helm", "push")] if not exists else []),
                    *([("cr", "upload")] if body is None else []),
                    *([("gh", "release", "upload")] if not has_asset else []),
                    *([("gh", "release", "edit")] if body != linked else []),
                    ("cr", "index"),
                ]

    def test_index_repair_preserves_existing_files(self) -> None:
        """Repair a real local Pages index, keeping old versions and unrelated files."""
        with chart_repository() as root, tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            seed_dir = store / "seed"
            seed_dir.mkdir()
            seed = publish_chart.package_chart(root, "1.0.2", seed_dir)
            pages = store / "pages"
            pages.mkdir()
            (pages / "index.yaml").write_text(
                "apiVersion: v1\nentries:\n  nfs-server:\n  - apiVersion: v2\n    name: nfs-server\n    version: 1.0.1\n    urls: [https://example.invalid/old.tgz]\n"
            )
            (pages / "untouched.txt").write_text("Keep me\n")
            for args in (
                ("init", "-q", "-b", "gh-pages"),
                ("config", "user.name", "Tests"),
                ("config", "user.email", "test@example.invalid"),
                ("config", "core.fsmonitor", "false"),
                ("add", "."),
                ("commit", "-qm", "Initial index"),
            ):
                prepare_release.run(pages, "git", *args)
            remote = store / "remote.git"
            prepare_release.run(
                store, "git", "clone", "--quiet", "--bare", str(pages), str(remote)
            )
            real_run = release_common.run
            linked = release_common.add_release_section(
                "Custom notes\n",
                publish_chart.chart_section(REF, "1.0.2", DIGEST, "test/repo"),
                "helm-chart",
            )

            def pull(checkout: Path, ref: str, version: str, destination: Path) -> str:
                """Supply a real package for the already-published OCI chart."""
                destination.mkdir()
                shutil.copyfile(seed, destination / seed.name)
                return DIGEST

            def command(
                checkout: Path,
                *args: str,
                check: bool = True,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                """Run only local Git/Helm, mocking remote reads and index generation."""
                if args[:2] == ("git", "ls-remote"):
                    return real_run(
                        checkout, *args[:4], str(remote), args[-1], check=check
                    )
                if args[:2] == ("git", "clone"):
                    return real_run(
                        checkout, *args[:-2], str(remote), args[-1], check=check
                    )
                if args[:3] == ("gh", "auth", "token"):
                    return command_result(checkout, *args)
                if args[:3] == ("gh", "release", "download"):
                    shutil.copyfile(
                        seed, Path(args[args.index("--dir") + 1]) / seed.name
                    )
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ("cr", "index"):
                    packages = Path(args[args.index("--package-path") + 1])
                    assert (packages / seed.name).read_bytes() == seed.read_bytes()
                    current = checkout / "index.yaml"
                    if "version: 1.0.2" not in current.read_text():
                        real_run(
                            checkout,
                            "helm",
                            "repo",
                            "index",
                            str(packages),
                            "--merge",
                            str(current),
                            "--url",
                            "https://example.invalid/new",
                        )
                        shutil.copyfile(
                            packages / "index.yaml",
                            Path(args[args.index("--index-path") + 1]),
                        )
                    return subprocess.CompletedProcess(args, 0, "", "")
                if (
                    args[0] == "git"
                    and args[1]
                    in ("config", "checkout", "add", "diff", "commit", "push")
                    or args[:2] in (("helm", "lint"), ("helm", "package"))
                ):
                    if args[:2] == ("git", "push"):
                        assert args[2] == "origin"
                        assert prepare_release.run(
                            checkout, "git", "remote", "get-url", "origin"
                        ) == str(remote)
                    return real_run(checkout, *args, check=check, env=env)
                raise AssertionError(f"Unexpected external command: {args}")

            with (
                patch.object(release_common, "run", side_effect=command),
                patch.object(release_common, "verify_tag"),
                patch.object(release_common, "release_body", return_value=linked),
                patch.object(publish_chart, "release_asset", return_value=True),
                patch.object(publish_chart, "pull_chart", side_effect=pull),
                patch("publish_chart.shutil.which", return_value="/tools/tool"),
            ):
                before = prepare_release.run(root, "git", "status", "--porcelain")
                publish_chart.publish_chart(
                    root, "test/repo", "registry.example/ns", "gh-pages"
                )
                index = prepare_release.run(
                    store,
                    "git",
                    "--git-dir",
                    str(remote),
                    "show",
                    "gh-pages:index.yaml",
                )
                assert "version: 1.0.1" in index
                assert "version: 1.0.2" in index
                assert (
                    prepare_release.run(
                        store,
                        "git",
                        "--git-dir",
                        str(remote),
                        "show",
                        "gh-pages:untouched.txt",
                    )
                    == "Keep me"
                )
                sha = prepare_release.run(
                    store, "git", "--git-dir", str(remote), "rev-parse", "gh-pages"
                )
                publish_chart.publish_chart(
                    root, "test/repo", "registry.example/ns", "gh-pages"
                )
                assert (
                    prepare_release.run(
                        store, "git", "--git-dir", str(remote), "rev-parse", "gh-pages"
                    )
                    == sha
                )
                assert (
                    prepare_release.run(root, "git", "status", "--porcelain") == before
                )

    def test_cli_and_just_arguments(self) -> None:
        """Call the same endpoint outside the checkout, with a path containing spaces."""
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "tools"
            tools.mkdir()
            cr = tools / "cr"
            cr.write_text("#!/bin/sh\nexit 99\n")
            cr.chmod(493)
            env = dict(
                os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", "")
            )
            result = subprocess.run(
                [
                    "just",
                    "--justfile",
                    str(ROOT / "justfile"),
                    "release",
                    "publish-chart",
                    "--help",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            assert "--registry-namespace" in result.stdout
            alias = Path(directory) / "checkout with spaces"
            alias.symlink_to(ROOT, target_is_directory=True)
            result = subprocess.run(
                [
                    "just",
                    "--justfile",
                    str(ROOT / "justfile"),
                    "release",
                    "publish-chart",
                    "--root",
                    str(alias),
                    "--registry-namespace",
                    "invalid namespace",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1
            assert "--registry-namespace:" in result.stderr
        with (
            chart_repository() as root,
            patch.object(
                sys,
                "argv",
                [
                    "publish_chart.py",
                    "--root",
                    str(root),
                    "--registry-namespace",
                    "registry.example/ns",
                ],
            ),
            patch.object(publish_chart, "publish_chart") as publish,
        ):
            publish_chart.main()
            publish.assert_called_once_with(
                root, "nebius/nfs-server", "registry.example/ns", "gh-pages"
            )


if __name__ == "__main__":
    unittest.main()
