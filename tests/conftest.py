from datetime import date

import pytest

from rental_tracker import db as dbmod
from rental_tracker.config import DataDir
from rental_tracker.services import leases, portfolio, tenants


@pytest.fixture
def data_dir(tmp_path):
    return DataDir(tmp_path / "RentalTracker").ensure()


@pytest.fixture
def conn(data_dir):
    c = dbmod.connect(data_dir.db)
    dbmod.migrate(c)
    yield c
    c.close()


@pytest.fixture
def owner_id(conn):
    return portfolio.default_owner_id(conn)


def make_property(conn, owner_id, code="P-1", units=None):
    return portfolio.save_property(conn, None, {
        "owner_id": owner_id, "code": code, "name": f"Property {code}", "property_type": "single_family",
        "address_line1": "1 Main St", "city": "Springfield", "state": "IL", "postal_code": "62701"},
        unit_labels=units)


def make_lease(conn, owner_id, *, code="P-1", start="2026-01-01", end="2026-12-31", rent=100000,
               today=date(2026, 1, 1), **terms):
    pid = make_property(conn, owner_id, code)
    unit_id = conn.execute("SELECT id FROM units WHERE property_id = ?", (pid,)).fetchone()[0]
    tid = tenants.save_tenant(conn, None, {"first_name": "Ann", "last_name": f"Lee-{code}"})
    return leases.create_lease(conn, unit_id=unit_id, tenants=[(tid, "primary")], start=start, end=end,
                               rent_cents=rent, today=today, **terms)
