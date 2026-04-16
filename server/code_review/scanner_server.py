# -*- coding: utf-8 -*-
"""scanner_server.py — run bandit security scan over Python source.

Endpoint
--------

    POST /scan?fields=<csv>
        Input : {"source": "<py src>",
                 "ast_json":  <str>  (optional; round-trip payload),
                 "ast_id":    <str>  (optional; O1 opt),
                 "trace_id":  <str>, "node_id": <str>}
        Output: {"issues": [...],
                 "cwe_refs": [...]   (dropped if fields excludes it),
                 "severity_max": "LOW"|"MEDIUM"|"HIGH"|"UNDEFINED"}

Engineered inefficiencies match linter_server:
  * Redundant ``ast.parse`` when no ast_id is supplied.
  * ``cwe_refs`` is always computed; dropped from the response body when the
    ``fields`` query param does not include it (O2 dead-output elimination).

Bandit's stdin support is historically unreliable, so the source is written to
a NamedTemporaryFile and bandit is invoked against that path. The tempfile is
removed immediately after the subprocess completes.

Usage
-----

    python server/code_review/scanner_server.py --port 8103
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from profiler_utils import guess_bytes, now_ms, obj_id_from_str  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _shared import BaseReq, EXEC_OPS_LOG, emit, find_tool, run_subprocess_ms  # noqa: E402


app = FastAPI(title="ToolIR code_review Scanner Server")

PARSER_URL = "http://127.0.0.1:8101"

_SEVERITY_ORDER = {"UNDEFINED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
_BANDIT_BIN = find_tool("bandit")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ScanReq(BaseReq):
    source: str
    ast_json: Optional[str] = None
    ast_id: Optional[str] = None


class ScanResp(BaseModel):
    issues: List[Dict[str, Any]]
    cwe_refs: Optional[List[str]] = None
    severity_max: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fields(raw: Optional[str]) -> set[str]:
    if not raw:
        return {"issues", "severity_max", "cwe_refs"}
    return {f.strip() for f in raw.split(",") if f.strip()}


def _fetch_ast_by_id(ast_id: str, trace_id: str, node_id: str) -> Optional[str]:
    try:
        resp = requests.get(
            f"{PARSER_URL}/ast/{ast_id}",
            params={"trace_id": trace_id, "node_id": f"{node_id}_fetch"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("ast_json")
    except Exception as exc:
        print(f"  [scanner] AST fetch failed for {ast_id}: {exc}")
    return None


def _run_bandit(source: str) -> tuple[list[Dict[str, Any]], int]:
    """Return (issues, elapsed_ms). Issues are bandit result dicts."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(source)
        tmp_path = tmp.name
    try:
        stdout, _stderr, _rc, elapsed = run_subprocess_ms(
            [_BANDIT_BIN, "-f", "json", "-q", tmp_path],
            timeout=30,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    try:
        parsed = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    raw = parsed.get("results", []) if isinstance(parsed, dict) else []

    issues: list[Dict[str, Any]] = []
    for r in raw:
        issues.append({
            "test_id": r.get("test_id", "?"),
            "test_name": r.get("test_name", ""),
            "severity": (r.get("issue_severity") or "UNDEFINED").upper(),
            "confidence": (r.get("issue_confidence") or "UNDEFINED").upper(),
            "issue_text": r.get("issue_text", ""),
            "line_number": r.get("line_number"),
            "cwe_id": (r.get("issue_cwe") or {}).get("id"),
        })
    return issues, elapsed


def _severity_max(issues: List[Dict[str, Any]]) -> str:
    if not issues:
        return "NONE"
    top = "UNDEFINED"
    for iss in issues:
        sev = (iss.get("severity") or "UNDEFINED").upper()
        if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(top, 0):
            top = sev
    return top


def _cwe_refs(issues: List[Dict[str, Any]]) -> List[str]:
    """Padded human-readable CWE reference list — the 'dead output'."""
    refs = []
    for iss in issues:
        cwe = iss.get("cwe_id")
        if cwe:
            refs.append(
                f"CWE-{cwe}: {iss.get('test_name','?')} "
                f"(severity={iss.get('severity')}, "
                f"confidence={iss.get('confidence')})"
            )
    if not refs:
        refs.append("No CWE references for this file.")
    # Padding line to match the linter's dead-output pattern.
    refs.append("-- end of cwe_refs --")
    return refs


# ---------------------------------------------------------------------------
# POST /scan
# ---------------------------------------------------------------------------

@app.post("/scan", response_model=ScanResp)
def scan(req: ScanReq, fields: Optional[str] = None) -> ScanResp:
    trace_id = req.trace_id or "tr_server_unknown"
    node_id = req.node_id or "scan_server"
    include = _parse_fields(fields)

    t_start = now_ms()
    try:
        src_id = obj_id_from_str(req.source, kind="src")

        redundant_parse = False
        fetch_ms = 0
        if req.ast_id:
            t_f0 = now_ms()
            _fetch_ast_by_id(req.ast_id, trace_id, node_id)
            fetch_ms = now_ms() - t_f0
        elif req.ast_json is None:
            try:
                ast.parse(req.source)
            except SyntaxError:
                pass
            redundant_parse = True

        t_b0 = now_ms()
        issues, bandit_ms = _run_bandit(req.source)
        t_b1 = now_ms()

        sev_max = _severity_max(issues)
        cwe = _cwe_refs(issues)

        payload_in = guess_bytes(req.source) + guess_bytes(req.ast_json)
        resp_body: Dict[str, Any] = {"issues": issues, "severity_max": sev_max}
        if "cwe_refs" in include:
            resp_body["cwe_refs"] = cwe
        payload_out = guess_bytes(resp_body)

        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.scan",
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
                "issues": {
                    "id": obj_id_from_str(json.dumps(issues, sort_keys=True), kind="iss"),
                    "bytes": guess_bytes(issues),
                    "type": "list[dict]",
                },
                "severity_max": {
                    "id": f"const:{sev_max}",
                    "bytes": len(sev_max),
                    "type": "str",
                },
            },
            t_start_ms=t_start,
            t_end_ms=t_end,
            payload_in_bytes=payload_in,
            payload_out_bytes=payload_out,
            stage_ms={
                "ast_fetch": fetch_ms,
                "bandit": bandit_ms,
                "format": (t_end - t_b1),
            },
            status_code=200,
            extra={
                "fields": sorted(include),
                "redundant_parse": redundant_parse,
                "dropped_cwe_refs": "cwe_refs" not in include,
                "findings": len(issues),
            },
        )
        print(f"  [scanner] {node_id} findings={len(issues)} sev_max={sev_max} "
              f"fields={sorted(include)}")

        return ScanResp(
            issues=issues,
            cwe_refs=cwe if "cwe_refs" in include else None,
            severity_max=sev_max,
        )

    except HTTPException:
        raise
    except Exception as exc:
        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.scan",
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
    parser = argparse.ArgumentParser(description="code_review scanner server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8103)
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
    print(f"Starting scanner at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
