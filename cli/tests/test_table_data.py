import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli import flow

TABLE = {"id": "t1", "name": "hotel_metadata", "context": "example.configurations"}
COLUMNS = [
    {"name": "id", "type": "uuid", "primary": True, "default": "gen_random_uuid()"},
    {"name": "createdAt", "type": "timestamptz", "default": "now()"},
    {"name": "updatedAt", "type": "timestamptz", "default": "now()"},
    {"name": "hotel_id", "type": "text", "required": True, "unique": True},
    {"name": "name", "type": "text", "required": True},
    {"name": "rooms", "type": "integer"},
    {"name": "rate", "type": "numeric"},
    {"name": "stars", "type": "integer", "default": 3},
    {"name": "enabled", "type": "boolean", "required": True, "default": "true"},
    {"name": "opened_at", "type": "timestamptz"},
    {"name": "settings", "type": "jsonb"},
    {"name": "note", "type": "text", "default": "none"},
]
COLUMN_MAP = {column["name"]: column for column in COLUMNS}
OLD = "2020-01-01T00:00:00.000Z"
FILE_TIME = datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()
NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
STAMP = "2026-11-25T12:00:00.000Z"


def stored(row_id, hotel_id, updated_at=OLD, **columns):
    return {
        "id": row_id,
        "createdAt": OLD,
        "updatedAt": updated_at,
        "hotel_id": hotel_id,
        "name": f"Hotel {hotel_id}",
        "rooms": None,
        "rate": None,
        "stars": 3,
        "enabled": True,
        "opened_at": None,
        "settings": None,
        "note": "none",
        **columns,
    }


class TabiClient:
    """Flow's API client over one in-memory table.

    Like Tabi, it binds equal values to one query parameter, refusing a request
    that gives one value two column types (42P08), and writes NULL into the
    columns a row of a multi-row insert leaves out.
    """

    def __init__(self, rows=(), table=True, columns=COLUMNS):
        self.rows = [dict(row) for row in rows]
        self.table = table
        self.columns = columns
        self.calls = []
        self.next_id = 100
        self.fail_on = None

    def bound_conflict(self, cells):
        types = {}
        storage_of = {column["name"]: column["type"] for column in self.columns}
        for column, value in cells:
            storage = flow.table_storage_type(storage_of.get(column))
            parameter = flow.table_bound_parameter(storage, value)
            if types.setdefault(parameter, storage) != storage:
                return True
        return False

    def request(self, method, path, *, payload=None, read_only=False, **_):
        self.calls.append((method, path, payload, read_only))
        if (method, path) == ("GET", "/tables"):
            return {
                "results": [TABLE] if self.table else [],
                "pagination": {"pages": 1},
            }
        if (method, path) == ("GET", "/tables/t1"):
            return {**TABLE, "columns": self.columns}
        if path == "/tables/t1/select":
            rows = sorted(self.rows, key=lambda row: row["id"])
            offset = payload["offset"]
            return {"results": rows[offset : offset + payload["limit"]]}
        if path == self.fail_on:
            raise flow.ApiError(method, path, 503)
        if path == "/tables/t1/insert":
            names = sorted({column for row in payload["data"] for column in row})
            if self.bound_conflict(
                (column, row.get(column)) for row in payload["data"] for column in names
            ):
                raise flow.ApiError(method, path, 500)
            created = []
            for row in payload["data"]:
                self.next_id += 1
                created.append(
                    {
                        "id": f"r{self.next_id}",
                        "createdAt": OLD,
                        "updatedAt": OLD,
                        **{column: row.get(column) for column in names},
                    }
                )
            self.rows.extend(created)
            return {"results": created}
        if path == "/tables/t1/update":
            row_id = payload["where"]["id"]["equals"]
            if self.bound_conflict([("id", row_id), *payload["data"].items()]):
                raise flow.ApiError(method, path, 500)
            changed = [row for row in self.rows if row["id"] == row_id]
            for row in changed:
                row.update(payload["data"])
            return {"results": changed}
        if path == "/tables/t1/delete":
            ids = set(payload["where"]["id"]["in"])
            before = len(self.rows)
            self.rows = [row for row in self.rows if row["id"] not in ids]
            return {"deleted": before - len(self.rows)}
        raise AssertionError(f"Unexpected {method} {path}")

    def writes(self, kind):
        return [call[2] for call in self.calls if call[1] == f"/tables/t1/{kind}"]

    def row(self, hotel_id):
        return next(row for row in self.rows if row.get("hotel_id") == hotel_id)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    monkeypatch.delenv("POLY_DEPLOY_TABLE_DATA_ENV", raising=False)
    monkeypatch.delenv("POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS", raising=False)
    monkeypatch.delenv("POLY_DEPLOY_USE_MTIME_GUARD", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path.resolve()


def args(**changes):
    return replace(flow.CliArgs(False, False, False, False, (), None, False), **changes)


def env_file(repo, environment=None):
    """The selected env file: only it may name the table data folder."""
    path = repo / "workflows/selected.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "" if environment is None else f"POLY_DEPLOY_TABLE_DATA_ENV={environment}\n"
    )
    return flow.EnvLoadResult(
        display_path="workflows/selected.env",
        resolved_path=path,
        exists=True,
        from_envpath=True,
    )


def config(repo, environment=None):
    return flow.build_config(repo, args(), env_file(repo, environment))


def write(repo, relative, content):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def data_file(repo, rows, folder="", filename="hotel_metadata", **document):
    folder = f"{folder}/" if folder else ""
    return write(
        repo,
        f"workflows/src/hotels/artefacts/tabi/data/{folder}{filename}.json",
        {
            "context": "example.configurations",
            "name": "hotel_metadata",
            "key": "hotel_id",
            "rows": rows,
            **document,
        },
    )


def table_definition(repo):
    return write(
        repo,
        "workflows/src/hotels/artefacts/tabi/hotel_metadata.json",
        {"name": TABLE["name"], "context": TABLE["context"], "columns": COLUMNS},
    )


def sync(repo, client, environment=None, **cli_changes):
    summary = flow.Summary()
    logger = Mock()
    flow.sync_table_data(
        client=client,
        config=config(repo, environment),
        cli_args=args(**cli_changes),
        logger=logger,
        summary=summary,
        file_timestamp=lambda path: FILE_TIME,
        now=NOW,
    )
    return summary.table_data, logger


def logged(logger):
    return "\n".join(str(call.args[0]) for call in logger.method_calls if call.args)


def validate(repo, environment=None, **cli_changes):
    return flow.validate_table_data(config(repo, environment), args(**cli_changes))


# --- discovery and settings ---


def test_data_files_are_a_resource_type_after_tables():
    assert "table-data" in flow.DEFAULT_ENABLED_TYPES
    order = flow.DEFAULT_ENABLED_TYPES
    assert order.index("table-data") == order.index("tables") + 1
    assert flow.parse_type_selection(("table-data",)) == (("table-data",), None)


def test_files_sync_from_data_and_environment_folders(repo):
    table_definition(repo)
    data_file(repo, [])
    data_file(repo, [], folder="dev")
    data_file(repo, [], folder="dev/old")
    data_file(repo, [], folder="Prod")
    data_file(repo, [], filename="rows.test")
    write(repo, "workflows/src/hotels/artefacts/tabi/data/broken.json", '{"name": ')
    write(
        repo,
        "workflows/src/hotels/artefacts/tabi/data/bom.json",
        "﻿" + json.dumps({"name": "t"}),
    )
    found = sorted(
        (
            str(file.path.relative_to(repo / "workflows/src/hotels/artefacts/tabi")),
            file.environment,
            file.error or "ok",
        )
        for file in flow.find_table_data_files(config(repo))
    )
    assert found == [
        (
            "data/Prod/hotel_metadata.json",
            "Prod",
            "folder 'Prod' is not an environment name: use lowercase letters, "
            "digits, '_' or '-'",
        ),
        ("data/bom.json", "", "ok"),
        ("data/broken.json", "", "not valid JSON"),
        ("data/dev/hotel_metadata.json", "dev", "ok"),
        (
            "data/dev/old/hotel_metadata.json",
            "dev",
            "nested too deep: keep table data files in data/ or data/<environment>/",
        ),
        ("data/hotel_metadata.json", "", "ok"),
    ]
    # Table definitions never pick up data files.
    assert flow.discover_paths(repo, config(repo).table_globs, (), ()) == [
        repo / "workflows/src/hotels/artefacts/tabi/hotel_metadata.json"
    ]


def test_data_files_are_plain_json(repo, monkeypatch):
    monkeypatch.setenv("HOTEL_NAME", "Substituted")
    path = data_file(repo, [{"hotel_id": "A", "name": "{{HOTEL_NAME}}"}])
    assert flow.read_table_data_file(path).data["rows"][0]["name"] == "{{HOTEL_NAME}}"
    path.write_text('{"name": "t", // comment\n "key": "k", "rows": []}')
    assert flow.read_table_data_file(path).error == "not valid JSON"
    path.write_text('{"name": "t", "key": "k", "rows": [{"k": NaN}]}')
    assert flow.read_table_data_file(path).error == "not valid JSON"


@pytest.mark.parametrize("environment", ["Prod", "../prod", "a b", "-x"])
def test_an_invalid_environment_name_is_rejected(repo, environment):
    with pytest.raises(flow.DeployError, match="POLY_DEPLOY_TABLE_DATA_ENV"):
        config(repo, environment)


def test_the_prune_flag_is_parsed_and_kept_out_of_reports():
    parsed = flow.parse_args(["push", "--prune-table-data", "--dry-run"])
    assert parsed.prune_table_data is True
    assert flow.parse_args(["push"]).prune_table_data is False
    with pytest.raises(flow.DeployError, match="table rows"):
        flow.parse_args(["push-dynamic", "--why", "a.b", "--prune-table-data"])


# --- validation ---


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "must be a JSON object"),
        ({"name": ""}, "name is required"),
        ({"context": 1}, "context must be"),
        ({"description": 1}, "description must be text"),
        ({"key": []}, "key is required"),
        ({"key": ["hotel_id", "hotel_id"]}, "key is required"),
        ({"key": "id"}, "key cannot use id"),
        ({"extra": True}, "unknown key 'extra'"),
        ({"rows": {}}, "rows is required"),
        ({"rows": ["x"]}, "row 1 must be an object"),
        ({"rows": [{"hotel_id": "A", "id": "x"}]}, "row 1 sets 'id'"),
        ({"rows": [{"name": "A"}]}, "row 1 needs a text or number value"),
        ({"rows": [{"hotel_id": " "}]}, "row 1 needs a text or number value"),
        ({"rows": [{"hotel_id": True}]}, "row 1 needs a text or number value"),
        (
            {"rows": [{"hotel_id": "A", "name": "a"}, {"hotel_id": "A", "name": "b"}]},
            "row 2 repeats the key of row 1",
        ),
    ],
)
def test_invalid_files_name_the_problem_without_values(document, message):
    base = {
        "context": "example.configurations",
        "name": "hotel_metadata",
        "key": "hotel_id",
        "rows": [{"hotel_id": "A", "name": "A"}],
    }
    data = document if isinstance(document, list) else {**base, **document}
    with pytest.raises(flow.TableDataError, match=message):
        flow.parse_table_data_document(data)


def test_rows_are_checked_against_the_local_table_definition(repo):
    table_definition(repo)
    data_file(repo, [{"hotel_id": "A", "name": "A", "nmae": "typo"}])
    data_file(repo, [{"hotel_id": "B", "enabled": None}], filename="nulls")
    found = validate(repo).problems
    assert any(
        problem.endswith(
            "data/hotel_metadata.json: row 1 sets 'nmae', which is not a column of "
            "the table"
        )
        for problem in found
    )
    assert any(
        problem.endswith("nulls.json: row 1 sets required column 'enabled' to null")
        for problem in found
    )
    assert all("typo" not in problem for problem in found)


def test_a_row_may_list_only_the_columns_it_changes(repo):
    # Required columns matter only when the row is new.
    table_definition(repo)
    data_file(repo, [{"hotel_id": "A", "enabled": False, "rooms": None}])
    assert validate(repo).problems == []


def test_local_validation_covers_every_environment_and_names_the_problem(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    data_file(repo, [{"name": "missing key"}], folder="prod")
    cfg = replace(config(repo, "dev"), enabled_types=("table-data",))
    with pytest.raises(flow.DeployError) as raised:
        flow.validate_local_artifacts(cfg, args(), Mock())
    message = str(raised.value)
    assert "data/prod/hotel_metadata.json" in message
    assert "row 1 needs a text or number value for key column 'hotel_id'" in message
    assert "missing key" not in message


def test_validation_checks_the_selected_tables_and_every_unreadable_file(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    data_file(repo, [{"hotel_id": "B", "name": "B"}], folder="prod")
    data_file(repo, [{"hotel_id": "B", "name": "B"}], folder="prod", filename="more")
    broken = write(repo, "workflows/src/other/artefacts/tabi/data/x.json", "{")
    result = validate(repo)
    assert len(result.problems) == 2
    assert result.problems[0].endswith(
        "other/artefacts/tabi/data/x.json: not valid JSON"
    )
    assert "prod/more.json: row B is also in" in result.problems[1]
    assert result.invalid_paths == {
        broken.resolve(),
        (repo / "workflows/src/hotels/artefacts/tabi/data/prod/more.json").resolve(),
    }
    scoped = validate(repo, path_filters=(Path("workflows/src/other"),))
    assert len(scoped.problems) == 1


def test_data_files_must_belong_to_the_namespace(repo):
    data_file(repo, [], context="other.configurations")
    cfg = replace(config(repo), enabled_types=("table-data",))
    with pytest.raises(flow.DeployError):
        flow.validate_local_namespace(cfg, args(), flow.Logger(False))


# --- merging files ---


def test_environment_rows_overlay_shared_rows_by_key(repo):
    shared = data_file(
        repo,
        [
            {"hotel_id": "A", "name": "Shared A", "enabled": False},
            {"hotel_id": "B", "name": "Shared B"},
        ],
    )
    dev = data_file(
        repo,
        [{"hotel_id": "A", "enabled": True}, {"hotel_id": "D", "name": "Dev only"}],
        folder="dev",
    )
    data_file(repo, [{"hotel_id": "P", "name": "Prod only"}], folder="prod")
    [data_set] = flow.collect_table_data(config(repo, "dev"), args())
    assert data_set.key == ("hotel_id",)
    assert [(row.values, row.paths) for row in data_set.rows] == [
        (
            {"hotel_id": "A", "name": "Shared A", "enabled": True},
            (shared.resolve(), dev.resolve()),
        ),
        ({"hotel_id": "B", "name": "Shared B"}, (shared.resolve(),)),
        ({"hotel_id": "D", "name": "Dev only"}, (dev.resolve(),)),
    ]


def test_a_scoped_run_still_uses_every_file_of_the_table(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    dev = data_file(repo, [{"hotel_id": "D", "name": "D"}], folder="dev")
    [data_set] = flow.collect_table_data(
        config(repo, "dev"), args(path_filters=(dev.relative_to(repo),))
    )
    assert [row.values["hotel_id"] for row in data_set.rows] == ["A", "D"]
    other = data_file(repo, [], filename="other", name="other")
    assert [
        item.name
        for item in flow.collect_table_data(
            config(repo, "dev"), args(path_filters=(other.relative_to(repo),))
        )
    ] == ["other"]


def test_files_of_one_table_must_share_a_key_and_list_a_row_once(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    data_file(repo, [{"name": "D"}], folder="dev", key="name")
    with pytest.raises(flow.TableDataError, match="dev/hotel_metadata.json: every"):
        flow.collect_table_data(config(repo, "dev"), args())
    data_file(repo, [{"hotel_id": "A", "name": "again"}], filename="again")
    with pytest.raises(flow.TableDataError, match="row A is also in"):
        flow.collect_table_data(config(repo), args())


def test_excluded_contexts_are_skipped(repo, monkeypatch):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    monkeypatch.setenv("POLY_DEPLOY_EXCLUDE_ORPHAN_CONTEXTS", "example.configurations")
    assert flow.collect_table_data(config(repo), args()) == []


# --- comparing values ---


@pytest.mark.parametrize(
    "column_type, local, remote, equal",
    [
        ("numeric", 10.5, "10.50", True),
        ("number", "7", 7, True),
        ("integer", 3, 3, True),
        ("bigint", 3, "3", True),
        ("integer", 3, 4, False),
        ("integer", True, 1, False),
        ("timestamptz", "2026-11-01T10:00:00+01:00", "2026-11-01T09:00:00.000Z", True),
        ("timestamptz", "2026-11-01T10:00:00Z", "2026-11-01T10:00:01.000Z", False),
        ("date", "2026-11-01", "2026-11-01T00:00:00.000Z", True),
        ("timestamp", "2026-11-01 10:00:00", "2026-11-01T10:00:00.000Z", True),
        ("timestamptz", "soon", "soon", True),
        ("timestamptz", "soon", "2026-11-01T10:00:00.000Z", False),
        ("uuid", "ABC-1", "abc-1", True),
        ("jsonb", {"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}, True),
        ("jsonb", {"a": True}, {"a": 1}, False),
        ("jsonb", {"a": 1}, '{"a": 1}', True),
        ("object", {"a": 1}, '{"a": 1}', True),
        ("text", "x", "x", True),
        ("text", "x", "X", False),
        ("boolean", True, True, True),
        ("text", None, None, True),
        ("text", "", None, False),
    ],
)
def test_values_compare_as_the_column_stores_them(column_type, local, remote, equal):
    assert flow.table_value_equal(column_type, local, remote) is equal


# --- planning writes ---


def plan_inserts(rows, columns=COLUMN_MAP):
    return flow.plan_table_data_inserts(rows, ("hotel_id",), columns)


def test_inserts_group_rows_that_set_the_same_columns():
    requests = plan_inserts(
        [
            {"hotel_id": "A", "name": "A", "rooms": None},
            {"hotel_id": "B", "name": "B", "rooms": 3},
            {"hotel_id": "C", "name": "C"},
            {"hotel_id": "D", "name": "D", "note": None},
        ]
    )
    assert [(request.rows, request.follow_ups) for request in requests] == [
        (({"hotel_id": "A", "name": "A"}, {"hotel_id": "C", "name": "C"}), ()),
        (({"hotel_id": "B", "name": "B", "rooms": 3},), ()),
        # note has a default, so its null must be written.
        (({"hotel_id": "D", "name": "D", "note": None},), ()),
    ]


def test_inserts_stay_within_the_row_and_byte_limits():
    def sizes(rows):
        return [len(request.rows) for request in plan_inserts(rows)]

    assert sizes([{"hotel_id": f"H{i}", "name": "n"} for i in range(250)]) == [
        100,
        100,
        50,
    ]
    big = "x" * (300 * 1024)
    assert sizes([{"hotel_id": f"H{i}", "name": big} for i in range(5)]) == [2, 2, 1]
    assert sizes([{"hotel_id": "A", "name": "x" * (2 * 1024 * 1024)}]) == [1]


def test_one_value_never_meets_columns_of_two_types_in_one_request():
    # rate 2 (numeric) would share a parameter with rooms 2 (integer).
    assert [
        [row["hotel_id"] for row in request.rows]
        for request in plan_inserts(
            [
                {"hotel_id": "A", "name": "A", "rooms": 2, "rate": 1.5},
                {"hotel_id": "B", "name": "B", "rooms": 3, "rate": 2},
            ]
        )
    ] == [["A"], ["B"]]
    # Within one row: insert what fits, then update the rest.
    [request] = plan_inserts(
        [
            {
                "hotel_id": "A",
                "name": "A",
                "rooms": 2,
                "rate": 2,
                "stars": None,
                "note": None,
            }
        ]
    )
    assert request.rows == ({"hotel_id": "A", "name": "A", "rooms": 2, "stars": None},)
    assert request.follow_ups == ({"rate": 2, "note": None},)
    strict = {
        **COLUMN_MAP,
        "code": {"name": "code", "type": "integer", "required": True},
        "floors": {"name": "floors", "type": "numeric", "required": True},
    }
    with pytest.raises(flow.TableDataError, match="required columns 'code' and"):
        plan_inserts([{"hotel_id": "A", "name": "A", "code": 7, "floors": 7}], strict)
    # To Tabi, true and 1 are different values.
    [single] = plan_inserts(
        [{"hotel_id": "A", "name": "A", "rooms": 1, "enabled": True}]
    )
    assert single.follow_ups == ()
    assert flow.plan_table_data_updates(
        {"rooms": None, "settings": None, "opened_at": None, "rate": 2},
        COLUMN_MAP,
        "r1",
    ) == ({"rooms": None, "settings": None, "rate": 2}, {"opened_at": None})


# --- sync ---


def test_new_rows_are_inserted_and_changed_columns_updated(repo):
    path = data_file(
        repo,
        [
            {"hotel_id": "A", "name": "Hotel A", "rate": 10.5},
            {"hotel_id": "B", "name": "Renamed B", "settings": {"floor": 2}},
            {"hotel_id": "C", "name": "Hotel C", "rooms": 12, "opened_at": None},
            {"hotel_id": "D", "name": "Hotel D"},
        ],
    )
    client = TabiClient(
        [
            stored("r1", "A", rate="10.50", settings={"kept": True}),
            stored("r2", "B", settings={"floor": 1}),
            stored("r3", "X"),
        ]
    )
    stats, logger = sync(repo, client)
    assert client.writes("insert") == [
        {"data": [{"hotel_id": "C", "name": "Hotel C", "rooms": 12}]},
        {"data": [{"hotel_id": "D", "name": "Hotel D"}]},
    ]
    assert client.writes("update") == [
        {
            "where": {"id": {"equals": "r2"}},
            "data": {
                "name": "Renamed B",
                "settings": {"floor": 2},
                "updatedAt": STAMP,
            },
        }
    ]
    assert client.writes("delete") == []
    # Columns the file omits keep their values.
    assert client.row("A")["settings"] == {"kept": True}
    assert (stats.created, stats.updated, stats.skipped, stats.deleted) == (2, 1, 1, 0)
    text = logged(logger)
    assert "insert: C, D" in text and "update B: name, settings" in text
    assert "Rows no file lists, kept: X (--prune-table-data deletes them)" in text
    assert "2 inserted, 1 updated, 0 deleted, 1 unchanged" in text
    assert path.exists()


def test_a_second_sync_changes_nothing(repo):
    data_file(
        repo, [{"hotel_id": "A", "name": "A", "opened_at": "2026-01-01T00:00:00Z"}]
    )
    client = TabiClient()
    sync(repo, client)
    client.rows[0]["opened_at"] = "2026-01-01T00:00:00.000Z"
    client.calls.clear()
    stats, _ = sync(repo, client)
    assert (stats.created, stats.updated, stats.skipped) == (0, 0, 1)
    assert client.writes("insert") == client.writes("update") == []


def test_a_dry_run_reads_without_writing_or_otp(repo):
    data_file(
        repo, [{"hotel_id": "A", "name": "New name"}, {"hotel_id": "B", "name": "B"}]
    )
    client = TabiClient([stored("r1", "A"), stored("r9", "Z")])
    stats, logger = sync(repo, client, dry_run=True, prune_table_data=True)
    assert [call for call in client.calls if call[0] == "POST" and not call[3]] == []
    assert (stats.created, stats.updated, stats.deleted) == (1, 1, 1)
    text = logged(logger)
    assert "Would insert: B" in text and "Would update A: name" in text
    assert "Would delete rows no file lists: Z" in text
    assert "Plan: 1 inserted, 1 updated, 1 deleted, 0 unchanged" in text


def test_extra_rows_are_deleted_only_with_the_flag(repo):
    data_file(repo, [{"hotel_id": "A", "name": "Hotel A"}])
    client = TabiClient([stored("r1", "A"), stored("r2", "X"), stored("r3", "Y")])
    stats, _ = sync(repo, client)
    assert client.writes("delete") == [] and stats.deleted == 0
    stats, _ = sync(repo, client, prune_table_data=True)
    assert client.writes("delete") == [{"where": {"id": {"in": ["r2", "r3"]}}}]
    assert [row["hotel_id"] for row in client.rows] == ["A"] and stats.deleted == 2


def test_rows_changed_after_the_file_are_blocked_unless_forced(repo, monkeypatch):
    data_file(repo, [{"hotel_id": "A", "name": "From file"}])
    client = TabiClient(
        [stored("r1", "A", updated_at="2026-11-21T00:00:00Z", name="From app")]
    )
    stats, logger = sync(repo, client)
    assert client.writes("update") == [] and stats.blocked == 1
    assert (
        "Not updated: A (changed in the table at 2026-11-21T00:00:00Z; its file last "
        "changed at 2026-11-20T00:00:00Z); --force-table-data overwrites it"
    ) in logged(logger)
    stats, _ = sync(repo, client, force=True)
    assert stats.updated == 0 and stats.blocked == 1  # --force leaves rows alone
    stats, _ = sync(repo, client, force_table_data=True)
    assert stats.updated == 1 and client.row("A")["name"] == "From file"
    # The update stamped updatedAt; with the guard off an older file still wins.
    client.row("A")["name"] = "From app"
    monkeypatch.setenv("POLY_DEPLOY_USE_MTIME_GUARD", "false")
    stats, _ = sync(repo, client)
    assert stats.updated == 1 and stats.blocked == 0


def test_writes_are_batched(repo, monkeypatch):
    monkeypatch.setattr(flow, "TABLE_DATA_WRITE_BATCH", 2)
    monkeypatch.setattr(flow, "TABLE_DATA_PAGE_SIZE", 2)
    data_file(repo, [{"hotel_id": f"N{index}", "name": "n"} for index in range(5)])
    client = TabiClient([stored(f"r{index}", f"X{index}") for index in range(5)])
    sync(repo, client, prune_table_data=True)
    assert [len(write["data"]) for write in client.writes("insert")] == [2, 2, 1]
    assert [len(write["where"]["id"]["in"]) for write in client.writes("delete")] == [
        2,
        2,
        1,
    ]
    assert [
        call[2]["offset"] for call in client.calls if call[1].endswith("select")
    ] == [0, 2, 4]


def test_a_value_meeting_columns_of_two_types_takes_requests_tabi_accepts(repo):
    data_file(
        repo,
        [
            {"hotel_id": "A", "name": "A", "rooms": 2, "rate": 2},
            {"hotel_id": "B", "rooms": None, "opened_at": None},
        ],
    )
    client = TabiClient([stored("r1", "B", rooms=5, opened_at="2026-01-01T00:00:00Z")])
    stats, logger = sync(repo, client)
    assert stats.failed == 0, logged(logger)
    assert (stats.created, stats.updated) == (1, 1)
    assert (client.row("A")["rooms"], client.row("A")["rate"]) == (2, 2)
    assert (client.row("B")["rooms"], client.row("B")["opened_at"]) == (None, None)
    assert len(client.writes("update")) == 3
    assert "2 row(s) take several requests" in logged(logger)


def test_numeric_keys_match_text_and_number_forms(repo):
    columns = [*COLUMNS[:3], {"name": "code", "type": "integer", "unique": True}]
    data_file(repo, [{"code": 101}], key="code")
    client = TabiClient(
        [{"id": "r1", "createdAt": OLD, "updatedAt": OLD, "code": "101"}],
        columns=columns,
    )
    stats, _ = sync(repo, client)
    assert (stats.created, stats.skipped) == (0, 1)


def test_partial_rows_update_existing_rows_but_new_rows_need_required_columns(repo):
    data_file(repo, [{"hotel_id": "A", "enabled": False}])
    client = TabiClient([stored("r1", "A")])
    stats, _ = sync(repo, client)
    assert stats.updated == 1 and client.row("A")["name"] == "Hotel A"
    data_file(repo, [{"hotel_id": "A", "enabled": False}, {"hotel_id": "N"}])
    stats, logger = sync(repo, client)
    assert stats.failed == 1
    assert client.writes("insert") == []
    assert "new row N needs a value for required column 'name'" in logged(logger)


def test_a_missing_table_is_planned_in_a_dry_run_and_fails_otherwise(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    stats, logger = sync(repo, TabiClient(table=False), dry_run=True)
    assert stats.created == 1
    assert "once the table exists" in logged(logger)
    stats, logger = sync(repo, TabiClient(table=False))
    assert stats.failed == 1
    assert "table not found; deploy its definition first" in logged(logger)


@pytest.mark.parametrize(
    "rows, remote, message",
    [
        (
            [{"hotel_id": "A", "nmae": "x"}],
            [],
            "row A sets 'nmae', which is not a column of the deployed table",
        ),
        (
            [{"hotel_id": "A", "name": "A"}],
            [stored("r1", "A"), stored("r2", "A")],
            "several table rows have key A",
        ),
    ],
)
def test_problems_with_the_table_fail_that_table_only(repo, rows, remote, message):
    data_file(repo, rows)
    data_file(repo, [], filename="other", name="other")
    stats, logger = sync(repo, TabiClient(remote))
    # Each table fails on its own: the other one is not deployed yet.
    assert stats.failed == 2
    assert message in logged(logger)
    assert "table not found; deploy its definition first" in logged(logger)


def test_a_table_with_a_broken_data_file_fails_alone(repo):
    data_file(repo, [{"hotel_id": "A", "name": "A"}])
    write(
        repo,
        "workflows/src/brands/artefacts/tabi/data/brands.json",
        {"context": "example.brands", "name": "brands", "key": "code", "rows": {}},
    )
    client = TabiClient()
    stats, logger = sync(repo, client)
    assert (stats.failed, stats.created) == (1, 1)
    assert "brands.json: rows is required: a list of row objects" in logged(logger)
    assert [row["hotel_id"] for row in client.rows] == ["A"]


def test_skipped_invalid_files_skip_the_tables_that_read_them(repo):
    shared = data_file(repo, [{"hotel_id": "A", "name": "A"}])
    prod = data_file(repo, [], folder="prod")
    client = TabiClient()
    stats, logger = sync(repo, client, invalid_paths=(shared.resolve(),))
    assert stats.failed == 1
    assert "skipped: its data files failed local validation" in logged(logger)
    # A problem in another environment's folder does not stop this one.
    stats, _ = sync(
        repo, client, environment="dev", dry_run=True, invalid_paths=(prod.resolve(),)
    )
    assert (stats.failed, stats.created) == (0, 1)


def test_a_failed_write_reports_what_was_already_written(repo):
    data_file(
        repo, [{"hotel_id": "N", "name": "New"}, {"hotel_id": "A", "name": "Renamed"}]
    )
    client = TabiClient([stored("r1", "A")])
    client.fail_on = "/tables/t1/update"
    stats, logger = sync(repo, client)
    assert (stats.created, stats.updated, stats.failed) == (1, 0, 1)
    assert (
        "POST /tables/t1/update failed with status 503 (after 1 inserted, 0 "
        "updated, 0 deleted)"
    ) in logged(logger)


def test_environment_folders_report_which_one_syncs(repo):
    data_file(repo, [{"hotel_id": "A", "name": "Shared"}])
    data_file(repo, [{"hotel_id": "A", "name": "Dev"}], folder="dev")
    data_file(repo, [{"hotel_id": "P", "name": "Prod only"}], folder="prod")
    _, logger = sync(repo, TabiClient(), dry_run=True)
    assert (
        "Skipping table data folder(s) dev, prod: set POLY_DEPLOY_TABLE_DATA_ENV to "
        "this environment's folder in its env file"
    ) in logged(logger)
    client = TabiClient()
    _, logger = sync(repo, client, environment="dev")
    text = logged(logger)
    assert "Environment folder: data/dev/ (POLY_DEPLOY_TABLE_DATA_ENV)" in text
    assert "Other environment folders, not synced: prod" in text
    assert "Skipping" not in text
    assert [(row["hotel_id"], row["name"]) for row in client.rows] == [("A", "Dev")]


def test_show_config_reports_the_table_data_folders(repo, capsys):
    flow.print_config(config(repo, "dev"), args())
    shown = json.loads(capsys.readouterr().out.split("Resolved config:\n", 1)[1])
    assert shown["table_data_environment"] == "dev"
    assert shown["table_data_folders"] == [
        "workflows/src/**/artefacts/tabi/data",
        "workflows/src/**/artefacts/tabi/data/dev",
    ]
    flow.print_config(config(repo), args())
    shown = json.loads(capsys.readouterr().out.split("Resolved config:\n", 1)[1])
    assert shown["table_data_environment"] is None


def test_read_only_requests_skip_otp():
    otp = Mock()
    otp.delete_headers.return_value = {"x-otp": "123456"}
    client = flow.PolyApiClient.__new__(flow.PolyApiClient)
    client.base_url, client.timeout_seconds, client.otp_manager = "https://x", 5, otp
    client.session = Mock()
    client.session.request.return_value = Mock(status_code=204, content=b"")
    client.request("POST", "/tables/t1/select", payload={}, read_only=True)
    assert "x-otp" not in client.session.request.call_args.kwargs["headers"]
    otp.delete_headers.assert_not_called()
    client.request("POST", "/tables/t1/insert", payload={})
    assert client.session.request.call_args.kwargs["headers"]["x-otp"] == "123456"


def test_the_push_syncs_table_data_after_tables():
    source = Path(flow.__file__).read_text()
    tables = source.index(
        '    if "tables" in config.enabled_types:\n        sync_tables('
    )
    data = source.index(
        '    if "table-data" in config.enabled_types:\n        sync_table_data('
    )
    schemas = source.index(
        '    if "schemas" in config.enabled_types:\n        sync_schemas('
    )
    assert tables < data < schemas


def test_a_data_file_changed_last_when_committed_or_edited(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        GIT_AUTHOR_NAME="Flow",
        GIT_AUTHOR_EMAIL="flow@example.test",
        GIT_COMMITTER_NAME="Flow",
        GIT_COMMITTER_EMAIL="flow@example.test",
        GIT_AUTHOR_DATE="2020-01-02T03:04:05Z",
        GIT_COMMITTER_DATE="2020-01-02T03:04:05Z",
    )

    def git(*arguments):
        subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            env=environment,
            check=True,
            capture_output=True,
        )

    later = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()

    def touched(name, content="{}"):
        path = tmp_path / name
        path.write_text(content)
        os.utime(path, (later, later))
        return path

    git("init", "--quiet")
    committed = touched("committed.json")
    edited = touched("edited.json")
    git("add", ".")
    git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", "rows")
    touched("edited.json", '{"edited": true}')
    untracked = touched("untracked.json")
    committed_at = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    assert flow.table_data_file_timestamp(committed) == committed_at
    assert flow.table_data_file_timestamp(edited) == later
    assert flow.table_data_file_timestamp(untracked) == later


def test_project_table_data_files_are_valid(monkeypatch):
    """Every data file of this project, in every environment folder, so CI
    rejects an invalid one without a push. Validation does not check the
    namespace, so any one will do."""
    monkeypatch.setenv("POLYAPI_NAMESPACE", "example")
    monkeypatch.delenv("POLYAPI_NAMESPACES", raising=False)
    root = Path(__file__).resolve().parents[2]
    cfg = flow.build_config(root, args())
    assert flow.validate_table_data(cfg, args()).problems == []


# --- review fixes ---


def test_the_table_data_folder_comes_from_the_selected_env_file_only(repo, monkeypatch):
    monkeypatch.setenv("POLY_DEPLOY_TABLE_DATA_ENV", "prod")
    assert flow.build_config(repo, args()).table_data_environment == ""
    assert config(repo).table_data_environment == ""
    assert flow.declared_table_data_environment(env_file(repo)) is None
    assert config(repo, "dev").table_data_environment == "dev"
    assert flow.declared_table_data_environment(env_file(repo, " dev ")) == "dev"


def test_force_table_data_is_its_own_flag():
    assert flow.parse_args(["push", "--force-table-data"]).force_table_data is True
    parsed = flow.parse_args(["push", "--force"])
    assert (parsed.force, parsed.force_table_data) == (True, False)


def test_a_prune_keeps_rows_another_environment_folder_lists(repo):
    data_file(repo, [{"hotel_id": "A", "name": "Hotel A"}])
    data_file(repo, [{"hotel_id": "D", "name": "Dev"}], folder="dev")
    client = TabiClient([stored("r1", "A"), stored("r2", "D"), stored("r3", "X")])
    # The env file does not name the dev folder: dev's rows stay anyway.
    stats, _ = sync(repo, client, prune_table_data=True)
    assert client.writes("delete") == [{"where": {"id": {"in": ["r3"]}}}]
    assert sorted(row["hotel_id"] for row in client.rows) == ["A", "D"]
    assert stats.deleted == 1
    # A folder name that matches no folder is reported.
    _, logger = sync(repo, client, environment="develop", dry_run=True)
    assert (
        "No data/develop/ folder holds table data; check POLY_DEPLOY_TABLE_DATA_ENV "
        "in the selected env file"
    ) in logged(logger)


def test_a_prune_deletes_nothing_while_a_file_it_cannot_read_could_list_rows(repo):
    data_file(repo, [{"hotel_id": "A", "name": "Hotel A"}])
    client = TabiClient([stored("r1", "A"), stored("r3", "X")])
    prod = write(repo, "workflows/src/hotels/artefacts/tabi/data/prod/rows.json", "{")
    stats, logger = sync(repo, client, prune_table_data=True)
    assert client.writes("delete") == [] and stats.deleted == 0
    assert (
        "Not pruning X: workflows/src/hotels/artefacts/tabi/data/prod/rows.json "
        "could not be read, so its rows cannot be told apart from rows no file lists"
    ) in logged(logger)
    prod.write_text(
        json.dumps(
            {"context": TABLE["context"], "name": TABLE["name"], "key": "hotel_id"}
        )
    )
    _, logger = sync(repo, client, prune_table_data=True)
    assert client.writes("delete") == []
    assert (
        "Not pruning X: a data file of this table in another environment folder "
        "could not be read, so the rows it lists are unknown"
    ) in logged(logger)


def test_a_shallow_clone_that_cuts_off_a_file_history_blocks_its_rows(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    base = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }

    def git(cwd, date, *arguments):
        subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            env={
                **base,
                "GIT_AUTHOR_NAME": "Flow",
                "GIT_AUTHOR_EMAIL": "flow@example.test",
                "GIT_COMMITTER_NAME": "Flow",
                "GIT_COMMITTER_EMAIL": "flow@example.test",
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            },
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "2020-01-01T00:00:00Z", "init", "--quiet")
    (origin / "rows.json").write_text("{}")
    (origin / "recent.json").write_text("{}")
    for date, message, change in (
        ("2020-01-01T00:00:00Z", "rows", None),
        ("2021-01-01T00:00:00Z", "other", ("other.txt", "x")),
        ("2025-06-01T00:00:00Z", "recent", ("recent.json", '{"changed": true}')),
    ):
        if change:
            (origin / change[0]).write_text(change[1])
        git(origin, date, "add", ".")
        git(origin, date, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
    # The clone's history starts at "other", which seems to add every file.
    git(
        tmp_path,
        "2025-06-01T00:00:00Z",
        "clone",
        "-q",
        "--depth",
        "2",
        f"file://{origin}",
        "clone",
    )
    clone = tmp_path / "clone"
    assert flow.table_data_file_timestamp(clone / "rows.json") is None
    assert (
        flow.table_data_file_timestamp(clone / "recent.json")
        == datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp()
    )

    data_set = flow.TableDataSet(
        context=TABLE["context"],
        name=TABLE["name"],
        key=("hotel_id",),
        rows=(flow.TableDataRow({"hotel_id": "A", "name": "From file"}, (clone,)),),
        paths=(clone,),
    )

    def plan(guard):
        return flow.plan_table_data_set(
            data_set,
            columns=COLUMN_MAP,
            remote_rows=[stored("r1", "A", name="From app")],
            guard=guard,
            file_timestamp=lambda path: None,
            now=NOW,
        )

    assert plan(True).blocked == [
        "A (this shallow Git clone does not hold its file's last change; fetch the "
        "full history, such as fetch-depth: 0)"
    ]
    assert len(plan(False).updates) == 1


def plan_rows(rows, remote, columns=COLUMN_MAP, key=("hotel_id",)):
    data_set = flow.TableDataSet(
        context=TABLE["context"],
        name=TABLE["name"],
        key=key,
        rows=tuple(flow.TableDataRow(row, (Path("/p/f.json"),)) for row in rows),
        paths=(Path("/p/f.json"),),
    )
    return flow.plan_table_data_set(
        data_set,
        columns=columns,
        remote_rows=remote,
        guard=True,
        file_timestamp=lambda path: FILE_TIME,
        now=NOW,
    )


def test_keys_on_date_and_timestamp_columns_match_the_stored_instants():
    columns = {**COLUMN_MAP, "day": {"name": "day", "type": "date", "required": True}}
    plan = plan_rows(
        [
            {
                "hotel_id": "A",
                "day": "2024-01-01",
                "opened_at": "2024-01-01T10:00:00+01:00",
                "rooms": 5,
            }
        ],
        [
            stored(
                "r1",
                "A",
                day="2024-01-01T00:00:00.000Z",
                opened_at="2024-01-01T09:00:00.000Z",
                rooms=5,
            )
        ],
        columns,
        ("hotel_id", "day", "opened_at"),
    )
    assert (len(plan.inserts), plan.unchanged, len(plan.extras)) == (0, 1, 0)


def test_rows_without_key_values_or_unlisted_duplicates_are_only_extras():
    remote = [
        stored("r1", "A"),
        {**stored("r2", "X"), "hotel_id": None},
        {**stored("r3", "Y"), "hotel_id": None},
        stored("r4", "D"),
        stored("r5", "D"),
    ]
    plan = plan_rows([{"hotel_id": "A", "name": "Hotel A"}], remote)
    assert plan.unchanged == 1
    assert [row["id"] for row in plan.extras] == ["r4", "r5", "r2", "r3"]
    # A listed key that several rows hold is ambiguous, so the table stops.
    with pytest.raises(flow.TableDataError, match="several table rows have key D"):
        plan_rows([{"hotel_id": "D", "name": "D"}], remote)


def test_whole_numbers_beyond_2_53_must_be_written_as_strings():
    document = {
        "name": "t",
        "key": "hotel_id",
        "rows": [{"hotel_id": "A", "settings": {"external": [2**60]}}],
    }
    with pytest.raises(
        flow.TableDataError,
        match="row 1 sets 'settings' to a whole number beyond 2\\^53, which JSON "
        "cannot carry exactly; write it as a string",
    ):
        flow.parse_table_data_document(document)
    assert not flow.table_value_equal("bigint", "9007199254740993", "9007199254740992")
    columns = {"hotel_id": {"name": "hotel_id", "type": "bigint"}}
    assert flow.table_row_key(
        {"hotel_id": "9007199254740993"}, ("hotel_id",), columns
    ) != flow.table_row_key({"hotel_id": "9007199254740992"}, ("hotel_id",), columns)
