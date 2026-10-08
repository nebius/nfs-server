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
    """Check branch policy, required checks, PR arguments, and promotions."""

    def test_native_image_endpoints(self) -> None:
        """Pass architecture and digest artifacts through Just without remote writes."""
        with tempfile.TemporaryDirectory(prefix="image digests ") as directory:
            root = Path(directory)
            (root / "digests").mkdir()
            for arch, digit in (("amd64", "a"), ("arm64", "b")):
                (root / f"digests/{arch}.digest").write_text(
                    "sha256:" + digit * 64 + "\n"
                )
            variables = {
                "RUNNER_TEMP": str(root),
                "GITHUB_REPOSITORY": "test/repo",
                "PLATFORM": "linux/arm64",
                "ARCH": "arm64",
            }
            mock = 'just() { printf "%s\\n" "$@"; }\n'
            result = shell(
                root,
                mock + step("releasing.yaml", "Build and push one platform by digest"),
                variables,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.splitlines() == [
                "release",
                "build-image",
                "--platform",
                "linux/arm64",
                "--digest-file",
                str(root / "digests/arm64.digest"),
                "--github-repository",
                "test/repo",
            ]
            result = shell(
                root,
                mock
                + step(
                    "releasing.yaml",
                    "Assemble and publish the verified multi-platform image",
                ),
                variables,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.splitlines() == [
                "release",
                "publish-image",
                "--platform-digest",
                "sha256:" + "a" * 64,
                "--platform-digest",
                "sha256:" + "b" * 64,
                "--github-repository",
                "test/repo",
            ]

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
            assert (result.returncode == 0) == valid, result.stderr

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
        assert shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode == 0
        for name in jobs:
            for status in ("failure", "cancelled", "skipped"):
                changed = {key: dict(value) for key, value in jobs.items()}
                changed[name]["result"] = status
                assert (
                    shell(ROOT, code, {"RESULTS": json.dumps(changed)}).returncode != 0
                )
        for component in ("scripts", "docker", "helm"):
            jobs[f"{component}-test"]["result"] = "skipped"
        jobs["changes"]["outputs"] = {
            "scripts": "false",
            "docker": "false",
            "helm": "false",
        }
        assert shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode == 0
        # Missing outputs must not silently count as unselected checks.
        jobs["changes"]["outputs"] = {}
        assert shell(ROOT, code, {"RESULTS": json.dumps(jobs)}).returncode != 0

    def test_preparation_pr_and_noop(self) -> None:
        """Check no-op selection and PR arguments without Git or GitHub writes."""
        mocks = r"""
        just() { printf 'just %s\n' "$*" >> "$CALLS"; }
        git() {
            printf 'git %s\n' "$*" >> "$CALLS"
            if [[ "$1" == status ]]; then printf '%s' "$CHANGED"; fi
        }
        gh() {
            printf 'gh %s\n' "$*" >> "$CALLS"
            printf '%s\n' 'https://example.invalid/pr/1'
        }
        """
        for changed in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".changes").mkdir()
                (root / ".changes/release.json").write_text(
                    json.dumps(
                        {
                            "versions": {"chart": "1.0.2", "image": "1.0.1"},
                            "source_sha": "a" * 40,
                        }
                    )
                )
                variables = {
                    "CALLS": str(root / "calls"),
                    "CHANGED": " M helm/Chart.yaml" if changed else "",
                    "GITHUB_OUTPUT": str(root / "output"),
                    "GITHUB_STEP_SUMMARY": str(root / "summary"),
                    "GITHUB_REPOSITORY": "test/repo",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1",
                }
                result = shell(
                    root,
                    mocks
                    + step("prepare-release.yaml", "Generate prepared release files"),
                    variables,
                )
                assert result.returncode == 0, result.stderr
                assert (
                    f"prepared={str(changed).lower()}" in (root / "output").read_text()
                )
                assert (root / "calls").read_text().splitlines() == [
                    "just release prepare",
                    "git status --porcelain",
                ]
                if not changed:
                    continue
                result = shell(
                    root,
                    mocks
                    + step(
                        "prepare-release.yaml",
                        "Commit prepared files and open an editable PR",
                    ),
                    variables,
                )
                assert result.returncode == 0, result.stderr
                calls = (root / "calls").read_text()
                assert (
                    "git add --all -- .changes helm/Chart.yaml helm/CHANGELOG.md docker/CHANGELOG.md"
                    in calls
                )
                assert "push origin HEAD:refs/heads/release/prepare-123-1" in calls
                assert (
                    "gh pr create --repo test/repo --base next --head release/prepare-123-1"
                    in calls
                )
                assert "https://example.invalid/pr/1" in (root / "summary").read_text()

    def test_promotion_preflight(self) -> None:
        """Validate real Git promotions before any network or publication step."""
        code = step(
            "releasing.yaml", "Validate version increase and committed preparation"
        )
        code = 'python() { "$PYTHON_EXECUTABLE" "$@"; }\n' + code
        for scenario in (
            "chart",
            "image",
            "description",
            "changelog-only",
            "missing-manifest",
            "not-merge",
        ):
            with repository() as root:
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
                        assert result.returncode == 0, result.stderr
                        expected = (
                            "publish=false"
                            if scenario == "description"
                            else "publish=true"
                        )
                        assert expected in output.read_text()
                        if scenario != "description":
                            assert (
                                f"image_changed={str(scenario == 'image').lower()}"
                                in output.read_text()
                            )
                    else:
                        assert result.returncode != 0


if __name__ == "__main__":
    unittest.main()
