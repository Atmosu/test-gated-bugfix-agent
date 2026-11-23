import pytest

from textstats import mean, median, mode


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd_length():
    assert median([3, 1, 2]) == 2
    assert median([5]) == 5


def test_mode():
    assert mode([1, 2, 2, 3]) == 2


@pytest.mark.parametrize("fn", [mean, median, mode])
def test_empty_raises(fn):
    with pytest.raises(ValueError):
        fn([])
