import uuid

import pytest
from sqlalchemy import create_engine, text

from kanzlei_ai.settings import Settings


@pytest.fixture
def database():
    settings = Settings()
    engine = create_engine(
        settings.database_url.get_secret_value().replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        connect_args={
            "password": settings.database_password.get_secret_value()
            if settings.database_password
            else None
        },
    )
    schema = "test_ingestion_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_engine(
        engine.url,
        connect_args={
            "password": settings.database_password.get_secret_value()
            if settings.database_password
            else None,
            "options": f"-csearch_path={schema}",
        },
    )
    try:
        yield scoped
    finally:
        scoped.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
