#!/usr/bin/env python3
"""Test chart publication with real Git/Helm fixtures and simulated remote services."""

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_release
from prepare_release_test import ROOT, repository
import publish_chart
import release_common


@contextmanager
def chart_repository(image_changed: bool = False) -> Iterator[Path]:
    """Prepare and commit the real chart with chart-only or coupled image changes."""
    with repository() as root:
        root = root.resolve()
        shutil.copytree(ROOT / "helm", root / "helm", dirs_exist_ok=True)

        metadata = root / "helm/Chart.yaml"
        text = metadata.read_text()
        metadata.write_text(
            re.sub(
                r"^(version|appVersion):.*$",
                lambda match: f'{match[1]}: "1.0.1"',
                text,
                flags=re.M,
            )
        )

        for args in (
            (
                "changie",
                "new",
                "-j",
                "image" if image_changed else "chart",
                "-k",
                "Fixed",
                "-b",
                "Test release",
                "--interactive=false",
            ),
            ("git", "add", "."),
            ("git", "commit", "-qm", "Changes"),
        ):
            prepare_release.run(root, *args)

        prepare_release.prepare(root)
        prepare_release.run(root, "git", "add", ".")
        prepare_release.run(root, "git", "commit", "-qm", "Prepare release")

        yield root


class ChartPublicationTests(unittest.TestCase):
    """Check immutable packages, publication ordering, and index repair."""

    def test_package_and_archive_validation(self) -> None:
        """Use real Helm packages; reject unsafe, duplicate, or different contents."""
        with chart_repository() as root, tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            package = publish_chart.package_chart(root, "1.0.2", work)
            contents = publish_chart.archive_contents(package)

            self.assertEqual(
                contents["nfs-server/RELEASE-NOTES.md"][1],
                (root / ".changes/chart/1.0.2.md").read_bytes(),
            )
            self.assertFalse((root / "helm/RELEASE-NOTES.md").exists())
            self.assertFalse(prepare_release.run(root, "git", "status", "--porcelain"))

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
            self.assertNotEqual(package.read_bytes(), modified.read_bytes())

            for unsafe in (
                "../bad",
                "/bad",
                "other/Chart.yaml",
                "nfs-server/link",
                "duplicate",
                "different",
            ):
                with self.subTest(unsafe=unsafe):
                    with tarfile.open(modified, "w:gz") as archive:
                        item = tarfile.TarInfo("nfs-server/Chart.yaml")
                        item.size = 1
                        archive.addfile(item, io.BytesIO(b"x"))

                        if unsafe != "different":
                            item = tarfile.TarInfo(
                                "nfs-server/Chart.yaml"
                                if unsafe == "duplicate"
                                else unsafe
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
            with (
                self.subTest(stderr=stderr),
                tempfile.TemporaryDirectory() as directory,
            ):
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

                    if (status and not missing) or output == "Digest: broken":
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
                        self.assertEqual(
                            digest, None if status else "sha256:" + "a" * 64
                        )

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

        self.assertTrue(notes.startswith("Custom notes\n"))
        self.assertEqual(
            release_common.add_release_section(notes, section, "helm-chart"), notes
        )

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

    def test_publication_and_retries(self) -> None:
        """Exercise real local Git/Helm and retry every external publication stage."""
        cases = [
            (False, False, False, False, ""),
            (True, False, False, False, ""),
            (False, True, True, False, ""),
            (True, True, False, False, ""),
            (True, True, True, True, ""),
            *[
                (True, True, True, False, failure)
                for failure in (
                    "pages",
                    "tag",
                    "auth",
                    "registry",
                    "oci-conflict",
                    "asset-conflict",
                    "notes",
                    "clone",
                )
            ],
            *[
                (False, False, False, False, failure)
                for failure in ("push", "upload", "asset-upload", "edit", "index")
            ],
        ]

        for oci_exists, github_exists, has_asset, linked, failure in cases:
            with (
                self.subTest(
                    failure=failure,
                    oci=oci_exists,
                    github=github_exists,
                    asset=has_asset,
                ),
                chart_repository(failure == "push") as root,
                tempfile.TemporaryDirectory() as directory,
            ):
                store = Path(directory)
                sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
                before = prepare_release.run(root, "git", "status", "--porcelain")
                digest = "sha256:" + "a" * 64
                ref = "oci://registry.example/ns/nfs-server"
                section = publish_chart.chart_section(ref, "1.0.2", digest, "test/repo")

                body = (
                    release_common.add_release_section(
                        "Custom notes\n", section, "helm-chart"
                    )
                    if linked
                    else "Custom notes\n"
                )

                if failure == "notes":
                    body = "<!-- helm-chart:start -->"

                # Package the known source once to simulate persisted remote assets.
                seed_dir = store / "seed"
                seed_dir.mkdir()
                seed = publish_chart.package_chart(root, "1.0.2", seed_dir)

                oci, asset = store / "oci.tgz", store / "asset.tgz"
                shutil.copyfile(seed, oci)
                shutil.copyfile(seed, asset)

                if failure in ("oci-conflict", "asset-conflict"):
                    target = oci if failure == "oci-conflict" else asset
                    with tarfile.open(target, "w:gz") as archive:
                        item = tarfile.TarInfo("nfs-server/Chart.yaml")
                        item.size = 9
                        archive.addfile(item, io.BytesIO(b"different"))

                # A real Pages remote checks that index repair preserves old entries/files.
                pages = store / "pages-source"
                pages.mkdir()
                for args in (
                    ("init", "-q", "-b", "gh-pages"),
                    ("config", "user.name", "Tests"),
                    ("config", "user.email", "tests@example.invalid"),
                ):
                    prepare_release.run(pages, "git", *args)

                old_package_dir = store / "old-packages"
                old_package_dir.mkdir()
                old_chart = store / "old-chart"
                old_chart.mkdir()
                (old_chart / "Chart.yaml").write_text(
                    "apiVersion: v2\nname: nfs-server\nversion: 1.0.1\n"
                )

                prepare_release.run(
                    store,
                    "helm",
                    "package",
                    str(old_chart),
                    "--destination",
                    str(old_package_dir),
                )
                prepare_release.run(
                    store,
                    "helm",
                    "repo",
                    "index",
                    str(old_package_dir),
                    "--url",
                    "https://example.invalid/old",
                )
                shutil.copyfile(old_package_dir / "index.yaml", pages / "index.yaml")

                (pages / "untouched.txt").write_text("Keep me\n")
                prepare_release.run(pages, "git", "add", ".")
                prepare_release.run(pages, "git", "commit", "-qm", "Initial index")
                remote = store / "remote.git"
                prepare_release.run(
                    store, "git", "clone", "--quiet", "--bare", str(pages), str(remote)
                )
                page_sha = prepare_release.run(pages, "git", "rev-parse", "HEAD")

                active_failure = failure
                calls: list[tuple[str, ...]] = []
                workspaces: list[Path] = []
                real_run = release_common.run

                def command(
                    checkout: Path,
                    *args: str,
                    check: bool = True,
                    env: dict[str, str] | None = None,
                ) -> subprocess.CompletedProcess[str]:
                    """Simulate GitHub/OCI/CR, running package and index Git operations locally."""
                    nonlocal oci_exists, github_exists, has_asset, body
                    calls.append(args)
                    output, stderr, status = "", "", 0

                    if args[:3] in (("helm", "lint", str(checkout)),):
                        self.fail("Lint must use the temporary chart")

                    if args[:2] in (("helm", "lint"), ("helm", "package")):
                        return real_run(checkout, *args, check=check, env=env)

                    if args[:3] == ("gh", "auth", "token"):
                        output = "secret-token"
                        if active_failure == "auth":
                            status, stderr = 1, "authentication failed"

                    elif args[:2] == ("git", "ls-remote"):
                        if "--heads" in args:
                            if active_failure == "pages":
                                status = 2
                            else:
                                output = f"{page_sha}\trefs/heads/gh-pages\n"

                        elif github_exists:
                            output = f'{"b" * 40 if active_failure == "tag" else sha}\trefs/tags/nfs-server-1.0.2\n'

                        else:
                            status = 2

                    elif args[:2] == ("gh", "api"):
                        output = json.dumps({"body": body})
                        if not github_exists:
                            status, stderr = 1, "Not Found (HTTP 404)"

                    elif args[:2] == ("helm", "pull"):
                        destination = Path(args[args.index("--destination") + 1])
                        workspaces.append(destination.parent)

                        if active_failure == "registry":
                            status, stderr = 1, "unauthorized"

                        elif not oci_exists:
                            status, stderr = (
                                1,
                                'Error: failed to perform "FetchReference" on source: registry.example/ns/nfs-server:1.0.2: not found',
                            )

                        else:
                            shutil.copyfile(oci, destination / "nfs-server-1.0.2.tgz")
                            stderr = "Digest: " + digest

                    elif args[:3] == ("gh", "release", "view"):
                        output = json.dumps(
                            {
                                "assets": (
                                    [{"name": "nfs-server-1.0.2.tgz"}]
                                    if has_asset
                                    else []
                                )
                            }
                        )

                    elif args[:3] == ("gh", "release", "download"):
                        destination = Path(args[args.index("--dir") + 1])
                        shutil.copyfile(asset, destination / "nfs-server-1.0.2.tgz")

                    elif args[:2] == ("git", "clone"):
                        if active_failure == "clone":
                            status, stderr = 1, "clone failed"
                        else:
                            self.assertIn("--no-checkout", args)
                            return real_run(
                                checkout, *args[:-2], str(remote), args[-1], check=check
                            )

                    elif args[0] == "git" and args[1] in (
                        "config",
                        "checkout",
                        "add",
                        "diff",
                        "commit",
                        "push",
                    ):
                        return real_run(checkout, *args, check=check)

                    elif args[:2] == ("helm", "push"):
                        if active_failure == "push":
                            status, stderr = 1, "push failed"

                        else:
                            oci_exists = True
                            shutil.copyfile(Path(args[2]), oci)
                            stderr = "Digest: " + digest

                    elif args[:2] == ("cr", "upload"):
                        self.assertEqual(env and env.get("CR_TOKEN"), "secret-token")
                        self.assertNotIn("secret-token", args)
                        self.assertEqual(args[args.index("--commit") + 1], sha)
                        package = (
                            Path(args[args.index("--package-path") + 1])
                            / "nfs-server-1.0.2.tgz"
                        )
                        # Simulate release creation followed by an interrupted asset upload.
                        github_exists = True
                        body = (root / ".changes/chart/1.0.2.md").read_text()

                        if active_failure == "upload":
                            status, stderr = 1, "release upload failed"

                        elif active_failure != "asset-upload":
                            has_asset = True
                            shutil.copyfile(package, asset)

                    elif args[:3] == ("gh", "release", "upload"):
                        self.assertNotIn("--clobber", args)

                        if active_failure == "asset-upload":
                            status, stderr = 1, "asset upload failed"
                        else:
                            has_asset = True
                            shutil.copyfile(Path(args[4]), asset)

                    elif args[:3] == ("gh", "release", "edit"):
                        notes = args[args.index("--notes") + 1]
                        self.assertTrue(notes.startswith(body))
                        self.assertIn(section, notes)

                        if active_failure == "edit":
                            status, stderr = 1, "notes update failed"
                        else:
                            body = notes

                    elif args[:2] == ("cr", "index"):
                        self.assertNotEqual(checkout, root)
                        self.assertEqual(env and env.get("CR_TOKEN"), "secret-token")
                        self.assertIn("--push=false", args)
                        self.assertNotIn("--force", args)

                        if active_failure == "index":
                            status, stderr = 1, "index push failed"
                        else:
                            packages = Path(args[args.index("--package-path") + 1])
                            self.assertEqual(
                                (packages / "nfs-server-1.0.2.tgz").read_bytes(),
                                asset.read_bytes(),
                            )
                            current = checkout / "index.yaml"
                            self.assertIn("1.0.1", current.read_text())
                            self.assertEqual(
                                (checkout / "untouched.txt").read_text(), "Keep me\n"
                            )
                            # Simulate cr's no-op when the version is already indexed.
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
                                    "https://github.com/test/repo/releases/download/nfs-server-1.0.2",
                                )
                                shutil.copyfile(
                                    packages / "index.yaml",
                                    Path(args[args.index("--index-path") + 1]),
                                )
                    else:
                        self.fail(f"Unexpected external command: {args}")

                    result = subprocess.CompletedProcess(args, status, output, stderr)
                    if check:
                        result.check_returncode()

                    return result

                with (
                    patch.object(release_common, "run", side_effect=command),
                    patch("publish_chart.shutil.which", return_value="/tools/tool"),
                    redirect_stdout(io.StringIO()),
                ):
                    if failure:
                        with self.assertRaises(
                            (ValueError, subprocess.CalledProcessError)
                        ):
                            publish_chart.publish_chart(
                                root, "test/repo", "registry.example/ns", "gh-pages"
                            )
                        if failure in (
                            "pages",
                            "tag",
                            "auth",
                            "registry",
                            "oci-conflict",
                            "asset-conflict",
                            "notes",
                            "clone",
                        ):
                            self.assertFalse(
                                any(
                                    args[:2]
                                    in (
                                        ("helm", "push"),
                                        ("cr", "upload"),
                                        ("cr", "index"),
                                    )
                                    or args[:3] == ("gh", "release", "edit")
                                    for args in calls
                                )
                            )
                        else:
                            active_failure = ""
                            calls.clear()
                            publish_chart.publish_chart(
                                root, "test/repo", "registry.example/ns", "gh-pages"
                            )
                            if failure != "push":
                                self.assertFalse(
                                    any(args[:2] == ("helm", "push") for args in calls)
                                )
                    else:
                        publish_chart.publish_chart(
                            root, "test/repo", "registry.example/ns", "gh-pages"
                        )

                    if not failure or failure in (
                        "push",
                        "upload",
                        "asset-upload",
                        "edit",
                        "index",
                    ):
                        calls.clear()
                        publish_chart.publish_chart(
                            root, "test/repo", "registry.example/ns", "gh-pages"
                        )
                        self.assertFalse(
                            any(
                                args[:2] in (("helm", "push"), ("cr", "upload"))
                                or args[:3]
                                in (
                                    ("gh", "release", "upload"),
                                    ("gh", "release", "edit"),
                                )
                                for args in calls
                            )
                        )
                        self.assertTrue(
                            any(args[:2] == ("cr", "index") for args in calls)
                        )

                self.assertTrue(all(not path.exists() for path in workspaces))
                self.assertEqual(
                    prepare_release.run(root, "git", "status", "--porcelain"), before
                )

    def test_cli_and_just_arguments(self) -> None:
        """Call the same endpoint outside the checkout, with a path containing spaces."""
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "tools"
            tools.mkdir()
            cr = tools / "cr"
            cr.write_text("#!/bin/sh\nexit 99\n")
            cr.chmod(0o755)
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

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--registry-namespace", result.stdout)

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

            self.assertEqual(result.returncode, 1)
            self.assertIn("--registry-namespace must be", result.stderr)

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
