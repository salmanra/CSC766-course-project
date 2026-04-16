"""Security issue the cheap guard misses.

Uses hashlib.md5 (bandit flags it as a weak hash), but the guard's regexes
only look for eval/pickle/TODO, so it predicts approve. The real verdict is
request_changes — a guard miss (not a rollback, since approve was the
speculative path).
"""

import hashlib


def fingerprint(data: bytes) -> str:
    digest = hashlib.md5(data).hexdigest()
    return digest[:12]


def batch_fingerprint(items: list[bytes]) -> list[str]:
    return [fingerprint(x) for x in items]


if __name__ == "__main__":
    print(fingerprint(b"hello"))
