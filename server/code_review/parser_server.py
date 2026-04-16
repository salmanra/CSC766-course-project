# -*- coding: utf-8 -*-
"""parser_server.py — parse Python source into an AST.

Endpoints
---------

    POST /parse
        Input:  {"source": "<python source>", "trace_id": "...", "node_id": "..."}
        Output: {"ast_json": "<ast.dump>", "symbols": [...], "loc": int,
                 "ast_id":   "obj:ast:<hash16>"}

    GET /ast/{ast_id}
        Returns the cached AST produced by a prior /parse call, or 404.
        The optimized workflow uses this endpoint so the linter / scanner can
        fetch the AST server-to-server instead of the client re-uploading it.

Both endpoints emit one EXEC_OP record to the shared JSONL log.

Usage
-----

    python server/code_review/parser_server.py --port 8101

The parser is CPU-only (Python stdlib ast). The AST cache is an in-process dict
that lives for the lifetime of the server process.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Make repo root importable for profiler_utils.
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from profiler_utils import guess_bytes, now_ms, obj_id_from_str  # noqa: E402

# Package-local imports (run with module-relative path so _shared loads).
sys.path.insert(0, os.path.dirname(__file__))
from _shared import AST_CACHE, BaseReq, EXEC_OPS_LOG, emit  # noqa: E402


app = FastAPI(title="ToolIR code_review Parser Server")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class ParseReq(BaseReq):
    source: str


class ParseResp(BaseModel):
    ast_json: str
    symbols: list[str]
    loc: int
    ast_id: str


# ---------------------------------------------------------------------------
# Core parse
# ---------------------------------------------------------------------------

def _collect_symbols(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


# ---------------------------------------------------------------------------
# POST /parse
# ---------------------------------------------------------------------------

@app.post("/parse", response_model=ParseResp)
def parse_source(req: ParseReq) -> ParseResp:
    trace_id = req.trace_id or "tr_server_unknown"
    node_id = req.node_id or "parse_server"

    t_start = now_ms()
    try:
        src_id = obj_id_from_str(req.source, kind="src")
        payload_in = guess_bytes(req.source)

        t_p0 = now_ms()
        try:
            tree = ast.parse(req.source)
        except SyntaxError as exc:
            raise HTTPException(status_code=400, detail=f"SyntaxError: {exc}") from exc
        t_p1 = now_ms()

        t_d0 = now_ms()
        ast_json = ast.dump(tree, include_attributes=False)
        t_d1 = now_ms()

        symbols = _collect_symbols(tree)
        loc = len(req.source.splitlines())
        ast_id = obj_id_from_str(ast_json, kind="ast")
        AST_CACHE[ast_id] = ast_json

        payload_out = guess_bytes(
            {"ast_json": ast_json, "symbols": symbols, "loc": loc, "ast_id": ast_id}
        )
        t_end = now_ms()

        emit(
            trace_id=trace_id,
            op="tool.parse",
            node_id=node_id,
            args_hash=src_id,
            inputs_meta={
                "source": {"id": src_id, "bytes": payload_in, "type": "str"},
            },
            outputs_meta={
                "ast": {"id": ast_id, "bytes": guess_bytes(ast_json), "type": "str"},
                "symbols": {
                    "id": obj_id_from_str(",".join(symbols), kind="syms"),
                    "bytes": guess_bytes(symbols),
                    "type": "list[str]",
                },
            },
            t_start_ms=t_start,
            t_end_ms=t_end,
            payload_in_bytes=payload_in,
            payload_out_bytes=payload_out,
            stage_ms={"parse": t_p1 - t_p0, "dump": t_d1 - t_d0},
            status_code=200,
        )
        print(f"  [parser] {node_id} loc={loc} ast_bytes={guess_bytes(ast_json)} "
              f"ast_id={ast_id}")

        return ParseResp(ast_json=ast_json, symbols=symbols, loc=loc, ast_id=ast_id)

    except HTTPException:
        raise
    except Exception as exc:
        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.parse",
            node_id=node_id,
            t_start_ms=t_start,
            t_end_ms=t_end,
            error=repr(exc),
            status_code=500,
        )
        raise HTTPException(status_code=500, detail=repr(exc))


# ---------------------------------------------------------------------------
# GET /ast/{ast_id} — server-to-server AST fetch (used by O1)
# ---------------------------------------------------------------------------

@app.get("/ast/{ast_id}")
def fetch_ast(ast_id: str, trace_id: str | None = None, node_id: str | None = None):
    tid = trace_id or "tr_server_unknown"
    nid = node_id or "parse_fetch"

    t_start = now_ms()
    ast_json = AST_CACHE.get(ast_id)
    t_end = now_ms()

    if ast_json is None:
        emit(
            trace_id=tid,
            op="tool.parse_fetch",
            node_id=nid,
            args_hash=ast_id,
            t_start_ms=t_start,
            t_end_ms=t_end,
            error="ast_id not found",
            status_code=404,
            extra={"ast_id": ast_id},
        )
        raise HTTPException(status_code=404, detail=f"ast_id {ast_id} not found")

    payload_out = guess_bytes(ast_json)
    emit(
        trace_id=tid,
        op="tool.parse_fetch",
        node_id=nid,
        args_hash=ast_id,
        inputs_meta={"ast_id": {"id": ast_id, "bytes": len(ast_id), "type": "str"}},
        outputs_meta={"ast": {"id": ast_id, "bytes": payload_out, "type": "str"}},
        t_start_ms=t_start,
        t_end_ms=t_end,
        payload_in_bytes=len(ast_id),
        payload_out_bytes=payload_out,
        stage_ms={"lookup": t_end - t_start},
        status_code=200,
        extra={"ast_id": ast_id, "cache_lookup": True},
    )
    return {"ast_id": ast_id, "ast_json": ast_json}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="code_review parser server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(EXEC_OPS_LOG), exist_ok=True)
    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Starting parser at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
