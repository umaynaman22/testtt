"""A made-up portfolio for trying the app: 60 properties, 78 units, 6 months of history."""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import db as dbmod
from .config import DataDir
from .domain.periods import add_periods, due_date, period_of, period_start
from .services import expenses, late_fees, leases, ledger, portfolio, rent_posting, search, tenants

STREETS = ["Maple", "Oak", "Cedar", "Pine", "Elm", "Birch", "Walnut", "Chestnut", "Spruce", "Willow", "Hickory",
           "Aspen", "Magnolia", "Sycamore", "Poplar", "Juniper", "Laurel", "Linden", "Alder", "Cypress"]
SUFFIX = ["St", "Ave", "Rd", "Ln", "Dr", "Ct", "Way", "Pl"]
CITIES = [("Springfield", "IL", "627"), ("Riverton", "IL", "625"), ("Lakeview", "IL", "626")]
FIRST = ["Ann", "Bo", "Carlos", "Dana", "Eli", "Fatima", "Grace", "Hiro", "Isla", "Jamal", "Kate", "Luis", "Maya",
         "Noah", "Olga", "Priya", "Quinn", "Rosa", "Sam", "Tariq", "Uma", "Victor", "Wen", "Ximena", "Yusuf", "Zoe",
         "José", "Chloé", "Ngozi", "Mateo", "Aisha", "Liam", "Sofia", "Ethan", "Mia", "Omar", "Lena", "Ravi"]
LAST = ["Lee", "Kim", "Garcia", "Nguyen", "Smith", "Patel", "Okafor", "Müller", "Rossi", "Haddad", "Johnson",
        "Brown", "Lopez", "Chen", "Silva", "Ivanova", "Yamamoto", "Novak", "Diaz", "Khan", "Walker", "Hughes"]
VENDORS = [("Ace Plumbing", "plumber", "Repairs"), ("BrightSpark Electric", "electrician", "Repairs"),
           ("CoolAir HVAC", "HVAC", "Repairs"), ("GreenCut Lawn", "landscaping", "Landscaping and snow removal"),
           ("BugOff Pest Control", "pest control", "Pest control"), ("Sparkle Cleaning", "cleaning", "Cleaning and maintenance"),
           ("HandyPro Services", "handyman", "Repairs"), ("City Water & Sewer", "utility", "Utilities"),
           ("Home Depot", "supplies", "Supplies"), ("Keystone Insurance", "insurance", "Insurance")]


def build_demo(data: DataDir, today: date | None = None, properties: int = 60, seed: int = 7) -> None:
    today = today or date.today()
    rnd = random.Random(seed)
    conn = dbmod.connect(data.db)
    dbmod.migrate(conn)
    billing_start = period_start(add_periods(period_of(today), -6))
    with dbmod.transaction(conn):
        owner = portfolio.save_owner(conn, None, {"name": "Maple Street Holdings LLC", "entity_type": "llc",
                                                  "email": "office@example.com", "phone": "555-0100",
                                                  "mailing_address": "PO Box 100, Springfield, IL 62701"},
                                     reindex=False)
        owner2 = portfolio.save_owner(conn, None, {"name": "R. & J. Family Trust", "entity_type": "trust"},
                                      reindex=False)
        cats = {c["name"]: c["id"] for c in expenses.list_categories(conn)}
        vendor_ids = {name: expenses.save_vendor(conn, None, {"name": name, "trade": trade, "phone": f"555-02{i:02d}",
                                                              "needs_1099": int(trade not in ("utility", "supplies", "insurance"))},
                                                 reindex=False)
                      for i, (name, trade, _) in enumerate(VENDORS)}
        used_codes: set[str] = set()
        units: list[tuple[int, int, int]] = []  # (property_id, unit_id, market rent)
        property_ids = []
        for i in range(properties):
            street = rnd.choice(STREETS)
            number = rnd.randint(10, 9800)
            code = f"{street[:4].upper()}-{number}"
            while code in used_codes:
                number += 1
                code = f"{street[:4].upper()}-{number}"
            used_codes.add(code)
            city, state, zip3 = rnd.choice(CITIES)
            kind = "single_family" if i < 45 else ("multi_family" if i < 57 else "condo")
            labels = ["Main"] if kind != "multi_family" else (["A", "B"] if i < 54 else ["1", "2", "3", "4"])
            address = f"{number} {street} {rnd.choice(SUFFIX)}"
            price = rnd.randint(95, 260) * 1000_00
            pid = portfolio.save_property(conn, None, {
                "owner_id": owner if i % 7 else owner2, "code": code, "name": address, "property_type": kind,
                "address_line1": address, "city": city, "state": state, "postal_code": f"{zip3}{rnd.randint(10, 99)}",
                "year_built": rnd.randint(1925, 2015), "purchase_date": f"{rnd.randint(2012, 2023)}-0{rnd.randint(1, 9)}-15",
                "purchase_price_cents": price, "estimated_value_cents": int(price * rnd.uniform(1.05, 1.5)),
                "cash_invested_cents": int(price * 0.25),
            }, tags=[t for t in [city if city != "Springfield" else "", "Section 8" if i % 9 == 0 else "",
                                 "Duplexes" if len(labels) == 2 else ""] if t], unit_labels=labels, reindex=False)
            property_ids.append(pid)
            for u in conn.execute("SELECT id FROM units WHERE property_id = ?", (pid,)):
                rent = rnd.randint(9, 19) * 100_00 + rnd.choice([0, 2500, 5000, 7500])
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
                                                       "phone": f"555-{rnd.randint(1000, 9999)}",
                                                       "email": None if k else f"tenant{n}@example.com"}, reindex=False)
                people.append((tid, "primary" if k == 0 else "co_tenant"))
            start = today - timedelta(days=rnd.randint(40, 1100))
            start = start.replace(day=1) if rnd.random() < .8 else start
            end = date(start.year + 1, start.month, 1) - timedelta(days=1) if start.day == 1 else start + timedelta(days=364)
            rent = market - rnd.choice([0, 0, 2500, 5000, 10000, 15000])
            lid = leases.create_lease(
                conn, unit_id=unit_id, tenants=people, start=start.isoformat(), end=end.isoformat(), rent_cents=rent,
                today=today, billing_start=max(start, billing_start).isoformat(), deposit_cents=rent,
                late_fee_type=rnd.choice(["flat", "flat", "flat", "percent"]), late_fee_flat_cents=5000,
                late_fee_percent_bp=500, late_fee_grace_days=5, move_in_date=start.isoformat())
            if end < today:
                conn.execute("UPDATE leases SET status = 'month_to_month' WHERE id = ?", (lid,))
            ledger.record_deposit(conn, lid, "received", rent, start.isoformat(), "Security deposit")
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
                ledger.record_payment(conn, lid, amount, max(when, due - timedelta(days=5)).isoformat(),
                                      rnd.choice(["check", "check", "bank_transfer", "app_transfer", "money_order"]),
                                      str(rnd.randint(1000, 9999)) if rnd.random() < .5 else None)

        # post late fees for past months; leave the current month in the review queue
        this_period = period_of(today)
        keys = [c.key for c in late_fees.find_candidates(conn, today) if c.period < this_period]
        late_fees.apply(conn, keys, "approve", today)

        # a tenant who gave notice, and one lease that ended with the deposit still held
        if len(lease_ids) > 10:
            leases.give_notice(conn, lease_ids[3][0], (today - timedelta(days=10)).isoformat(),
                               (today + timedelta(days=20)).isoformat())
            ended = lease_ids[5][0]
            charges = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM charges WHERE lease_id = ? AND voided_at IS NULL",
                                   (ended,)).fetchone()[0]
            paid = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE lease_id = ? AND voided_at IS NULL",
                                (ended,)).fetchone()[0]
            if charges > paid:
                ledger.record_payment(conn, ended, charges - paid, today.isoformat(), "check", "4410")
            leases.end_lease(conn, ended, (today - timedelta(days=12)).isoformat())

        # expenses over the last 12 months
        start_day = today - timedelta(days=365)
        for _ in range(420):
            name, _trade, cat = rnd.choice(VENDORS[:7] + VENDORS[8:9])
            d = start_day + timedelta(days=rnd.randint(0, 365))
            expenses.create_expense(conn, category_id=cats[cat], expense_date=d.isoformat(),
                                    amount_cents=rnd.randint(40, 900) * 100 + rnd.randint(0, 99),
                                    property_id=rnd.choice(property_ids), vendor_id=vendor_ids[name],
                                    payment_method=rnd.choice(["check", "card", "bank_transfer"]),
                                    description=rnd.choice(["Service call", "Leaky faucet", "Replaced outlet",
                                                            "Filter change", "Monthly service", "Materials",
                                                            "Clogged drain", "Smoke detector batteries"]))
        for pid in property_ids:
            for m in range(12):
                p = add_periods(period_of(today), -m)
                d = due_date(p, 10)
                if d > today:
                    continue
                if m % 3 == 0:
                    expenses.create_expense(conn, category_id=cats["Insurance"], expense_date=d.isoformat(),
                                            amount_cents=rnd.randint(250, 420) * 100, property_id=pid,
                                            vendor_id=vendor_ids["Keystone Insurance"], payment_method="autopay",
                                            description="Quarterly landlord policy premium")
            expenses.create_expense(conn, category_id=cats["Property taxes"],
                                    expense_date=due_date(add_periods(period_of(today), -rnd.randint(1, 10)), 1).isoformat(),
                                    amount_cents=rnd.randint(1800, 5200) * 100, property_id=pid,
                                    payment_method="check", description="Property tax installment")
        expenses.create_expense(conn, category_id=cats["Legal and professional fees"],
                                expense_date=(today - timedelta(days=100)).isoformat(), amount_cents=1_450_00,
                                description="Accountant — annual tax preparation")
        # a vacant-ready unit gets an upcoming lease
        vacant_unit = units[sorted(vacant)[0]][1]
        tid = tenants.save_tenant(conn, None, {"first_name": "Priya", "last_name": "Raman", "phone": "555-7777"},
                                  reindex=False)
        leases.create_lease(conn, unit_id=vacant_unit, tenants=[(tid, "primary")],
                            start=(today + timedelta(days=9)).isoformat(),
                            end=(today + timedelta(days=9 + 364)).isoformat(), rent_cents=units[sorted(vacant)[0]][2],
                            today=today, deposit_cents=units[sorted(vacant)[0]][2], late_fee_type="flat",
                            late_fee_flat_cents=5000)
        search.rebuild(conn)
    conn.close()
