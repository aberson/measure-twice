"""Suite schema, fail-loud loader, canonical item-hash, and the ``mt validate`` CLI tests.

Covers the Step-2 done-when: the smoke fixture round-trips to a valid ``Suite``; ``suite_hash``
is deterministic across reloads and changes on any item edit; each malformed suite raises
``SuiteError`` with a DISTINCT, identifiable message (bad name, missing ``expected``, duplicate ids
named explicitly, plus invalid JSON, unknown scoring type, and out-of-range ``difficulty_prior``);
and ``mt validate`` exits 0 on the fixture while a malformed suite exits non-zero — all through
``measure_twice.cli.main``.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from measure_twice.cli import main
from measure_twice.suite import (
    Item,
    ScoringSpec,
    Suite,
    SuiteError,
    load_suite,
    suite_hash,
    validate,
)

# Repo-root-relative resolution of the committed fixture (tests/ -> repo root -> suites/smoke.json).
REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_PATH = REPO_ROOT / "suites" / "smoke.json"

_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _valid_suite_dict() -> dict[str, Any]:
    """A fresh, schema-valid suite mapping to mutate into malformed variants (deep-copied)."""
    return deepcopy(
        {
            "suite": "unit",
            "version": 1,
            "description": "in-memory valid suite",
            "domain": "testing",
            "scoring": {"type": "verdict", "labels": ["pass", "flag"]},
            "items": [
                {
                    "id": "001-a",
                    "tags": ["t"],
                    "prompt": "say pass",
                    "expected": "pass",
                    "difficulty_prior": 0.2,
                    "provenance": "authored",
                },
                {
                    "id": "002-b",
                    "tags": ["t"],
                    "prompt": "say flag",
                    "expected": "flag",
                    "difficulty_prior": 0.4,
                    "provenance": "authored",
                },
            ],
        }
    )


def _write(tmp_path: Path, payload: Any, name: str = "s.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# --- Round-trip --------------------------------------------------------------------------


def test_smoke_fixture_exists() -> None:
    assert SMOKE_PATH.is_file(), f"fixture missing at {SMOKE_PATH}"


def test_round_trip_smoke_fixture() -> None:
    """The committed smoke fixture loads to a valid Suite with 2 verdict items."""
    suite = load_suite(SMOKE_PATH)

    assert isinstance(suite, Suite)
    assert suite.suite == "smoke"
    assert suite.version == 1
    assert len(suite.items) == 2
    assert isinstance(suite.scoring, ScoringSpec)
    assert suite.scoring.type == "verdict"
    assert suite.scoring.labels == ["pass", "flag"]
    ids = [item.id for item in suite.items]
    assert ids == ["001-smoke-pass", "002-smoke-flag"]
    # expected values are drawn from the declared labels (fixture sanity).
    for item in suite.items:
        assert suite.scoring.labels is not None and item.expected in suite.scoring.labels


def test_validate_alias_matches_load_suite() -> None:
    """``validate`` is the CLI-facing entry and shares the loader's code path."""
    assert validate(SMOKE_PATH).suite == "smoke"


# --- Canonical item hash -----------------------------------------------------------------


def test_hash_is_64_char_hex() -> None:
    h = load_suite(SMOKE_PATH).item_hash
    assert isinstance(h, str)
    assert _HEX64.fullmatch(h), f"not a 64-char sha256 hex: {h!r}"


def test_hash_stable_across_reloads() -> None:
    """Two independent loads of identical content produce the identical hash (determinism)."""
    assert load_suite(SMOKE_PATH).item_hash == load_suite(SMOKE_PATH).item_hash


def test_hash_stable_across_copied_content(tmp_path: Path) -> None:
    """Hash depends on content, not path: a byte-identical copy hashes the same as the original."""
    original = load_suite(SMOKE_PATH)
    copy_path = _write(tmp_path, json.loads(SMOKE_PATH.read_text(encoding="utf-8")), "copy.json")
    assert load_suite(copy_path).item_hash == original.item_hash


def test_hash_changes_when_item_content_changes(tmp_path: Path) -> None:
    """Mutating any item's content (a prompt) yields a different hash — a changed instrument."""
    base = _valid_suite_dict()
    baseline = load_suite(_write(tmp_path, base, "base.json")).item_hash

    mutated = _valid_suite_dict()
    mutated["items"][0]["prompt"] = "say pass, but differently"
    changed = load_suite(_write(tmp_path, mutated, "mutated.json")).item_hash

    assert changed != baseline


def test_hash_ignores_suite_metadata(tmp_path: Path) -> None:
    """Only item content feeds the hash: re-titling the suite leaves the item-hash unchanged."""
    base = _valid_suite_dict()
    retitled = _valid_suite_dict()
    retitled["suite"] = "renamed"
    retitled["description"] = "totally different description"
    assert (
        load_suite(_write(tmp_path, base, "b.json")).item_hash
        == load_suite(_write(tmp_path, retitled, "r.json")).item_hash
    )


# The frozen canonical item-hash of suites/smoke.json (ensure_ascii=True; smoke.json is pure ASCII
# and its difficulty_priors are already 0.0, so this equals the pre-hardening value). A change to
# the fixture's item content OR to the canonicalization would break this — a genuine instrument-
# identity regression guard (plan §3: "A changed hash = a different instrument").
FROZEN_SMOKE_HASH = "e6775605efca89a1311c5f54974693665b994768ad8ae346a96aedc3fb971df0"


def test_smoke_item_hash_is_frozen_golden() -> None:
    """Golden regression: the smoke fixture's canonical item-hash matches its frozen value.

    Exercised through both the ``Suite.item_hash`` property and the ``suite_hash`` module function
    (they must agree — the property is a thin delegate — and both must equal the frozen value).
    """
    suite = load_suite(SMOKE_PATH)
    assert suite.item_hash == FROZEN_SMOKE_HASH
    assert suite_hash(suite.items) == FROZEN_SMOKE_HASH


def test_hash_int_vs_float_difficulty_prior_are_equal(tmp_path: Path) -> None:
    """Instrument identity: difficulty_prior 1 and 1.0 are the same value and MUST hash the same."""
    as_int = _valid_suite_dict()
    as_int["items"][0]["difficulty_prior"] = 1
    as_float = _valid_suite_dict()
    as_float["items"][0]["difficulty_prior"] = 1.0
    assert (
        load_suite(_write(tmp_path, as_int, "i.json")).item_hash
        == load_suite(_write(tmp_path, as_float, "f.json")).item_hash
    )
    # And the coerced field is genuinely a float on the built Item.
    built = load_suite(_write(tmp_path, as_int, "i2.json"))
    assert isinstance(built.items[0].difficulty_prior, float)


# --- Fail loud: each malformed suite raises a DISTINCT, identifiable SuiteError -----------


def test_invalid_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not valid json,", encoding="utf-8")
    with pytest.raises(SuiteError, match="could not read suite"):
        load_suite(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="suite file not found"):
        load_suite(tmp_path / "nope.json")


def test_top_level_not_object_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, [1, 2, 3])
    with pytest.raises(SuiteError, match="must be a JSON object"):
        load_suite(p)


def test_bad_suite_name_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["suite"] = "not a safe name!"
    with pytest.raises(SuiteError, match=r"suite name .* unsafe characters"):
        load_suite(_write(tmp_path, bad))


def test_bad_item_id_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"][0]["id"] = "bad id/../x"
    with pytest.raises(SuiteError, match=r"item id .* unsafe characters"):
        load_suite(_write(tmp_path, bad))


def test_missing_expected_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    del bad["items"][1]["expected"]
    with pytest.raises(SuiteError, match=r"missing required item field\(s\): \['expected'\]"):
        load_suite(_write(tmp_path, bad))


def test_duplicate_item_ids_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"][1]["id"] = bad["items"][0]["id"]  # collide the two ids
    with pytest.raises(SuiteError, match=r"duplicate item id\(s\): \['001-a'\]"):
        load_suite(_write(tmp_path, bad))


def test_unknown_top_level_key_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["surprise"] = 1
    with pytest.raises(SuiteError, match=r"unknown suite key\(s\): \['surprise'\]"):
        load_suite(_write(tmp_path, bad))


def test_unknown_item_key_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"][0]["weight"] = 3
    with pytest.raises(SuiteError, match=r"unknown item key\(s\): \['weight'\]"):
        load_suite(_write(tmp_path, bad))


def test_unknown_scoring_type_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["scoring"]["type"] = "vibes"
    with pytest.raises(SuiteError, match=r"scoring\.type must be one of"):
        load_suite(_write(tmp_path, bad))


def test_difficulty_prior_out_of_range_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"][0]["difficulty_prior"] = 1.5
    with pytest.raises(SuiteError, match=r"difficulty_prior must be within \[0.0, 1.0\]"):
        load_suite(_write(tmp_path, bad))


def test_difficulty_prior_wrong_type_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"][0]["difficulty_prior"] = "easy"
    with pytest.raises(SuiteError, match="difficulty_prior must be a number"):
        load_suite(_write(tmp_path, bad))


def test_empty_items_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"] = []
    with pytest.raises(SuiteError, match="at least one item"):
        load_suite(_write(tmp_path, bad))


def test_items_wrong_container_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["items"] = {"not": "a list"}
    with pytest.raises(SuiteError, match="items must be a list"):
        load_suite(_write(tmp_path, bad))


def test_scoring_wrong_type_raises(tmp_path: Path) -> None:
    bad = _valid_suite_dict()
    bad["scoring"] = 42
    with pytest.raises(SuiteError, match="scoring must be a JSON object"):
        load_suite(_write(tmp_path, bad))


def test_lone_surrogate_in_prompt_raises(tmp_path: Path) -> None:
    """A string field with a lone UTF-16 surrogate can't UTF-8 round-trip -> SuiteError, not a
    raw UnicodeEncodeError escaping through the hash. The JSON escape ``\\ud800`` decodes to a
    lone surrogate str, so this reproduces the reviewer's live crash at load time."""
    p = tmp_path / "surrogate.json"
    p.write_text(
        json.dumps(_valid_suite_dict()).replace('"say pass"', '"say \\ud800 pass"'),
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="lone surrogate"):
        load_suite(p)


def test_deeply_nested_json_raises_suiteerror_not_recursionerror(tmp_path: Path) -> None:
    """A nesting-bomb JSON raises RecursionError from json.load; it must be folded into the
    fail-loud contract as a SuiteError, never escape as a raw RecursionError."""
    depth = 100_000
    p = tmp_path / "bomb.json"
    p.write_text("[" * depth + "]" * depth, encoding="utf-8")
    with pytest.raises(SuiteError, match="could not read suite"):
        load_suite(p)


def test_dot_only_suite_name_raises(tmp_path: Path) -> None:
    """A dot-only suite name ('..') passes _SAFE_NAME_RE but is a path-traversal token -> reject."""
    bad = _valid_suite_dict()
    bad["suite"] = ".."
    with pytest.raises(SuiteError, match="must contain at least one alphanumeric"):
        load_suite(_write(tmp_path, bad))


def test_dot_only_item_id_raises(tmp_path: Path) -> None:
    """A dot-only item id ('.') passes _SAFE_NAME_RE but is a path-traversal token -> reject."""
    bad = _valid_suite_dict()
    bad["items"][0]["id"] = "."
    with pytest.raises(SuiteError, match="must contain at least one alphanumeric"):
        load_suite(_write(tmp_path, bad))


def test_malformed_messages_are_pairwise_distinct(tmp_path: Path) -> None:
    """The three named violations (bad name, missing expected, dup ids) carry distinct messages."""
    variants: dict[str, dict[str, Any]] = {}
    v_name = _valid_suite_dict()
    v_name["suite"] = "bad name!"
    variants["name"] = v_name
    v_exp = _valid_suite_dict()
    del v_exp["items"][0]["expected"]
    variants["expected"] = v_exp
    v_dup = _valid_suite_dict()
    v_dup["items"][1]["id"] = v_dup["items"][0]["id"]
    variants["dup"] = v_dup

    messages: list[str] = []
    for key, payload in variants.items():
        with pytest.raises(SuiteError) as exc_info:
            load_suite(_write(tmp_path, payload, f"{key}.json"))
        messages.append(str(exc_info.value))
    assert len(set(messages)) == len(messages), messages


# --- Dataclass is itself the fail-loud boundary (not just the loader) --------------------


def test_direct_item_construction_validates() -> None:
    with pytest.raises(SuiteError, match=r"item id .* unsafe characters"):
        Item(
            id="has space",
            tags=["t"],
            prompt="p",
            expected="pass",
            difficulty_prior=0.1,
            provenance="authored",
        )


def test_direct_suite_construction_rejects_duplicate_ids() -> None:
    item = Item(
        id="dup",
        tags=["t"],
        prompt="p",
        expected="pass",
        difficulty_prior=0.1,
        provenance="authored",
    )
    with pytest.raises(SuiteError, match=r"duplicate item id\(s\)"):
        Suite(
            suite="s",
            version=1,
            description="d",
            domain="testing",
            scoring=ScoringSpec(type="verdict", labels=["pass"]),
            items=[item, item],
        )


# --- Integration through the production CLI entry point (measure_twice.cli.main) ----------


def test_cli_validate_smoke_exits_zero_and_prints_hash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mt validate suites/smoke.json` via main() -> exit 0 and a 64-char hash on stdout."""
    rc = main(["validate", str(SMOKE_PATH)])
    out = capsys.readouterr().out
    assert rc == 0
    assert _HEX64.search(out), f"no item-hash printed; stdout was: {out!r}"


def test_cli_validate_malformed_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed suite through the CLI returns non-zero and reports the error on stderr."""
    bad = _valid_suite_dict()
    del bad["items"][0]["expected"]
    rc = main(["validate", str(_write(tmp_path, bad))])
    err = capsys.readouterr().err
    assert rc != 0
    assert "expected" in err


def test_cli_validate_surrogate_exits_nonzero_and_prints_no_valid_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (the BLOCK): a lone-surrogate suite through the CLI must exit non-zero AND print
    NO 'valid' line. Pre-fix, the loader accepted the surrogate and ``mt validate`` printed
    '<suite>: valid ...' then crashed with a raw traceback past the ``except SuiteError`` when the
    hash tried to UTF-8 encode it. Now the surrogate is rejected at load (before any 'valid' line),
    and the hash is additionally computed before printing — so this asserts the fail-loud contract
    holds end-to-end through the CLI entry point."""
    p = tmp_path / "surrogate.json"
    p.write_text(
        json.dumps(_valid_suite_dict()).replace('"say pass"', '"say \\ud800 pass"'),
        encoding="utf-8",
    )
    rc = main(["validate", str(p)])
    captured = capsys.readouterr()
    assert rc != 0
    assert "valid" not in captured.out
    assert "lone surrogate" in captured.err
