#!/usr/bin/env python3
"""Test image publication with local Git fixtures and mocked external writes."""

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_release
from prepare_release_test import repository
import publish_image
import release_common


@contextmanager
def prepared_repository(image_changed: bool = True) -> Iterator[Path]:
    """Commit real generated metadata with an image or chart fragment."""
    with repository() as root:
        root = root.resolve()
        (root / "docker").mkdir()
        (root / "docker/Dockerfile").write_text("FROM alpine:3.22\n")
        prepare_release.run(
            root,
            "changie",
            "new",
            "-j",
            "image" if image_changed else "chart",
            "-k",
            "Fixed",
            "-b",
            "Test release",
            "--interactive=false",
        )
        prepare_release.run(root, "git", "add", ".")
        prepare_release.run(root, "git", "commit", "-qm", "Changes")
        prepare_release.prepare(root)
        prepare_release.run(root, "git", "add", ".")
        prepare_release.run(root, "git", "commit", "-qm", "Prepare release")
        yield root


class ImagePublicationTests(unittest.TestCase):
    """Exercise local validation and publication failure/retry behavior."""

    def test_default_image_repository(self) -> None:
        """Publish nfsd by default without changing chart or GitHub naming."""
        manifest: prepare_release.ReleaseManifest = {
            "source_sha": "a" * 40,
            "previous": {"image": "1.0.1", "chart": "1.0.1"},
            "versions": {"image": "1.0.2", "chart": "1.0.2"},
            "image_changed": True,
        }

        with (
            patch.object(sys, "argv", ["publish_image.py"]),
            patch.object(publish_image, "validate_manifest", return_value=manifest),
            patch("publish_image.shutil.which", return_value="/tools/tool"),
            patch.object(publish_image, "run"),
            patch.object(prepare_release, "run", return_value="b" * 40),
            patch.object(publish_image, "publish_image") as publish,
            redirect_stdout(io.StringIO()),
        ):
            publish_image.main()
            publish.assert_called_once_with(
                Path(__file__).resolve().parents[1],
                "nebius/nfs-server",
                "cr.eu-north1.nebius.cloud/soperator/nfsd:1.0.2",
                "1.0.2",
                "b" * 40,
            )

    def test_cli_from_another_directory(self) -> None:
        """Use the same Just endpoint outside the checkout without publishing a chart-only release."""
        with (
            prepared_repository(False) as root,
            tempfile.TemporaryDirectory() as directory,
        ):
            alias = Path(directory) / "checkout with spaces"
            alias.symlink_to(root, target_is_directory=True)
            justfile = Path(__file__).resolve().parents[1] / "justfile"
            result = subprocess.run(
                [
                    "just",
                    "--justfile",
                    str(justfile),
                    "release",
                    "publish-image",
                    "--root",
                    str(alias),
                ],
                cwd=directory,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Chart-only release", result.stdout)

    def test_manifest_validation(self) -> None:
        """Reject dirty, malformed, inconsistent, missing-note, and changed-source releases."""
        for failure in (
            "",
            "dirty",
            "boolean",
            "baseline",
            "versions",
            "notes",
            "source",
        ):
            with self.subTest(failure=failure), prepared_repository() as root:
                path = root / ".changes/release.json"
                data: dict[str, object] = json.loads(path.read_text())
                if failure == "dirty":
                    (root / "uncommitted.txt").write_text("Change\n")
                elif failure == "boolean":
                    data["image_changed"] = "true"
                elif failure == "baseline":
                    data["previous"] = {"image": "1.0.0", "chart": "1.0.1"}
                elif failure == "versions":
                    data["versions"] = {"image": "1.0.9", "chart": "1.0.2"}
                elif failure == "notes":
                    (root / ".changes/image/1.0.2.md").unlink()
                elif failure == "source":
                    (root / "docker/Dockerfile").write_text("FROM alpine:other\n")
                if failure and failure != "dirty":
                    path.write_text(json.dumps(data) + "\n")
                    prepare_release.run(root, "git", "add", "-A")
                    prepare_release.run(root, "git", "commit", "-qm", "Invalid release")
                if failure:
                    with self.assertRaises(ValueError):
                        publish_image.validate_manifest(root)
                else:
                    self.assertTrue(
                        publish_image.validate_manifest(root)["image_changed"]
                    )
        with (
            repository() as root,
            self.assertRaisesRegex(ValueError, "No release manifest"),
        ):
            publish_image.validate_manifest(root)

    def test_manifest_shapes(self) -> None:
        """Reject incorrectly shaped JSON and non-stable version strings."""
        invalid: list[object] = [
            None,
            [],
            {},
            {"image": 1, "chart": "1.0.1"},
            {"image": "01.0.1", "chart": "1.0.1"},
            {"image": "1.0.1-rc1", "chart": "1.0.1"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                release_common.manifest_versions(value)

    def test_tag_checks(self) -> None:
        """Handle absent, lightweight, annotated, conflicting, and failed tag lookups."""
        sha = "a" * 40
        ref = "refs/tags/image-v1.0.2"
        cases = [
            (2, "", ""),
            (0, f"{sha}\t{ref}\n", ""),
            (0, f'{"b" * 40}\t{ref}\n{sha}\t{ref}^{{}}\n', ""),
            (0, f'{"b" * 40}\t{ref}\n', "conflict"),
            (0, "broken", "malformed"),
            (128, "", "network"),
        ]
        for status, output, failure in cases:
            with (
                self.subTest(failure=failure),
                patch.object(
                    release_common,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["git"], status, output, "network error"
                    ),
                ),
            ):
                if failure:
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        publish_image.verify_tag(
                            Path("."), "test/repo", "image-v1.0.2", sha
                        )
                else:
                    publish_image.verify_tag(
                        Path("."), "test/repo", "image-v1.0.2", sha
                    )

    def test_http_and_json_errors(self) -> None:
        """Treat only explicit 404 as missing and fail closed on invalid responses."""
        for status, output, stderr in (
            (0, "{}", ""),
            (1, "", "Not Found (HTTP 404)"),
            (1, "", "Forbidden (HTTP 403)"),
            (1, "", "network"),
            (0, "[]", ""),
            (0, "{", ""),
        ):
            with (
                self.subTest(status=status, output=output, stderr=stderr),
                patch.object(
                    release_common,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["gh"], status, output, stderr
                    ),
                ),
            ):
                if (status and "404" not in stderr) or output in ("[]", "{"):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        publish_image.release_body(Path("."), "test/repo", "test")
                else:
                    self.assertEqual(
                        publish_image.release_body(Path("."), "test/repo", "test"),
                        "" if status == 0 else None,
                    )
        for output in ('{"body": 1}', '{"body": []}'):
            with (
                self.subTest(output=output),
                patch.object(
                    release_common,
                    "run",
                    return_value=subprocess.CompletedProcess(["gh"], 0, output, ""),
                ),
                self.assertRaises(ValueError),
            ):
                publish_image.release_body(Path("."), "test/repo", "test")

    def test_platform_validation(self) -> None:
        """Require exactly the supported platforms with valid nonduplicate digests."""
        valid = [
            {
                "platform": {"os": "linux", "architecture": arch},
                "digest": "sha256:" + digit * 64,
            }
            for arch, digit in (("amd64", "a"), ("arm64", "b"))
        ]
        self.assertEqual(
            set(publish_image.platform_digests(json.dumps({"manifests": valid}))),
            {"amd64", "arm64"},
        )
        invalid: list[object] = [
            {},
            {"manifests": []},
            {"manifests": valid[:1]},
            {"manifests": valid + valid[:1]},
            {"manifests": [None]},
            {
                "manifests": [
                    {
                        "platform": {"os": "linux", "architecture": "amd64"},
                        "digest": "bad",
                    },
                    valid[1],
                ]
            },
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                publish_image.platform_digests(json.dumps(value))

    def test_release_sections(self) -> None:
        """Preserve notes and make identical retries a no-op; reject conflicts."""
        section = publish_image.image_section(
            "registry.example/ns/image:1.0.2", "sha256:" + "1" * 64
        )
        original = "Custom notes\n"
        notes = publish_image.add_image_section(original, section)
        self.assertTrue(notes.startswith(original))
        self.assertIn("docker pull registry.example/ns/image:1.0.2", notes)
        self.assertIn("registry.example/ns/image@sha256:", notes)
        self.assertEqual(publish_image.add_image_section(notes, section), notes)
        self.assertEqual(publish_image.add_image_section("", section), section + "\n")
        for body in (
            "<!-- container-image:start -->",
            "<!-- container-image:end -->",
            "<!-- container-image:end --><!-- container-image:start -->",
            notes + section,
            notes.replace("image:1.0.2", "image:1.0.3"),
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                publish_image.add_image_section(body, section)

    def test_publication_scenarios(self) -> None:
        """Exercise registry-only CLI publication, failures, and resumable retries."""
        scenarios = [
            (False, False, False, True, ""),
            (True, False, False, True, ""),
            (True, True, False, True, ""),
            (True, True, True, True, ""),
            (True, False, False, False, ""),
            *[
                (True, True, True, True, failure)
                for failure in (
                    "tag",
                    "http",
                    "registry",
                    "manifest",
                    "digest",
                    "revision",
                    "arm64-revision",
                    "version",
                    "platform",
                    "metadata",
                    "notes",
                    "conflict",
                )
            ],
            (False, False, False, True, "build"),
            (False, False, False, True, "create"),
            (True, True, False, True, "edit"),
        ]
        for registry_exists, github_exists, linked, changed, failure in scenarios:
            with (
                self.subTest(
                    registry=registry_exists,
                    github=github_exists,
                    linked=linked,
                    failure=failure,
                ),
                prepared_repository(changed) as root,
            ):
                sha = prepare_release.run(root, "git", "rev-parse", "HEAD")
                calls: list[tuple[str, ...]] = []
                built = False
                active_failure = failure
                digest = "sha256:" + "1" * 64
                image = "registry.example/ns/image:1.0.2"
                section = publish_image.image_section(image, digest)
                body = "Custom notes\n"
                if linked:
                    body = publish_image.add_image_section(body, section)
                if failure == "conflict":
                    body = body.replace("image:1.0.2", "image:1.0.3")
                if failure == "notes":
                    body = "<!-- container-image:start -->"
                digests = {
                    "sha256:" + digit * 64: platform
                    for platform, digit in (("amd64", "a"), ("arm64", "b"))
                }

                def command(
                    checkout: Path, *args: str, check: bool = True
                ) -> subprocess.CompletedProcess[str]:
                    """Simulate registry and GitHub state, rejecting materialization commands."""
                    nonlocal built, github_exists, body
                    self.assertEqual(checkout, root)
                    calls.append(args)
                    output, stderr, status = "", "", 0
                    if args[:2] == ("git", "ls-remote"):
                        if github_exists:
                            output = f'{"b" * 40 if active_failure == "tag" else sha}\trefs/tags/image-v1.0.2\n'
                        else:
                            status = 2
                    elif args in (("docker", "info"), ("docker", "buildx", "version")):
                        pass
                    elif args[:3] == ("docker", "buildx", "build"):
                        self.assertIn(f"org.opencontainers.image.revision={sha}", args)
                        self.assertIn("org.opencontainers.image.version=1.0.2", args)
                        self.assertIn("--push", args)
                        self.assertEqual(
                            args[args.index("--platform") + 1],
                            "linux/amd64,linux/arm64",
                        )
                        self.assertEqual(args[args.index("--tag") + 1], image)
                        if active_failure == "build":
                            status, stderr = 1, "build failed"
                        else:
                            built = True
                    elif args[:3] == ("docker", "buildx", "imagetools"):
                        if active_failure == "registry":
                            status, stderr = 1, "authentication failed"
                        elif not registry_exists and not built:
                            status, stderr = 1, "manifest unknown"
                        elif args[-1] == "--raw":
                            self.assertEqual(args[4], image + "@" + digest)
                            output = (
                                "{}"
                                if active_failure == "manifest"
                                else json.dumps(
                                    {
                                        "manifests": [
                                            {
                                                "digest": ref,
                                                "platform": {
                                                    "os": "linux",
                                                    "architecture": arch,
                                                },
                                            }
                                            for ref, arch in digests.items()
                                        ]
                                    }
                                )
                            )
                        elif "--format" in args:
                            self.assertEqual(args[-1], "{{json .Image}}")
                            arch = digests[args[4].split("@")[1]]
                            output = (
                                "{}"
                                if active_failure == "metadata"
                                else json.dumps(
                                    {
                                        "os": "linux",
                                        "architecture": (
                                            "other"
                                            if active_failure == "platform"
                                            else arch
                                        ),
                                        "config": {
                                            "Labels": {
                                                "org.opencontainers.image.revision": (
                                                    "other"
                                                    if active_failure == "revision"
                                                    or (
                                                        active_failure
                                                        == "arm64-revision"
                                                        and arch == "arm64"
                                                    )
                                                    else sha
                                                ),
                                                "org.opencontainers.image.version": (
                                                    "other"
                                                    if active_failure == "version"
                                                    else "1.0.2"
                                                ),
                                            }
                                        },
                                    }
                                )
                            )
                        else:
                            output = "Digest: " + (
                                "bad" if active_failure == "digest" else digest
                            )
                    elif args[:2] == ("gh", "api"):
                        self.assertEqual(
                            args[2], "repos/test/repo/releases/tags/image-v1.0.2"
                        )
                        output = json.dumps(
                            {"body": body, "assets": [{"name": "untouched.tar.gz"}]}
                        )
                        if active_failure == "http":
                            status, stderr = 1, "Forbidden (HTTP 403)"
                        elif not github_exists:
                            status, stderr = 1, "Not Found (HTTP 404)"
                    elif args[:3] in (
                        ("gh", "release", "create"),
                        ("gh", "release", "edit"),
                    ):
                        action = args[2]
                        self.assertEqual(args[args.index("--repo") + 1], "test/repo")
                        notes = args[args.index("--notes") + 1]
                        self.assertIn(section, notes)
                        if action == "create":
                            self.assertEqual(args[args.index("--target") + 1], sha)
                            self.assertIn(
                                (root / ".changes/image/1.0.2.md").read_text(), notes
                            )
                        else:
                            self.assertTrue(notes.startswith(body))
                        if active_failure == action:
                            status, stderr = 1, "GitHub write failed"
                        else:
                            github_exists = True
                            body = notes
                    else:
                        self.fail(f"Unexpected or materializing command: {args}")
                    result = subprocess.CompletedProcess(args, status, output, stderr)
                    if check:
                        result.check_returncode()
                    return result

                argv = [
                    "publish_image.py",
                    "--root",
                    str(root),
                    "--registry-repository",
                    "registry.example/ns/image",
                    "--github-repository",
                    "test/repo",
                ]
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(publish_image, "run", side_effect=command),
                    patch.object(release_common, "run", side_effect=command),
                    patch("publish_image.shutil.which", return_value="/tools/tool"),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    if failure:
                        with self.assertRaises(SystemExit) as error:
                            publish_image.main()
                        self.assertEqual(error.exception.code, 1)
                    else:
                        publish_image.main()
                    writes = [args for args in calls if args[:2] == ("gh", "release")]
                    if failure:
                        self.assertEqual(
                            len(writes), int(failure in ("create", "edit"))
                        )
                        if failure in ("create", "edit"):
                            self.assertEqual(calls[-1], writes[0])
                            # The registry image survives a failed GitHub write.
                            active_failure = ""
                            calls.clear()
                            publish_image.main()
                            self.assertFalse(
                                any(
                                    args[:3] == ("docker", "buildx", "build")
                                    for args in calls
                                )
                            )
                            self.assertEqual(
                                sum(args[:2] == ("gh", "release") for args in calls), 1
                            )
                            calls.clear()
                            publish_image.main()
                            self.assertFalse(
                                any(args[:2] == ("gh", "release") for args in calls)
                            )
                    elif not changed:
                        self.assertFalse(calls)
                    else:
                        self.assertEqual(built, not registry_exists)
                        self.assertEqual(len(writes), int(not linked))
                        self.assertEqual(
                            {
                                args[4].split("@")[1]
                                for args in calls
                                if args[:3] == ("docker", "buildx", "imagetools")
                                and "--format" in args
                            },
                            set(digests),
                        )


if __name__ == "__main__":
    unittest.main()
