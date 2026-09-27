import pytest
from hypothesis import given, strategies as st

from rental_tracker.domain.money import cents_to_input, format_money, parse_money, percent_of, round_div


@pytest.mark.parametrize("text,cents", [
    ("1250", 125000), ("1,250.50", 125050), ("₱1250.5", 125050), ("PHP 1,250.50", 125050), ("php1250", 125000),
    ("$1250.5", 125050), (" 0.07 ", 7),
    (".5", 50), ("-12", -1200), ("(12.00)", -1200), ("₱-3.10", -310), ("0", 0),
])
def test_parse_money(text, cents):
    assert parse_money(text) == cents


@pytest.mark.parametrize("bad", ["", "  ", "abc", "1.234", "1e5", "12..0", "--1", None, "99999999999999999999",
                                 "10000000000.01"])
def test_parse_money_rejects(bad):
    with pytest.raises(ValueError):
        parse_money(bad)


def test_format_money():
    assert format_money(125050) == "₱1,250.50"
    assert format_money(-7) == "-₱0.07"
    assert format_money(0) == "₱0.00"


@given(st.integers(min_value=-10**12, max_value=10**12))
def test_round_trip(cents):
    assert parse_money(cents_to_input(cents)) == cents
    assert parse_money(format_money(cents)) == cents


def test_round_div_half_up():
    assert round_div(5, 2) == 3
    assert round_div(4, 3) == 1
    assert round_div(-5, 2) == -3
    assert percent_of(145000, 500) == 7250
    assert percent_of(99, 500) == 5  # 4.95 -> 5
