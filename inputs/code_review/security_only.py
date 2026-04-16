"""Security-only issues: the real verdict is request_changes.

Guard should predict request_changes (pickle.loads is in the regex list).
"""

import pickle


def load_session(blob: bytes):
    return pickle.loads(blob)


def evaluate_expression(expr: str):
    return eval(expr)


if __name__ == "__main__":
    data = b""
    print(load_session(data))
