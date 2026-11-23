"""Calendar helpers used for delivery estimates."""

DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def is_leap_year(year):
    """Gregorian leap year rule."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    """Number of days in a month; month is 1 to 12."""
    if not 1 <= month <= 12:
        raise ValueError("month must be 1 to 12")
    if month == 2 and is_leap_year(year):
        return 29
    return DAYS[month - 1]
