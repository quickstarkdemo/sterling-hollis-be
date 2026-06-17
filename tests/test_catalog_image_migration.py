from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


def test_catalog_image_migration_adds_workflow_draft_and_idempotency_fields(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog-images.db'}", future=True)
    metadata = MetaData()
    Table("catalog_workflows", metadata, Column("id", String(64), primary_key=True))
    Table("catalog_draft_revisions", metadata, Column("id", String(64), primary_key=True))
    Table("image_generation_jobs", metadata, Column("id", String(64), primary_key=True))
    metadata.create_all(engine)

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/a5b6c7d8e9f0_add_catalog_image_workflow_fields.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_image_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("image_generation_jobs")}
    assert {
        "workflow_id",
        "draft_revision_id",
        "expected_draft_version",
        "requested_action",
        "requested_variant_index",
        "idempotency_key_hash",
        "request_hash",
        "refinement_prompt",
        "source_image_path",
    } <= columns
    assert "uq_image_generation_jobs_workflow_idempotency" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("image_generation_jobs")
    }
