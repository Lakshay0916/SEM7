"""A small calculator library used as the controlled demo target."""


def add(a, b):
    """Return the sum of a and b."""
    return a + b


def subtract(a, b):
    """Return a minus b."""
    return a - b


def multiply(a, b):
    """Return the product of a and b."""
    return a * b


def divide(a, b):
    """Return a divided by b (true division).

    Raises ValueError when b is zero.
    """
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b


def average(values):
    """Return the arithmetic mean of a non-empty list of numbers."""
    if not values:
        raise ValueError("average() requires at least one value")
    return sum(values) / len(values)
