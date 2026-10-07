"""Exercise workflow shell steps without GitHub or registry writes."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import prepare_release
from prepare_release_test import ROOT, repository


def step(workflow: str, name: str) -> str:
    """Read a named literal shell block from our consistently indented workflows."""
    lines = (ROOT / ".github/workflows" / workflow).read_text().splitlines()
    start = lines.index(f"      - name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")

    body: list[str] = []
    for line in lines[run + 1 :]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:] if line else "")

    return "\n".join(body)


def shell(
    root: Path, code: str, variables: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run an extracted step with explicit fixture environment variables."""
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **variables},
        capture_output=True,
        text=True,
    )


class WorkflowTests(unittest.TestCase):
    """Cover branch policy, check aggregation, editable preparation, and promotion."""

    def test_branch_policy(self) -> None:
        """Reject direct feature and fork-next PRs into main, but allow feature work."""
        code = step(
            "testing.yaml", "Allow only this repository's next branch to target main"
        )

        for event, base, head, source, valid in (
            ("pull_request", "main", "next", "test/repo", True),
            ("pull_request", "main", "feature", "test/repo", False),
            ("pull_request", "main", "next", "fork/repo", False),
            ("pull_request", "next", "feature", "test/repo", True),
            ("workflow_dispatch", "", "", "", True),
            ("push", "", "", "", True),
        ):
            with self.subTest(event=event, base=base, head=head, source=source):
                result = shell(
                    ROOT,
                    code,
                    {
                        "EVENT": event,
                        "BASE": base,
                        "HEAD": head,
                        "SOURCE_REPOSITORY": source,
                        "GITHUB_REPOSITORY": "test/repo",
                    },
                )
                self.assertEqual(result.returncode == 0, valid, result.stderr)

    def test_required_result(self) -> None:
        """Only unselected test skips pass; failures and cancellations block merge."""
        code = step(
            "testing.yaml",
            "Require successful checks, allowing only unselected tests to skip",
        )

        jobs: dict[str, dict[str, object]] = {
            name: {"result": "success"}
            for name in (
                "policy",
                "changes",
                "lint",
                "scripts-test",
                "docker-test",
                "helm-test",
            )
        }
        jobs["changes"]["outputs"] = {
            "scripts": "true",
            "docker": "true",
            "helm": "true",
        }
        self.assertEqual(shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode, 0)

        for name in jobs:
            for status in ("failure", "cancelled", "skipped"):
                with self.subTest(job=name, status=status):
                    changed = {key: dict(value) for key, value in jobs.items()}
                    changed[name]["result"] = status
                    self.assertNotEqual(
                        shell(ROOT, code, {"RESULTS": json.dumps(changed)}).returncode,
                        0,
                    )

        for component in ("scripts", "docker", "helm"):
            jobs[f"{component}-test"]["result"] = "skipped"

        jobs["changes"]["outputs"] = {
            "scripts": "false",
            "docker": "false",
            "helm": "false",
        }
        self.assertEqual(shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode, 0)

        # Missing selection outputs must not silently count as unselected tests.
        jobs["changes"]["outputs"] = {}
        self.assertNotEqual(
            shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode, 0
        )

    def test_preparation_pr_and_noop(self) -> None:
        """Prepare real files, push only to a temporary bare repo, and mock PR writes."""
        for changed in (False, True):
            with self.subTest(changed=changed), repository() as root:
                with tempfile.TemporaryDirectory() as directory:
                    work = Path(directory)
                    remote = work / "remote.git"

                    prepare_release.run(root, "git", "init", "--bare", str(remote))
                    prepare_release.run(
                        root, "git", "remote", "add", "origin", str(remote)
                    )

                    if changed:
                        prepare_release.run(
                            root,
                            "changie",
                            "new",
                            "-j",
                            "chart",
                            "-k",
                            "Fixed",
                            "-b",
                            "Workflow fixture",
                            "--interactive=false",
                        )
                        prepare_release.run(root, "git", "add", ".")
                        prepare_release.run(root, "git", "commit", "-qm", "Fragment")

                    before = prepare_release.run(root, "git", "rev-parse", "HEAD")
                    output, summary, calls = (
                        work / name for name in ("output", "summary", "calls")
                    )

                    gh = work / "gh"
                    gh.write_text(
                        '#!/bin/sh\nprintf "%s\\n" "$@" >> "$GH_CALLS"\n'
                        'printf "%s\\n" "${GH_PR_URL:-https://example.invalid/pr/1}"\n'
                    )
                    gh.chmod(0o755)

                    variables = {
                        "PATH": f"{work}:{os.environ['PATH']}",
                        "GH_CALLS": str(calls),
                        "GITHUB_OUTPUT": str(output),
                        "GITHUB_STEP_SUMMARY": str(summary),
                        "GITHUB_REPOSITORY": "test/repo",
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "PREPARE_SCRIPT": str(ROOT / "scripts/prepare_release.py"),
                        "PYTHON_EXECUTABLE": sys.executable,
                    }
                    code = (
                        'just() { "$PYTHON_EXECUTABLE" "$PREPARE_SCRIPT" --root .; }\n'
                        + step(
                            "prepare-release.yaml", "Generate prepared release files"
                        )
                    )

                    result = shell(root, code, variables)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(
                        f"prepared={str(changed).lower()}", output.read_text()
                    )

                    if not changed:
                        self.assertEqual(
                            prepare_release.run(root, "git", "rev-parse", "HEAD"),
                            before,
                        )
                        self.assertFalse(calls.exists())
                        continue

                    result = shell(
                        root,
                        step(
                            "prepare-release.yaml",
                            "Commit prepared files and open an editable PR",
                        ),
                        variables,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        prepare_release.run(root, "git", "status", "--porcelain"), ""
                    )
                    self.assertIn(
                        "release/prepare-123-1",
                        prepare_release.run(root, "git", "ls-remote", str(remote)),
                    )
                    self.assertIn("\n--base\nnext\n", calls.read_text())
                    self.assertIn("Manual edits are welcome", calls.read_text())
                    self.assertIn("Approve workflows to run", calls.read_text())

                    manual = root / "helm/CHANGELOG.md"
                    manual.write_text(manual.read_text() + "Manual notes\n")
                    content = manual.read_bytes()
                    variables["GH_PR_URL"] = "https://example.invalid/pr/1"

                    result = shell(
                        root,
                        step(
                            "prepare-release.yaml", "Preserve any open preparation PR"
                        ),
                        variables,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(manual.read_bytes(), content)
                    self.assertIn(
                        "url=https://example.invalid/pr/1", output.read_text()
                    )

    def test_promotion_preflight(self) -> None:
        """Validate real Git promotions before any network or publication step."""
        code = step(
            "releasing.yaml", "Validate version increase and committed preparation"
        )
        # setup-python supplies `python` in CI; use the test interpreter locally.
        code = 'python() { "$PYTHON_EXECUTABLE" "$@"; }\n' + code

        for scenario in (
            "chart",
            "image",
            "description",
            "changelog-only",
            "missing-manifest",
            "stale-manifest",
            "missing-notes",
            "changed-image-source",
            "dirty",
            "not-merge",
        ):
            with self.subTest(scenario=scenario), repository() as root:
                baseline = prepare_release.run(root, "git", "rev-parse", "HEAD")
                prepare_release.run(root, "git", "switch", "-c", "next")

                if scenario == "description":
                    chart = root / "helm/Chart.yaml"
                    chart.write_text(chart.read_text() + "# Description change\n")
                elif scenario == "changelog-only":
                    (root / "helm/CHANGELOG.md").write_text(
                        "Changed notes without bump\n"
                    )
                else:
                    prepare_release.run(
                        root,
                        "changie",
                        "new",
                        "-j",
                        "image" if scenario == "image" else "chart",
                        "-k",
                        "Fixed",
                        "-b",
                        "Promotion fixture",
                        "--interactive=false",
                    )
                    prepare_release.run(root, "git", "add", ".")
                    prepare_release.run(root, "git", "commit", "-qm", "Fragment")
                    prepare_release.prepare(root)
                    manifest = root / ".changes/release.json"

                    if scenario == "missing-manifest":
                        manifest.unlink()
                    elif scenario == "stale-manifest":
                        data = json.loads(manifest.read_text())
                        data["versions"]["chart"] = "9.9.9"
                        manifest.write_text(json.dumps(data))
                    elif scenario == "missing-notes":
                        (root / ".changes/chart/1.0.2.md").unlink()
                    elif scenario == "changed-image-source":
                        (root / "docker/Dockerfile").write_text("FROM scratch\n")

                prepare_release.run(root, "git", "add", ".")
                prepare_release.run(root, "git", "commit", "-qm", "Prepared changes")

                if scenario != "not-merge":
                    prepare_release.run(
                        root, "git", "branch", "--force", "main", baseline
                    )
                    prepare_release.run(root, "git", "switch", "main")
                    prepare_release.run(
                        root, "git", "merge", "--no-ff", "next", "-m", "Promote"
                    )

                if scenario == "dirty":
                    (root / "untracked.txt").write_text("dirty\n")

                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "output"
                    result = shell(
                        root,
                        code,
                        {
                            "PYTHONPATH": str(ROOT / "scripts"),
                            "PYTHON_EXECUTABLE": sys.executable,
                            "GITHUB_OUTPUT": str(output),
                        },
                    )

                    if scenario in ("chart", "image", "description"):
                        self.assertEqual(result.returncode, 0, result.stderr)
                        expected = (
                            "publish=false"
                            if scenario == "description"
                            else "publish=true"
                        )
                        self.assertIn(expected, output.read_text())

                        if scenario != "description":
                            self.assertIn(
                                f"image_changed={str(scenario == 'image').lower()}",
                                output.read_text(),
                            )
                    else:
                        self.assertNotEqual(result.returncode, 0)

    def test_workflow_wiring(self) -> None:
        """Keep promotion selection, non-overwrite behavior, and immutable checkout."""
        testing = (ROOT / ".github/workflows/testing.yaml").read_text()
        preparation = (ROOT / ".github/workflows/prepare-release.yaml").read_text()
        releasing = (ROOT / ".github/workflows/releasing.yaml").read_text()

        self.assertIn("branches: [next, main]", testing)
        self.assertIn("github.base_ref == 'main'", testing)
        self.assertIn("name: Testing result", testing)
        self.assertIn("if: always()", testing)
        self.assertIn("if: needs.lookup.outputs.url == ''", preparation)
        self.assertNotIn("--force", preparation)
        self.assertNotIn("publish-image", preparation)
        self.assertIn("types: [closed]", releasing)
        self.assertIn("github.event.pull_request.merged == true", releasing)
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            releasing,
        )
        self.assertIn("ref: ${{ needs.validate.outputs.sha }}", releasing)
        self.assertIn("ref: ${{ needs.publish.outputs.pages_sha }}", releasing)
        self.assertNotIn("just docker build", releasing)
        self.assertLess(
            releasing.index("just release publish-image"),
            releasing.index("just release publish-chart"),
        )
        self.assertEqual(releasing.count("pages: write"), 1)
        self.assertEqual(releasing.count("id-token: write"), 1)


if __name__ == "__main__":
    unittest.main()
