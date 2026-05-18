# -*- coding: utf-8 -*-
"""Tests for visibility listing index migration."""

# Standard
import importlib
import os

# Third-Party
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

MODULE_NAME = "mcpgateway.alembic.versions.8d0f7c2a9b31_add_visibility_listing_order_indexes"
INDEX_NAME = "idx_tools_private_owner_team_order"
_PG_URL = os.environ.get("TEST_POSTGRES_URL")


def _migration_context(conn):
    """Return an Alembic migration context bound to conn."""
    return MigrationContext.configure(conn, opts={"as_sql": False})


def _pg_engine():
    """Return a PostgreSQL engine for migration tests."""
    return sa.create_engine(_PG_URL)


def _create_tools_table(conn):
    """Create a minimal tools table that satisfies migration preconditions."""
    conn.execute(sa.text("DROP TABLE IF EXISTS tools CASCADE"))
    conn.execute(sa.text("""
            CREATE TABLE tools (
                id VARCHAR PRIMARY KEY,
                team_id VARCHAR,
                owner_email VARCHAR,
                visibility VARCHAR NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
            """))
    conn.commit()


def _drop_tools_table(conn):
    """Drop the minimal tools table."""
    conn.execute(sa.text("DROP TABLE IF EXISTS tools CASCADE"))
    conn.commit()


def _index_names(conn):
    """Return indexes on the tools table."""
    return {index["name"] for index in sa.inspect(conn).get_indexes("tools")}


def _index_definition(conn):
    """Return the PostgreSQL index definition for the migration index."""
    row = conn.execute(
        sa.text("SELECT indexdef FROM pg_indexes WHERE tablename = 'tools' AND indexname = :name"),
        {"name": INDEX_NAME},
    ).one_or_none()
    return row[0] if row else None


@pytest.mark.skipif(not _PG_URL, reason="TEST_POSTGRES_URL not set")
def test_visibility_listing_index_upgrade_is_idempotent_and_downgrades():
    """Migration creates the private owner/team index once and drops it."""
    engine = _pg_engine()
    try:
        module = importlib.import_module(MODULE_NAME)
        with engine.connect() as conn:
            _create_tools_table(conn)
            ctx = _migration_context(conn)
            with Operations.context(ctx):
                module.upgrade()
                module.upgrade()

            assert INDEX_NAME in _index_names(conn)
            index_definition = _index_definition(conn)
            assert "team_id" in index_definition
            assert "owner_email" in index_definition
            assert "enabled" in index_definition
            assert "visibility" in index_definition
            assert "private" in index_definition

            with Operations.context(ctx):
                module.downgrade()

            assert INDEX_NAME not in _index_names(conn)
    finally:
        with engine.connect() as conn:
            _drop_tools_table(conn)
        engine.dispose()
