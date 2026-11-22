"""Explicit connection construction and packaged Alembic migration entrypoint."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from kanzlei_ai.settings import Settings


def engine_from_settings(settings: Settings):
    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql+psycopg")
    connect_args = {"connect_timeout": 5}
    if settings.database_password is not None:
        connect_args["password"] = settings.database_password.get_secret_value()
    return create_engine(url, connect_args=connect_args, hide_parameters=True)


def migrate(engine):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
