"""Partial descriptor updates must not erase the rest of a table's schema."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from conftest import call_mcp
from grayson.cli import app
from grayson.knowledge import KnowledgeStore
from grayson.mcp.server import build_server

TABLE = "DB.S.ORDERS"
COLUMNS = [
    {
        "name": "ORDER_ID",
        "type": "NUMBER",
        "description": "Unique order identifier",
        "nullable": False,
        "tags": ["primary_key"],
    },
    {
        "name": "AMOUNT",
        "type": "NUMBER(18,2)",
        "description": "Gross amount including refunds",
        "nullable": True,
    },
]


@pytest.fixture
def store(workspace):
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile(TABLE, {"columns": deepcopy(COLUMNS), "grain": "One row per order"})
    return store


@pytest.mark.parametrize("entrypoint", ["store", "mcp", "cli"])
@pytest.mark.parametrize("case", ["partial", "names_only", "empty", "other_field"])
def test_partial_column_updates_preserve_saved_knowledge(workspace, store, entrypoint, case):
    profile = {
        "partial": {"columns": [{"name": "order_id", "description": "Warehouse order key"}]},
        "names_only": {"columns": [{"name": "AMOUNT"}, {"name": "ORDER_ID"}]},
        "empty": {"columns": []},
        "other_field": {"grain": "One row per completed order"},
    }[case]
    original_profile = deepcopy(profile)
    if entrypoint == "store":
        out = store.set_profile(TABLE, profile)
    elif entrypoint == "mcp":
        out = call_mcp(
            build_server(workspace), "knowledge_set", {"table": TABLE, "profile": profile}
        )
    else:
        result = CliRunner().invoke(app, ["knowledge", "set", TABLE, "--json", json.dumps(profile)])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
    expected = deepcopy(COLUMNS)
    if case == "partial":
        expected[0]["description"] = "Warehouse order key"
    assert store.read(TABLE)["columns"] == expected
    assert out["columns"] == expected
    assert out["warnings"] == []
    assert out["grain"] == profile.get("grain", "One row per order")
    assert profile == original_profile


def test_append_columns_and_update_only_explicit_attributes(store):
    store.set_profile(
        TABLE,
        {
            "columns": [
                {"name": "STATUS", "description": "Fulfilment state"},
                {"name": "AMOUNT", "description": "", "nullable": False},
                {"name": "CREATED_AT", "type": "TIMESTAMP_NTZ"},
            ]
        },
    )
    expected = deepcopy(COLUMNS)
    expected[1].update(description="", nullable=False)
    expected.extend(
        [
            {"name": "STATUS", "description": "Fulfilment state"},
            {"name": "CREATED_AT", "type": "TIMESTAMP_NTZ"},
        ]
    )
    assert store.read(TABLE)["columns"] == expected


@pytest.mark.parametrize(
    "columns",
    [
        None,
        {},
        ["ORDER_ID"],
        [{}],
        [{"name": ""}],
        [{"name": "  "}],
        [{"name": 42}],
        [{"name": "ORDER_ID"}, {"name": "ORDER_ID"}],
        [{"name": "order_id"}, {"name": "ORDER_ID"}],
        [{"name": "NEW"}, {"name": "NEW"}],
    ],
)
def test_invalid_column_updates_leave_entire_file_unchanged(workspace, store, columns):
    path = workspace.knowledge_dir / "DB" / "S" / "ORDERS.md"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="column"):
        store.set_profile(TABLE, {"grain": "Must not be written", "columns": columns})
    assert path.read_bytes() == before


def test_case_distinct_columns_match_exact_names_and_reject_ambiguity(workspace, store):
    store.set_profile(
        TABLE,
        {
            "columns": [
                {"name": "label", "description": "Lowercase name"},
                {"name": "LABEL", "description": "Uppercase name"},
            ]
        },
    )
    store.set_profile(TABLE, {"columns": [{"name": "label", "type": "VARCHAR"}]})
    assert store.read(TABLE)["columns"][2:] == [
        {"name": "label", "description": "Lowercase name", "type": "VARCHAR"},
        {"name": "LABEL", "description": "Uppercase name"},
    ]
    path = workspace.knowledge_dir / "DB" / "S" / "ORDERS.md"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ambiguous column name"):
        store.set_profile(TABLE, {"columns": [{"name": "Label", "description": "Ambiguous"}]})
    assert path.read_bytes() == before


def test_mcp_rejects_duplicate_targets_without_overwriting(workspace, store):
    out = call_mcp(
        build_server(workspace),
        "knowledge_set",
        {
            "table": TABLE,
            "profile": {
                "columns": [{"name": "ORDER_ID", "type": "VARCHAR"}, {"name": "order_id"}],
            },
        },
    )
    assert "duplicate column update" in out["error"]
    assert store.read(TABLE)["columns"] == COLUMNS


@pytest.mark.parametrize("entrypoint", ["store", "mcp", "cli"])
@pytest.mark.parametrize("include_existing", [False, True])
def test_add_case_distinct_column_to_existing_schema(
    workspace, store, entrypoint, include_existing
):
    lower = {"name": "label", "description": "Lowercase name", "type": "VARCHAR"}
    upper = {"name": "LABEL", "description": "Uppercase name"}
    store.set_profile(TABLE, {"columns": [lower]})
    profile = {"columns": ([{"name": "label"}] if include_existing else []) + [upper]}
    if entrypoint == "store":
        out = store.set_profile(TABLE, profile, exact_column_names=True)
    elif entrypoint == "mcp":
        out = call_mcp(
            build_server(workspace),
            "knowledge_set",
            {
                "table": TABLE,
                "profile": profile,
                "exact_column_names": True,
            },
        )
    else:
        result = CliRunner().invoke(
            app,
            [
                "knowledge",
                "set",
                TABLE,
                "--json",
                json.dumps(profile),
                "--exact-column-names",
            ],
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
    assert out["columns"] == COLUMNS + [lower, upper]
    assert store.read(TABLE)["columns"] == COLUMNS + [lower, upper]
