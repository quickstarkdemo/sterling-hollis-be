from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


def test_image_trace_lineage_migration_round_trips(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'image-trace.db'}", future=True)
    metadata = MetaData()
    Table("image_generation_jobs", metadata, Column("id", String(64), primary_key=True))
    metadata.create_all(engine)
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/e6f7a8b9c0d1_add_image_job_trace_lineage.py"
    )
    spec = importlib.util.spec_from_file_location("image_trace_lineage", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("image_generation_jobs")
        }
        assert {
            "api_trace_id",
            "api_trace_span_id",
            "api_trace_retry_of_job_id",
        } <= columns
        assert "ix_image_generation_jobs_api_trace_id" in {
            index["name"]
            for index in inspect(connection).get_indexes("image_generation_jobs")
        }

        migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("image_generation_jobs")
        }
        assert "api_trace_id" not in columns
        assert "api_trace_span_id" not in columns
        assert "api_trace_retry_of_job_id" not in columns

    engine.dispose()
