from datetime import date

from rental_tracker.domain.rent import plan_charges, rent_in_effect


def test_rent_in_effect():
    changes = [(date(2026, 6, 1), 145000), (date(2027, 6, 1), 150000)]
    assert rent_in_effect(140000, changes, date(2026, 5, 31)) == 140000
    assert rent_in_effect(140000, changes, date(2026, 6, 1)) == 145000
    assert rent_in_effect(140000, changes, date(2028, 1, 1)) == 150000


def test_plan_prorates_first_and_last_month():
    plan = list(plan_charges(start=date(2026, 1, 15), end=date(2026, 3, 10), due_day=1,
                             base_cents=100000, through=date(2026, 12, 31)))
    assert [p.period for p in plan] == ["2026-01", "2026-02", "2026-03"]
    assert plan[0].due_date == date(2026, 1, 15)  # partial first month due at move-in
    assert plan[0].amount_cents == 54839 and plan[0].prorated_days == 17
    assert plan[1].amount_cents == 100000 and plan[1].prorated_days is None
    assert plan[2].amount_cents == 32258  # 10/31


def test_plan_stops_at_through_and_resumes():
    kw = dict(start=date(2026, 1, 1), end=None, due_day=5, base_cents=1000)
    first = list(plan_charges(**kw, through=date(2026, 3, 4)))
    assert [p.period for p in first] == ["2026-01", "2026-02"]
    rest = list(plan_charges(**kw, from_period="2026-03", through=date(2026, 3, 5)))
    assert [p.period for p in rest] == ["2026-03"]


def test_plan_uses_rent_changes_and_no_proration_option():
    plan = list(plan_charges(start=date(2026, 5, 20), end=None, due_day=1, base_cents=1000,
                             changes=[(date(2026, 7, 1), 1200)], prorate_partial=False,
                             through=date(2026, 7, 1)))
    assert [(p.period, p.amount_cents) for p in plan] == [("2026-05", 1000), ("2026-06", 1000), ("2026-07", 1200)]
