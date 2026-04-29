# -*- coding: utf-8 -*-
"""parse_and_compare.py — analyze code_review EXEC_OP traces.

Reads ``profiler_logs/code_review_exec_ops.jsonl``, groups records by
trace_id, classifies each trace as basic vs optimized via the
``tool.client_mode`` marker, then reports:

  * End-to-end latency (mean ± std over all runs per mode)
  * RPC call count per mode
  * Payload bytes transferred (in + out)
  * Per-optimization savings:
      O1 round-trip        — ast_json bytes eliminated from lint/scan requests
      O2 dead-output       — full_report_text + cwe_refs bytes saved
      O3 redundant-inv     — duplicate parse calls replaced with cache hits
      O4 speculation       — hit / rollback counts, guard accuracy, guard cost
  * A speedup breakdown table

Usage
-----

    python analysis/code_review/parse_and_compare.py
    python analysis/code_review/parse_and_compare.py --log path/to/log.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple


EXEC_OPS_LOG = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..",
                 "profiler_logs", "code_review_exec_ops.jsonl")
)


# ---------------------------------------------------------------------------
# Load + group
# ---------------------------------------------------------------------------

def load_records(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"ERROR: log not found: {path}", file=sys.stderr)
        print("Run basic_client.py and optimized_client.py first.", file=sys.stderr)
        sys.exit(1)
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"WARNING: bad line {lineno}: {exc}", file=sys.stderr)
    return out


def group_by_trace(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in records:
        if r.get("kind") != "EXEC_OP":
            continue
        groups[r.get("trace_id", "tr_unknown")].append(r)
    return dict(groups)


def _extra(r: Dict[str, Any]) -> Dict[str, Any]:
    return r.get("extra") or {}


def classify_trace(nodes: List[Dict[str, Any]]) -> Optional[str]:
    for n in nodes:
        if n.get("op") == "tool.client_mode":
            mode = _extra(n).get("client_mode")
            if mode in ("basic", "optimized"):
                return mode
    return None


# ---------------------------------------------------------------------------
# Per-trace metrics
# ---------------------------------------------------------------------------

def analyze_trace(trace_id: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Client-visible RPCs — what the client actually issued to the tool cluster.
    # parse_fetch is a server-to-server call introduced by the O1 optimization;
    # we track it separately so it does not inflate the optimized RPC count.
    client_rpc_ops = {"tool.parse", "tool.lint", "tool.scan", "tool.summarize"}
    server_internal_ops = {"tool.parse_fetch"}
    rpc_nodes = [n for n in nodes
                 if n.get("op") in client_rpc_ops
                 and not _extra(n).get("cache_hit")]
    server_rpc_nodes = [n for n in nodes
                        if n.get("op") in server_internal_ops]
    cache_hits = [n for n in nodes if _extra(n).get("cache_hit")]
    spec_hits = [n for n in nodes if _extra(n).get("speculation_hit")]
    rollbacks = [n for n in nodes if _extra(n).get("rollback")]

    # End-to-end wall-clock = (max t_end) - (min t_start) excluding the
    # client-mode marker (which has zero latency but is a valid bound).
    starts = [n.get("t_start_ms", 0) for n in nodes if n.get("t_start_ms")]
    ends = [n.get("t_end_ms", 0) for n in nodes if n.get("t_end_ms")]
    wall_ms = (max(ends) - min(starts)) if starts and ends else 0

    payload_in = sum((n.get("payload_in_bytes") or 0) for n in rpc_nodes)
    payload_out = sum((n.get("payload_out_bytes") or 0) for n in rpc_nodes)

    op_counts = collections.Counter(n.get("op", "?") for n in rpc_nodes)

    # Redundant invocation: same (op, args_hash) appearing > 1 in this trace.
    by_op_args: Dict[str, int] = collections.Counter(
        f"{n.get('op')}:{n.get('args_hash', 'none')}" for n in rpc_nodes
    )
    redundant = {k: v for k, v in by_op_args.items() if v > 1}

    # Dead-output bytes: for lint/scan, payload_out includes dropped fields in
    # basic mode and not in optimized. We can't subtract directly from a single
    # trace — we compare across modes below.
    lint_out = sum((n.get("payload_out_bytes") or 0) for n in rpc_nodes
                   if n.get("op") == "tool.lint")
    scan_out = sum((n.get("payload_out_bytes") or 0) for n in rpc_nodes
                   if n.get("op") == "tool.scan")
    # Round-trip bytes on lint/scan requests
    lint_in = sum((n.get("payload_in_bytes") or 0) for n in rpc_nodes
                  if n.get("op") == "tool.lint")
    scan_in = sum((n.get("payload_in_bytes") or 0) for n in rpc_nodes
                  if n.get("op") == "tool.scan")

    # Guard cost (only present in optimized traces).
    guard_ms_vals = [int(_extra(n).get("guard_ms", 0) or 0)
                     for n in (spec_hits + rollbacks)]
    tokens_wasted = sum(int(_extra(n).get("speculative_tokens_wasted", 0) or 0)
                        for n in rollbacks)
    # Per-trace rollback cost — the wall-clock of the redo summarize call,
    # measured by the optimized client and emitted on rollback events. This
    # is the honest cost the speculation policy paid that the basic policy
    # did not, and is used in place of the old cross-trace mean difference.
    rollback_cost_vals = [int(_extra(n).get("rollback_cost_ms", 0) or 0)
                          for n in rollbacks
                          if _extra(n).get("rollback_cost_ms") is not None]

    return {
        "trace_id": trace_id,
        "node_count": len(nodes),
        "rpc_calls": len(rpc_nodes),
        "server_internal_rpcs": len(server_rpc_nodes),
        "cache_hits": len(cache_hits),
        "wall_ms": wall_ms,
        "payload_in_bytes": payload_in,
        "payload_out_bytes": payload_out,
        "total_bytes": payload_in + payload_out,
        "op_counts": dict(op_counts),
        "redundant": redundant,
        "lint_in_bytes": lint_in,
        "lint_out_bytes": lint_out,
        "scan_in_bytes": scan_in,
        "scan_out_bytes": scan_out,
        "speculation_hits": len(spec_hits),
        "rollbacks": len(rollbacks),
        "guard_ms_values": guard_ms_vals,
        "rollback_cost_values": rollback_cost_vals,
        "speculative_tokens_wasted": tokens_wasted,
    }


# ---------------------------------------------------------------------------
# Aggregate per mode
# ---------------------------------------------------------------------------

def aggregate(mode_traces: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not mode_traces:
        return {}

    def mean_std(vals: List[float]) -> Tuple[float, float]:
        if not vals:
            return 0.0, 0.0
        if len(vals) == 1:
            return float(vals[0]), 0.0
        return statistics.mean(vals), statistics.pstdev(vals)

    wall_mean, wall_std = mean_std([t["wall_ms"] for t in mode_traces])
    bytes_mean, bytes_std = mean_std([t["total_bytes"] for t in mode_traces])
    rpc_mean, _ = mean_std([t["rpc_calls"] for t in mode_traces])

    total_hits = sum(t["speculation_hits"] for t in mode_traces)
    total_rollbacks = sum(t["rollbacks"] for t in mode_traces)
    total_guard_runs = total_hits + total_rollbacks
    guard_ms_vals: List[int] = []
    rollback_cost_vals: List[int] = []
    for t in mode_traces:
        guard_ms_vals.extend(t["guard_ms_values"])
        rollback_cost_vals.extend(t.get("rollback_cost_values", []))
    guard_mean, _ = mean_std(guard_ms_vals)
    rollback_cost_mean, rollback_cost_std = mean_std(rollback_cost_vals)
    if rollback_cost_vals:
        rollback_cost_median = statistics.median(rollback_cost_vals)
        if len(rollback_cost_vals) >= 4:
            qs = statistics.quantiles(rollback_cost_vals, n=4)
            rollback_cost_iqr = qs[2] - qs[0]
        else:
            rollback_cost_iqr = max(rollback_cost_vals) - min(rollback_cost_vals)
    else:
        rollback_cost_median = 0.0
        rollback_cost_iqr = 0.0

    return {
        "trace_count": len(mode_traces),
        "wall_mean": wall_mean,
        "wall_std": wall_std,
        "bytes_mean": bytes_mean,
        "bytes_std": bytes_std,
        "rpc_mean": rpc_mean,
        "total_rpc": sum(t["rpc_calls"] for t in mode_traces),
        "total_server_internal_rpc": sum(t["server_internal_rpcs"]
                                          for t in mode_traces),
        "total_cache_hits": sum(t["cache_hits"] for t in mode_traces),
        "total_redundant": sum(len(t["redundant"]) for t in mode_traces),
        "lint_in": sum(t["lint_in_bytes"] for t in mode_traces),
        "lint_out": sum(t["lint_out_bytes"] for t in mode_traces),
        "scan_in": sum(t["scan_in_bytes"] for t in mode_traces),
        "scan_out": sum(t["scan_out_bytes"] for t in mode_traces),
        "spec_hits": total_hits,
        "rollbacks": total_rollbacks,
        "hit_rate": (total_hits / total_guard_runs) if total_guard_runs else 0.0,
        "guard_ms_mean": guard_mean,
        "rollback_cost_values": rollback_cost_vals,
        "rollback_cost_mean": rollback_cost_mean,
        "rollback_cost_std": rollback_cost_std,
        "rollback_cost_median": rollback_cost_median,
        "rollback_cost_iqr": rollback_cost_iqr,
        "rollback_cost_n": len(rollback_cost_vals),
        "tokens_wasted": sum(t["speculative_tokens_wasted"] for t in mode_traces),
    }


# ---------------------------------------------------------------------------
# Always-expensive (synthetic) baseline
# ---------------------------------------------------------------------------

def compute_always_expensive(
    opt_traces: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Synthesize an "always-expensive" baseline from optimized trace data.

    The always-expensive case models a speculation policy that always rolls
    back — i.e., always pays the cost of issuing a real summarize call after
    the speculative one. We compute the per-trace synthetic wall as:

      * rollback trace  : wall_ms  (it already paid the rollback cost)
      * hit trace       : wall_ms + rollback_cost (it would have paid it)

    ``rollback_cost`` is measured directly from rollback traces by the
    optimized client (``rollback_cost_ms`` extra on each rollback EXEC_OP)
    — i.e., the wall-clock duration of the redo summarize call. This is an
    honest per-trace measurement, not a cross-trace mean of total wall
    times (which is confounded by source-file size). No clipping at zero;
    if the measurement is small or zero on a small sample, that's reported
    as-is.

    The returned dict has the same shape as ``aggregate(...)`` so
    ``print_mode(...)`` can display it without changes.
    """
    if not opt_traces:
        return {}

    base = aggregate(opt_traces)
    rollback_cost = base.get("rollback_cost_mean", 0.0) or 0.0

    synth_walls = [
        t["wall_ms"] + (rollback_cost if t["speculation_hits"] > 0 else 0)
        for t in opt_traces
    ]
    if len(synth_walls) > 1:
        wall_mean = statistics.mean(synth_walls)
        wall_std = statistics.pstdev(synth_walls)
    elif synth_walls:
        wall_mean = float(synth_walls[0])
        wall_std = 0.0
    else:
        wall_mean = wall_std = 0.0

    # Bytes / RPC counts don't change in the always-expensive synthesis —
    # only the wall-clock latency does. So we copy the optimized aggregate
    # and override the wall fields.
    base["wall_mean"] = wall_mean
    base["wall_std"] = wall_std
    base["rollback_extra_synth"] = rollback_cost
    return base


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_bytes(b: float) -> str:
    b = float(b)
    if b < 1024:
        return f"{b:.0f}B"
    if b < 1024 ** 2:
        return f"{b/1024:.1f}KB"
    return f"{b/1024**2:.2f}MB"


def pct_reduction(basic: float, opt: float) -> float:
    if basic <= 0:
        return 0.0
    return (1 - opt / basic) * 100.0


def print_mode(label: str, agg: Dict[str, Any]) -> None:
    if not agg:
        print(f"{label}: (no traces)")
        return
    print(f"{label}:")
    print(f"  traces:              {agg['trace_count']}")
    print(f"  wall-clock latency:  "
          f"{agg['wall_mean']:.0f}ms ± {agg['wall_std']:.0f}ms")
    print(f"  client RPC calls:    {agg['total_rpc']}  "
          f"(mean {agg['rpc_mean']:.1f}/trace)")
    print(f"  server-internal RPC: {agg['total_server_internal_rpc']}  "
          f"(parse_fetch; O1 artifact)")
    print(f"  cache hits (total):  {agg['total_cache_hits']}")
    print(f"  payload (total):     {fmt_bytes(agg['bytes_mean'])} "
          f"± {fmt_bytes(agg['bytes_std'])} per trace")
    print(f"  lint    in/out:      {fmt_bytes(agg['lint_in'])} / "
          f"{fmt_bytes(agg['lint_out'])}")
    print(f"  scan    in/out:      {fmt_bytes(agg['scan_in'])} / "
          f"{fmt_bytes(agg['scan_out'])}")
    if agg["spec_hits"] + agg["rollbacks"] > 0:
        print(f"  speculation hits:    {agg['spec_hits']}")
        print(f"  rollbacks:           {agg['rollbacks']}")
        print(f"  guard hit rate:      {agg['hit_rate']*100:.1f}%")
        print(f"  guard cost (mean):   {agg['guard_ms_mean']:.1f}ms")
        print(f"  speculative tokens wasted: {agg['tokens_wasted']}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="code_review trace analyzer")
    p.add_argument("--log", default=EXEC_OPS_LOG)
    args = p.parse_args()

    records = load_records(args.log)
    by_trace = group_by_trace(records)

    basics: List[Dict[str, Any]] = []
    optims: List[Dict[str, Any]] = []
    unlabelled: List[Dict[str, Any]] = []
    for tid, nodes in by_trace.items():
        mode = classify_trace(nodes)
        ana = analyze_trace(tid, nodes)
        if mode == "basic":
            basics.append(ana)
        elif mode == "optimized":
            optims.append(ana)
        else:
            unlabelled.append(ana)

    print("=" * 64)
    print("=== code_review ToolIR analysis ===")
    print("=" * 64)
    print(f"Log:          {args.log}")
    print(f"Total traces: {len(by_trace)} "
          f"(basic={len(basics)}, optimized={len(optims)}, "
          f"unlabelled={len(unlabelled)})")
    print()

    basic_agg = aggregate(basics)
    opt_agg = aggregate(optims)
    ae_agg = compute_always_expensive(optims)
    print_mode("BASIC", basic_agg)
    print_mode("OPTIMIZED", opt_agg)
    if ae_agg:
        print(f"ALWAYS-EXPENSIVE (synthetic — every speculation rolls back, "
              f"derived from optimized + rollback_extra={ae_agg.get('rollback_extra_synth', 0):.0f}ms):")
        print(f"  wall-clock latency:  "
              f"{ae_agg['wall_mean']:.0f}ms ± {ae_agg['wall_std']:.0f}ms")
        print()

    # Comparison table
    if basic_agg and opt_agg:
        print("=== Per-optimization breakdown ===")

        # O1 round-trip: lint/scan request-body bytes delta
        basic_trip = basic_agg["lint_in"] + basic_agg["scan_in"]
        opt_trip = opt_agg["lint_in"] + opt_agg["scan_in"]
        print(f"  O1 round-trip:  "
              f"{fmt_bytes(basic_trip)} -> {fmt_bytes(opt_trip)}  "
              f"({pct_reduction(basic_trip, opt_trip):.1f}% fewer request bytes)")

        # O2 dead-output: lint/scan response-body bytes delta
        basic_dead = basic_agg["lint_out"] + basic_agg["scan_out"]
        opt_dead = opt_agg["lint_out"] + opt_agg["scan_out"]
        print(f"  O2 dead-output: "
              f"{fmt_bytes(basic_dead)} -> {fmt_bytes(opt_dead)}  "
              f"({pct_reduction(basic_dead, opt_dead):.1f}% fewer response bytes)")

        # O3 redundant-invocation: cache hits replacing duplicate parse calls
        print(f"  O3 redundant:   basic duplicate-(op,args) pairs="
              f"{basic_agg['total_redundant']},  "
              f"optimized cache hits={opt_agg['total_cache_hits']}")

        # O4 speculation
        print(f"  O4 speculation: hit_rate={opt_agg['hit_rate']*100:.1f}%  "
              f"guard_ms_mean={opt_agg['guard_ms_mean']:.1f}ms  "
              f"rollbacks={opt_agg['rollbacks']}  "
              f"tokens_wasted={opt_agg['tokens_wasted']}")
        print()

        # Top-line speedup — three-way (Basic vs Optimized vs always-expensive)
        lat_reduction = pct_reduction(basic_agg["wall_mean"], opt_agg["wall_mean"])
        byte_reduction = pct_reduction(basic_agg["bytes_mean"], opt_agg["bytes_mean"])
        rpc_reduction = pct_reduction(basic_agg["total_rpc"], opt_agg["total_rpc"])
        speedup = (basic_agg["wall_mean"] / max(opt_agg["wall_mean"], 1.0))
        print("=== Speedup ===")
        print(f"  Latency:   basic={basic_agg['wall_mean']:.0f}ms -> "
              f"optimized={opt_agg['wall_mean']:.0f}ms  "
              f"({lat_reduction:.1f}% reduction, {speedup:.2f}x speedup)")
        if ae_agg:
            ae_speedup = ae_agg["wall_mean"] / max(opt_agg["wall_mean"], 1.0)
            ae_reduction = pct_reduction(ae_agg["wall_mean"], opt_agg["wall_mean"])
            print(f"             always-expensive={ae_agg['wall_mean']:.0f}ms -> "
                  f"optimized={opt_agg['wall_mean']:.0f}ms  "
                  f"({ae_reduction:.1f}% reduction vs always-expensive, "
                  f"{ae_speedup:.2f}x)")
        print(f"  Bytes:     {fmt_bytes(basic_agg['bytes_mean'])} -> "
              f"{fmt_bytes(opt_agg['bytes_mean'])}  ({byte_reduction:.1f}% reduction)")
        print(f"  RPC calls: {basic_agg['total_rpc']} -> {opt_agg['total_rpc']}  "
              f"({rpc_reduction:.1f}% reduction)")
        print()

        # E[C] under speculation
        # E[C] = p*fast + (1-p)*(fast+rollback)
        # observed_optimized ≈ E[C]; always_expensive = fast + rollback (worst).
        hit_rate = opt_agg["hit_rate"]
        rollback_n = opt_agg.get("rollback_cost_n", 0)
        rollback_mean = opt_agg.get("rollback_cost_mean", 0.0)
        rollback_median = opt_agg.get("rollback_cost_median", 0.0)
        rollback_iqr = opt_agg.get("rollback_cost_iqr", 0.0)
        print("=== Speculation analysis (E[C]) ===")
        print(f"  hit_rate (p):             {hit_rate*100:.1f}%")
        print(f"  guard cost (g):           {opt_agg['guard_ms_mean']:.1f}ms")
        print(f"  observed optimized E[C]:  {opt_agg['wall_mean']:.0f}ms")
        if rollback_n > 0:
            print(f"  rollback cost (measured): "
                  f"mean={rollback_mean:.1f}ms  "
                  f"median={rollback_median:.1f}ms  "
                  f"IQR={rollback_iqr:.1f}ms  (N={rollback_n})")
            print(f"    (per-trace wall-clock of the redo summarize call)")
        else:
            print(f"  rollback cost (measured): n/a (no rollback traces)")
        if ae_agg:
            saved = ae_agg["wall_mean"] - opt_agg["wall_mean"]
            print(f"  always-expensive cost:    {ae_agg['wall_mean']:.0f}ms  "
                  f"(synthesized using measured rollback cost)")
            print(f"  speculation savings:      {saved:+.0f}ms vs always-expensive "
                  f"({pct_reduction(ae_agg['wall_mean'], opt_agg['wall_mean']):.1f}%)")
        print(f"  basic (sequential):       {basic_agg['wall_mean']:.0f}ms")
        print()
        print("Speculation is beneficial when "
              "guard_cost + (1-p)*rollback_cost < serial_summarize_latency.")
    else:
        print("Not enough data to compare (need both basic and optimized runs).")


if __name__ == "__main__":
    main()
