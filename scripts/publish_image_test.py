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
from unittest.mock import MagicMock, patch

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


SHA = "a" * 40
DIGEST = "sha256:" + "1" * 64
IMAGE = "registry.example/ns/image:1.0.2"


def inspections() -> list[subprocess.CompletedProcess[str]]:
    """Return successful inspect responses for both immutable platform images."""
    platforms = [
        {
            "digest": "sha256:" + digit * 64,
            "platform": {"os": "linux", "architecture": arch},
        }
        for arch, digit in (("amd64", "a"), ("arm64", "b"))
    ]
    metadata = [
        {
            "os": "linux",
            "architecture": arch,
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": SHA,
                    "org.opencontainers.image.version": "1.0.2",
                }
            },
        }
        for arch in ("amd64", "arm64")
    ]
    return [
        subprocess.CompletedProcess([], 0, output, "")
        for output in (
            "Digest: " + DIGEST,
            json.dumps({"manifests": platforms}),
            *(json.dumps(value) for value in metadata),
        )
    ]


@contextmanager
def publication(body: str | None = None) -> Iterator[tuple[Path, MagicMock]]:
    """Isolate publication orchestration from Git, registry, and GitHub reads."""
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(publish_image, "verify_tag"),
        patch.object(publish_image, "release_body", return_value=body),
        patch.object(publish_image, "run") as command,
    ):
        root = Path(directory)
        notes = root / ".changes/image/1.0.2.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("Custom notes\n")
        yield (root, command)


class ImagePublicationTests(unittest.TestCase):
    """Check validation, immutable images, and resumable GitHub writes."""

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
            assert result.returncode == 0, result.stderr
            assert "Chart-only release" in result.stdout

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
            with prepared_repository() as root:
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
                    assert publish_image.validate_manifest(root)["image_changed"]
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
            with self.assertRaises(ValueError):
                release_common.manifest_versions(value)

    def test_tag_checks(self) -> None:
        """Handle absent, lightweight, annotated, conflicting, and failed tag lookups."""
        sha = "a" * 40
        ref = "refs/tags/image-v1.0.2"
        cases = [
            (2, "", ""),
            (0, f"{sha}\t{ref}\n", ""),
            (0, f"{'b' * 40}\t{ref}\n{sha}\t{ref}^{{}}\n", ""),
            (0, f"{'b' * 40}\t{ref}\n", "conflict"),
            (0, "broken", "malformed"),
            (128, "", "network"),
        ]
        for status, output, failure in cases:
            with patch.object(
                release_common,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git"], status, output, "network error"
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
            with patch.object(
                release_common,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["gh"], status, output, stderr
                ),
            ):
                if status and "404" not in stderr or output in ("[]", "{"):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        publish_image.release_body(Path("."), "test/repo", "test")
                else:
                    assert publish_image.release_body(
                        Path("."), "test/repo", "test"
                    ) == ("" if status == 0 else None)
        for output in ('{"body": 1}', '{"body": []}'):
            with (
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
        assert set(
            publish_image.platform_digests(json.dumps({"manifests": valid}))
        ) == {"amd64", "arm64"}
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
            with self.assertRaises(ValueError):
                publish_image.platform_digests(json.dumps(value))

    def test_release_sections(self) -> None:
        """Preserve notes and make identical retries a no-op; reject conflicts."""
        section = publish_image.image_section(
            "registry.example/ns/image:1.0.2", "sha256:" + "1" * 64
        )
        original = "Custom notes\n"
        notes = publish_image.add_image_section(original, section)
        assert notes.startswith(original)
        assert "docker pull registry.example/ns/image:1.0.2" in notes
        assert "registry.example/ns/image@sha256:" in notes
        assert publish_image.add_image_section(notes, section) == notes
        assert publish_image.add_image_section("", section) == section + "\n"
        for body in (
            "<!-- container-image:start -->",
            "<!-- container-image:end -->",
            "<!-- container-image:end --><!-- container-image:start -->",
            notes + section,
            notes.replace("image:1.0.2", "image:1.0.3"),
        ):
            with self.assertRaises(ValueError):
                publish_image.add_image_section(body, section)

    def test_publication_scenarios(self) -> None:
        """Build only missing images and leave already-linked releases untouched."""
        linked = publish_image.add_image_section(
            "Custom notes\n", publish_image.image_section(IMAGE, DIGEST)
        )
        for exists, body in (
            (False, None),
            (True, None),
            (True, "Custom notes\n"),
            (True, linked),
        ):
            with publication(body) as (root, command):
                responses = inspections()
                if not exists:
                    responses = [
                        subprocess.CompletedProcess([], 1, "", "manifest unknown"),
                        subprocess.CompletedProcess([], 0, "", ""),
                        *responses,
                    ]
                command.side_effect = responses + [
                    subprocess.CompletedProcess([], 0, "", "")
                ]
                publish_image.publish_image(root, "test/repo", IMAGE, "1.0.2", SHA)
                calls = [call.args[1:] for call in command.call_args_list]
                builds = [
                    args for args in calls if args[:3] == ("docker", "buildx", "build")
                ]
                assert len(builds) == int(not exists)
                if builds:
                    assert "--push" in builds[0]
                    assert (
                        builds[0][builds[0].index("--platform") + 1]
                        == "linux/amd64,linux/arm64"
                    )
                    assert f"org.opencontainers.image.revision={SHA}" in builds[0]
                    assert "org.opencontainers.image.version=1.0.2" in builds[0]
                    assert builds[0][builds[0].index("--tag") + 1] == IMAGE
                writes = [args for args in calls if args[:2] == ("gh", "release")]
                assert len(writes) == int(body != linked)
                if writes:
                    assert writes[0][2] == ("create" if body is None else "edit")
                    assert linked in writes[0][-1]
                    assert "upload" not in writes[0]
                    if body is None:
                        assert writes[0][writes[0].index("--target") + 1] == SHA

    def test_publication_retries(self) -> None:
        """Retry failed GitHub writes without rebuilding the persisted image."""
        for body in (None, "Custom notes\n"):
            with publication(body) as (root, command):
                command.side_effect = [
                    *inspections(),
                    subprocess.CalledProcessError(1, ["gh"], stderr="write failed"),
                ]
                with self.assertRaises(subprocess.CalledProcessError):
                    publish_image.publish_image(root, "test/repo", IMAGE, "1.0.2", SHA)
                command.reset_mock()
                command.side_effect = [
                    *inspections(),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
                publish_image.publish_image(root, "test/repo", IMAGE, "1.0.2", SHA)
                assert not any(
                    call.args[1:4] == ("docker", "buildx", "build")
                    for call in command.call_args_list
                )
                assert (
                    sum(
                        call.args[1:3] == ("gh", "release")
                        for call in command.call_args_list
                    )
                    == 1
                )

    def test_publication_rejects_invalid_images(self) -> None:
        """Fail before GitHub writes on registry, digest, platform, or label errors."""
        for failure in (
            "auth",
            "digest",
            "manifest",
            "amd64-revision",
            "arm64-revision",
            "version",
            "platform",
            "build",
        ):
            with publication() as (root, command):
                responses = inspections()
                if failure in ("auth", "build"):
                    if failure == "build":
                        command.side_effect = [
                            subprocess.CompletedProcess([], 1, "", "manifest unknown"),
                            subprocess.CalledProcessError(
                                1, ["docker"], stderr="build failed"
                            ),
                        ]
                    else:
                        command.side_effect = [
                            subprocess.CompletedProcess([], 1, "", "unauthorized")
                        ]
                else:
                    if failure == "digest":
                        responses[0].stdout = "Digest: bad"
                    elif failure == "manifest":
                        responses[1].stdout = "{}"
                    else:
                        index = 2 if failure == "amd64-revision" else 3
                        metadata = json.loads(responses[index].stdout)
                        if failure == "platform":
                            metadata["architecture"] = "other"
                        else:
                            key = "version" if failure == "version" else "revision"
                            metadata["config"]["Labels"][
                                f"org.opencontainers.image.{key}"
                            ] = "other"
                        responses[index].stdout = json.dumps(metadata)
                    command.side_effect = responses
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    publish_image.publish_image(root, "test/repo", IMAGE, "1.0.2", SHA)
                assert not any(call.args[1] == "gh" for call in command.call_args_list)


if __name__ == "__main__":
    unittest.main()
