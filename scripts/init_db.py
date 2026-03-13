from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import get_settings

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    settings = get_settings()
    database_url = settings.database_url

    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    engine = create_engine(database_url, future=True)
    with engine.connect() as connection:
        alembic_version = connection.execute(text("select to_regclass('public.alembic_version')")).scalar()
        synthetic_runs = connection.execute(text("select to_regclass('public.synthetic_runs')")).scalar()

    if alembic_version is None and synthetic_runs is not None:
        command.stamp(cfg, "head")

    command.upgrade(cfg, "head")
    print("Database migrated to head")
