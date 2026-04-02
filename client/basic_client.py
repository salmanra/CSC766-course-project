# -*- coding: utf-8 -*-
"""basic_client.py — ToolIR Caption Benchmark: Basic (No Optimization)

Calls POST /caption/interrogate N times on the same image.
No caching. Every call goes to the server even though the image never changes.

This is the INEFFICIENT baseline: identical computation is repeated N times,
wasting network bandwidth and server compute.

Usage:
    python client/basic_client.py --image test_image.jpg --repeats 5
    python client/basic_client.py --image test_image.jpg --repeats 5 --port 8765

Output per call:
    [call 1/5] latency=1243ms  caption="[clip] a scenic landscape..."
    [call 2/5] latency=1251ms  caption="[clip] a scenic landscape..."
    ...
    === Summary ===
    Total latency: 6234ms
    RPC calls: 5
    Exec ops written to: ../profiler_logs/caption_exec_ops.jsonl
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Tuple

import requests

# Make profiler_utils importable from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from profiler_utils import now_ms

DEFAULT_PORT = 8765
EXEC_OPS_LOG = os.path.join(
    os.path.dirname(__file__), "..", "profiler_logs", "caption_exec_ops.jsonl"
)
EXEC_OPS_LOG = os.path.normpath(EXEC_OPS_LOG)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_image_b64(path: str) -> str:
    """Read an image file and return its base64 encoding."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def image_hash(image_b64: str) -> str:
    """Return a short sha256 hash of the image for display purposes."""
    return hashlib.sha256(image_b64.encode("utf-8")).hexdigest()[:12]


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                            default=str) + "\n")


def call_caption(
    *,
    base_url: str,
    image_b64: str,
    model: str,
    trace_id: str,
    node_id: str,
    timeout: int,
) -> Tuple[str, int]:
    """Call POST /caption/interrogate and return (caption, latency_ms).

    Every call sends the full image to the server — no caching.
    """
    url = f"{base_url}/caption/interrogate"
    payload = {
        "image": image_b64,
        "model": model,
        "trace_id": trace_id,
        "node_id": node_id,
    }

    t0 = now_ms()
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        caption = data.get("caption", "")
    except Exception as exc:
        raise RuntimeError(f"RPC failed: {exc}") from exc
    t1 = now_ms()

    return caption, t1 - t0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Caption benchmark basic client (no caching)"
    )
    parser.add_argument("--image", required=True, help="Path to input image file")
    parser.add_argument("--repeats", type=int, default=5, help="Number of RPC calls")
    parser.add_argument("--model", default="clip")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    print(f"TRACE_ID: {trace_id}  (basic, no caching)")
    print(f"Image:    {args.image}")
    print(f"Repeats:  {args.repeats}")
    print(f"Server:   {base_url}")
    print()

    # Load image once; send it on every call (baseline inefficiency)
    image_b64 = load_image_b64(args.image)
    img_hash = image_hash(image_b64)
    print(f"Image hash: {img_hash} (same hash every repeat → memoization opportunity)")
    print()

    latencies: List[int] = []
    captions: List[str] = []

    t_wall_start = now_ms()
    for i in range(args.repeats):
        node_id = f"interrogate_{i + 1}"
        caption, latency_ms = call_caption(
            base_url=base_url,
            image_b64=image_b64,
            model=args.model,
            trace_id=trace_id,
            node_id=node_id,
            timeout=args.timeout,
        )
        latencies.append(latency_ms)
        captions.append(caption)
        print(f"[call {i+1}/{args.repeats}] latency={latency_ms}ms  "
              f"caption=\"{caption[:60]}\"")
    t_wall_end = now_ms()

    total_latency = t_wall_end - t_wall_start
    print()
    print("=== Summary ===")
    print(f"Total latency: {total_latency}ms")
    print(f"RPC calls:     {args.repeats}")
    print(f"Exec ops written to: {EXEC_OPS_LOG}")


if __name__ == "__main__":
    main()
