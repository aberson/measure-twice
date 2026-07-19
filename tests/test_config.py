"""Config resolution + fail-loud tests, plus the switchboard path-dep and CLI entry-point proofs.

Covers the Step-1 done-when: ``mt --version`` exits 0 through the production dispatch,
``from switchboard.harness import aggregate_agreement`` succeeds (path dep wired), and the
config resolver aborts on malformed input while honoring explicit > env > cwd > defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from measure_twice import __version__
from measure_twice.cli import main
from measure_twice.config import (
    ALLOWED_CONFIG_FIELDS,
    DEFAULT_CLAUDE_POOL,
    DEFAULT_JUDGES,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MAX_TOKENS,
    DEFAULT_LOCAL_TIMEOUT_S,
    DEFAULT_MAX_CALLS,
    DEFAULT_ROSTER,
    DEFAULT_SAMPLES_PER_CELL,
    ENV_VAR,
    MIN_LOCAL_MAX_TOKENS,
    ConfigError,
    RunConfig,
    load_config,
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- Defaults ----------------------------------------------------------------------------


def test_defaults_when_no_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit path, no env var, no cwd file -> built-in defaults tagged 'defaults'.

    A simply-ABSENT implicit cwd config is not an error: resolution falls through to defaults
    (this folds in the former standalone test_absent_cwd_file_is_not_an_error). Every default
    field is asserted against its DEFAULT_* constant, not a re-typed literal, so a drift in the
    constant can't pass unnoticed (code-quality § one source of truth).
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)  # empty dir: no measure-twice.json here

    cfg = load_config()

    assert cfg.config_source == "defaults"
    assert cfg.roster == DEFAULT_ROSTER
    assert cfg.local_base_url == DEFAULT_LOCAL_BASE_URL
    assert cfg.local_max_tokens == DEFAULT_LOCAL_MAX_TOKENS
    assert cfg.claude_pool == DEFAULT_CLAUDE_POOL
    assert cfg.samples_per_cell == DEFAULT_SAMPLES_PER_CELL
    assert cfg.judges == DEFAULT_JUDGES
    assert cfg.max_calls == DEFAULT_MAX_CALLS


# --- Fail loud on malformed JSON / shape -------------------------------------------------


def test_malformed_invalid_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not valid json,", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(bad))


@pytest.mark.parametrize("payload", [[1, 2, 3], 42, "a bare string"])
def test_json_top_level_not_a_dict_raises(tmp_path: Path, payload: Any) -> None:
    """A non-object top-level JSON hits the 'must be a JSON object' fail-loud branch."""
    p = tmp_path / "cfg.json"
    _write_json(p, payload)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_malformed_unknown_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    _write_json(p, {"roster": ["haiku"], "totally_unknown_key": 1})
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_malformed_bad_value_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    _write_json(p, {"local_max_tokens": "lots"})  # str where an int is required
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_malformed_bad_list_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    _write_json(p, {"roster": "haiku"})  # str where a list is required
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_config_cannot_spoof_config_source(tmp_path: Path) -> None:
    """A file setting config_source is rejected (resolver-owned metadata, not file content)."""
    p = tmp_path / "cfg.json"
    _write_json(p, {"config_source": "explicit:lies"})
    with pytest.raises(ConfigError):
        load_config(str(p))


# --- Fail loud on missing / empty provided sources ---------------------------------------


def test_explicit_missing_path_raises(tmp_path: Path) -> None:
    """Operator named a --config file that isn't there: raise, never fall through to defaults."""
    missing = tmp_path / "nope.json"
    with pytest.raises(ConfigError):
        load_config(str(missing))


def test_env_missing_path_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """$MEASURE_TWICE_CONFIG pointing at a nonexistent file raises (provided-tier, must exist)."""
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "nope.json"))
    with pytest.raises(ConfigError):
        load_config()


def test_env_empty_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: MEASURE_TWICE_CONFIG='' is PRESENT (operator named nothing) -> must raise,
    not silently fall through to defaults. Symmetric with the explicit-path tier."""
    monkeypatch.setenv(ENV_VAR, "")
    with pytest.raises(ConfigError):
        load_config()


# --- Resolution-order precedence ---------------------------------------------------------


def test_explicit_beats_env_beats_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All three sources present -> explicit wins (distinguished by max_calls sentinel)."""
    explicit_p = tmp_path / "explicit.json"
    env_p = tmp_path / "env.json"
    _write_json(explicit_p, {"max_calls": 111})
    _write_json(env_p, {"max_calls": 222})
    _write_json(tmp_path / "measure-twice.json", {"max_calls": 333})

    monkeypatch.setenv(ENV_VAR, str(env_p))
    monkeypatch.chdir(tmp_path)

    cfg = load_config(str(explicit_p))

    assert cfg.max_calls == 111
    assert cfg.config_source == f"explicit:{explicit_p}"


def test_env_beats_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_p = tmp_path / "env.json"
    _write_json(env_p, {"max_calls": 222})
    _write_json(tmp_path / "measure-twice.json", {"max_calls": 333})

    monkeypatch.setenv(ENV_VAR, str(env_p))
    monkeypatch.chdir(tmp_path)

    cfg = load_config()  # no explicit path

    assert cfg.max_calls == 222
    assert cfg.config_source == "env:MEASURE_TWICE_CONFIG"


def test_cwd_used_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_json(tmp_path / "measure-twice.json", {"max_calls": 333})
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = load_config()

    assert cfg.max_calls == 333
    assert cfg.config_source == "cwd:measure-twice.json"


def test_valid_explicit_partial_config_merges_defaults(tmp_path: Path) -> None:
    """A partial config keeps defaults for unspecified fields and records the explicit source."""
    p = tmp_path / "cfg.json"
    _write_json(p, {"claude_pool": 4})

    cfg = load_config(str(p))

    assert cfg.claude_pool == 4
    assert cfg.roster == DEFAULT_ROSTER  # untouched field keeps its default
    assert cfg.config_source == f"explicit:{p}"


# --- Field validation (the dataclass is the fail-loud boundary, not just the loader) -----


def test_runconfig_rejects_nonpositive_count() -> None:
    with pytest.raises(ConfigError):
        RunConfig(claude_pool=0)


def test_local_max_tokens_below_floor_rejected(tmp_path: Path) -> None:
    """Below switchboard's reasoning-model floor a local verdict truncates to content=''."""
    p = tmp_path / "cfg.json"
    _write_json(p, {"local_max_tokens": MIN_LOCAL_MAX_TOKENS - 1})
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_local_max_tokens_at_floor_ok() -> None:
    cfg = RunConfig(local_max_tokens=MIN_LOCAL_MAX_TOKENS)
    assert cfg.local_max_tokens == MIN_LOCAL_MAX_TOKENS


@pytest.mark.parametrize(
    "bad_url",
    ["not-a-url", "ftp://x", "http://x/v1; rm -rf /", "http://x/../etc", ""],
)
def test_local_base_url_rejects_unsafe(bad_url: str) -> None:
    with pytest.raises(ConfigError):
        RunConfig(local_base_url=bad_url)


def test_local_base_url_accepts_valid() -> None:
    cfg = RunConfig(local_base_url="https://example.com:9000/v1")
    assert cfg.local_base_url == "https://example.com:9000/v1"


@pytest.mark.parametrize("bad_name", ["haiku; DROP", "../../etc/passwd", "has space"])
def test_roster_rejects_unsafe_model_name(bad_name: str) -> None:
    with pytest.raises(ConfigError):
        RunConfig(roster=[bad_name])


def test_judges_rejects_unsafe_model_name() -> None:
    with pytest.raises(ConfigError):
        RunConfig(judges=["sonnet; rm"])


def test_empty_roster_rejected() -> None:
    with pytest.raises(ConfigError):
        RunConfig(roster=[])


def test_empty_judges_rejected() -> None:
    with pytest.raises(ConfigError):
        RunConfig(judges=[])


# --- switchboard path dependency ---------------------------------------------------------


def test_switchboard_aggregate_agreement_importable() -> None:
    """Proves [tool.uv.sources] switchboard path dep is wired and importable."""
    from switchboard.harness import aggregate_agreement

    verdict = aggregate_agreement(verdicted=4, agreements=4)
    assert verdict.kill is False  # perfect agreement never kills


# --- CLI entry point (production dispatch) -----------------------------------------------


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Integration: --version through the real argparse dispatch returns 0 and prints version."""
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_cli_no_command_returns_nonzero() -> None:
    """A bare invocation with no subcommand has nothing to do -> non-zero exit."""
    assert main([]) == 1


def test_local_timeout_s_default_matches_constant() -> None:
    """Default local_timeout_s is DEFAULT_LOCAL_TIMEOUT_S (one source of truth), not a literal."""
    assert RunConfig().local_timeout_s == DEFAULT_LOCAL_TIMEOUT_S == 120.0


def test_local_timeout_s_from_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config file may raise the local timeout (e.g. for cold model loads > the 120s default)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    p = tmp_path / "measure-twice.json"
    _write_json(p, {"local_timeout_s": 300})
    cfg = load_config(str(p))
    assert cfg.local_timeout_s == 300
    assert "local_timeout_s" in ALLOWED_CONFIG_FIELDS  # auto-derived allow-list picks it up


@pytest.mark.parametrize("bad", [0, -1, -0.5, "abc", True, None])
def test_local_timeout_s_invalid_rejected(bad: object) -> None:
    """Non-positive / non-numeric / bool timeouts fail loud (measurement-validity)."""
    with pytest.raises(ConfigError, match="local_timeout_s"):
        RunConfig(local_timeout_s=bad)  # type: ignore[arg-type]


def test_local_timeout_s_accepts_float() -> None:
    """A fractional-second timeout is a valid positive number."""
    assert RunConfig(local_timeout_s=90.5).local_timeout_s == 90.5
