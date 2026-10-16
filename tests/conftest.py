import pytest
from sqlalchemy import select

import app.main
from app.db import make_engine, make_sessionmaker, migrate
from app.models import Group, Person
from tests.family import seed_family

# Every app the tests start gets the made-up household, not just the bare defaults.
app.main.seed = seed_family


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(str(tmp_path))
    migrate(engine)
    return engine


@pytest.fixture
def session(engine):
    with make_sessionmaker(engine)() as session:
        seed_family(session)
        yield session


@pytest.fixture
def people(session):
    """Seeded people by name."""
    return {p.name: p for p in session.scalars(select(Person))}


@pytest.fixture
def groups(session):
    return {g.name: g for g in session.scalars(select(Group))}
