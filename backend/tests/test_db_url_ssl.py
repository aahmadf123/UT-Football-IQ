"""The shared Postgres URL may be written for either driver.

asyncpg (the app) only understands ``ssl=``; psycopg2 (Alembic) only
understands ``sslmode=``. A production secret written for one driver used to
crash the other: every hosted boot failed at the migration step.
"""

from app.config import Settings, rename_query_param

_BASE = "postgresql+asyncpg://user:p%40ss@db.example.com:5432/footiq"


def _settings(**kwargs: str) -> Settings:
    kwargs.setdefault("database_sync_url", "")
    return Settings(secret_key="x" * 32, **kwargs)  # type: ignore[arg-type]


def test_async_url_gets_ssl_and_sync_url_gets_sslmode() -> None:
    s = _settings(database_url=f"{_BASE}?sslmode=require")
    assert s.database_url.endswith("?ssl=require")
    assert (
        s.database_sync_url == "postgresql://user:p%40ss@db.example.com:5432/footiq?sslmode=require"
    )


def test_explicit_sync_url_written_for_asyncpg_is_fixed() -> None:
    s = _settings(
        database_url=f"{_BASE}?ssl=require",
        database_sync_url="postgresql://user:pw@db/footiq?ssl=require&application_name=x",
    )
    assert s.database_url.endswith("?ssl=require")
    assert (
        s.database_sync_url == "postgresql://user:pw@db/footiq?sslmode=require&application_name=x"
    )


def test_only_the_query_string_is_rewritten() -> None:
    url = "postgresql://user:sslmode=secret@db/footiq?sslmode=require"
    assert rename_query_param(url, "sslmode", "ssl") == (
        "postgresql://user:sslmode=secret@db/footiq?ssl=require"
    )
    assert (
        rename_query_param("postgresql://db/footiq", "ssl", "sslmode") == "postgresql://db/footiq"
    )


def test_question_mark_inside_a_query_value() -> None:
    url = "postgresql://u:p@db/footiq?ssl=require&application_name=coach?mode=hosted"
    assert rename_query_param(url, "ssl", "sslmode") == (
        "postgresql://u:p@db/footiq?sslmode=require&application_name=coach?mode=hosted"
    )
