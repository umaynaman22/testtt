"""A made-up portfolio for trying the app: 60 properties, 78 units, 6 months of payments, in pesos."""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import db as dbmod
from .config import DataDir
from .domain.periods import add_periods, period_of, period_start
from .services import late_fees, leases, ledger, portfolio, rent_posting, search, tenants

STREETS = ["Rizal", "Mabini", "Bonifacio", "Luna", "Del Pilar", "Burgos", "Aguinaldo", "Quezon", "Roxas",
           "Magsaysay", "Osmeña", "Legaspi", "Sampaguita", "Narra", "Molave", "Acacia", "Kamagong", "Yakal",
           "Ilang-Ilang", "Dao"]
SUFFIX = ["St", "Ave", "Rd", "Ext", "Blvd", "Dr"]
CITIES = [("Quezon City", "Metro Manila", "11"), ("Makati", "Metro Manila", "12"), ("Pasig", "Metro Manila", "16"),
          ("Cebu City", "Cebu", "60")]
FIRST = ["Juan", "Maria", "Jose", "Ana", "Mark", "Angelica", "Paolo", "Kristine", "Carlo", "Jasmine", "Miguel",
         "Patricia", "Rafael", "Camille", "Joshua", "Andrea", "Gabriel", "Nicole", "Christian", "Bea", "Ramon",
         "Lorna", "Enrique", "Maricel", "Rowena", "Dennis", "Liza", "Arnel", "Joy", "Noel"]
LAST = ["Santos", "Reyes", "Cruz", "Bautista", "Garcia", "Mendoza", "Dela Cruz", "Ramos", "Aquino", "Villanueva",
        "Castillo", "Fernandez", "Lopez", "Gonzales", "Torres", "Flores", "Rivera", "Navarro", "Tan", "Lim", "Sy",
        "Domingo"]


def build_demo(data: DataDir, today: date | None = None, properties: int = 60, seed: int = 7) -> None:
    today = today or date.today()
    rnd = random.Random(seed)
    conn = dbmod.connect(data.db)
    dbmod.migrate(conn)
    billing_start = period_start(add_periods(period_of(today), -6))
    with dbmod.transaction(conn):
        used_codes: set[str] = set()
        units: list[tuple[int, int, int]] = []  # (property_id, unit_id, market rent)
        property_ids = []
        for i in range(properties):
            street = rnd.choice(STREETS)
            number = rnd.randint(1, 980)
            suffix = rnd.choice(SUFFIX)
            while f"{number} {street} {suffix}" in used_codes:
                number += 1
            used_codes.add(f"{number} {street} {suffix}")
            city, state, zip3 = rnd.choice(CITIES)
            kind = "single_family" if i < 45 else ("multi_family" if i < 57 else "condo")
            labels = ["Main"] if kind != "multi_family" else (["A", "B"] if i < 54 else ["1", "2", "3", "4"])
            address = f"{number} {street} {suffix}"
            pid = portfolio.save_property(conn, None, {
                "name": address, "property_type": kind, "address_line1": address, "city": city, "state": state,
                "postal_code": f"{zip3}{rnd.randint(0, 99):02d}"}, unit_labels=labels, reindex=False)
            property_ids.append(pid)
            for u in conn.execute("SELECT id FROM units WHERE property_id = ?", (pid,)):
                rent = rnd.randint(8, 35) * 1000_00 + rnd.choice([0, 500_00])
                beds = rnd.choice([1, 2, 2, 3, 3, 4])
                conn.execute("UPDATE units SET market_rent_cents = ?, bedrooms = ?, bathrooms = ?, square_feet = ? WHERE id = ?",
                             (rent, beds, rnd.choice([1, 1.5, 2]), 500 + beds * 300 + rnd.randint(0, 300), u["id"]))
                units.append((pid, u["id"], rent))

        lease_ids = []
        vacant = set(rnd.sample(range(len(units)), 6))
        for n, (pid, unit_id, market) in enumerate(units):
            if n in vacant:
                continue
            people = []
            for k in range(rnd.choice([1, 1, 2, 2, 3])):
                tid = tenants.save_tenant(conn, None, {"first_name": rnd.choice(FIRST), "last_name": rnd.choice(LAST),
                                                       "phone": f"09{rnd.randint(15, 99)}-{rnd.randint(100, 999)}-{rnd.randint(1000, 9999)}"}, reindex=False)
                people.append((tid, "primary" if k == 0 else "co_tenant"))
            start = today - timedelta(days=rnd.randint(40, 1100))
            start = start.replace(day=1) if rnd.random() < .8 else start
            rent = market - rnd.choice([0, 0, 500_00, 1000_00, 1500_00])
            lid = leases.create_lease(
                conn, unit_id=unit_id, tenants=people, start=start.isoformat(), end=None, rent_cents=rent,
                today=today, billing_start=max(start, billing_start).isoformat(),
                late_fee_type="flat", late_fee_flat_cents=500_00,
                late_fee_percent_bp=500, late_fee_grace_days=5, move_in_date=start.isoformat())
            lease_ids.append((lid, rent))

        rent_posting.post_rent(conn, today)

        # payment behaviour: most pay on time, a few late, partial or behind
        for lid, rent in lease_ids:
            habit = rnd.choices(["ontime", "late", "partial", "behind"], [82, 9, 5, 4])[0]
            charges = conn.execute("SELECT period, amount_cents, due_date FROM charges WHERE lease_id = ? "
                                   "AND charge_type = 'rent' ORDER BY period", (lid,)).fetchall()
            for c in charges:
                due = date.fromisoformat(c["due_date"])
                if habit == "ontime":
                    when, amount = due + timedelta(days=rnd.randint(-3, 4)), c["amount_cents"]
                elif habit == "late":
                    when, amount = due + timedelta(days=rnd.randint(3, 18)), c["amount_cents"]
                elif habit == "partial":
                    when, amount = due + timedelta(days=rnd.randint(0, 6)), c["amount_cents"] // 2
                else:
                    if rnd.random() < .5:
                        continue
                    when, amount = due + timedelta(days=rnd.randint(5, 25)), c["amount_cents"]
                if when > today:
                    continue
                method = rnd.choice(["cash", "cash", "gcash", "gcash", "bank_transfer", "check", "other"])
                ledger.record_payment(conn, lid, amount, max(when, due - timedelta(days=5)).isoformat(), method,
                                      method_other="Maya" if method == "other" else None)

        # post late fees for past months; leave the current month in the review queue
        this_period = period_of(today)
        keys = [c.key for c in late_fees.find_candidates(conn, today) if c.period < this_period]
        late_fees.apply(conn, keys, "approve", today)

        # a few tenants with a debt besides rent
        for lid, _ in lease_ids[3:30:9]:
            ledger.add_charge(conn, lid, "other", rnd.choice([750_00, 1500_00, 3200_00]),
                              (today - timedelta(days=rnd.randint(3, 20))).isoformat(),
                              "Debt — " + rnd.choice(["broken window", "water bill", "lock change"]))

        # a vacant-ready unit gets an upcoming lease
        vacant_unit = units[sorted(vacant)[0]][1]
        tid = tenants.save_tenant(conn, None, {"first_name": "Teresa", "last_name": "Salazar", "phone": "0917-555-7777"},
                                  reindex=False)
        leases.create_lease(conn, unit_id=vacant_unit, tenants=[(tid, "primary")],
                            start=(today + timedelta(days=9)).isoformat(), end=None,
                            rent_cents=units[sorted(vacant)[0]][2], today=today, late_fee_type="flat",
                            late_fee_flat_cents=500_00)
        search.rebuild(conn)
    conn.close()
