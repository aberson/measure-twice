"""Static coverage and frozen scorer anchors for the first flagship suite."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from measure_twice.scoring import make_deterministic_scorer
from measure_twice.suite import load_suite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = PROJECT_ROOT / "suites" / "tier-judging-v0.json"
ANCHORS_PATH = PROJECT_ROOT / "tests" / "anchors" / "tier_judging_v0_anchors.json"


def test_flagship_suite_has_the_required_curated_coverage() -> None:
    suite = load_suite(SUITE_PATH)

    assert suite.suite == "tier-judging-v0"
    assert suite.domain == "tier-routing-judging"
    assert suite.scoring.type == "verdict"
    assert suite.scoring.labels == ["pass", "flag"]
    assert len(suite.items) >= 100
    assert {item.expected for item in suite.items} == {"pass", "flag"}

    tag_counts = Counter(tag for item in suite.items for tag in item.tags)
    assert min(tag_counts.values()) >= 8
    assert {tag for tag in tag_counts if tag.startswith("lens-")} == {
        "lens-style",
        "lens-correctness",
        "lens-grading",
    }
    assert {tag for tag in tag_counts if tag.startswith("difficulty-")} == {
        "difficulty-easy",
        "difficulty-lower-mid",
        "difficulty-upper-mid",
        "difficulty-hard",
        "difficulty-adversarial",
    }

    difficulty_counts = Counter(item.difficulty_prior for item in suite.items)
    assert len(difficulty_counts) >= 4
    assert all(count >= 8 for count in difficulty_counts.values())
    provenance_counts = Counter(item.provenance for item in suite.items)
    assert max(provenance_counts.values()) <= 20
    assert all(item.provenance.startswith("authored:counterfactual:") for item in suite.items)


def test_flagship_anchor_pairs_order_through_the_production_verdict_scorer() -> None:
    suite = load_suite(SUITE_PATH)
    raw: dict[str, Any] = json.loads(ANCHORS_PATH.read_text(encoding="utf-8"))
    pairs = raw["pairs"]
    assert isinstance(pairs, list) and len(pairs) >= 6
    item_by_id = {item.id: item for item in suite.items}
    scorer = make_deterministic_scorer(suite.scoring)

    covered_lenses: set[str] = set()
    for pair in pairs:
        assert isinstance(pair, dict)
        item_id = pair["item_id"]
        assert isinstance(item_id, str)
        item = item_by_id[item_id]
        good = scorer(item, pair["good"])
        garbage = scorer(item, pair["garbage"])
        assert good.score is not None and garbage.score is not None
        assert good.score > garbage.score
        assert good.score == 1.0
        assert garbage.score == 0.0
        covered_lenses.add(next(tag for tag in item.tags if tag.startswith("lens-")))

    assert covered_lenses == {"lens-style", "lens-correctness", "lens-grading"}
