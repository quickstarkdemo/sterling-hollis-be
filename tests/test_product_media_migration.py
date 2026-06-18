from pathlib import Path
import importlib.util

from sqlalchemy import create_engine, inspect, text


def test_product_media_migration_adds_media_table_and_job_references(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("create table catalog_products (id varchar(64) primary key)"))
        connection.execute(text("create table image_generation_jobs (id varchar(64) primary key)"))

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/c7d8e9f0a1b2_add_product_media_assets.py"
    )
    spec = importlib.util.spec_from_file_location("product_media_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = inspect(engine)
    assert "product_media_assets" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("product_media_assets")} >= {
        "id",
        "catalog_product_id",
        "role",
        "intent",
        "source_media_id",
        "image_set",
        "parameters",
        "provenance",
        "display_order",
    }
    assert {column["name"] for column in inspector.get_columns("image_generation_jobs")} >= {
        "source_media_id",
        "target_media_id",
        "requested_intent",
    }

    engine.dispose()
