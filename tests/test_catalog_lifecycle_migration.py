from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_lifecycle_migration_preserves_existing_public_catalog_rows(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                create table catalog_products (
                    id varchar(64) primary key,
                    title varchar(255) not null
                )
                """
            )
        )
        connection.execute(
            text("insert into catalog_products (id, title) values ('cat_existing', 'Existing product')")
        )

        migration_path = (
            Path(__file__).parents[1]
            / "alembic/versions/d2e3f4a5b6c7_add_catalog_draft_lifecycle.py"
        )
        spec = importlib.util.spec_from_file_location("catalog_lifecycle_migration", migration_path)
        assert spec and spec.loader
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        rows = connection.execute(
            text("select id, lifecycle_status, version from catalog_products")
        ).mappings().all()

    assert rows == [
        {"id": "cat_existing", "lifecycle_status": "published", "version": 1}
    ]
