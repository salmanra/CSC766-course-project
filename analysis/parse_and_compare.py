# -*- coding: utf-8 -*-
"""parse_and_compare.py — ToolIR Trace Analysis

Reads ../profiler_logs/caption_exec_ops.jsonl and performs a simple
ToolIR pre-pass analysis over the recorded traces.

For each trace the analysis reports:
  - Number of nodes (EXEC_OP records)
  - Total latency
  - Total data transferred
  - Detected optimization opportunities

Then prints a side-by-side comparison of the basic vs. optimized traces.

Usage:
    python analysis/parse_and_compare.py
    python analysis/parse_and_compare.py --log ../profiler_logs/caption_exec_ops.jsonl
"""

import argparse
import collections
import json
import os
import sys
from typing import Any, Dict, List

EXEC_OPS_LOG = os.path.join(
    os.path.dirname(__file__), "..", "profiler_logs", "caption_exec_ops.jsonl"
)
EXEC_OPS_LOG = os.path.normpath(EXEC_OPS_LOG)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def load_records(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"ERROR: log file not found: {path}")
        print("Run the basic and optimized clients first.")
        sys.exit(1)
    records = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"WARNING: skipping malformed line {lineno}: {exc}")
    return records


def group_by_trace(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in records:
        if r.get("kind") == "EXEC_OP":
            tid = r.get("trace_id", "tr_unknown")
            groups[tid].append(r)
    return dict(groups)


# ---------------------------------------------------------------------------
# Per-trace analysis
# ---------------------------------------------------------------------------

def analyze_trace(trace_id: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run a simple pre-pass over one trace's EXEC_OP records."""
    total_latency_ms = sum(n.get("latency_ms", 0) for n in nodes)
    total_payload_in = sum(n.get("payload_in_bytes", 0) or 0 for n in nodes)
    total_payload_out = sum(n.get("payload_out_bytes", 0) or 0 for n in nodes)
    total_bytes = total_payload_in + total_payload_out

    # Detect repeated invocations (same op + same args_hash)
    op_arg_counts: Dict[str, int] = collections.Counter(
        f"{n.get('op')}:{n.get('args_hash', 'none')}"
        for n in nodes
    )
    repeated_invocations = {k: v for k, v in op_arg_counts.items() if v > 1}

    # Count cache hits (client-side records with cache_hit=True)
    cache_hits = sum(
        1 for n in nodes
        if n.get("extra", {}) and n["extra"].get("cache_hit", False)
    )

    # Real RPC calls = total nodes minus cache-hit records
    rpc_calls = len(nodes) - cache_hits

    opportunities = []
    if repeated_invocations:
        for key, count in repeated_invocations.items():
            op_name = key.split(":")[0]
            opportunities.append(
                f"Repeated invocations: {count} calls with same (op, args_hash) "
                f"for '{op_name}'\n"
                f"          → Memoization candidate"
            )

    return {
        "trace_id": trace_id,
        "node_count": len(nodes),
        "rpc_calls": rpc_calls,
        "cache_hits": cache_hits,
        "total_latency_ms": total_latency_ms,
        "total_bytes": total_bytes,
        "total_payload_in": total_payload_in,
        "total_payload_out": total_payload_out,
        "opportunities": opportunities,
    }


def fmt_bytes(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1024 ** 2:
        return f"{b/1024:.1f}KB"
    return f"{b/1024**2:.2f}MB"


# ---------------------------------------------------------------------------
# Heuristic: classify basic vs optimized
# ---------------------------------------------------------------------------

def classify_traces(
    trace_analyses: List[Dict[str, Any]]
) -> tuple:
    """Return (basic, optimized).

    The optimized trace has at least one cache-hit node (payload_in_bytes=0,
    extra.cache_hit=True). The basic trace has none.
    If ambiguous, fall back to: more real-RPC nodes → basic.
    """
    if len(trace_analyses) == 0:
        return None, None
    if len(trace_analyses) == 1:
        return trace_analyses[0], None

    optimized_candidates = [t for t in trace_analyses if t["cache_hits"] > 0]
    basic_candidates = [t for t in trace_analyses if t["cache_hits"] == 0]

    if optimized_candidates and basic_candidates:
        return basic_candidates[0], optimized_candidates[0]

    # Fallback: more nodes → basic
    sorted_traces = sorted(trace_analyses, key=lambda t: -t["node_count"])
    return sorted_traces[0], sorted_traces[-1]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ToolIR trace analyzer")
    parser.add_argument(
        "--log",
        default=EXEC_OPS_LOG,
        help="Path to caption_exec_ops.jsonl",
    )
    args = parser.parse_args()

    records = load_records(args.log)
    by_trace = group_by_trace(records)

    if not by_trace:
        print("No EXEC_OP records found in log.")
        sys.exit(1)

    analyses = [analyze_trace(tid, nodes) for tid, nodes in by_trace.items()]

    print("=" * 60)
    print("=== ToolIR Analysis ===")
    print("=" * 60)
    print(f"Log file: {args.log}")
    print(f"Traces found: {len(analyses)}")
    print()

    basic, optimized = classify_traces(analyses)

    def print_trace(label: str, a: Dict[str, Any]) -> None:
        print(f"{label} trace ({a['trace_id']}):")
        print(f"  Nodes (total):          {a['node_count']}  "
              f"(RPC={a['rpc_calls']}, cache_hit={a['cache_hits']})")
        print(f"  Total latency:          {a['total_latency_ms']}ms")
        print(f"  Total data transferred: {fmt_bytes(a['total_bytes'])}")
        if a["opportunities"]:
            print("  Optimization opportunities detected:")
            for opp in a["opportunities"]:
                for line in opp.split("\n"):
                    print(f"    - {line}")
        else:
            print("  No repeated-invocation opportunities detected.")
        print()

    if basic:
        print_trace("Basic", basic)
    if optimized and optimized is not basic:
        print_trace("Optimized", optimized)

    # Comparison
    if basic and optimized and optimized is not basic:
        lat_basic = max(basic["total_latency_ms"], 1)
        lat_opt = max(optimized["total_latency_ms"], 1)
        bytes_basic = max(basic["total_bytes"], 1)
        bytes_opt = max(optimized["total_bytes"], 1)
        lat_reduction = (1 - lat_opt / lat_basic) * 100
        bytes_reduction = (1 - bytes_opt / bytes_basic) * 100
        rpc_basic = max(basic["rpc_calls"], 1)
        rpc_opt = optimized["rpc_calls"]
        rpc_reduction = (1 - rpc_opt / rpc_basic) * 100

        print("Improvement:")
        print(f"  Latency reduction:  {lat_reduction:.1f}%  "
              f"({basic['total_latency_ms']}ms → {optimized['total_latency_ms']}ms)")
        print(f"  RPC reduction:      {rpc_reduction:.1f}%  "
              f"({rpc_basic} RPC calls → {rpc_opt} RPC calls)")
        print(f"  Data reduction:     {bytes_reduction:.1f}%  "
              f"({fmt_bytes(bytes_basic)} → {fmt_bytes(bytes_opt)})")
        print()

    # Always print opportunity summary
    all_opps = [opp for a in analyses for opp in a["opportunities"]]
    if all_opps:
        print("Memoization opportunity confirmed in basic trace.")
    else:
        print("(No memoization opportunities found — "
              "did the basic client run yet?)")


if __name__ == "__main__":
    main()
