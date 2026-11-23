from shopkit.dates import days_in_month, is_leap_year


def test_leap_years():
    assert is_leap_year(2024)
    assert not is_leap_year(2023)


def test_days_in_month():
    assert days_in_month(2023, 4) == 30
    assert days_in_month(2024, 2) == 29
