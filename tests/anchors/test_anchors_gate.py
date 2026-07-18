"""Anchor calibration gate — the permanent, deterministic ``score(good) > score(garbage)`` layer.

measurement-validity.md § "Calibrate with anchors before comparing candidates": before trusting a
metric, feed it a frozen known-good and a known-garbage input and assert ``score(good) >
score(garbage)``. A scorer that cannot fail garbage cannot pick winners. This is the CI ORDERING
GATE — one assertion per deterministic scorer, FAST + DETERMINISTIC (no live model), holding
forever. It is the project-neutral port of void_furnace's ``AnchorPair`` pattern
(``void_furnace/tests/test_benchmark/test_anchors.py``).

The pairs are frozen as DATA (``scorer_anchors.json``) so they are a stable regression contract,
decoupled from code. Each pair is driven through the PRODUCTION scoring path
(:func:`make_deterministic_scorer`), so the gate proves the shipping scorer discriminates — not a
re-implementation of it (measurement-validity.md § assemble through the production code path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from measure_twice.scoring import make_deterministic_scorer
from measure_twice.suite import Item, ScoringSpec

_ANCHORS_PATH = Path(__file__).with_name("scorer_anchors.json")


def _load_pairs() -> list[dict[str, Any]]:
    data = json.loads(_ANCHORS_PATH.read_text(encoding="utf-8"))
    pairs = data["pairs"]
    assert isinstance(pairs, list) and pairs
    return pairs


_PAIRS = _load_pairs()


def _item(expected: str) -> Item:
    """A minimal, schema-valid item carrying only the gold ``expected`` the scorer keys on."""
    return Item(
        id="anchor-item",
        tags=["anchor"],
        prompt="anchor calibration prompt",
        expected=expected,
        difficulty_prior=0.5,
        provenance="authored",
    )


def test_anchors_cover_every_deterministic_scorer() -> None:
    """Both deterministic scorers have a frozen pair, and good != garbage for each."""
    covered = {pair["scorer"] for pair in _PAIRS}
    assert covered == {"verdict", "exact"}
    for pair in _PAIRS:
        assert pair["good"] != pair["garbage"], f"{pair['scorer']} good/garbage must differ"


@pytest.mark.parametrize("pair", _PAIRS, ids=[p["scorer"] for p in _PAIRS])
def test_anchor_good_outscores_garbage(pair: dict[str, Any]) -> None:
    """THE ORDERING GATE: the known-good response strictly out-scores the known-garbage one,
    through the production deterministic scorer, for every scorer. Forever."""
    scoring = ScoringSpec(type=pair["scorer"], labels=pair["labels"])
    scorer = make_deterministic_scorer(scoring)
    item = _item(pair["expected"])

    good = scorer(item, pair["good"])
    garbage = scorer(item, pair["garbage"])

    assert good.score is not None and garbage.score is not None
    assert good.score > garbage.score, (
        f"{pair['scorer']} anchor did not discriminate: "
        f"good={good.score} garbage={garbage.score} (non-discriminating scorer)"
    )
    # The known-good is a full-credit response and the known-garbage earns zero — the anchors are
    # deliberately divergent in the dimension the scorer keys on, so the ordering is unambiguous.
    assert good.score == 1.0
    assert garbage.score == 0.0
