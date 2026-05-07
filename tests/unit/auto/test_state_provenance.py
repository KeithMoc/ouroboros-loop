"""Tests for the optional gateway-provenance metadata on auto state."""

from __future__ import annotations

import json

import pytest

from ouroboros.auto.state import (
    PROVENANCE_ALLOWED_KEYS,
    AutoPhase,
    AutoPipelineState,
    AutoStore,
    redact_provenance,
)


def _state(**overrides) -> AutoPipelineState:
    base = {"goal": "Build a CLI", "cwd": "/tmp/project"}
    base.update(overrides)
    return AutoPipelineState(**base)


def test_default_state_has_no_provenance() -> None:
    state = _state()
    assert state.provenance is None
    payload = state.to_dict()
    assert payload["provenance"] is None


def test_redact_provenance_drops_unknown_keys() -> None:
    raw = {
        "source": "discord",
        "rewrite": True,
        "user_token": "xoxb-supersecret",
        "raw_message": "delete prod database now",
        "channel_id_hash": "ab12cd",
    }
    cleaned = redact_provenance(raw)
    assert cleaned == {
        "source": "discord",
        "rewrite": True,
        "channel_id_hash": "ab12cd",
    }
    # The allowlist must not silently grow without intent.
    assert "user_token" not in PROVENANCE_ALLOWED_KEYS
    assert "raw_message" not in PROVENANCE_ALLOWED_KEYS


def test_redact_provenance_returns_none_for_empty() -> None:
    assert redact_provenance(None) is None
    assert redact_provenance({}) is None
    assert redact_provenance({"unrelated": "x"}) is None


def test_redact_provenance_rejects_wrong_types() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        redact_provenance("discord")
    with pytest.raises(ValueError, match="rewrite must be a boolean"):
        redact_provenance({"rewrite": "yes"})
    with pytest.raises(ValueError, match="source must be a string"):
        redact_provenance({"source": 7})
    with pytest.raises(ValueError, match="exceeds 32"):
        redact_provenance({"source": "x" * 33})
    with pytest.raises(ValueError, match="hex digest"):
        redact_provenance({"channel_id_hash": "not-hex!"})
    with pytest.raises(ValueError, match="printable without whitespace"):
        redact_provenance({"platform_message_id": "id with space"})


def test_state_persists_only_allowlisted_provenance(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _state()
    state.provenance = redact_provenance(
        {
            "source": "discord",
            "rewrite": True,
            "channel_id_hash": "deadbeef",
            "secret": "leak",
        }
    )
    state.transition(AutoPhase.INTERVIEW, "starting")
    path = store.save(state)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["provenance"] == {
        "source": "discord",
        "rewrite": True,
        "channel_id_hash": "deadbeef",
    }
    assert "secret" not in raw["provenance"]

    loaded = store.load(state.auto_session_id)
    assert loaded.provenance == state.provenance


def test_state_save_rejects_unredacted_provenance(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _state()
    state.provenance = {"source": "discord", "raw_message": "leak"}

    with pytest.raises(ValueError, match="redact_provenance"):
        store.save(state)


def test_legacy_state_without_provenance_field_loads(tmp_path) -> None:
    """State files written before this field exists must still load."""
    store = AutoStore(tmp_path)
    state = _state()
    state.transition(AutoPhase.INTERVIEW, "starting")
    path = store.save(state)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("provenance", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.load(state.auto_session_id)
    assert loaded.provenance is None
