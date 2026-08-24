from __future__ import annotations

import pytest

from grayson.cache.local import LocalQueryError, query_artifacts
from grayson.cache.store import CacheStore, compare_artifacts, staleness

ROWS = [{"ID": 1, "VAL": "a"}, {"ID": 2, "VAL": "b"}, {"ID": 3, "VAL": None}]


@pytest.fixture
def store(tmp_path):
    s = CacheStore(tmp_path / "data")
    s.save(
        "q_0001",
        ROWS,
        sql="SELECT * FROM DB.S.T1",
        source_tables=["DB.S.T1"],
        truncated=False,
        source_last_altered={"DB.S.T1": "2026-08-20 00:00:00"},
    )
    return s


def test_save_creates_table_and_sidecar(store):
    assert "q_0001" in store.artifact_tables()
    sc = store.get("q_0001")
    assert sc["row_count"] == 3
    assert sc["source_tables"] == ["DB.S.T1"]
    assert sc["query_hash"]
    assert sc["executed_at"]


def test_empty_result_sidecar_only(store):
    store.save("q_0002", [], sql="SELECT 1 WHERE 1=0", source_tables=[], truncated=False)
    assert "q_0002" not in store.artifact_tables()
    assert store.get("q_0002")["row_count"] == 0


def test_invalid_qid_rejected(store):
    with pytest.raises(ValueError):
        store.save("evil; DROP", ROWS, sql="x", source_tables=[], truncated=False)
    assert store.get("../../etc/passwd") is None


def test_find_by_table(store):
    assert store.find(tables=["db.s.t1"])
    assert not store.find(tables=["db.s.other"])


def test_preview(store):
    rows = store.preview("q_0001", limit=2)
    assert len(rows) == 2 and rows[0]["ID"] == 1


def test_awkward_column_names_roundtrip(tmp_path):
    s = CacheStore(tmp_path / "d2")
    s.save(
        "q_0009",
        [{"weird col": 1, 'has"quote': "x", "SELECT": "kw"}],
        sql="x",
        source_tables=[],
        truncated=False,
    )
    rows = s.preview("q_0009")
    assert rows[0]["weird col"] == 1 and rows[0]['has"quote'] == "x"


def test_drop_all_data_keeps_sidecars(store):
    assert store.drop_all_data() == 1
    assert store.artifact_tables() == set()
    assert store.get("q_0001") is not None


def test_staleness():
    sc = {"source_last_altered": {"DB.S.T1": "2026-08-20 00:00:00"}}
    assert staleness(sc, {"DB.S.T1": "2026-08-20 00:00:00"}) == "fresh"
    assert staleness(sc, {"DB.S.T1": "2026-08-21 09:00:00"}) == "stale"
    assert staleness(sc, {}) == "unknown"
    assert staleness({"source_last_altered": {}}, {}) == "unknown"


# -- guarded local analysis ---------------------------------------------


def test_local_query(store):
    cols, rows = query_artifacts(store.data_dir, "SELECT COUNT(*) AS n FROM q_0001")
    assert cols == ["n"] and rows[0][0] == 3


def test_local_query_join_and_cte(store):
    store.save("q_0003", ROWS, sql="x", source_tables=["DB.S.T2"], truncated=False)
    cols, rows = query_artifacts(
        store.data_dir,
        "WITH a AS (SELECT * FROM q_0001) SELECT COUNT(*) AS n FROM a JOIN q_0003 USING (ID)",
    )
    assert rows[0][0] == 3


def test_local_query_max_rows(store):
    cols, rows = query_artifacts(store.data_dir, "SELECT * FROM q_0001", max_rows=2)
    assert len(rows) == 2


def test_compare_uses_real_table_count_not_sidecar(store, tmp_path):
    import json

    store.save("q_0002", [{"ID": 1}], sql="a", source_tables=["DB.S.T1"], truncated=False)
    # tamper the sidecar to claim 0 rows while the table still holds 1
    sc_path = store.sidecar_path("q_0002")
    sc = json.loads(sc_path.read_text())
    sc["row_count"] = 0
    sc_path.write_text(json.dumps(sc))
    result = compare_artifacts(store, "q_0001", "q_0002")
    # real table count (1) is used, so after is NOT reported empty
    assert result["after"]["row_count"] == 1
    assert result["after_empty"] is False


def test_compare_truncated_never_identical(tmp_path):
    s = CacheStore(tmp_path / "d3")
    rows = [{"ID": i} for i in range(3)]
    s.save("q_0001", rows, sql="a", source_tables=[], truncated=True)
    s.save("q_0002", rows, sql="b", source_tables=[], truncated=True)
    result = compare_artifacts(s, "q_0001", "q_0002")
    assert result["identical"] is False
    assert result["counts_truncated"] is True


BAD_LOCAL = [
    "DROP TABLE q_0001",
    "CREATE TABLE x AS SELECT 1",
    "DELETE FROM q_0001",
    "UPDATE q_0001 SET ID = 0",
    "INSERT INTO q_0001 VALUES (9, 'z')",
    "SELECT * FROM unknown_table",
    "SELECT * FROM other.db.table",
    "SELECT * FROM pragma_table_info('q_0001')",
    "ATTACH DATABASE 'other.db' AS x",
    "PRAGMA database_list",
    "SELECT 1; SELECT 2",
    "SELECT * FROM sqlite_master",
]


@pytest.mark.parametrize("sql", BAD_LOCAL)
def test_local_query_rejects(store, sql):
    with pytest.raises(LocalQueryError):
        query_artifacts(store.data_dir, sql)
