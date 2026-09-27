"""Catch-up jobs that run when the app opens and once a day after (BLUEPRINT §11.1)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from ..db import transaction
from . import late_fees, leases, portfolio, rent_posting


@dataclass
class CatchUpResult:
    activated: int = 0
    rolled_to_month_to_month: int = 0
    rent_posted: int = 0
    rent_amount_cents: int = 0
    late_fees_posted: int = 0
    units_split: int = 0

    def summary(self) -> str:
        parts = []
        if self.rent_posted:
            parts.append(f"billed {self.rent_posted} rent/recurring charge{'s' if self.rent_posted != 1 else ''}")
        if self.activated:
            parts.append(f"started {self.activated} lease{'s' if self.activated != 1 else ''}")
        if self.rolled_to_month_to_month:
            parts.append(f"moved {self.rolled_to_month_to_month} expired lease(s) to month-to-month")
        if self.units_split:
            parts.append(f"made {self.units_split} separate unit{'s' if self.units_split != 1 else ''} "
                         "from properties that had several")
        if self.late_fees_posted:
            parts.append(f"posted {self.late_fees_posted} late fee(s)")
        return "; ".join(parts)


def run_catch_up(conn: sqlite3.Connection, today: date) -> CatchUpResult:
    result = CatchUpResult()
    with transaction(conn):
        result.units_split = portfolio.flatten_units(conn)
        result.activated = leases.activate_due(conn, today)
        result.rolled_to_month_to_month = leases.roll_expired(conn, today)
        posted = rent_posting.post_rent(conn, today)
        result.rent_posted, result.rent_amount_cents = posted.posted, posted.amount_cents
        result.late_fees_posted = late_fees.auto_post(conn, today)
    return result
