"""Basic descriptive statistics for lists of numbers."""
from collections import Counter


def mean(values):
    """Arithmetic mean. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("mean() of an empty list")
    return sum(values) / len(values)


def median(values):
    """Middle value of the sorted list. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("median() of an empty list")
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle]


def mode(values):
    """Most common value; ties go to the value seen first. Raises ValueError for an empty list."""
    if not values:
        raise ValueError("mode() of an empty list")
    return Counter(values).most_common(1)[0][0]
