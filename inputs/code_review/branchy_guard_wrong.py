"""Clean code with a misleading comment — forces a guard rollback.

The comment mentions ``eval()`` and has a ``# TODO`` marker, so the cheap
regex guard predicts request_changes. The actual code is clean and ruff +
bandit both say approve, so the optimized client must roll back the
speculative summarize call. This file exists to measure rollback cost.
"""

from typing import List


# TODO: refactor the old eval() legacy path when the new parser ships.
def normalize(values: List[int]) -> List[int]:
    return [v for v in values if v is not None]


def summarize(values: List[int]) -> int:
    return sum(normalize(values))


if __name__ == "__main__":
    print(summarize([1, 2, 3]))
