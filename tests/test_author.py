"""Offline tests for the Phase-C candidate authoring pipeline."""

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from measure_twice.author import AuthorError, harvest, harvest_git_history, harvest_goldens
from measure_twice.cli import main
from measure_twice.suite import Item


def _golden_workspace(root: Path) -> Path:
    """Create a committed-shape golden corpus with 20+ distinct verified candidates."""
    golden = root / ".claude" / "skills" / "review-demo" / "evals" / "golden"
    golden.mkdir(parents=True)
    (golden / "good.md").write_text("The required result is complete.\n", encoding="utf-8")
    bads: list[dict[str, object]] = []
    for index in range(21):
        filename = f"bad-{index}.md"
        (golden / filename).write_text(f"Missing required behavior {index}.\n", encoding="utf-8")
        bads.append({"file": filename, "verified_fails": True})
    (golden / "bad-duplicate.md").write_text("Missing required behavior 0.\n", encoding="utf-8")
    bads.append({"file": "bad-duplicate.md", "verified_fails": True})
    (golden / "manifest.json").write_text(json.dumps({"bads": bads}), encoding="utf-8")
    return root


def _review_workspace(root: Path) -> Path:
    """Create the minimal recorded review-deep fixture shape used by the production harvester."""
    fixtures = root / ".review-deep"
    fixtures.mkdir(parents=True)
    payload = {
        "lens_verdicts": [
            {
                "lens_id": "style",
                "authority": "nearby conventions",
                "coverage_claim": "style only",
                "findings": [{"severity": "FYI", "excerpt": "clear names"}],
                "overall_verdict": "PASS",
            },
            {
                "lens_id": "correctness",
                "authority": "intent",
                "coverage_claim": "logic errors",
                "findings": [{"severity": "Block", "excerpt": "swallows errors"}],
                "overall_verdict": "NEEDS-WORK",
            },
            {
                "lens_id": "plan-conformance",
                "overall_verdict": "SKIPPED",
            },
        ]
    }
    (fixtures / "recorded.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _git(workspace: Path, *args: str) -> None:
    """Run a local Git setup command for the isolated temp-repository harvest fixture."""
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run(  # noqa: S603 - isolated test fixture passes only fixed local Git commands.
        [executable, "-C", str(workspace), *args], check=True, capture_output=True
    )


def test_golden_harvest_yields_twenty_schema_valid_candidates_and_deduplicates(
    tmp_path: Path,
) -> None:
    result = harvest("goldens", _golden_workspace(tmp_path / "workspace"))

    assert len(result.candidates) >= 20
    assert result.duplicates_dropped == 1
    assert {candidate.expected for candidate in result.candidates} == {"pass", "flag"}
    for candidate in result.candidates:
        assert Item.from_mapping(asdict(candidate)) == candidate
        assert candidate.provenance.startswith("harvested:.claude/skills/")
        assert ";sha256=" in candidate.provenance


def test_review_deep_harvest_uses_recorded_gold_without_leaking_it_to_the_prompt(
    tmp_path: Path,
) -> None:
    result = harvest("review-deep", _review_workspace(tmp_path / "workspace"))

    assert [candidate.expected for candidate in result.candidates] == ["pass", "flag"]
    assert {"correctness", "style"} == {candidate.tags[2] for candidate in result.candidates}
    assert all("overall_verdict" not in candidate.prompt for candidate in result.candidates)
    assert all(
        candidate.provenance.startswith("harvested:.review-deep/")
        for candidate in result.candidates
    )


def test_git_harvest_marks_history_as_needing_human_gold(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.invalid")
    _git(workspace, "config", "user.name", "Test User")
    (workspace / "sample.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    _git(workspace, "add", "sample.py")
    _git(workspace, "commit", "-m", "add sample")

    candidates = harvest_git_history(workspace)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.expected == "CURATE"
    assert {"git-history", "needs-gold", "candidate-only"} <= set(candidate.tags)
    assert candidate.provenance.startswith("harvested:git:")


def test_unverified_golden_negative_is_not_silently_labeled(tmp_path: Path) -> None:
    golden = tmp_path / "workspace" / ".claude" / "skills" / "demo" / "evals" / "golden"
    golden.mkdir(parents=True)
    (golden / "bad.md").write_text("unverified\n", encoding="utf-8")
    (golden / "manifest.json").write_text(
        json.dumps({"bads": [{"file": "bad.md", "verified_fails": False}]}), encoding="utf-8"
    )

    with pytest.raises(AuthorError, match="no verified"):
        harvest_goldens(tmp_path / "workspace")


def test_author_cli_writes_candidates_and_a_stub_that_mt_validate_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _golden_workspace(tmp_path / "workspace")
    candidates_path = tmp_path / "candidates.json"
    stub_path = tmp_path / "draft-suite.json"

    assert (
        main(
            [
                "author",
                "harvest",
                "goldens",
                "--workspace-root",
                str(workspace),
                "--output",
                str(candidates_path),
            ]
        )
        == 0
    )
    harvested = json.loads(candidates_path.read_text(encoding="utf-8"))
    assert len(harvested["candidates"]) >= 20
    assert "semantic duplicate" in capsys.readouterr().err

    assert main(["author", "stub", "draft-suite", "--output", str(stub_path)]) == 0
    assert main(["validate", str(stub_path)]) == 0
    assert "valid" in capsys.readouterr().out

    assert main(["author", "stub", "draft-suite", "--output", str(stub_path)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
