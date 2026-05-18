# -*- coding: utf-8 -*-
"""Tests for team visibility pagination fast path."""

# Standard
import asyncio
from datetime import datetime
import os

# Third-Party
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# First-Party
from mcpgateway.services.base_service import BaseService
from mcpgateway.utils.pagination import encode_cursor, unified_paginate


class _Base(DeclarativeBase):
    """Base class for local test mappings."""

    pass


class _VisibleItem(_Base):
    """Minimal mapped item for compiling visibility pagination SQL."""

    __tablename__ = "visible_items"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    team_id: Mapped[str] = mapped_column(sa.String)
    owner_email: Mapped[str] = mapped_column(sa.String)
    name: Mapped[str] = mapped_column(sa.String)
    visibility: Mapped[str] = mapped_column(sa.String)
    enabled: Mapped[bool] = mapped_column(sa.Boolean)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime)


class _Item:
    """Simple row object returned by the DB stub."""

    def __init__(self, item_id: str, created_at: datetime):
        """Initialize an item with cursor fields."""
        self.id = item_id
        self.created_at = created_at


class _Result:
    """Minimal SQLAlchemy result stub."""

    def __init__(self, rows):
        """Store rows returned by the stub."""
        self._rows = rows

    def all(self):
        """Return all rows."""
        return self._rows

    def scalars(self):
        """Return the scalar-result stub."""
        return self


class _Dialect:
    """DB dialect stub."""

    name = "postgresql"


class _Bind:
    """DB bind stub."""

    def __init__(self, dialect_name="postgresql"):
        """Initialize the bind with a dialect name."""
        self.dialect = _Dialect()
        self.dialect.name = dialect_name


class _Db:
    """Capture executed statements without touching a real database."""

    def __init__(self, dialect_name="postgresql"):
        """Initialize captured statement storage."""
        self.bind = _Bind(dialect_name)
        self.statements = []
        self._created = datetime(2026, 1, 1, 12, 0, 0)

    def execute(self, statement):
        """Capture the statement and return one extra ordered object."""
        self.statements.append(statement)
        return _Result([_Item("3", self._created), _Item("2", self._created), _Item("1", self._created)])


class _VisibleItemService(BaseService):
    """Minimal service that produces real fast-path metadata."""

    _visibility_model_cls = _VisibleItem


def _base_query():
    """Build a representative team-scoped query through BaseService."""
    base_query = sa.select(_VisibleItem).where(_VisibleItem.enabled).order_by(_VisibleItem.created_at.desc(), _VisibleItem.id.desc())
    return _VisibleItemService()._apply_visibility_filter(base_query, "owner@test.com", ["team-1"], "team-1").where(_VisibleItem.name == "kept")


def test_team_visibility_fast_path_uses_branch_limited_union_and_scoped_fetch():
    """Postgres fast path uses a branch-limited ID subquery in a scoped fetch."""
    db = _Db()
    unified_result = asyncio.run(unified_paginate(db, _base_query(), limit=2))
    items, next_cursor = unified_result

    assert [item.id for item in items] == ["3", "2"]
    assert next_cursor is not None
    assert len(unified_result) == 2
    assert len(db.statements) == 1

    compiled = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert compiled.count("UNION ALL") == 2
    assert compiled.count("LIMIT 3") == 4
    assert "visible_items.id IN (SELECT" in compiled
    assert "visible_items.visibility = 'public'" in compiled
    assert "visible_items.visibility = 'team'" in compiled
    assert "visible_items.visibility = 'private'" in compiled
    assert compiled.count("visible_items.name = 'kept'") >= 3


def test_team_visibility_fast_path_applies_cursor_to_union_branches():
    """Cursor continuation is pushed into every visibility branch."""
    db = _Db()
    cursor = encode_cursor({"created_at": datetime(2026, 1, 1, 12, 0, 0).isoformat(), "id": "3"})

    asyncio.run(unified_paginate(db, _base_query(), cursor=cursor, limit=2))

    compiled = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert compiled.count("visible_items.created_at < '2026-01-01 12:00:00'") == 3
    assert compiled.count("visible_items.id < '3'") == 3


def test_team_visibility_fast_path_falls_back_for_non_postgres():
    """Non-Postgres dialects use the regular cursor path."""
    db = _Db(dialect_name="sqlite")

    asyncio.run(unified_paginate(db, _base_query(), limit=2))

    compiled = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "UNION ALL" not in compiled


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not set")
def test_team_visibility_fast_path_executes_on_postgres():
    """Postgres fast path returns ordered filtered rows across cursor pages."""
    engine = sa.create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        _Base.metadata.drop_all(engine, tables=[_VisibleItem.__table__])
        _Base.metadata.create_all(engine, tables=[_VisibleItem.__table__])
        created = datetime(2026, 1, 1, 12, 0, 0)
        rows = [
            _VisibleItem(
                id=f"drop-{idx}",
                name="drop",
                enabled=True,
                visibility="public",
                team_id="team-x",
                owner_email="other@test.com",
                created_at=created.replace(minute=idx),
            )
            for idx in range(5)
        ]
        rows.extend(
            [
                _VisibleItem(
                    id="keep-4",
                    name="kept",
                    enabled=True,
                    visibility="private",
                    team_id="team-1",
                    owner_email="owner@test.com",
                    created_at=created.replace(second=4),
                ),
                _VisibleItem(
                    id="keep-3",
                    name="kept",
                    enabled=True,
                    visibility="private",
                    team_id="team-1",
                    owner_email="owner@test.com",
                    created_at=created.replace(second=3),
                ),
                _VisibleItem(
                    id="keep-2",
                    name="kept",
                    enabled=True,
                    visibility="team",
                    team_id="team-1",
                    owner_email="other@test.com",
                    created_at=created.replace(second=2),
                ),
                _VisibleItem(
                    id="keep-1b",
                    name="kept",
                    enabled=True,
                    visibility="public",
                    team_id="team-x",
                    owner_email="other@test.com",
                    created_at=created.replace(second=1),
                ),
                _VisibleItem(
                    id="keep-1a",
                    name="kept",
                    enabled=True,
                    visibility="public",
                    team_id="team-x",
                    owner_email="other@test.com",
                    created_at=created.replace(second=1),
                ),
            ]
        )
        with Session(engine) as db:
            db.add_all(rows)
            db.commit()

            page_one, cursor = asyncio.run(unified_paginate(db, _base_query(), limit=3))
            assert [item.id for item in page_one] == ["keep-4", "keep-3", "keep-2"]
            assert cursor is not None

            page_two, next_cursor = asyncio.run(unified_paginate(db, _base_query(), cursor=cursor, limit=3))
            assert [item.id for item in page_two] == ["keep-1b", "keep-1a"]
            assert next_cursor is None
    finally:
        _Base.metadata.drop_all(engine, tables=[_VisibleItem.__table__])
        engine.dispose()
