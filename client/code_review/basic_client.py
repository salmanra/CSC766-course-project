# -*- coding: utf-8 -*-
"""basic_client.py — unoptimized code_review workflow.

Sequential pipeline that intentionally triggers all four ToolIR inefficiency
patterns so the optimized version has something to beat:

    1. parse(source)                   →  ast_json (large)
    2. lint(source, ast_json=<FULL>, fields=all)
                                         ↑ round-trip + dead full_report
    3. scan(source, ast_json=<FULL>, fields=all)
                                         ↑ round-trip + dead cwe_refs
    4. parse(source)   **again**        ← redundant invocation
    5. summarize(source, lint_summary, sec_summary)

The basic client waits for lint + scan to finish before invoking the
summarizer (no speculation / control-flow prediction).

A single client-side EXEC_OP record is emitted at the start of each workflow
with ``extra.client_mode="basic"`` so the analyzer can label the trace.

Usage
-----

    python client/code_review/basic_client.py --input inputs/code_review/mixed.py
    python client/code_review/basic_client.py --all --runs 5
"""

from __future__ import annotations

import argparse
import glob
import os
import statistics
import sys
import uuid
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from profiler_utils import (  # noqa: E402
    append_jsonl,
    build_exec_op_record,
    guess_bytes,
    now_ms,
    obj_id_from_str,
)


EXEC_OPS_LOG = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..",
                 "profiler_logs", "code_review_exec_ops.jsonl")
)
INPUTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "inputs", "code_review")
)

DEFAULTS = {
    "parser": "http://127.0.0.1:8101",
    "linter": "http://127.0.0.1:8102",
    "scanner": "http://127.0.0.1:8103",
    "summarizer": "http://127.0.0.1:8104",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def emit_mode_marker(trace_id: str, input_path: str) -> None:
    """One client-side EXEC_OP per workflow so the analyzer can label the trace."""
    t = now_ms()
    src_id = obj_id_from_str(input_path, kind="path")
    record = build_exec_op_record(
        trace_id=trace_id,
        op="tool.client_mode",
        node_id="client_mode",
        args_hash=src_id,
        inputs_meta={
            "input_path": {"id": src_id, "bytes": len(input_path), "type": "str"},
        },
        outputs_meta={},
        t_start_ms=t,
        t_end_ms=t,
        payload_in_bytes=0,
        payload_out_bytes=0,
        status_code=200,
        extra={"client_mode": "basic", "input_path": input_path},
    )
    append_jsonl(EXEC_OPS_LOG, record)


def post_json(url: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Basic workflow
# ---------------------------------------------------------------------------

def run_workflow(
    source: str,
    input_path: str,
    urls: Dict[str, str],
    *,
    trace_id: str,
) -> Dict[str, Any]:
    """Execute one basic-mode workflow and return a summary dict."""
    emit_mode_marker(trace_id, input_path)

    t_wall_start = now_ms()

    # (1) parse
    parse_resp = post_json(
        f"{urls['parser']}/parse",
        {"source": source, "trace_id": trace_id, "node_id": "parse_1"},
    )
    ast_json = parse_resp["ast_json"]

    # (2) lint — round-trip ast_json + request full report (dead output)
    lint_resp = post_json(
        f"{urls['linter']}/lint?fields=diagnostics,counts,full_report",
        {
            "source": source,
            "ast_json": ast_json,
            "trace_id": trace_id,
            "node_id": "lint_1",
        },
    )
    _ = lint_resp.pop("full_report_text", None)  # received, then discarded

    # (3) scan — round-trip ast_json + request cwe_refs (dead output)
    scan_resp = post_json(
        f"{urls['scanner']}/scan?fields=issues,severity_max,cwe_refs",
        {
            "source": source,
            "ast_json": ast_json,
            "trace_id": trace_id,
            "node_id": "scan_1",
        },
    )
    _ = scan_resp.pop("cwe_refs", None)  # received, then discarded

    # (4) parse AGAIN — redundant "pre-summarize validation"
    _ = post_json(
        f"{urls['parser']}/parse",
        {"source": source, "trace_id": trace_id, "node_id": "parse_2"},
    )

    # (5) summarize
    lint_summary = {
        "counts": lint_resp.get("counts", {}),
        "top3": lint_resp.get("diagnostics", [])[:3],
    }
    sec_summary = {
        "severity_max": scan_resp.get("severity_max", "NONE"),
        "issues": scan_resp.get("issues", []),
    }
    sum_resp = post_json(
        f"{urls['summarizer']}/summarize",
        {
            "source": source,
            "lint_summary": lint_summary,
            "sec_summary": sec_summary,
            "trace_id": trace_id,
            "node_id": "summarize_1",
        },
    )

    t_wall_end = now_ms()

    return {
        "trace_id": trace_id,
        "input": input_path,
        "wall_ms": t_wall_end - t_wall_start,
        "action": sum_resp.get("action"),
        "review_excerpt": (sum_resp.get("review_text", "") or "")[:80],
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def resolve_inputs(input_arg: Optional[str], use_all: bool) -> List[str]:
    if use_all:
        files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.py")))
        if not files:
            raise SystemExit(f"No inputs found in {INPUTS_DIR}")
        return files
    if not input_arg:
        raise SystemExit("Either --input <path> or --all is required.")
    if not os.path.exists(input_arg):
        raise SystemExit(f"Input not found: {input_arg}")
    return [os.path.abspath(input_arg)]


def main() -> None:
    p = argparse.ArgumentParser(description="code_review basic (unoptimized) client")
    p.add_argument("--input", help="Single input .py file.")
    p.add_argument("--all", action="store_true",
                   help="Iterate every file in inputs/code_review/.")
    p.add_argument("--runs", type=int, default=1,
                   help="Runs per input for latency stats (default 1).")
    for name, url in DEFAULTS.items():
        p.add_argument(f"--{name}-url", default=url)
    args = p.parse_args()

    urls = {name: getattr(args, f"{name}_url") for name in DEFAULTS}
    inputs = resolve_inputs(args.input, args.all)

    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Mode: BASIC (no optimizations)")
    print(f"Inputs: {len(inputs)} file(s), runs/input={args.runs}")
    print()

    all_results: List[Dict[str, Any]] = []
    for path in inputs:
        source = read_source(path)
        per_file_latencies: List[int] = []
        last_action: Optional[str] = None
        for run_idx in range(args.runs):
            trace_id = f"tr_{uuid.uuid4().hex[:12]}"
            summary = run_workflow(source, path, urls, trace_id=trace_id)
            per_file_latencies.append(summary["wall_ms"])
            last_action = summary["action"]
            print(f"[{os.path.basename(path)}] run {run_idx+1}/{args.runs} "
                  f"trace={trace_id} wall={summary['wall_ms']}ms action={summary['action']}")
        mean = statistics.mean(per_file_latencies) if per_file_latencies else 0
        std = statistics.pstdev(per_file_latencies) if len(per_file_latencies) > 1 else 0
        all_results.append({
            "input": os.path.basename(path),
            "mean_ms": int(mean),
            "std_ms": int(std),
            "runs": args.runs,
            "action": last_action,
        })

    print()
    print("=== Basic summary ===")
    for r in all_results:
        print(f"  {r['input']:30s}  mean={r['mean_ms']}ms  "
              f"std={r['std_ms']}ms  action={r['action']}")
    print()
    print(f"EXEC_OP records appended to: {EXEC_OPS_LOG}")


if __name__ == "__main__":
    main()
