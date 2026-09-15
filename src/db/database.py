from threading import RLock
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.secrets.vault_client import get_database_secrets

Base = declarative_base()

_engine = None
_session_factory = None
_initialization_lock = RLock()


def build_database_url(database_settings: dict[str, str]) -> str:
    """
    Формирует SQLAlchemy URL для подключения к PostgreSQL.

    Данные подключения приходят из Hashicorp Vault.
    """
    user = quote(database_settings["POSTGRES_USER"], safe="")
    password = quote(database_settings["POSTGRES_PASSWORD"], safe="")
    host = database_settings["POSTGRES_HOST"]
    port = database_settings["POSTGRES_PORT"]
    database = database_settings["POSTGRES_DB"]

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


def get_engine():
    """
    Возвращает SQLAlchemy engine.

    Engine создаётся лениво, чтобы секреты из Vault читались только при реальном обращении к БД.
    """
    global _engine

    with _initialization_lock:
        if _engine is None:
            database_settings = get_database_secrets()
            database_url = build_database_url(database_settings)

            _engine = create_engine(
                database_url,
                pool_pre_ping=True,
                hide_parameters=True,
                pool_size=5,
                max_overflow=5,
                pool_timeout=10,
                connect_args={"connect_timeout": 5},
            )

    return _engine


def get_session_factory():
    """
    Возвращает фабрику SQLAlchemy-сессий.
    """
    global _session_factory

    with _initialization_lock:
        if _session_factory is None:
            _session_factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=get_engine(),
            )

    return _session_factory


def get_db():
    """
    FastAPI dependency для получения сессии БД.
    """
    session_factory = get_session_factory()
    db = session_factory()

    try:
        yield db
    finally:
        db.close()
