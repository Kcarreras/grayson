"""Per-user identity: set/get, validation, and the stamps it leaves behind."""

from __future__ import annotations

import pytest

from grayson.identity import get_user_id, set_user_id, user_config_path
from grayson.knowledge import KnowledgeStore


def test_unset_by_default():
    assert get_user_id() is None


def test_set_and_get_roundtrip():
    assert set_user_id("kcg") == "kcg"
    assert get_user_id() == "kcg"
    assert user_config_path().is_file()


@pytest.mark.parametrize("bad", ["", "  ", "-lead", "has space", "x" * 33, "a/b", "é"])
def test_invalid_ids_rejected(bad):
    with pytest.raises(ValueError):
        set_user_id(bad)


def test_env_overrides_file(monkeypatch):
    set_user_id("filed")
    monkeypatch.setenv("GRAYSON_USER_ID", "envd")
    assert get_user_id() == "envd"


def test_reset_overwrites_only_user_section():
    set_user_id("first")
    path = user_config_path()
    extra = '\n[other]\nkeep = "yes"\n'
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    set_user_id("second")
    text = path.read_text(encoding="utf-8")
    assert 'id = "second"' in text
    assert "first" not in text
    assert 'keep = "yes"' in text
    assert get_user_id() == "second"


def test_facts_carry_author(workspace):
    set_user_id("kcg")
    ks = KnowledgeStore(workspace.knowledge_dir)
    fact = ks.add_fact("DB.S.T", "grain is one row per order", fact_id="grain")
    assert fact["author"] == "kcg"
    assert fact["created_by"] == "agent"  # actor kind unchanged
    assert ks.fact("DB.S.T", "grain")["author"] == "kcg"


def test_confirm_resolves_generic_user_to_id(workspace):
    set_user_id("kcg")
    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.add_fact("DB.S.T", "x", fact_id="a")
    assert ks.confirm_fact("DB.S.T", "a")["confirmed_by"] == "kcg"
    # an explicit name is kept as given
    ks.add_fact("DB.S.T", "y", fact_id="b")
    assert ks.confirm_fact("DB.S.T", "b", by="kane")["confirmed_by"] == "kane"


def test_facts_without_id_have_no_author(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    fact = ks.add_fact("DB.S.T", "no id configured", fact_id="anon")
    assert fact["author"] is None
