# -*- coding: utf-8 -*-
"""optimized_client.py — code_review workflow with ToolIR-style optimizations.

Optimizations applied
---------------------
    O1 round-trip        — parser returns an ast_id; lint/scan take only
                            {source, ast_id} so the large ast_json is not
                            shuttled through the client. Linter / scanner
                            fetch the AST server-to-server via GET /ast/{id}.
    O2 dead-output       — fields=counts,top3 for lint (drops full_report_text);
                            fields=issues,severity_max for scan (drops cwe_refs).
    O3 redundant-inv     — client-side memoization of parse keyed by
                            sha256(source). The second "pre-summarize"
                            parse is served from the cache and emits a
                            client-side EXEC_OP with cache_hit=True.
    O4 speculation       — a cheap regex/LOC guard predicts the final action
                            before lint + scan finish. The speculative
                            summarize call is fired in parallel with
                            lint + scan. On a match the speculative review is
                            kept; on a mismatch we roll back and re-issue
                            summarize against the real findings.

Emits a workflow-start EXEC_OP with ``extra.client_mode="optimized"`` so the
analyzer can label the trace. Additional client-side EXEC_OPs are emitted for
cache hits, speculation hits, and rollbacks.

Usage
-----

    python client/code_review/optimized_client.py --input inputs/code_review/mixed.py
    python client/code_review/optimized_client.py --all --runs 5
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import statistics
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
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

_LOC_THRESHOLD = 200
_GUARD_PATTERNS = [
    re.compile(r"\beval\("),
    re.compile(r"\bpickle\.loads\b"),
    re.compile(r"#\s*TODO"),
]


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def cheap_guard(source: str) -> tuple[str, int]:
    """Return (predicted_action, elapsed_ms).

    Predicts request_changes if any of the regex patterns fire OR the file is
    over the LOC threshold. Otherwise predicts approve.
    """
    t0 = now_ms()
    triggered = False
    for pat in _GUARD_PATTERNS:
        if pat.search(source):
            triggered = True
            break
    if not triggered and source.count("\n") + 1 > _LOC_THRESHOLD:
        triggered = True
    t1 = now_ms()
    return ("request_changes" if triggered else "approve"), t1 - t0


def speculative_summaries(guard_action: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build placeholder lint/sec summaries matching the guard's prediction.

    The speculative summarize call uses these so it can start before real
    lint + scan results are available.
    """
    if guard_action == "approve":
        lint = {"counts": {"error": 0, "warning": 0, "info": 0}, "top3": []}
        sec = {"severity_max": "NONE", "issues": []}
    else:
        lint = {
            "counts": {"error": 1, "warning": 0, "info": 0},
            "top3": [{
                "code": "GUARD",
                "message": "cheap guard predicted an issue",
                "line": 0,
                "severity": "error",
            }],
        }
        sec = {"severity_max": "LOW", "issues": []}
    return lint, sec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def source_key(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()


def post_json(url: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _decide_action_from_real(lint_resp: Dict[str, Any], scan_resp: Dict[str, Any]) -> str:
    counts = lint_resp.get("counts", {}) or {}
    if int(counts.get("error", 0) or 0) > 0:
        return "request_changes"
    sev = str(scan_resp.get("severity_max", "") or "").upper()
    if sev in {"MEDIUM", "HIGH"}:
        return "request_changes"
    return "approve"


# ---------------------------------------------------------------------------
# Client-side EXEC_OP emitters
# ---------------------------------------------------------------------------

def emit_mode_marker(trace_id: str, input_path: str) -> None:
    t = now_ms()
    record = build_exec_op_record(
        trace_id=trace_id,
        op="tool.client_mode",
        node_id="client_mode",
        args_hash=obj_id_from_str(input_path, kind="path"),
        inputs_meta={},
        outputs_meta={},
        t_start_ms=t,
        t_end_ms=t,
        payload_in_bytes=0,
        payload_out_bytes=0,
        status_code=200,
        extra={"client_mode": "optimized", "input_path": input_path},
    )
    append_jsonl(EXEC_OPS_LOG, record)


def emit_parse_cache_hit(trace_id: str, node_id: str, source: str, ast_id: str) -> None:
    t = now_ms()
    src_id = obj_id_from_str(source, kind="src")
    record = build_exec_op_record(
        trace_id=trace_id,
        op="tool.parse",
        node_id=node_id,
        args_hash=src_id,
        inputs_meta={"source": {"id": src_id, "bytes": 0, "type": "str"}},
        outputs_meta={"ast": {"id": ast_id, "bytes": 0, "type": "str"}},
        t_start_ms=t,
        t_end_ms=t,
        payload_in_bytes=0,
        payload_out_bytes=0,
        status_code=200,
        extra={"cache_hit": True},
    )
    append_jsonl(EXEC_OPS_LOG, record)


def emit_speculation_event(
    *,
    trace_id: str,
    kind: str,  # "speculation_hit" or "rollback"
    guard_action: str,
    real_action: str,
    guard_ms: int,
    speculative_tokens: int,
) -> None:
    t = now_ms()
    record = build_exec_op_record(
        trace_id=trace_id,
        op="tool.speculation",
        node_id=kind,
        t_start_ms=t,
        t_end_ms=t,
        payload_in_bytes=0,
        payload_out_bytes=0,
        status_code=200,
        extra={
            kind: True,
            "guard_action": guard_action,
            "real_action": real_action,
            "guard_ms": guard_ms,
            "speculative_tokens_wasted": speculative_tokens if kind == "rollback" else 0,
        },
    )
    append_jsonl(EXEC_OPS_LOG, record)


# ---------------------------------------------------------------------------
# Optimized workflow
# ---------------------------------------------------------------------------

def run_workflow(
    source: str,
    input_path: str,
    urls: Dict[str, str],
    *,
    trace_id: str,
    parse_memo: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    emit_mode_marker(trace_id, input_path)
    t_wall_start = now_ms()

    # ----- (1) parse (O3: memoized) ----------------------------------------
    skey = source_key(source)
    if skey in parse_memo:
        parse_resp = parse_memo[skey]
        emit_parse_cache_hit(trace_id, "parse_1", source, parse_resp["ast_id"])
    else:
        parse_resp = post_json(
            f"{urls['parser']}/parse",
            {"source": source, "trace_id": trace_id, "node_id": "parse_1"},
        )
        parse_memo[skey] = parse_resp
    ast_id: str = parse_resp["ast_id"]

    # ----- (O4) run the guard and speculatively fire summarize --------------
    guard_action, guard_ms = cheap_guard(source)
    spec_lint, spec_sec = speculative_summaries(guard_action)

    executor = ThreadPoolExecutor(max_workers=3)

    # O1 + O2: lint / scan send only {source, ast_id} and ask for trimmed output.
    lint_future: Future = executor.submit(
        post_json,
        f"{urls['linter']}/lint?fields=diagnostics,counts",
        {
            "source": source,
            "ast_id": ast_id,
            "trace_id": trace_id,
            "node_id": "lint_1",
        },
    )
    scan_future: Future = executor.submit(
        post_json,
        f"{urls['scanner']}/scan?fields=issues,severity_max",
        {
            "source": source,
            "ast_id": ast_id,
            "trace_id": trace_id,
            "node_id": "scan_1",
        },
    )
    spec_sum_future: Future = executor.submit(
        post_json,
        f"{urls['summarizer']}/summarize",
        {
            "source": source,
            "lint_summary": spec_lint,
            "sec_summary": spec_sec,
            "trace_id": trace_id,
            "node_id": "summarize_speculative",
        },
    )

    # Block on lint + scan; the speculative summarize runs in parallel.
    lint_resp = lint_future.result()
    scan_resp = scan_future.result()

    real_action = _decide_action_from_real(lint_resp, scan_resp)

    # ----- (4) second parse — redundant in basic, served from O3 memo here --
    if skey in parse_memo:
        emit_parse_cache_hit(trace_id, "parse_2", source, ast_id)
    else:
        _ = post_json(
            f"{urls['parser']}/parse",
            {"source": source, "trace_id": trace_id, "node_id": "parse_2"},
        )

    # ----- O4 speculation outcome ------------------------------------------
    spec_result = spec_sum_future.result()  # always wait so the request completes
    executor.shutdown(wait=False)

    if real_action == guard_action:
        # Speculation hit — keep the speculative review.
        sum_resp = spec_result
        emit_speculation_event(
            trace_id=trace_id,
            kind="speculation_hit",
            guard_action=guard_action,
            real_action=real_action,
            guard_ms=guard_ms,
            speculative_tokens=int(spec_result.get("tokens_out", 0) or 0),
        )
    else:
        # Rollback — re-issue summarize with real summaries.
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
                "node_id": "summarize_rollback",
            },
        )
        emit_speculation_event(
            trace_id=trace_id,
            kind="rollback",
            guard_action=guard_action,
            real_action=real_action,
            guard_ms=guard_ms,
            speculative_tokens=int(spec_result.get("tokens_out", 0) or 0),
        )

    t_wall_end = now_ms()
    return {
        "trace_id": trace_id,
        "input": input_path,
        "wall_ms": t_wall_end - t_wall_start,
        "guard_action": guard_action,
        "real_action": real_action,
        "rollback": real_action != guard_action,
        "action": sum_resp.get("action"),
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
    p = argparse.ArgumentParser(description="code_review optimized client")
    p.add_argument("--input", help="Single input .py file.")
    p.add_argument("--all", action="store_true")
    p.add_argument("--runs", type=int, default=1)
    for name, url in DEFAULTS.items():
        p.add_argument(f"--{name}-url", default=url)
    args = p.parse_args()

    urls = {name: getattr(args, f"{name}_url") for name in DEFAULTS}
    inputs = resolve_inputs(args.input, args.all)

    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Mode: OPTIMIZED (O1 round-trip, O2 dead-output, "
          f"O3 parse-memo, O4 speculation)")
    print(f"Inputs: {len(inputs)} file(s), runs/input={args.runs}")
    print()

    # Parse memo lives for the lifetime of the client process so the second
    # "pre-summarize" parse within a single workflow is served from cache.
    parse_memo: Dict[str, Dict[str, Any]] = {}

    results: List[Dict[str, Any]] = []
    rollbacks = 0
    spec_hits = 0
    for path in inputs:
        source = read_source(path)
        per_file_latencies: List[int] = []
        last: Dict[str, Any] = {}
        for run_idx in range(args.runs):
            trace_id = f"tr_{uuid.uuid4().hex[:12]}"
            summary = run_workflow(source, path, urls,
                                    trace_id=trace_id, parse_memo=parse_memo)
            per_file_latencies.append(summary["wall_ms"])
            last = summary
            if summary["rollback"]:
                rollbacks += 1
            else:
                spec_hits += 1
            tag = "ROLLBACK" if summary["rollback"] else "HIT"
            print(f"[{os.path.basename(path)}] run {run_idx+1}/{args.runs} "
                  f"trace={trace_id} wall={summary['wall_ms']}ms "
                  f"guard={summary['guard_action']} real={summary['real_action']} {tag}")
        mean = statistics.mean(per_file_latencies) if per_file_latencies else 0
        std = statistics.pstdev(per_file_latencies) if len(per_file_latencies) > 1 else 0
        results.append({
            "input": os.path.basename(path),
            "mean_ms": int(mean),
            "std_ms": int(std),
            "action": last.get("action"),
            "guard_action": last.get("guard_action"),
            "real_action": last.get("real_action"),
            "rollback": last.get("rollback"),
        })

    total = spec_hits + rollbacks
    hit_rate = (spec_hits / total) if total else 0.0

    print()
    print("=== Optimized summary ===")
    for r in results:
        flag = "ROLLBACK" if r["rollback"] else "HIT"
        print(f"  {r['input']:30s}  mean={r['mean_ms']}ms  std={r['std_ms']}ms "
              f"guard={r['guard_action']} real={r['real_action']} {flag}")
    print()
    print(f"Speculation hit rate: {hit_rate*100:.1f}%  "
          f"({spec_hits} hits, {rollbacks} rollbacks across {total} runs)")
    print(f"EXEC_OP records appended to: {EXEC_OPS_LOG}")


if __name__ == "__main__":
    main()
