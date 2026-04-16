"""A tidy utility module with no lint or security issues."""

from typing import Iterable


def sum_even(values: Iterable[int]) -> int:
    """Return the sum of the even integers in ``values``."""
    return sum(v for v in values if v % 2 == 0)


def count_positive(values: Iterable[int]) -> int:
    """Return the number of strictly positive integers in ``values``."""
    return sum(1 for v in values if v > 0)


if __name__ == "__main__":
    sample = [1, 2, 3, 4, 5, 6]
    print(f"sum_even={sum_even(sample)} count_positive={count_positive(sample)}")
