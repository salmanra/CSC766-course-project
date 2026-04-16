"""Lint issues only — no security smell.

Guard should predict approve (no eval/pickle/TODO, under 200 LOC),
but ruff flags unused imports + redefinitions, so the real verdict is
request_changes. This case measures guard false-negatives.
"""

import os
import sys
import json
import math


def compute(value):
    result = value * 2
    result = value * 2
    return result


def another(value):
    x = 1
    x = 2
    return x + value


if __name__ == "__main__":
    print(compute(3))
