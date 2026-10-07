#!/usr/bin/env python3
"""Test local release preparation with real Changie in temporary Git repositories."""

from collections.abc import Iterator
from contextlib import contextmanager
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import prepare_release

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def repository() -> Iterator[Path]:
    """Create an isolated committed baseline with chart and image version 1.0.1."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name in (
            ".changie.yaml",
            ".changes/header.md",
            ".changes/image/1.0.1.md",
            ".changes/chart/1.0.1.md",
            "helm/Chart.yaml",
        ):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (root / ".changes/unreleased").mkdir()

        chart = root / "helm/Chart.yaml"
        chart.write_text(
            re.sub(
                r"^(version|appVersion):.*$",
                lambda match: f'{match[1]}: "1.0.1"',
                chart.read_text(),
                flags=re.M,
            )
        )

        for args in (
            ("init", "-q"),
            ("config", "user.email", "test@example.invalid"),
            ("config", "user.name", "Release tests"),
            ("config", "core.fsmonitor", "false"),
            ("add", "."),
            ("commit", "-qm", "Baseline"),
        ):
            prepare_release.run(root, "git", *args)

        yield root


class PreparationTests(unittest.TestCase):
    """Check version coupling, no-op behavior, and preparation preconditions."""

    def test_version_matrix(self) -> None:
        """Generate correct versions, notes, and provenance without creating commits."""
        scenarios: list[tuple[dict[str, str], dict[str, str]]] = [
            ({"image": "Fixed"}, {"image": "1.0.2", "chart": "1.0.2"}),
            ({"image": "Added"}, {"image": "1.1.0", "chart": "1.0.2"}),
            ({"image": "Breaking"}, {"image": "2.0.0", "chart": "1.0.2"}),
            ({"chart": "Fixed"}, {"image": "1.0.1", "chart": "1.0.2"}),
            ({"chart": "Added"}, {"image": "1.0.1", "chart": "1.1.0"}),
            ({"chart": "Breaking"}, {"image": "1.0.1", "chart": "2.0.0"}),
            (
                {"image": "Fixed", "chart": "Added"},
                {"image": "1.0.2", "chart": "1.1.0"},
            ),
            (
                {"image": "Breaking", "chart": "Breaking"},
                {"image": "2.0.0", "chart": "2.0.0"},
            ),
        ]
        for fragments, expected in scenarios:
            with self.subTest(fragments=fragments), repository() as root:
                for project, kind in fragments.items():
                    prepare_release.run(
                        root,
                        "changie",
                        "new",
                        "-j",
                        project,
                        "-k",
                        kind,
                        "-b",
                        f"Test {project} {kind}",
                        "--interactive=false",
                    )
                prepare_release.run(root, "git", "add", ".")
                prepare_release.run(root, "git", "commit", "-qm", "Changes")

                source = prepare_release.run(root, "git", "rev-parse", "HEAD")

                manifest = prepare_release.prepare(root)
                self.assertIsNotNone(manifest)
                if manifest is None:
                    self.fail("Pending fragments must generate a manifest")
                self.assertEqual(manifest["versions"], expected)
                self.assertEqual(
                    manifest["previous"], {"image": "1.0.1", "chart": "1.0.1"}
                )
                self.assertEqual(manifest["image_changed"], "image" in fragments)
                self.assertEqual(manifest["source_sha"], source)
                self.assertEqual(
                    json.loads((root / ".changes/release.json").read_text()), manifest
                )

                self.assertEqual(prepare_release.versions(root), expected)
                self.assertEqual(
                    prepare_release.run(root, "git", "rev-parse", "HEAD"), source
                )
                self.assertFalse(list((root / ".changes/unreleased").glob("*.yaml")))
                for project, version in expected.items():
                    if version != "1.0.1":
                        self.assertTrue(
                            (root / f".changes/{project}/{version}.md").is_file()
                        )

                chart_notes = (
                    root / f'.changes/chart/{expected["chart"]}.md'
                ).read_text()
                if "image" in fragments:
                    self.assertIn(
                        f'Update NFS server image to {expected["image"]}.', chart_notes
                    )

                for component in ("docker", "helm"):
                    self.assertTrue((root / component / "CHANGELOG.md").is_file())

    def test_no_changes(self) -> None:
        """Leave a clean checkout untouched when no fragments are pending."""
        with repository() as root:
            self.assertIsNone(prepare_release.prepare(root))
            self.assertFalse((root / ".changes/release.json").exists())
            self.assertEqual(
                prepare_release.run(root, "git", "status", "--porcelain"), ""
            )

    def test_dirty_checkout(self) -> None:
        """Reject untracked, unstaged, and staged changes before batching."""
        for state in ("untracked", "unstaged", "staged"):
            with self.subTest(state=state), repository() as root:
                target = root / (
                    "new.txt" if state == "untracked" else "helm/Chart.yaml"
                )
                target.write_text(
                    target.read_text() + "# Change\n" if target.exists() else "Change\n"
                )

                if state == "staged":
                    prepare_release.run(root, "git", "add", ".")

                before = target.read_bytes()
                with self.assertRaisesRegex(ValueError, "clean checkout"):
                    prepare_release.prepare(root)
                self.assertEqual(target.read_bytes(), before)

                self.assertFalse((root / ".changes/release.json").exists())

    def test_cli_from_another_directory(self) -> None:
        """Run the explicit-root CLI outside the checkout and retain useful errors."""
        with repository() as root, tempfile.TemporaryDirectory() as directory:
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare_release.py"),
                "--root",
                str(root),
            ]
            result = subprocess.run(
                command,
                cwd=directory,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No releasable changes", result.stdout)

            (root / "new.txt").write_text("Uncommitted\n")
            result = subprocess.run(
                command, cwd=directory, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("clean checkout", result.stderr)


if __name__ == "__main__":
    unittest.main()
