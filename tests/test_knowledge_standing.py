"""Standing: whether what a fact rests on still holds — a second axis beside
status, derived from anchors the store records at write time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from grayson.knowledge import (
    KnowledgeStore,
    StandingContext,
    annotate_doc,
    effective_standing,
)
from grayson.knowledge.standing import agent_actions, column_mentions, derive_anchors

T = "DB.S.T"


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


def _ctx(**kw) -> StandingContext:
    return StandingContext(**kw)


def _first(ks, table=T):
    doc = ks.read(table)
    return doc["facts"][0], doc


# -- anchors -----------------------------------------------------------------


def test_column_mentions_are_whole_words_of_recorded_columns():
    cols = [{"name": "AMOUNT"}, {"name": "ID"}, {"name": "OLD", "dropped": True}]
    assert column_mentions("AMOUNT is gross; the id is a surrogate; OLD too", cols) == [
        "AMOUNT",
        "ID",
    ]
    assert column_mentions("amounts are gross", cols) == []  # AMOUNTS is not AMOUNT


def test_add_fact_anchors_to_columns_and_definitions(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}, {"name": "STATUS"}]})
    ks.upsert_definition(T, {"path": "models/t.sql", "hash": "sha256:aaaa", "kind": "dbt_model"})
    f = ks.add_fact(T, "AMOUNT is gross and status is lowercase")
    assert {"kind": "column", "name": "AMOUNT"} in f["anchors"]
    assert {"kind": "column", "name": "STATUS"} in f["anchors"]
    assert {"kind": "definition", "key": "models/t.sql", "hash": "sha256:aaaa"} in f["anchors"]


def test_derive_anchors_keeps_record_anchors_and_folds_duplicates():
    doc = {"columns": [{"name": "A"}], "definitions": []}
    keep = [{"kind": "record", "session": "s", "id": "p"}, {"kind": "column", "name": "ZZZ"}]
    out = derive_anchors("A and A again", doc, keep=keep)
    assert out == [{"kind": "record", "session": "s", "id": "p"}, {"kind": "column", "name": "A"}]


# -- the rules ---------------------------------------------------------------


def test_plain_fact_is_current_and_writes_no_lifecycle_keys(ks, workspace):
    ks.add_fact(T, "plain")
    text = (workspace.knowledge_dir / "DB" / "S" / "T.md").read_text(encoding="utf-8")
    for key in ("standing", "anchors", "supersedes", "retired_by", "kind:", "compatible_with"):
        assert key not in text
    fact, doc = _first(ks)
    assert effective_standing(fact, doc, _ctx()) == ("current", "")


def test_changed_definition_hash_makes_fact_unverified(ks):
    ks.upsert_definition(T, {"path": "models/t.sql", "hash": "sha256:aaaa", "kind": "dbt_model"})
    ks.add_fact(T, "one row per order")
    fact, doc = _first(ks)
    assert effective_standing(fact, doc, _ctx()) == ("current", "")
    ks.upsert_definition(T, {"path": "models/t.sql", "hash": "sha256:bbbb", "kind": "dbt_model"})
    fact, doc = _first(ks)
    standing, reason = effective_standing(fact, doc, _ctx())
    assert standing == "unverified" and "models/t.sql" in reason and "changed" in reason


def test_dropped_column_makes_fact_stale(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT", "description": "gross"}, {"name": "ID"}]})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    ks.sync_columns(T, [{"name": "ID", "type": "NUMBER"}])  # AMOUNT kept, flagged dropped
    fact, doc = _first(ks)
    assert effective_standing(fact, doc, _ctx()) == (
        "stale",
        "column AMOUNT was dropped from the warehouse",
    )


def test_live_columns_at_session_start_judge_before_any_sync(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}]})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    ctx = StandingContext.build(None, None, live_columns={T: [{"name": "ID"}]})
    fact, doc = _first(ks)
    standing, reason = effective_standing(fact, doc, ctx)
    assert standing == "stale" and "live DESCRIBE" in reason
    still = StandingContext.build(None, None, live_columns={T: [{"name": "amount"}]})
    assert effective_standing(fact, doc, still)[0] == "current"


def test_proposed_fact_past_horizon_is_unverified_until_confirmed(ks):
    ks.add_fact(T, "a hunch", fact_id="hunch")
    fact, doc = _first(ks)
    later = datetime.now(UTC) + timedelta(days=100)
    standing, reason = effective_standing(fact, doc, _ctx(now=later, proposed_horizon_days=90))
    assert standing == "unverified" and "never confirmed" in reason
    assert effective_standing(fact, doc, _ctx(now=later, proposed_horizon_days=0))[0] == "current"
    ks.confirm_fact(T, "hunch")
    fact, doc = _first(ks)
    assert effective_standing(fact, doc, _ctx(now=later))[0] == "current"


def test_record_anchor_follows_the_published_record(ks):
    anchor = {"kind": "record", "session": "s1", "id": "p_001", "record_kind": "proposal"}
    ks.add_fact(T, "verified fix", fact_id="fix", anchors=[anchor], kind="verified_fix")
    fact, doc = _first(ks)
    assert fact["kind"] == "verified_fix" and anchor in fact["anchors"]
    # nothing indexed at all: nothing is known to be gone
    assert effective_standing(fact, doc, _ctx())[0] == "current"
    present = _ctx(records={"s1/p_001": {"kind": "proposal", "superseded_by": None}})
    assert effective_standing(fact, doc, present)[0] == "current"
    gone = _ctx(records={"s1/other": {"kind": "finding"}})
    assert effective_standing(fact, doc, gone) == (
        "stale",
        "record s1/p_001 is no longer in the library",
    )
    superseded = _ctx(records={"s1/p_001": {"kind": "finding", "superseded_by": "s2/f_002"}})
    standing, reason = effective_standing(fact, doc, superseded)
    assert standing == "stale" and "superseded by s2/f_002" in reason


def test_retired_is_sticky_and_written(ks, workspace):
    ks.add_fact(T, "plain", fact_id="plain")
    ks.retire_fact(T, "plain", reason="wrong since June", by="user")
    text = (workspace.knowledge_dir / "DB" / "S" / "T.md").read_text(encoding="utf-8")
    assert "standing: retired" in text and "retired_by: user" in text
    fact, doc = _first(ks)
    assert effective_standing(fact, doc, _ctx()) == ("retired", "wrong since June")


def test_annotate_doc_counts_roles_and_contested(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}]})
    ks.add_fact(T, "AMOUNT is net", fact_id="net")
    ks.confirm_fact(T, "net")
    ks.add_fact(T, "AMOUNT is gross", fact_id="gross", evidence=["q_0001"], supersedes="net")
    ks.add_fact(T, "a hypothesis", fact_id="hyp")
    doc = annotate_doc(ks.read(T), _ctx(trust="data_inferred"))
    by_id = {f["id"]: f for f in doc["facts"]}
    assert by_id["net"]["role"] == "knowledge" and by_id["hyp"]["role"] == "hypothesis"
    assert doc["standing_counts"] == {"current": 3, "unverified": 0, "stale": 0, "retired": 0}
    # one pair, reported once, by its strongest signal
    assert len(doc["contested"]) == 1
    pair = doc["contested"][0]
    assert pair["kind"] == "supersession" and set(pair["facts"]) == {"gross", "net"}


def test_same_question_and_shared_column_pairs_and_compatibility(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}], "open_questions": ["What is the grain?"]})
    ks.answer_open_question(T, "grain", "one row per order")
    ks.add_fact(T, "What is the grain? — one row per line", fact_id="grain2")
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    ks.confirm_fact(T, "amt")
    ks.add_fact(T, "AMOUNT excludes refunds", fact_id="amt2")
    kinds = {c["kind"] for c in annotate_doc(ks.read(T), _ctx())["contested"]}
    assert kinds == {"same_question", "shared_column"}
    ks.resolve_contested(T, "amt", "amt2", by="user", note="both hold")
    kinds = {c["kind"] for c in annotate_doc(ks.read(T), _ctx())["contested"]}
    assert kinds == {"same_question"}


def test_agent_actions_are_windowed(ks):
    ks.add_fact(T, "x", fact_id="x")
    ks.retire_fact(T, "x", by="agent", evidence=["q_0009"])
    ks.set_profile(T, {"open_questions": ["moot?"]})
    ks.dismiss_question(T, "moot", "answered elsewhere", by="agent")
    doc = ks.read(T)
    acts = agent_actions(doc, _ctx(agent_window_days=30))
    assert {a["kind"] for a in acts} == {"retired", "dismissed_question"}
    retired = next(a for a in acts if a["kind"] == "retired")
    assert retired["fact_id"] == "x" and retired["evidence"] == ["q_0009"]
    later = datetime.now(UTC) + timedelta(days=40)
    assert agent_actions(doc, _ctx(now=later, agent_window_days=30)) == []


def test_lint_flags_dangling_lifecycle_references(ks):
    ks.add_fact(T, "x", fact_id="x")
    doc = ks.read(T)
    doc["facts"][0]["supersedes"] = "ghost"
    doc["facts"][0]["compatible_with"] = ["phantom"]
    ks.save(T, doc)
    problems = [w["problem"] for w in ks.lint()["warnings"]]
    assert any("supersedes 'ghost'" in p for p in problems)
    assert any("compatible with 'phantom'" in p for p in problems)
