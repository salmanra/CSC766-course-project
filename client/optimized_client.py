# -*- coding: utf-8 -*-
"""optimized_client.py — ToolIR Caption Benchmark: Optimized (Client-Side Memoization)

Same workflow as basic_client.py but with client-side result memoization.

Optimization applied: result caching keyed by (image_hash, model).
  - Before each call: check if (image_hash, model) is in cache.
  - If yes: return cached result immediately (0ms, no RPC).
  - If no:  call server, store result in cache.

For this benchmark (same image, same model, N repeats), only the FIRST call
hits the server. All subsequent calls are served from the local cache.

Usage:
    python client/optimized_client.py --image test_image.jpg --repeats 5
    python client/optimized_client.py --image test_image.jpg --repeats 5 --port 8765

Output per call:
    [call 1/5] latency=1243ms  caption="[clip] a scenic landscape..." [COMPUTED]
    [call 2/5] latency=0ms     caption="[clip] a scenic landscape..." [CACHED]
    ...
    === Summary ===
    Total latency: 1243ms  (vs ~6234ms basic)
    RPC calls: 1           (vs 5 basic)
    Speedup: 5.0x
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
from typing import Any, Dict, List, Optional, Tuple

import requests

# Make profiler_utils importable from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from profiler_utils import (
    append_jsonl,
    build_exec_op_record,
    guess_bytes,
    now_ms,
    obj_id_from_str,
)

# NOTE: append_jsonl, build_exec_op_record, guess_bytes, obj_id_from_str are
# used only by emit_cache_hit_record. Real RPC calls are logged server-side.

DEFAULT_PORT = 8765
EXEC_OPS_LOG = os.path.join(
    os.path.dirname(__file__), "..", "profiler_logs", "caption_exec_ops.jsonl"
)
EXEC_OPS_LOG = os.path.normpath(EXEC_OPS_LOG)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def image_hash(image_b64: str) -> str:
    return hashlib.sha256(image_b64.encode("utf-8")).hexdigest()[:12]


def cache_key(image_b64: str, model: str) -> str:
    """Stable cache key from (image content, model name)."""
    h = hashlib.sha256(image_b64.encode("utf-8")).hexdigest()
    return f"{h}:{model}"


# ---------------------------------------------------------------------------
# RPC with EXEC_OP logging (for real server calls)
# ---------------------------------------------------------------------------

def call_caption_rpc(
    *,
    base_url: str,
    image_b64: str,
    model: str,
    trace_id: str,
    node_id: str,
    timeout: int,
) -> Tuple[str, int]:
    """Call POST /caption/interrogate and return (caption, latency_ms).

    The server emits its own EXEC_OP record for each real call, so this
    function does NOT emit a duplicate client-side record. Only cache hits
    (zero-latency, no RPC) need client-side records (see emit_cache_hit_record).
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


def emit_cache_hit_record(
    *,
    trace_id: str,
    node_id: str,
    image_b64: str,
    caption: str,
) -> None:
    """Emit an EXEC_OP record for a cache hit (latency ~0ms, no payload transfer)."""
    t = now_ms()
    record = build_exec_op_record(
        trace_id=trace_id,
        op="tool.caption_interrogate",
        node_id=node_id,
        args_hash=obj_id_from_str(image_b64, kind="b64img"),
        inputs_meta={
            "image": {
                "id": obj_id_from_str(image_b64, kind="b64img"),
                "bytes": 0,    # not transferred — served from cache
                "type": "base64_image",
            }
        },
        outputs_meta={
            "caption": {
                "id": obj_id_from_str(caption, kind="txt"),
                "bytes": guess_bytes(caption),
                "type": "str",
            }
        },
        t_start_ms=t,
        t_end_ms=t,
        payload_in_bytes=0,
        payload_out_bytes=guess_bytes(caption),
        status_code=200,
        extra={"cache_hit": True},
    )
    append_jsonl(EXEC_OPS_LOG, record)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Caption benchmark optimized client (client-side memoization)"
    )
    parser.add_argument("--image", required=True, help="Path to input image file")
    parser.add_argument("--repeats", type=int, default=5, help="Number of calls")
    parser.add_argument("--model", default="clip")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    print(f"TRACE_ID: {trace_id}  (optimized, client-side memoization)")
    print(f"Image:    {args.image}")
    print(f"Repeats:  {args.repeats}")
    print(f"Server:   {base_url}")
    print()

    image_b64 = load_image_b64(args.image)
    img_hash = image_hash(image_b64)
    print(f"Image hash: {img_hash}")
    print()

    # Client-side memo cache: key = (image_hash, model), value = caption
    cache: Dict[str, str] = {}

    rpc_calls = 0
    latencies: List[int] = []
    basic_latency_estimate: Optional[int] = None  # used for speedup display

    t_wall_start = now_ms()
    for i in range(args.repeats):
        node_id = f"interrogate_{i + 1}"
        key = cache_key(image_b64, args.model)

        if key in cache:
            # Cache hit: no RPC, no data transfer.
            # Emit a client-side EXEC_OP with payload_in_bytes=0 and cache_hit=True
            # so the trace shows that this slot was served locally.
            caption = cache[key]
            latency_ms = 0
            emit_cache_hit_record(
                trace_id=trace_id,
                node_id=node_id,
                image_b64=image_b64,
                caption=caption,
            )
            tag = "[CACHED]"
        else:
            # Cache miss: call server (server emits its own EXEC_OP record).
            # Do NOT emit a duplicate client-side record for real calls —
            # the server record is the authoritative one.
            caption, latency_ms = call_caption_rpc(
                base_url=base_url,
                image_b64=image_b64,
                model=args.model,
                trace_id=trace_id,
                node_id=node_id,
                timeout=args.timeout,
            )
            cache[key] = caption
            rpc_calls += 1
            basic_latency_estimate = latency_ms  # store for speedup estimate
            tag = "[COMPUTED]"

        latencies.append(latency_ms)
        print(f"[call {i+1}/{args.repeats}] latency={latency_ms}ms  "
              f"caption=\"{caption[:60]}\" {tag}")
    t_wall_end = now_ms()

    total_latency = t_wall_end - t_wall_start

    print()
    print("=== Summary ===")
    print(f"Total latency: {total_latency}ms")
    print(f"RPC calls:     {rpc_calls}  (vs {args.repeats} basic)")

    if basic_latency_estimate and rpc_calls > 0:
        estimated_basic = basic_latency_estimate * args.repeats
        speedup = estimated_basic / max(total_latency, 1)
        print(f"Speedup:       {speedup:.1f}x  "
              f"(estimated basic ~{estimated_basic}ms)")

    print(f"Exec ops written to: {EXEC_OPS_LOG}")


if __name__ == "__main__":
    main()
