"""Runner tests — OFFLINE, stub adapter factories + a stub scorer, ZERO live calls (plan §5).

Every model call is routed through an injected DI-seam stub (a fake ``urllib`` transport for the
local adapter, a fake ``subprocess`` runner for the claude adapter) and every scored path uses a
stub scorer, so the suite touches NO network and NEVER invokes the real ``claude`` binary. Coverage
per the Step-4 done-when:

  * full sweep: N models x M items x S samples -> N*M*S rows; manifest with all §3 keys; id shape
  * no-response FORCE-0: score 0.0 / scorer "no_response", NEVER passed to the scorer
  * error row: terminal ``error`` = reason_class, score null
  * RESUME: kill mid-run (budget), then --resume re-calls EXACTLY the incomplete cells
  * TORN-LINE: a truncated trailing rows.jsonl line is tolerated on resume
  * BUDGET: aborts after exactly ``budget`` calls, rows persist, --resume w/ raised budget finishes
  * INTEGRATION: ``measure_twice.cli.main(["run", ...])`` on suites/smoke.json (production caller)
  * mt score: re-scores stored rows offline without re-calling models (Decision 10)
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
from pathlib import Path

import pytest

from measure_twice.adapters import claude_cli
from measure_twice.adapters.base import RC_OS_ERROR, RC_UNREACHABLE
from measure_twice.adapters.claude_cli import RunnerFactory, SubprocessResult
from measure_twice.adapters.local import TransportFactory
from measure_twice.cli import CliDeps, main
from measure_twice.config import ENV_VAR, RunConfig
from measure_twice.runner import (
    NO_RESPONSE_SCORER,
    RunError,
    ScoreOutcome,
    run,
    score_run,
)
from measure_twice.suite import Item, ScoringSpec, Suite, load_suite

RUN_ID_RE = re.compile(r"run_\d{8}T\d{6}Z_[0-9a-f]{6}")
ROW_KEYS = {
    "run_id",
    "model",
    "model_id_resolved",
    "item_id",
    "sample_k",
    "response_raw",
    "parsed",
    "score",
    "scorer",
    "judge_scores",
    "elapsed_s",
    "error",
}
MANIFEST_KEYS = {
    "run_id",
    "suite",
    "suite_hash",
    "roster",
    "samples_per_cell",
    "judges",
    "started_utc",
    "config_source",
    "budgets",
    "preregistration",
}


# --- Builders ----------------------------------------------------------------------------


def _iid(prompt: str) -> str:
    """Recover an item id from a test prompt (``PROMPT::<id>``), or the prompt itself if plain."""
    return prompt.split("::")[1] if "::" in prompt else prompt


def _suite(item_ids: list[str], *, name: str = "testsuite", scoring_type: str = "verdict") -> Suite:
    items = [
        Item(
            id=iid,
            tags=["t"],
            prompt=f"PROMPT::{iid}",
            expected="pass",
            difficulty_prior=0.5,
            provenance="authored",
        )
        for iid in item_ids
    ]
    labels = ["pass", "flag"] if scoring_type == "verdict" else None
    scoring = ScoringSpec(type=scoring_type, labels=labels)
    return Suite(
        suite=name, version=1, description="desc", domain="dom", scoring=scoring, items=items
    )


def _openai_body(content: str, *, model: str = "local-x", finish_reason: str = "stop") -> str:
    return json.dumps(
        {
            "id": "c",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


def _claude_stdout(result_text: str, *, model: str = "claude-x") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
            "model": model,
        }
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class StubAdapters:
    """Offline stub factories for both adapters, sharing a call recorder.

    ``local`` / ``claude`` are ``prompt -> behavior`` callables; a behavior returns the text to
    echo (``""`` yields the no-response state), a ``BaseException`` to raise (transport failure ->
    reason_class), or a ready-made :class:`SubprocessResult` (claude only). Default behavior echoes
    a constant, so a suite whose prompts carry no ``::`` id (e.g. suites/smoke.json) still works.
    """

    def __init__(self, *, local=None, claude=None) -> None:
        self.local_behavior = local if local is not None else (lambda prompt: "loc-answer")
        self.claude_behavior = claude if claude is not None else (lambda prompt: "cl-answer")
        self.local_calls: list[str] = []
        self.claude_calls: list[str] = []

    def local_factory(self) -> TransportFactory:
        def factory() -> object:
            def transport(url: str, data: bytes, timeout: float) -> str:
                body = json.loads(data.decode("utf-8"))
                prompt = body["messages"][0]["content"]
                self.local_calls.append(prompt)
                out = self.local_behavior(prompt)
                if isinstance(out, BaseException):
                    raise out
                return _openai_body(out)

            return transport

        return factory  # type: ignore[return-value]

    def claude_factory(self) -> RunnerFactory:
        def factory() -> object:
            def runner(argv: object, input_text: str, timeout: float) -> SubprocessResult:
                self.claude_calls.append(input_text)
                out = self.claude_behavior(input_text)
                if isinstance(out, BaseException):
                    raise out
                if isinstance(out, SubprocessResult):
                    return out
                return SubprocessResult(0, _claude_stdout(out), "")

            return runner

        return factory  # type: ignore[return-value]


# --- Full sweep --------------------------------------------------------------------------


def test_full_sweep_writes_manifest_and_all_rows(tmp_path: Path) -> None:
    suite = _suite(["a", "b", "c"])
    stub = StubAdapters()
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b", "haiku"],
        samples_per_cell=2,
        preregister="this run will decide X",
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )

    assert not result.aborted
    assert result.cells_total == 12  # 2 models x 3 items x 2 samples
    assert result.cells_this_run == 12
    assert result.cells_completed == 12
    assert RUN_ID_RE.fullmatch(result.run_id)

    run_dir = tmp_path / "runs" / result.run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == MANIFEST_KEYS  # EXACTLY the plan §3 keys
    assert manifest["run_id"] == result.run_id
    assert manifest["suite"] == "testsuite"
    assert manifest["suite_hash"] == suite.item_hash
    assert manifest["roster"] == ["general-35b", "haiku"]
    assert manifest["samples_per_cell"] == 2
    assert manifest["judges"] == ["sonnet"]  # config default
    assert manifest["config_source"] == "defaults"
    assert manifest["budgets"] == {"max_calls": RunConfig().max_calls}
    assert manifest["preregistration"] == "this run will decide X"

    rows = _read_jsonl(run_dir / "rows.jsonl")
    assert len(rows) == 12
    for row in rows:
        assert set(row) == ROW_KEYS
        assert row["run_id"] == result.run_id

    cells = {(r["model"], r["item_id"], r["sample_k"]) for r in rows}
    expected = {
        (m, i, k) for m in ("general-35b", "haiku") for i in ("a", "b", "c") for k in (0, 1)
    }
    assert cells == expected
    # routing: local models -> local adapter, claude aliases -> claude adapter
    assert len(stub.local_calls) == 6
    assert len(stub.claude_calls) == 6


def test_default_scorer_collects_only_leaving_score_null(tmp_path: Path) -> None:
    """The Step-4 default (collect-only) captures raw text but leaves score/scorer null."""
    suite = _suite(["a"])
    stub = StubAdapters()
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
    )
    row = _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")[0]
    assert row["response_raw"] == "loc-answer"  # raw captured
    assert row["score"] is None
    assert row["scorer"] is None
    assert row["parsed"] is None
    assert row["error"] is None


# --- No-response force-0 -----------------------------------------------------------------


def test_no_response_force_zero_never_reaches_scorer(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters(local=lambda prompt: "" if _iid(prompt) == "b" else "answer")
    scored: list[str] = []

    def scorer(item: Item, response_raw: str) -> ScoreOutcome:
        scored.append(item.id)
        return ScoreOutcome(parsed=response_raw, score=1.0, scorer="stub")

    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        scorer=scorer,
        local_transport_factory=stub.local_factory(),
    )
    rows = {r["item_id"]: r for r in _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")}
    assert rows["b"]["score"] == 0.0
    assert rows["b"]["scorer"] == NO_RESPONSE_SCORER
    assert rows["b"]["response_raw"] == ""
    assert rows["b"]["error"] is None
    assert rows["a"]["score"] == 1.0 and rows["a"]["scorer"] == "stub"
    # the no-response cell was force-0'd by the runner and NEVER handed to the scorer.
    assert scored == ["a"]


# --- Error row ---------------------------------------------------------------------------


def test_error_row_records_reason_class(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters(
        local=lambda prompt: urllib.error.URLError("refused") if _iid(prompt) == "b" else "answer"
    )
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
    )
    rows = {r["item_id"]: r for r in _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")}
    assert rows["b"]["error"] == RC_UNREACHABLE
    assert rows["b"]["score"] is None
    assert rows["b"]["scorer"] is None
    assert rows["a"]["error"] is None


# --- Resume: exactly the incomplete cells ------------------------------------------------


def test_resume_skips_exactly_completed_cells(tmp_path: Path) -> None:
    suite = _suite(["a", "b", "c", "d"])
    cfg = RunConfig()
    stub1 = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=2,  # kill mid-run after exactly 2 cells
        local_transport_factory=stub1.local_factory(),
    )
    assert r1.aborted
    assert [_iid(p) for p in stub1.local_calls] == ["a", "b"]
    completed = {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")}
    assert completed == {"a", "b"}

    stub2 = StubAdapters()
    r2 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=10,
        resume=r1.run_id,
        local_transport_factory=stub2.local_factory(),
    )
    assert not r2.aborted
    # the resume pass re-calls the model for EXACTLY the incomplete cells — no re-call of a,b.
    assert [_iid(p) for p in stub2.local_calls] == ["c", "d"]
    final = {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")}
    assert final == {"a", "b", "c", "d"}
    assert r2.cells_completed == 4


def test_resume_suite_hash_mismatch_fails_loud(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters()
    r1 = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=1,
        local_transport_factory=stub.local_factory(),
    )
    other = _suite(["a", "b", "z"])  # different items -> different item_hash
    with pytest.raises(RunError):
        run(
            suite=other,
            config=RunConfig(),
            out_dir=tmp_path,
            roster=["general-35b"],
            samples_per_cell=1,
            resume=r1.run_id,
            local_transport_factory=stub.local_factory(),
        )


# --- Torn trailing line ------------------------------------------------------------------


def test_torn_trailing_line_tolerated_on_resume(tmp_path: Path) -> None:
    suite = _suite(["a", "b", "c", "d"])
    cfg = RunConfig()
    stub1 = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=2,
        local_transport_factory=stub1.local_factory(),
    )
    rows_path = tmp_path / "runs" / r1.run_id / "rows.jsonl"
    # Simulate a crash mid-append: a truncated, unterminated trailing line (no newline).
    with rows_path.open("a", encoding="utf-8") as fh:
        fh.write('{"run_id": "x", "model": "gen')

    stub2 = StubAdapters()
    r2 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=10,
        resume=r1.run_id,
        local_transport_factory=stub2.local_factory(),
    )
    assert not r2.aborted
    final_rows = _read_jsonl(rows_path)
    assert len(final_rows) == 4  # the torn line was dropped, not counted
    assert {r["item_id"] for r in final_rows} == {"a", "b", "c", "d"}
    assert [_iid(p) for p in stub2.local_calls] == ["c", "d"]


def test_midfile_corruption_is_fatal(tmp_path: Path) -> None:
    """A parse failure on a NON-trailing line is real corruption -> fail loud (not tolerated)."""
    suite = _suite(["a", "b", "c"])
    cfg = RunConfig()
    stub = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=2,
        local_transport_factory=stub.local_factory(),
    )
    rows_path = tmp_path / "runs" / r1.run_id / "rows.jsonl"
    # Corrupt the FIRST line but keep a valid, newline-terminated line after it.
    good_tail = rows_path.read_text(encoding="utf-8").splitlines()[1]
    rows_path.write_text(f"not-json-at-all\n{good_tail}\n", encoding="utf-8")
    with pytest.raises(RunError):
        run(
            suite=suite,
            config=cfg,
            out_dir=tmp_path,
            roster=["general-35b"],
            samples_per_cell=1,
            resume=r1.run_id,
            local_transport_factory=stub.local_factory(),
        )


# --- Budget abort is resumable -----------------------------------------------------------


def test_budget_abort_stops_at_exactly_budget_and_resumes(tmp_path: Path) -> None:
    suite = _suite(["a", "b", "c", "d", "e"])
    cfg = RunConfig()
    stub1 = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=3,
        local_transport_factory=stub1.local_factory(),
    )
    assert r1.aborted
    assert r1.budget_used == 3
    assert r1.cells_this_run == 3
    assert len(stub1.local_calls) == 3  # aborted after EXACTLY budget calls
    assert len(_read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")) == 3  # rows persist

    stub2 = StubAdapters()
    r2 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=10,
        resume=r1.run_id,
        local_transport_factory=stub2.local_factory(),
    )
    assert not r2.aborted
    assert len(stub2.local_calls) == 2  # only the 2 remaining cells
    assert r2.cells_completed == 5
    assert {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")} == {
        "a",
        "b",
        "c",
        "d",
        "e",
    }


def test_claude_pool_budget_abort_resumes(tmp_path: Path) -> None:
    """The claude bounded-pool path also aborts resumably (chunked at pool size)."""
    suite = _suite(["a", "b", "c", "d"])
    cfg = RunConfig(claude_pool=2)
    stub1 = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        max_calls=2,  # == one full pool wave
        claude_runner_factory=stub1.claude_factory(),
    )
    assert r1.aborted
    assert r1.budget_used == 2
    completed = {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")}
    assert len(completed) == 2

    stub2 = StubAdapters()
    r2 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        max_calls=10,
        resume=r1.run_id,
        claude_runner_factory=stub2.claude_factory(),
    )
    assert not r2.aborted
    assert {_iid(p) for p in stub2.claude_calls} == {"a", "b", "c", "d"} - completed
    assert {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")} == {
        "a",
        "b",
        "c",
        "d",
    }


# --- Re-run mints a new run id -----------------------------------------------------------


def test_fresh_run_mints_new_run_id(tmp_path: Path) -> None:
    suite = _suite(["a"])
    stub = StubAdapters()
    kw = dict(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
    )
    r1 = run(**kw, local_transport_factory=stub.local_factory())
    r2 = run(**kw, local_transport_factory=stub.local_factory())
    assert r1.run_id != r2.run_id
    assert (tmp_path / "runs" / r1.run_id).is_dir()
    assert (tmp_path / "runs" / r2.run_id).is_dir()


# --- mt score: re-score stored rows offline ----------------------------------------------


def test_score_run_rescores_without_recalling_models(tmp_path: Path) -> None:
    suite = _suite(["a", "b", "c"])

    def local(prompt: str):
        iid = _iid(prompt)
        if iid == "b":
            return ""  # no-response
        if iid == "c":
            return urllib.error.URLError("down")  # error
        return "raw-answer"  # real response

    stub = StubAdapters(local=local)
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
    )
    rows0 = {x["item_id"]: x for x in _read_jsonl(tmp_path / "runs" / r.run_id / "rows.jsonl")}
    assert rows0["a"]["score"] is None and rows0["a"]["scorer"] is None  # collect-only default
    assert rows0["b"]["scorer"] == NO_RESPONSE_SCORER
    assert rows0["c"]["error"] == RC_UNREACHABLE

    stub.local_calls.clear()

    def scorer(item: Item, response_raw: str) -> ScoreOutcome:
        return ScoreOutcome(parsed=response_raw.upper(), score=1.0, scorer="det")

    summary = score_run(run_id=r.run_id, out_dir=tmp_path, scorer=scorer)
    assert summary.total == 3
    assert summary.scored == 1  # only the one real response
    assert summary.no_response == 1
    assert stub.local_calls == []  # NO model was re-called

    rows1 = {x["item_id"]: x for x in _read_jsonl(tmp_path / "runs" / r.run_id / "rows.jsonl")}
    assert rows1["a"]["score"] == 1.0
    assert rows1["a"]["scorer"] == "det"
    assert rows1["a"]["parsed"] == "RAW-ANSWER"
    assert rows1["b"]["score"] == 0.0 and rows1["b"]["scorer"] == NO_RESPONSE_SCORER  # stays 0
    assert rows1["c"]["error"] == RC_UNREACHABLE and rows1["c"]["score"] is None  # untouched


# --- Integration through the production CLI entry point (measure_twice.cli.main) ----------


def test_cli_run_integration_on_smoke_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root)  # so the relative suite path + config resolution are deterministic
    monkeypatch.delenv(ENV_VAR, raising=False)
    stub = StubAdapters()
    out = tmp_path / "data"
    deps = CliDeps(
        local_transport_factory=stub.local_factory(),
        claude_runner_factory=stub.claude_factory(),
    )

    rc = main(
        ["run", "--suite", "suites/smoke.json", "--models", "general-35b,haiku", "--out", str(out)],
        deps=deps,
    )

    assert rc == 0
    run_dirs = list((out / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert RUN_ID_RE.fullmatch(run_dir.name)
    assert (run_dir / "manifest.json").is_file()
    rows = _read_jsonl(run_dir / "rows.jsonl")
    assert len(rows) == 4  # 2 models x 2 smoke items x 1 sample
    captured = capsys.readouterr()
    assert run_dir.name in captured.out  # the one-line summary was printed


def test_cli_score_integration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters()
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
    )

    def scorer(item: Item, response_raw: str) -> ScoreOutcome:
        return ScoreOutcome(parsed="p", score=0.7, scorer="det")

    rc = main(["score", r.run_id, "--out", str(tmp_path)], deps=CliDeps(scorer=scorer))
    assert rc == 0
    out = capsys.readouterr().out
    assert r.run_id in out
    assert "2 rows, 2 scored" in out


# --- review-deep iteration 2: fail-loud, atomicity, dedup, path-traversal, partial-refusal --------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# BLOCK 1 — invalid budget must fail loud BEFORE any filesystem mutation (no orphaned run dir).


def test_zero_budget_fails_before_any_write(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters()
    with pytest.raises(RunError):
        run(
            suite=suite,
            config=RunConfig(),
            out_dir=tmp_path,
            roster=["general-35b"],
            samples_per_cell=1,
            max_calls=0,  # invalid
            local_transport_factory=stub.local_factory(),
        )
    assert not (tmp_path / "runs").exists()  # nothing minted / written
    assert stub.local_calls == []


def test_cli_zero_budget_fails_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo_root())
    monkeypatch.delenv(ENV_VAR, raising=False)
    stub = StubAdapters()
    rc = main(
        ["run", "--suite", "suites/smoke.json", "--budget", "0", "--out", str(tmp_path)],
        deps=CliDeps(local_transport_factory=stub.local_factory()),
    )
    assert rc == 1
    assert not (tmp_path / "runs").exists()


# BLOCK 2 — an unclassified claude-worker exception becomes a structured error row; the wave's
# already-succeeded (budget-consumed) sibling is PERSISTED, and no uncaught crash escapes run().


def test_claude_worker_exception_persists_sibling_success(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    cfg = RunConfig(claude_pool=2)  # both cells run in one concurrent wave

    def claude(prompt: str):
        return RuntimeError("boom") if _iid(prompt) == "b" else "ok"  # unclassified worker failure

    stub = StubAdapters(claude=claude)
    result = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        claude_runner_factory=stub.claude_factory(),
    )
    rows = {r["item_id"]: r for r in _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")}
    assert set(rows) == {"a", "b"}  # NO row lost — the success sibling survived the crash
    assert rows["a"]["response_raw"] == "ok" and rows["a"]["error"] is None
    assert rows["b"]["error"] == RC_OS_ERROR  # unclassified exception -> structured error row
    assert rows["b"]["score"] is None


# BLOCK A (iter 3) — a fault in the POST-PROCESSING tail of claude_call (past the subprocess:
# resolved_model_of / success()) must also become a structured error row, not propagate and discard
# a sibling's budget-consumed success. Discriminates the whole-function try from the spawn-only try.


def test_claude_post_processing_fault_persists_sibling_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(["a", "b"])
    cfg = RunConfig(claude_pool=2)  # both cells in one concurrent wave
    # Echo the item id into the envelope's `result` field so the patched resolved_model_of can fault
    # exactly one worker in the POST-subprocess tail (proving the try wraps the whole body).
    stub = StubAdapters(claude=lambda prompt: _iid(prompt))
    real_resolve = claude_cli.resolved_model_of

    def faulty_resolve(envelope: dict[str, object], requested: str) -> str:
        if envelope.get("result") == "b":
            raise RuntimeError("post-processing boom (past the subprocess)")
        return real_resolve(envelope, requested)

    monkeypatch.setattr(claude_cli, "resolved_model_of", faulty_resolve)
    result = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        claude_runner_factory=stub.claude_factory(),
    )
    rows = {r["item_id"]: r for r in _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")}
    assert set(rows) == {"a", "b"}  # sibling success PERSISTED despite the tail fault
    assert rows["a"]["error"] is None and rows["a"]["response_raw"] == "a"
    assert rows["b"]["error"] == RC_OS_ERROR  # post-processing fault -> structured error row
    assert rows["b"]["score"] is None


# BLOCK 3 — a duplicate model in the roster must write each cell exactly once (no double row).


def test_duplicate_roster_writes_each_cell_once(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    stub = StubAdapters()
    result = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b", "general-35b"],  # duplicate
        samples_per_cell=1,
        local_transport_factory=stub.local_factory(),
    )
    rows = _read_jsonl(tmp_path / "runs" / result.run_id / "rows.jsonl")
    assert len(rows) == 2  # unique_models(1) x items(2) x samples(1), NOT 4
    cells = {(r["model"], r["item_id"], r["sample_k"]) for r in rows}
    assert cells == {("general-35b", "a", 0), ("general-35b", "b", 0)}
    assert len(stub.local_calls) == 2  # each cell called exactly once
    manifest = json.loads(
        (tmp_path / "runs" / result.run_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["roster"] == ["general-35b"]  # deduped in the manifest too


# BLOCK 5b — a malformed --models / --judges name fails loud (config-roster validation applied).


def test_malformed_model_name_fails_loud(tmp_path: Path) -> None:
    suite = _suite(["a"])
    with pytest.raises(RunError):
        run(
            suite=suite,
            config=RunConfig(),
            out_dir=tmp_path,
            roster=["bad name!"],  # spaces + '!' -> not _SAFE_NAME_RE
            samples_per_cell=1,
            local_transport_factory=StubAdapters().local_factory(),
        )
    assert not (tmp_path / "runs").exists()


# BLOCK 4 / BLOCK B (iter 3) — a traversal run_id is rejected AND cannot read/overwrite a planted,
# existing run dir at the traversal target. These DISCRIMINATE: with both guards removed from
# _resolve_run_dir, resume/score would traverse into the planted dir (which exists, with a matching
# suite_hash) and append/overwrite it — so the raise + "planted rows unchanged" asserts would fail.


def _plant_run_at(target: Path, *, suite: Suite, scratch: Path, max_calls: int = 10) -> Path:
    """Create a REAL run (manifest + suite + rows) and copy it to ``target`` (the traversal
    destination), returning the planted ``rows.jsonl`` path. WITHOUT the guard, run / score_run
    would resolve into ``target`` and read/append/overwrite this file."""
    src = run(
        suite=suite,
        config=RunConfig(),
        out_dir=scratch,
        roster=["general-35b"],
        samples_per_cell=1,
        max_calls=max_calls,
        local_transport_factory=StubAdapters().local_factory(),
    )
    src_dir = scratch / "runs" / src.run_id
    target.mkdir(parents=True)
    for name in ("manifest.json", "suite.json", "rows.jsonl"):
        shutil.copy(src_dir / name, target / name)
    return target / "rows.jsonl"


def test_resume_traversal_rejected_does_not_touch_planted_dir(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    out = tmp_path / "data"
    # Plant an INCOMPLETE run (max_calls=1 -> 1 cell done, 1 pending) at the traversal target, so a
    # guard-less resume would APPEND the pending cell to the planted rows.jsonl.
    evil_rows = _plant_run_at(
        out.parent / "evil", suite=suite, scratch=tmp_path / "scratch", max_calls=1
    )
    assert (out / "runs" / "../../evil").resolve() == (
        out.parent / "evil"
    ).resolve()  # target sanity
    before = evil_rows.read_bytes()

    with pytest.raises(RunError):  # guard-less: resume would SUCCEED (no exception) -> this fails
        run(
            suite=suite,
            config=RunConfig(),
            out_dir=out,
            roster=["general-35b"],
            samples_per_cell=1,
            resume="../../evil",
            local_transport_factory=StubAdapters().local_factory(),
        )
    assert evil_rows.read_bytes() == before  # planted rows.jsonl NOT appended/overwritten


def test_score_traversal_rejected_does_not_touch_planted_dir(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    out = tmp_path / "data"
    evil_rows = _plant_run_at(out.parent / "evil", suite=suite, scratch=tmp_path / "scratch")
    before = evil_rows.read_bytes()

    def scorer(item: Item, response_raw: str) -> ScoreOutcome:
        return ScoreOutcome(parsed="p", score=1.0, scorer="det")

    with pytest.raises(RunError):  # guard-less: score would rewrite the planted rows.jsonl
        score_run(run_id="../../evil", out_dir=out, scorer=scorer)
    assert evil_rows.read_bytes() == before  # planted rows.jsonl NOT rewritten


def test_cli_resume_and_score_traversal_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo_root())
    monkeypatch.delenv(ENV_VAR, raising=False)
    smoke = load_suite(str(_repo_root() / "suites" / "smoke.json"))
    out = tmp_path / "data"
    # Plant an incomplete smoke-suite run at the traversal target (suite_hash matches the CLI's
    # --suite suites/smoke.json), so a guard-less CLI resume/score would traverse into + mutate it.
    evil_rows = _plant_run_at(
        out.parent / "evil", suite=smoke, scratch=tmp_path / "scratch", max_calls=1
    )
    before = evil_rows.read_bytes()

    rc_run = main(
        ["run", "--suite", "suites/smoke.json", "--resume", "../../evil", "--out", str(out)],
        deps=CliDeps(local_transport_factory=StubAdapters().local_factory()),
    )
    assert rc_run == 1  # clean non-zero exit, not a traceback (guard-less would be 0)

    def scorer(item: Item, response_raw: str) -> ScoreOutcome:
        return ScoreOutcome(parsed="p", score=1.0, scorer="det")

    rc_score = main(["score", "../../evil", "--out", str(out)], deps=CliDeps(scorer=scorer))
    assert rc_score == 1
    assert evil_rows.read_bytes() == before  # planted rows.jsonl untouched by either CLI path


# BLOCK 5a — mid-wave partial refusal: one concurrent call takes the LAST budget slot, its sibling
# is refused; exactly one row is written, the other cell is left pending, and resume completes it.


def test_claude_mid_wave_partial_refusal_resumes(tmp_path: Path) -> None:
    suite = _suite(["a", "b"])
    cfg = RunConfig(claude_pool=2)  # 2 concurrent workers, but only 1 budget slot
    stub1 = StubAdapters()
    r1 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        max_calls=1,  # one worker consumes it; its wave-sibling is refused mid-wave
        claude_runner_factory=stub1.claude_factory(),
    )
    assert r1.aborted
    assert r1.budget_used == 1
    completed = {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")}
    assert len(completed) == 1  # exactly one cell written; the refused sibling left pending

    stub2 = StubAdapters()
    r2 = run(
        suite=suite,
        config=cfg,
        out_dir=tmp_path,
        roster=["haiku"],
        samples_per_cell=1,
        max_calls=10,
        resume=r1.run_id,
        claude_runner_factory=stub2.claude_factory(),
    )
    assert not r2.aborted
    assert {_iid(p) for p in stub2.claude_calls} == {"a", "b"} - completed  # only the pending cell
    assert {r["item_id"] for r in _read_jsonl(tmp_path / "runs" / r1.run_id / "rows.jsonl")} == {
        "a",
        "b",
    }
