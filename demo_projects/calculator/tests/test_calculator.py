import pytest

from calculator import add, average, divide, multiply, subtract


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, -4) == -5


def test_subtract():
    assert subtract(10, 4) == 6


def test_multiply():
    assert multiply(3, 4) == 12


def test_divide():
    assert divide(7, 2) == 3.5


def test_divide_negative():
    assert divide(-7, 2) == -3.5


def test_divide_by_zero():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_average():
    assert average([1, 2, 3, 4]) == 2.5


def test_average_empty():
    with pytest.raises(ValueError):
        average([])
