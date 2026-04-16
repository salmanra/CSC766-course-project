# -*- coding: utf-8 -*-
"""linter_server.py — run ruff over Python source.

Endpoint
--------

    POST /lint?fields=<csv>
        Input : {"source": "<py src>",
                 "ast_json":  <str>  (optional; round-trip payload),
                 "ast_id":    <str>  (optional; O1 opt — fetch AST via parser),
                 "trace_id":  <str>, "node_id": <str>}
        Output: {"diagnostics": [...],
                 "counts":      {"error": int, "warning": int, "info": int},
                 "full_report_text": <str>  (dropped if fields excludes it)}

Engineered inefficiencies
-------------------------
  * **Redundant parse** — the linter always calls ``ast.parse(source)``
    internally for "symbol validation", even when the client hands in a parsed
    AST. The optimized workflow supplies ``ast_id`` and the server fetches the
    AST from the parser cache instead of reparsing (skips the redundant work).

  * **Dead output** — ``full_report_text`` is always computed. It is only
    returned to the client when the ``fields`` query param includes
    ``full_report``. The optimized client leaves that token off so the server
    drops ~KB of prose.

Usage
-----

    python server/code_review/linter_server.py --port 8102
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from profiler_utils import guess_bytes, now_ms, obj_id_from_str  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _shared import BaseReq, EXEC_OPS_LOG, emit, find_tool, run_subprocess_ms  # noqa: E402


app = FastAPI(title="ToolIR code_review Linter Server")

# Populated by main() from --parser-url; used by GET /ast/{id} fetch during O1.
PARSER_URL = "http://127.0.0.1:8101"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class LintReq(BaseReq):
    source: str
    ast_json: Optional[str] = None
    ast_id: Optional[str] = None


class LintResp(BaseModel):
    diagnostics: List[Dict[str, Any]]
    counts: Dict[str, int]
    full_report_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fields(raw: Optional[str]) -> set[str]:
    if not raw:
        # Default: full response (matches basic client behavior).
        return {"diagnostics", "counts", "full_report"}
    return {f.strip() for f in raw.split(",") if f.strip()}


def _fetch_ast_by_id(ast_id: str, trace_id: str, node_id: str) -> Optional[str]:
    """Pull the AST from the parser server via GET /ast/{id}.

    This is the server-to-server fetch that replaces the client-side round-trip
    in the optimized workflow.
    """
    try:
        resp = requests.get(
            f"{PARSER_URL}/ast/{ast_id}",
            params={"trace_id": trace_id, "node_id": f"{node_id}_fetch"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("ast_json")
    except Exception as exc:
        print(f"  [linter] AST fetch failed for {ast_id}: {exc}")
    return None


def _classify_ruff_severity(code: str) -> str:
    """Coarse map ruff rule codes → {error, warning, info}."""
    if not code:
        return "warning"
    head = code[0].upper()
    if head in ("E", "F"):
        return "error"
    if head in ("W",):
        return "warning"
    return "info"


_RUFF_BIN = find_tool("ruff")


def _run_ruff(source: str) -> tuple[list[Dict[str, Any]], str, int]:
    """Return (diagnostics, raw_json_stdout, elapsed_ms)."""
    stdout, _stderr, _rc, elapsed = run_subprocess_ms(
        [_RUFF_BIN, "check", "-", "--output-format=json", "--no-fix",
         "--stdin-filename", "input.py"],
        input_text=source,
        timeout=30,
    )
    # ruff returns non-zero when issues exist — that is normal, we inspect stdout.
    try:
        raw = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        raw = []

    diagnostics: list[Dict[str, Any]] = []
    for d in raw:
        code = d.get("code", "")
        loc = d.get("location", {}) or {}
        diagnostics.append({
            "code": code,
            "message": d.get("message", ""),
            "line": loc.get("row"),
            "column": loc.get("column"),
            "severity": _classify_ruff_severity(code),
        })
    return diagnostics, stdout, elapsed


def _counts(diagnostics: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"error": 0, "warning": 0, "info": 0}
    for d in diagnostics:
        out[d.get("severity", "info")] = out.get(d.get("severity", "info"), 0) + 1
    return out


def _full_report(diagnostics: List[Dict[str, Any]]) -> str:
    """Pretty human-readable rendering. Heavier than the structured diagnostics."""
    if not diagnostics:
        return "No lint findings."
    lines = [f"Lint report ({len(diagnostics)} finding(s)):"]
    for d in diagnostics:
        lines.append(
            f"  L{d.get('line', '?')}:{d.get('column', '?')}  "
            f"[{d.get('severity', '?').upper()}] "
            f"{d.get('code', '?')}  {d.get('message', '')}"
        )
    lines.append("")
    lines.append("Legend: E/F = error, W = warning, other = info.")
    # Padding to make the "dead output" visible in payload_out_bytes.
    lines.append("-" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# POST /lint
# ---------------------------------------------------------------------------

@app.post("/lint", response_model=LintResp)
def lint(req: LintReq, fields: Optional[str] = None) -> LintResp:
    trace_id = req.trace_id or "tr_server_unknown"
    node_id = req.node_id or "lint_server"
    include = _parse_fields(fields)

    t_start = now_ms()
    try:
        src_id = obj_id_from_str(req.source, kind="src")

        # O1: prefer cached AST over client-uploaded ast_json, and both over
        # reparsing locally.
        redundant_parse = False
        fetch_ms = 0
        if req.ast_id:
            t_f0 = now_ms()
            _fetch_ast_by_id(req.ast_id, trace_id, node_id)
            fetch_ms = now_ms() - t_f0
        elif req.ast_json is None:
            # ENGINEERED REDUNDANT PARSE: the linter doesn't actually need the
            # AST for ruff — but we "validate symbols" here as if we did. When
            # the client supplies ast_id (O1), this branch is skipped entirely.
            try:
                ast.parse(req.source)
            except SyntaxError:
                pass
            redundant_parse = True

        t_r0 = now_ms()
        diagnostics, _raw_stdout, ruff_ms = _run_ruff(req.source)
        t_r1 = now_ms()

        counts = _counts(diagnostics)
        full_text = _full_report(diagnostics)

        payload_in = guess_bytes(req.source) + guess_bytes(req.ast_json)
        # Response bytes track what we actually return to the client.
        resp_body: Dict[str, Any] = {"diagnostics": diagnostics, "counts": counts}
        if "full_report" in include:
            resp_body["full_report_text"] = full_text
        payload_out = guess_bytes(resp_body)

        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.lint",
            node_id=node_id,
            args_hash=src_id,
            inputs_meta={
                "source": {"id": src_id, "bytes": guess_bytes(req.source), "type": "str"},
                **({"ast_json": {
                    "id": obj_id_from_str(req.ast_json, kind="ast"),
                    "bytes": guess_bytes(req.ast_json),
                    "type": "str",
                }} if req.ast_json else {}),
                **({"ast_id": {
                    "id": req.ast_id,
                    "bytes": len(req.ast_id),
                    "type": "str",
                }} if req.ast_id else {}),
            },
            outputs_meta={
                "diagnostics": {
                    "id": obj_id_from_str(json.dumps(diagnostics, sort_keys=True), kind="diag"),
                    "bytes": guess_bytes(diagnostics),
                    "type": "list[dict]",
                },
                "counts": {
                    "id": obj_id_from_str(json.dumps(counts, sort_keys=True), kind="counts"),
                    "bytes": guess_bytes(counts),
                    "type": "dict",
                },
            },
            t_start_ms=t_start,
            t_end_ms=t_end,
            payload_in_bytes=payload_in,
            payload_out_bytes=payload_out,
            stage_ms={
                "ast_fetch": fetch_ms,
                "ruff": ruff_ms,
                "format": (t_end - t_r1),
            },
            status_code=200,
            extra={
                "fields": sorted(include),
                "redundant_parse": redundant_parse,
                "dropped_full_report": "full_report" not in include,
                "findings": len(diagnostics),
            },
        )
        print(f"  [linter] {node_id} findings={len(diagnostics)} "
              f"fields={sorted(include)} redundant_parse={redundant_parse}")

        return LintResp(
            diagnostics=diagnostics,
            counts=counts,
            full_report_text=full_text if "full_report" in include else None,
        )

    except HTTPException:
        raise
    except Exception as exc:
        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.lint",
            node_id=node_id,
            t_start_ms=t_start,
            t_end_ms=t_end,
            error=repr(exc),
            status_code=500,
        )
        raise HTTPException(status_code=500, detail=repr(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global PARSER_URL
    parser = argparse.ArgumentParser(description="code_review linter server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument(
        "--parser-url",
        default="http://127.0.0.1:8101",
        help="Base URL of the parser server (used for GET /ast/{id} fetches).",
    )
    args = parser.parse_args()
    PARSER_URL = args.parser_url

    os.makedirs(os.path.dirname(EXEC_OPS_LOG), exist_ok=True)
    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Parser URL:  {PARSER_URL}")
    print(f"Starting linter at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
