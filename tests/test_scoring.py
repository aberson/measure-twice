"""Deterministic-scoring tests — verdict spine, exact, 0-100 normalization, determinism (plan §4).

OFFLINE + zero live calls. Covers, per the Step-5 done-when and judge-core §5.6 / measurement-
validity:

  * verdict extraction across ALL shapes (JSON / bare / labeled-line / in-prose), case-insensitive
  * parse-fail paths -> score 0.0 + RECORDED ``parse_fail`` marker, COUNTABLE, never raises
  * a wrong-but-valid label -> 0.0 with the label RECORDED (distinct from a parse-fail)
  * exact match/mismatch + the documented trim+casefold normalization + regex mode
  * 0-100 suite normalization (all-0 -> 0, all-1 -> 100, mixed, empty -> fail loud)
  * make_deterministic_scorer dispatch incl. rubric -> NotImplementedError (Step 6 seam)
  * DETERMINISM: re-scoring a raw response is byte-identical, end-to-end via mt run -> mt score
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from measure_twice.cli import CliDeps, main
from measure_twice.config import ENV_VAR, RunConfig
from measure_twice.runner import NO_RESPONSE_SCORER, load_run_suite, run, score_run
from measure_twice.scoring import (
    EXACT_SCORER,
    PARSE_FAIL_MARKER,
    VERDICT_SCORER,
    ScoringError,
    exact_match,
    extract_verdict_label,
    make_deterministic_scorer,
    score_exact,
    score_verdict,
    suite_score,
)
from measure_twice.suite import Item, ScoringSpec, Suite, SuiteError, load_suite

_LABELS = ["flag", "pass"]


def _item(expected: str = "flag") -> Item:
    return Item(
        id="i1",
        tags=["t"],
        prompt="p",
        expected=expected,
        difficulty_prior=0.5,
        provenance="authored",
    )


def _verdict(raw: str, *, expected: str = "flag", labels: list[str] | None = None):
    return score_verdict(_item(expected), raw, labels=labels if labels is not None else _LABELS)


# --- Verdict: every extraction shape resolves, case-insensitively ------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict": "flag"}',  # JSON
        '{"Verdict": "FLAG"}',  # JSON, case-insensitive key + value
        '{"label": "flag"}',  # JSON, synonym key
        'Here is my call: {"verdict":"flag"} — done.',  # JSON embedded in prose
        "flag",  # bare label
        "  FLAG\n",  # bare, whitespace + case
        "flag.",  # bare, trailing sentence punctuation
        "VERDICT: flag",  # labeled line
        "verdict = flag",  # labeled line, '=' + lowercase key
        "VERDICT: flag\nRATIONALE: this is not a pass.",  # labeled line wins over prose ambiguity
        "After reviewing the diff I would flag this change.",  # single label in prose
    ],
)
def test_verdict_extraction_shapes_match_expected(raw: str) -> None:
    outcome = _verdict(raw, expected="flag")
    assert extract_verdict_label(raw, _LABELS) == "flag"
    assert outcome.parsed == "flag"
    assert outcome.score == 1.0
    assert outcome.scorer == VERDICT_SCORER


def test_verdict_wrong_but_valid_label_scores_zero_and_records_the_label() -> None:
    """A parsed-but-wrong label is 0.0 with the ACTUAL label recorded — distinct from a parse-fail
    (a different measurement event: the model answered, it was just wrong)."""
    outcome = _verdict("pass", expected="flag")
    assert outcome.parsed == "pass"  # the real label, NOT the parse-fail marker
    assert outcome.score == 0.0
    assert outcome.scorer == VERDICT_SCORER


# --- Verdict: parse-fail paths -> 0.0 + RECORDED marker, never raises ---------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "I have no idea what to say here.",  # no label at all
        "flags are raised all over unflagged code",  # substrings, not whole-word labels
        "It could be flag or pass, hard to tell.",  # ambiguous: two labels, no disambiguator
        '{"verdict": "maybe"}',  # JSON with an INVALID (non-label) verdict value
        "",  # empty response
        "{{{ not json at all }}}",  # brace soup, unparseable
    ],
)
def test_verdict_parse_fail_scores_zero_records_marker_never_raises(raw: str) -> None:
    outcome = _verdict(raw, expected="flag")
    assert outcome.score == 0.0
    assert outcome.parsed == PARSE_FAIL_MARKER  # RECORDED distinguishably
    assert outcome.scorer == VERDICT_SCORER


def test_parse_fail_is_countable_and_distinct_from_wrong_answer() -> None:
    """The load-bearing recording: parse-fails are countable (parsed == marker) and NOT conflated
    with wrong-but-parsed answers (which also score 0 but record the real label)."""
    raws = ["flag", "pass", "no verdict here", "also nothing", "flag"]
    outcomes = [_verdict(r, expected="flag") for r in raws]
    parse_fails = sum(1 for o in outcomes if o.parsed == PARSE_FAIL_MARKER)
    zeros = sum(1 for o in outcomes if o.score == 0.0)
    assert parse_fails == 2  # the two label-less responses
    assert zeros == 3  # 2 parse-fails + 1 wrong-but-valid ("pass")
    # a wrong-but-parsed answer is a zero that is NOT a parse-fail:
    assert zeros - parse_fails == 1


def test_extract_never_raises_on_hostile_input() -> None:
    for raw in ["퟿", "{" * 5000, '{"verdict": 123}', '{"verdict": null}', "🚩 flag 🚩"]:
        # must return a value or None, never raise
        assert extract_verdict_label(raw, _LABELS) in {"flag", "pass", None}


def test_verdict_no_labels_extracts_nothing() -> None:
    assert extract_verdict_label("flag", []) is None


# --- Exact scorer + documented normalization + regex mode --------------------------------------


def test_exact_match_normalization_trim_and_casefold() -> None:
    assert exact_match("  PARIS\n", "paris") is True
    assert exact_match("paris", "Paris") is True
    assert exact_match("London", "Paris") is False
    # full value, NEVER a substring: containing the expected is not matching it
    assert exact_match("the answer is paris", "paris") is False


def test_exact_scorer_records_match_verdict() -> None:
    good = score_exact(_item("42"), " 42 ")
    bad = score_exact(_item("42"), "forty-two")
    assert good.score == 1.0 and good.parsed == "match" and good.scorer == EXACT_SCORER
    assert bad.score == 0.0 and bad.parsed == "no_match" and bad.scorer == EXACT_SCORER
    assert good.parsed != PARSE_FAIL_MARKER  # exact never parse-fails


def test_exact_regex_mode_fullmatch_and_bad_pattern_fails_loud() -> None:
    assert exact_match("APPROVED", r"approve(d)?", regex=True) is True
    assert exact_match("approve that", r"approve(d)?", regex=True) is False  # fullmatch only
    assert score_exact(_item(r"\d{3}"), "404", regex=True).score == 1.0
    with pytest.raises(ScoringError):
        exact_match("x", r"(unclosed", regex=True)


# --- 0-100 suite normalization -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([0.0, 0.0, 0.0], 0.0),
        ([1.0, 1.0, 1.0], 100.0),
        ([1.0, 0.0], 50.0),
        ([1.0, 0.0, 0.0, 0.0], 25.0),
        ([1.0], 100.0),
        ([0.0], 0.0),
    ],
)
def test_suite_score_is_mean_times_100(scores: list[float], expected: float) -> None:
    assert suite_score(scores) == pytest.approx(expected)


def test_suite_score_empty_fails_loud() -> None:
    with pytest.raises(ScoringError):
        suite_score([])


# --- Dispatcher --------------------------------------------------------------------------------


def test_make_deterministic_scorer_dispatch_verdict_and_exact() -> None:
    v = make_deterministic_scorer(ScoringSpec(type="verdict", labels=_LABELS))
    assert v(_item("flag"), "VERDICT: flag").score == 1.0
    assert v(_item("flag"), "nonsense").parsed == PARSE_FAIL_MARKER
    e = make_deterministic_scorer(ScoringSpec(type="exact"))
    assert e(_item("paris"), "Paris").score == 1.0


def test_rubric_dispatch_raises_not_implemented_for_step6() -> None:
    with pytest.raises(NotImplementedError, match="Step 6"):
        make_deterministic_scorer(ScoringSpec(type="rubric"))


def test_verdict_without_labels_fails_loud() -> None:
    with pytest.raises(ScoringError, match="labels"):
        make_deterministic_scorer(ScoringSpec(type="verdict", labels=None))


def test_labels_reserving_parse_fail_marker_fails_loud() -> None:
    with pytest.raises(ScoringError, match="parse_fail"):
        make_deterministic_scorer(ScoringSpec(type="verdict", labels=["pass", "parse_fail"]))


def test_scorer_names_are_never_the_reserved_no_response_name() -> None:
    assert VERDICT_SCORER != NO_RESPONSE_SCORER
    assert EXACT_SCORER != NO_RESPONSE_SCORER


# --- review-deep iteration 2: measurement-validity defects in the verdict spine ----------------


# BLOCK 1 — the prose fallback must NOT scan JSON KEY NAMES: a response whose structured verdict
# field is invalid must parse-fail, not win a false 1.0 from an incidental same-spelled key.


@pytest.mark.parametrize(
    "raw",
    [
        '{"flag": true, "verdict": "unclear"}',  # incidental key "flag" + invalid verdict value
        '{"needs_flag": false, "verdict": "unclear"}',
        '{"flag": true, "verdict": "maybe", "pass": false}',  # two incidental keys, invalid verdict
    ],
)
def test_json_key_names_do_not_score_false_full_credit(raw: str) -> None:
    outcome = _verdict(raw, expected="flag")
    assert extract_verdict_label(raw, _LABELS) is None
    assert outcome.score == 0.0
    assert outcome.parsed == PARSE_FAIL_MARKER  # invalid structured verdict, not a key accident


def test_json_span_stripped_but_surrounding_prose_still_scanned() -> None:
    # a valid verdict in prose OUTSIDE the JSON still resolves (the strip preserves boundaries)
    raw = 'Metadata {"note": "ignore me"} — my verdict is flag.'
    assert extract_verdict_label(raw, _LABELS) == "flag"


# BLOCK 2 — two DISAGREEING recognized verdict signals resolve to parse_fail, order-independently.


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict": "flag"} then {"verdict": "pass"}',  # two JSON objects, flag-then-pass
        '{"verdict": "pass"} then {"verdict": "flag"}',  # reversed order
        "VERDICT: flag\nANSWER: pass",  # two labeled lines, flag-then-pass
        "ANSWER: pass\nVERDICT: flag",  # reversed order
    ],
)
def test_conflicting_recognized_signals_parse_fail_order_independent(raw: str) -> None:
    outcome = _verdict(raw, expected="flag")
    assert extract_verdict_label(raw, _LABELS) is None  # ambiguous -> never guess
    assert outcome.score == 0.0
    assert outcome.parsed == PARSE_FAIL_MARKER


def test_two_json_objects_agreeing_still_resolve() -> None:
    # agreement (not conflict) across objects is a single distinct label -> resolves
    raw = '{"verdict":"flag"} and again {"verdict":"flag"}'
    assert extract_verdict_label(raw, _LABELS) == "flag"


def test_two_labeled_lines_agreeing_still_resolve() -> None:
    # NIT: _single_or_conflict must not over-trigger on AGREEMENT at the labeled-line tier either
    raw = "VERDICT: flag\nANSWER: flag"
    assert extract_verdict_label(raw, _LABELS) == "flag"


# BLOCK 3 — a syntactically-valid deep-nesting bomb must parse-fail, never crash (RecursionError).


def test_balanced_nesting_bomb_parse_fails_without_crashing() -> None:
    bomb = '{"a":' * 3000 + "null" + "}" * 3000  # valid JSON, deeply nested -> RecursionError
    outcome = _verdict(bomb, expected="flag")  # must NOT raise
    assert outcome.score == 0.0
    assert outcome.parsed == PARSE_FAIL_MARKER
    assert extract_verdict_label(bomb, _LABELS) is None


def test_nesting_bomb_response_parse_fails_in_sweep_without_blanking_raw(tmp_path: Path) -> None:
    """Input-tolerance (not bug-masking): a hostile MODEL RESPONSE (a balanced nesting bomb) flows
    through a real sweep as a parse_fail row — the sweep does NOT crash, the row is a normal
    ``error=None`` parse_fail (0.0 + marker), and ``response_raw`` is preserved VERBATIM (never
    blanked). Then a re-score keeps the stored raw intact (non-destructive, Decision 10)."""
    bomb = '{"a":' * 3000 + "null" + "}" * 3000  # valid JSON, deeply nested -> RecursionError
    suite = load_suite(str(_repo_root() / "suites" / "smoke.json"))  # verdict, labels [pass, flag]
    scorer = make_deterministic_scorer(suite.scoring)
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        scorer=scorer,
        local_transport_factory=_fixed_local_factory(bomb),
    )
    rows_path = tmp_path / "runs" / r.run_id / "rows.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert len(rows) == 2  # sweep completed — the hostile response did NOT kill the instrument
    for row in rows:
        assert row["error"] is None  # a normal parse_fail, NOT a masked error row
        assert row["score"] == 0.0
        assert row["parsed"] == PARSE_FAIL_MARKER
        assert row["response_raw"] == bomb  # raw stored VERBATIM, never blanked

    score_run(run_id=r.run_id, out_dir=tmp_path, scorer=scorer)  # re-score must not destroy raw
    rows2 = [json.loads(line) for line in rows_path.read_text().splitlines()]
    for row in rows2:
        assert row["response_raw"] == bomb  # stored raw survived the re-score verbatim
        assert row["parsed"] == PARSE_FAIL_MARKER


def test_rescore_never_blanks_a_stored_raw(tmp_path: Path) -> None:
    """The BLOCK-A data-loss regression, guarded forever: ``mt score`` re-scoring NEVER blanks or
    overwrites a previously-persisted ``response_raw`` — a stored answer survives a re-score
    verbatim (the '404 Not Found' -> '' destruction must not recur)."""
    suite = load_suite(str(_repo_root() / "suites" / "smoke.json"))
    scorer = make_deterministic_scorer(suite.scoring)
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        scorer=scorer,
        local_transport_factory=_echo_local_factory(),  # stores real answers "pass" / "flag"
    )
    rows_path = tmp_path / "runs" / r.run_id / "rows.jsonl"
    before = {row["item_id"]: row["response_raw"] for row in _rows(rows_path)}
    assert set(before.values()) == {"pass", "flag"}  # real stored raws, scored 1.0 inline

    score_run(run_id=r.run_id, out_dir=tmp_path, scorer=scorer)  # re-score with the same scorer
    after = {row["item_id"]: row["response_raw"] for row in _rows(rows_path)}
    assert after == before  # every stored raw survived verbatim — none blanked to ""


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# BLOCK 4 — case-variant duplicate labels are a suite-authoring error: fail loud at load.


@pytest.mark.parametrize("labels", [["Pass", "pass"], ["flag", "FLAG", "pass"]])
def test_case_variant_duplicate_labels_fail_loud(labels: list[str]) -> None:
    with pytest.raises(SuiteError, match="case-variant"):
        ScoringSpec(type="verdict", labels=labels)


def test_case_distinct_labels_still_allowed() -> None:
    spec = ScoringSpec(type="verdict", labels=["flag", "pass"])
    assert spec.labels == ["flag", "pass"]


# NIT 1 — the regex exact mode is reachable from a real suite (wired through the production caller).


def _regex_suite_doc(expected: str) -> dict[str, object]:
    return {
        "suite": "rgx",
        "version": 1,
        "description": "a regex exact suite fixture",
        "domain": "d",
        "scoring": {"type": "exact", "regex": True},
        "items": [
            {
                "id": "r1",
                "tags": ["t"],
                "prompt": "an HTTP status code",
                "expected": expected,
                "difficulty_prior": 0.5,
                "provenance": "authored",
            }
        ],
    }


def test_regex_exact_scoring_wired_through_load_suite(tmp_path: Path) -> None:
    path = tmp_path / "rgx.json"
    path.write_text(json.dumps(_regex_suite_doc(r"[45]\d\d")), encoding="utf-8")
    suite = load_suite(str(path))  # PRODUCTION caller: load -> ScoringSpec.regex=True
    assert suite.scoring.regex is True
    scorer = make_deterministic_scorer(suite.scoring)
    item = suite.items[0]
    assert scorer(item, "404").score == 1.0  # regex full-match
    assert scorer(item, "200").score == 0.0  # not [45]xx
    assert scorer(item, "the code is 404").score == 0.0  # fullmatch, not substring


def test_regex_flag_rejected_on_non_exact_type() -> None:
    with pytest.raises(SuiteError, match="regex is only valid for exact"):
        ScoringSpec(type="verdict", labels=["flag", "pass"], regex=True)


# BLOCK B — a bad regex `expected` (scoring.regex=true) must fail loud at suite LOAD, not at score.


def test_bad_regex_pattern_fails_loud_at_load(tmp_path: Path) -> None:
    path = tmp_path / "bad_rgx.json"
    path.write_text(json.dumps(_regex_suite_doc("[unterminated")), encoding="utf-8")
    # The uncompilable pattern must abort at LOAD (SuiteError), BEFORE any run — not surface later
    # as a scoring-time exception. This closes the last input-independent raise path.
    with pytest.raises(SuiteError, match="not a valid regex"):
        load_suite(str(path))


def test_bad_regex_snapshot_reload_also_fails_loud(tmp_path: Path) -> None:
    """A per-run suite snapshot with a bad regex fails loud when reopened too (same constructor)."""
    with pytest.raises(SuiteError, match="not a valid regex"):
        Suite.from_mapping(_regex_suite_doc("(unclosed"))


# --- Determinism: re-scoring reproduces the outcome exactly (see the byte-identical test below) --


# --- Offline stub adapter (local transport) for the end-to-end determinism test ----------------


def _echo_local_factory():
    """A local-adapter transport that echoes the single word each smoke prompt asks for, so the
    verdict scorer produces real 1.0 scores (not parse-fails) to compare across a re-score."""

    def factory() -> object:
        def transport(url: str, data: bytes, timeout: float) -> str:
            body = json.loads(data.decode("utf-8"))
            prompt = body["messages"][0]["content"]
            answer = prompt.rsplit(": ", 1)[-1].strip()  # "...single word: pass" -> "pass"
            return json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "model": "local-x",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        return transport

    return factory  # type: ignore[return-value]


def _fixed_local_factory(response: str):
    """A local-adapter transport that returns a FIXED response for any prompt (used to inject a
    hostile model response — e.g. a nesting bomb — through a real sweep)."""

    def factory() -> object:
        def transport(url: str, data: bytes, timeout: float) -> str:
            return json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "model": "local-x",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": response},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        return transport

    return factory  # type: ignore[return-value]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_mt_score_rescoring_is_byte_identical_to_inline(tmp_path: Path) -> None:
    """Done-when: re-scoring stored raw rows via mt score matches the first-pass (inline) scores
    byte-for-byte. Sweep suites/smoke.json inline with the deterministic scorer, then re-score the
    stored raws with the same scorer, and assert the rows.jsonl bytes are identical."""
    suite = load_suite(str(_repo_root() / "suites" / "smoke.json"))
    scorer = make_deterministic_scorer(suite.scoring)

    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        scorer=scorer,  # INLINE deterministic scoring during the sweep
        local_transport_factory=_echo_local_factory(),
    )
    rows_path = tmp_path / "runs" / r.run_id / "rows.jsonl"
    after_run = rows_path.read_bytes()

    # inline scoring actually scored the responses (not collect-only / not parse-fail)
    inline_rows = [json.loads(line) for line in after_run.decode("utf-8").splitlines()]
    assert len(inline_rows) == 2
    assert all(row["score"] == 1.0 and row["scorer"] == VERDICT_SCORER for row in inline_rows)

    summary = score_run(run_id=r.run_id, out_dir=tmp_path, scorer=scorer)
    assert summary.scored == 2 and summary.no_response == 0
    after_score = rows_path.read_bytes()

    assert after_score == after_run  # BYTE-IDENTICAL re-score


# --- CLI wiring: auto-select the deterministic scorer from the suite ---------------------------


def test_cli_run_auto_selects_verdict_scorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mt run` with no injected scorer auto-selects the deterministic verdict scorer and scores
    inline as it sweeps (the Step-4 collect-only default is replaced)."""
    monkeypatch.chdir(_repo_root())
    monkeypatch.delenv(ENV_VAR, raising=False)
    out = tmp_path / "data"
    rc = main(
        ["run", "--suite", "suites/smoke.json", "--models", "general-35b", "--out", str(out)],
        deps=CliDeps(local_transport_factory=_echo_local_factory()),
    )
    assert rc == 0
    run_dir = next((out / "runs").iterdir())
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["score"] == 1.0 and row["scorer"] == VERDICT_SCORER for row in rows)


def test_cli_score_auto_selects_from_snapshot(tmp_path: Path) -> None:
    """`mt score` with no injected scorer auto-selects the deterministic scorer from the run's OWN
    suite snapshot (covers runner.load_run_suite), re-scoring collect-only rows."""
    suite = load_suite(str(_repo_root() / "suites" / "smoke.json"))
    # sweep collect-only (default runner scorer) so scores start null, then re-score via the CLI
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=_echo_local_factory(),
    )
    rows_path = tmp_path / "runs" / r.run_id / "rows.jsonl"
    rows0 = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert all(row["score"] is None for row in rows0)  # collect-only default

    rc = main(["score", r.run_id, "--out", str(tmp_path)], deps=CliDeps())  # no scorer -> auto
    assert rc == 0
    rows1 = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert all(row["score"] == 1.0 and row["scorer"] == VERDICT_SCORER for row in rows1)


def test_load_run_suite_returns_snapshot(tmp_path: Path) -> None:
    suite = load_suite(str(_repo_root() / "suites" / "smoke.json"))
    r = run(
        suite=suite,
        config=RunConfig(),
        out_dir=tmp_path,
        roster=["general-35b"],
        samples_per_cell=1,
        local_transport_factory=_echo_local_factory(),
    )
    reloaded = load_run_suite(r.run_id, tmp_path)
    assert reloaded.item_hash == suite.item_hash
    assert reloaded.scoring.type == "verdict"


def test_cli_run_rubric_suite_defers_to_step6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rubric suite does NOT crash mt run: responses are collected unscored (pending) with a
    clear stderr note deferring to Step 6's judge."""
    rubric_suite = {
        "suite": "rub",
        "version": 1,
        "description": "a rubric suite fixture",
        "domain": "d",
        "scoring": {"type": "rubric"},
        "items": [
            {
                "id": "r1",
                "tags": ["t"],
                "prompt": "explain something",
                "expected": "a thorough explanation",
                "difficulty_prior": 0.5,
                "provenance": "authored",
            }
        ],
    }
    suite_path = tmp_path / "rubric.json"
    suite_path.write_text(json.dumps(rubric_suite), encoding="utf-8")
    monkeypatch.delenv(ENV_VAR, raising=False)
    out = tmp_path / "data"
    rc = main(
        ["run", "--suite", str(suite_path), "--models", "general-35b", "--out", str(out)],
        deps=CliDeps(local_transport_factory=_echo_local_factory()),
    )
    assert rc == 0  # did not crash
    run_dir = next((out / "runs").iterdir())
    rows = [json.loads(line) for line in (run_dir / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["score"] is None and rows[0]["scorer"] is None  # collected unscored (pending)
    assert "Step 6" in capsys.readouterr().err  # the deferral note was surfaced
