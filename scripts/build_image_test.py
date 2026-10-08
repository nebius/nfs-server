"""Check environment configuration, native builds, and digest-based publication."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_image
import prepare_release
import publish_image
import release_common
from publish_image_test import DIGEST, IMAGE, SHA, inspections, publication


class NativeImageTests(unittest.TestCase):
    """Keep native builds and manifest assembly subject to release validation."""

    def test_registry_configuration(self) -> None:
        """Use explicit overrides first, and reject missing or malformed settings."""
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "NEBIUS_REGISTRY_HOST"):
                release_common.registry_namespace()
            assert (
                release_common.image_repository("registry.example/override/nfsd")
                == "registry.example/override/nfsd"
            )
        with patch.dict(
            "os.environ",
            {
                "NEBIUS_REGISTRY_HOST": "registry.example:5000",
                "NEBIUS_REGISTRY_NAMESPACE": "team/project",
            },
        ):
            assert (
                release_common.registry_namespace()
                == "registry.example:5000/team/project"
            )
            assert (
                release_common.image_repository()
                == "registry.example:5000/team/project/nfsd"
            )
            assert (
                release_common.registry_namespace("other.example/project")
                == "other.example/project"
            )
            for host, namespace in (
                ("https://registry.example", "team"),
                ("registry.example/team", "team"),
                ("registry.example", "team:tag"),
                ("registry.example", "/team"),
                ("", "team"),
            ):
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "NEBIUS_REGISTRY_HOST": host,
                            "NEBIUS_REGISTRY_NAMESPACE": namespace,
                        },
                    ),
                    self.assertRaises(ValueError),
                ):
                    release_common.registry_namespace()

    def test_native_build_and_reuse(self) -> None:
        """Push by digest only, preserving both architecture and provenance labels."""
        manifest: prepare_release.ReleaseManifest = {
            "source_sha": SHA,
            "previous": {"chart": "1.0.1", "image": "1.0.1"},
            "versions": {"chart": "1.0.2", "image": "1.0.2"},
            "image_changed": True,
        }
        for arch in ("amd64", "arm64"):
            for existing in (False, True):
                with (
                    patch.object(
                        release_common, "validate_manifest", return_value=manifest
                    ),
                    patch.object(prepare_release, "run", return_value=SHA),
                    patch.object(release_common, "verify_tag"),
                    patch.object(
                        publish_image,
                        "inspect_image",
                        return_value=inspections()[0] if existing else None,
                    ),
                    patch.object(
                        publish_image,
                        "verify_image",
                        return_value=(DIGEST, {"amd64": DIGEST, "arm64": DIGEST}),
                    ),
                    patch.object(publish_image, "verify_platform") as verify,
                ):

                    def command(
                        root: Path, *args: str
                    ) -> subprocess.CompletedProcess[str]:
                        """Write only the temporary Buildx metadata expected by the script."""
                        assert args[:3] == ("docker", "buildx", "build")
                        assert args[args.index("--platform") + 1] == f"linux/{arch}"
                        assert (
                            args[args.index("--output") + 1]
                            == "type=image,name=registry.example/ns/image,push-by-digest=true,name-canonical=true,push=true"
                        )
                        assert "--tag" not in args
                        assert f"org.opencontainers.image.revision={SHA}" in args
                        assert "org.opencontainers.image.version=1.0.2" in args
                        Path(args[args.index("--metadata-file") + 1]).write_text(
                            json.dumps({"containerimage.digest": DIGEST})
                        )
                        return subprocess.CompletedProcess(args, 0, "", "")

                    with patch.object(
                        release_common, "run", side_effect=command
                    ) as run:
                        assert (
                            build_image.build(
                                Path("."),
                                "registry.example/ns/image",
                                "test/repo",
                                f"linux/{arch}",
                            )
                            == DIGEST
                        )
                        assert run.call_count == int(not existing)
                        if not existing:
                            verify.assert_called_once_with(
                                Path("."),
                                f"registry.example/ns/image@{DIGEST}",
                                "1.0.2",
                                SHA,
                                arch,
                            )

    def test_digest_file_cli(self) -> None:
        """Keep the CLI callable outside the checkout and write a plain digest file."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "digests/arm64.digest"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "build_image.py",
                        "--platform",
                        "linux/arm64",
                        "--digest-file",
                        str(output),
                        "--registry-repository",
                        "registry.example/ns/image",
                    ],
                ),
                patch.object(release_common, "run"),
                patch.object(build_image, "build", return_value=DIGEST),
            ):
                build_image.main()
                assert output.read_text() == DIGEST + "\n"

    def test_invalid_build_metadata(self) -> None:
        """Reject a failed or malformed native build without trusting its digest."""
        manifest: prepare_release.ReleaseManifest = {
            "source_sha": SHA,
            "previous": {"chart": "1.0.1", "image": "1.0.1"},
            "versions": {"chart": "1.0.2", "image": "1.0.2"},
            "image_changed": True,
        }
        for failure in ("metadata", "json", "build"):
            with (
                patch.object(
                    release_common, "validate_manifest", return_value=manifest
                ),
                patch.object(prepare_release, "run", return_value=SHA),
                patch.object(release_common, "verify_tag"),
                patch.object(publish_image, "inspect_image", return_value=None),
                patch.object(publish_image, "verify_platform") as verify,
                patch.object(
                    Path,
                    "read_text",
                    return_value=(
                        "{" if failure == "json" else '{"containerimage.digest":"bad"}'
                    ),
                ),
                patch.object(
                    release_common,
                    "run",
                    side_effect=(
                        subprocess.CalledProcessError(1, ["docker"])
                        if failure == "build"
                        else None
                    ),
                ),
                self.assertRaises((ValueError, subprocess.CalledProcessError)),
            ):
                build_image.build(
                    Path("."), "registry.example/ns/image", "test/repo", "linux/amd64"
                )
            verify.assert_not_called()

    def test_manifest_assembly(self) -> None:
        """Combine verified digests only when missing; reject incompatible existing tags."""
        inputs = ["sha256:" + digit * 64 for digit in ("a", "b")]
        for existing in (False, True):
            with publication() as (root, command):
                replies = inspections()[2:]
                if existing:
                    replies += inspections()
                else:
                    replies += [
                        subprocess.CompletedProcess([], 1, "", "manifest unknown"),
                        subprocess.CompletedProcess([], 0, "", ""),
                        *inspections(),
                    ]
                command.side_effect = replies + [
                    subprocess.CompletedProcess([], 0, "", "")
                ]
                publish_image.publish_image(
                    root, "test/repo", IMAGE, "1.0.2", SHA, inputs
                )
                creates = [
                    call.args[1:]
                    for call in command.call_args_list
                    if call.args[1:5] == ("docker", "buildx", "imagetools", "create")
                ]
                assert len(creates) == int(not existing)
                if creates:
                    assert creates[0][-2:] == tuple(
                        f"registry.example/ns/image@{digest}" for digest in inputs
                    )
                assert not any(
                    call.args[1:4] == ("docker", "buildx", "build")
                    for call in command.call_args_list
                )
        for inputs in ([DIGEST], [DIGEST, DIGEST], [DIGEST, "bad"]):
            with publication() as (root, command), self.assertRaises(ValueError):
                publish_image.publish_image(
                    root, "test/repo", IMAGE, "1.0.2", SHA, inputs
                )
            assert not command.called
        with publication() as (root, command):
            command.side_effect = [inspections()[2], inspections()[2]]
            with self.assertRaisesRegex(ValueError, "Duplicate platform"):
                publish_image.publish_image(
                    root,
                    "test/repo",
                    IMAGE,
                    "1.0.2",
                    SHA,
                    ["sha256:" + "a" * 64, "sha256:" + "b" * 64],
                )
            assert command.call_count == 2
        with publication() as (root, command):
            command.side_effect = [*inspections()[2:], *inspections()]
            with self.assertRaisesRegex(ValueError, "different platform digests"):
                publish_image.publish_image(
                    root,
                    "test/repo",
                    IMAGE,
                    "1.0.2",
                    SHA,
                    ["sha256:" + "c" * 64, "sha256:" + "d" * 64],
                )
            assert not any(
                call.args[1] == "gh"
                or call.args[1:5] == ("docker", "buildx", "imagetools", "create")
                for call in command.call_args_list
            )
