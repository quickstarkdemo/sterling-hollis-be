from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models import (
    ApiTrace,
    ApiTraceArtifact,
    ApiTraceEvent,
    ApiTraceLink,
    ApiTraceSpan,
)


def test_api_trace_projection_migration_round_trips(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'api-traces.db'}", future=True)
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/d5e6f7a8b9c0_add_api_trace_projection.py"
    )
    spec = importlib.util.spec_from_file_location("api_trace_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {
            "api_traces",
            "api_trace_spans",
            "api_trace_links",
            "api_trace_events",
            "api_trace_artifacts",
        } <= tables
        for model in (
            ApiTrace,
            ApiTraceSpan,
            ApiTraceLink,
            ApiTraceEvent,
            ApiTraceArtifact,
        ):
            migrated_columns = {
                column["name"]
                for column in inspect(connection).get_columns(model.__tablename__)
            }
            assert migrated_columns == set(model.__table__.columns.keys())

        connection.execute(
            text(
                """
                insert into api_traces (
                    id, projection_version, owner_provider, owner_provider_user_id,
                    surface, name, root_span_id, status, attributes_json,
                    truncation_json, payload_expired, started_at, payload_expires_at,
                    metadata_expires_at, created_at, updated_at
                ) values (
                    'trace_1', '1.0', 'clerk', 'user_1', 'catalog', 'Trace',
                    'span_1', 'started', '{}', '{}', 0, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into api_trace_events (
                    id, trace_id, event_id, sequence, name, event_type,
                    attributes_json, occurred_at, created_at
                ) values (
                    'row_1', 'trace_1', 'event_1', 1, 'Started', 'status',
                    '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        assert connection.scalar(text("select count(*) from api_trace_events")) == 1

        migration.downgrade()
        remaining = set(inspect(connection).get_table_names())
        assert "api_traces" not in remaining
        assert "api_trace_events" not in remaining

    engine.dispose()
