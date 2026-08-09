"""Step-10 structural coverage checks for the benchmark-domain investigation."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVESTIGATION = PROJECT_ROOT / "docs" / "investigations" / "benchmark-domains.md"
METHODOLOGY = PROJECT_ROOT / "docs" / "methodology" / "04-domain-taxonomy.md"


def test_investigation_covers_all_required_sections_and_external_sources() -> None:
    text = INVESTIGATION.read_text(encoding="utf-8")

    for heading in (
        "## 1. Benchmark-domain taxonomy",
        "## 2. Item-design patterns",
        "## 3. Difficulty calibration methods",
        "## 4. Anti-saturation and coverage controls",
        "## 5. Judge-circularity and production-path guards",
        "## 6. Contamination and provenance controls",
        "## 7. Decision record: MT-DOM-01",
    ):
        assert heading in text
    for domain in (
        "Judging / grading",
        "Code authorship",
        "Planning",
        "Extraction",
        "Instruction following",
        "Synthesis",
    ):
        assert domain in text

    sources = set(re.findall(r"https://[^)]+", text))
    assert len(sources) >= 10
    assert "tier-judging-v0" in text
    assert "difficulty_prior" in text
    assert "pre-registered" in text


def test_methodology_records_the_v0_scope_and_non_circularity_boundary() -> None:
    text = METHODOLOGY.read_text(encoding="utf-8")

    assert "deterministic reviewer and gate judgments" in text
    assert "one `verdict` scorer" in text
    assert "a model cannot promote a decision by grading its own output" in text
