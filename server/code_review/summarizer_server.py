# -*- coding: utf-8 -*-
"""summarizer_server.py — turn lint + security findings into a human review.

Endpoint
--------

    POST /summarize
        Input : {"source": "<py src>",
                 "lint_summary": {"counts": {...}, "top3": [...]}, ...},
                 "sec_summary":  {"severity_max": "LOW", "issues": [...]}},
                 "trace_id": <str>, "node_id": <str>}
        Output: {"review_text": <str>,
                 "action": "approve"|"request_changes",
                 "tokens_in": int, "tokens_out": int}

Backends
--------
Backend selection is via ``--backend {template,local}`` on startup.

  * ``template`` (default) — deterministic formatter, zero dependencies.
  * ``local``              — stub for a small local LLM (Qwen2.5-Coder-0.5B).
                             See llm_backend.py for the TODO block.

Usage
-----

    python server/code_review/summarizer_server.py --port 8104
    python server/code_review/summarizer_server.py --port 8104 --backend local
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from profiler_utils import guess_bytes, now_ms, obj_id_from_str  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _shared import BaseReq, EXEC_OPS_LOG, emit  # noqa: E402
from llm_backend import ReviewBackend, get_backend  # noqa: E402


app = FastAPI(title="ToolIR code_review Summarizer Server")

# Set in main() from --backend flag.
BACKEND: Optional[ReviewBackend] = None
BACKEND_NAME = "template"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SumReq(BaseReq):
    source: str
    lint_summary: Dict[str, Any]
    sec_summary: Dict[str, Any]


class SumResp(BaseModel):
    review_text: str
    action: str
    tokens_in: int
    tokens_out: int


# ---------------------------------------------------------------------------
# POST /summarize
# ---------------------------------------------------------------------------

@app.post("/summarize", response_model=SumResp)
def summarize(req: SumReq) -> SumResp:
    if BACKEND is None:
        raise HTTPException(status_code=500, detail="backend not initialized")

    trace_id = req.trace_id or "tr_server_unknown"
    node_id = req.node_id or "summarize_server"

    t_start = now_ms()
    try:
        src_id = obj_id_from_str(req.source, kind="src")
        lint_id = obj_id_from_str(
            json.dumps(req.lint_summary, sort_keys=True, default=str), kind="lintsum"
        )
        sec_id = obj_id_from_str(
            json.dumps(req.sec_summary, sort_keys=True, default=str), kind="secsum"
        )

        t_pb0 = now_ms()
        # "Prompt build" is trivial for the template backend but gives the
        # local-LLM path a natural stage to attribute tokenization work to.
        _prompt_hint = {
            "source_len": len(req.source),
            "error_count": (req.lint_summary or {}).get("counts", {}).get("error", 0),
            "sev_max": (req.sec_summary or {}).get("severity_max", "NONE"),
        }
        t_pb1 = now_ms()

        t_g0 = now_ms()
        result = BACKEND.generate_review(req.source, req.lint_summary, req.sec_summary)
        t_g1 = now_ms()

        review_text = str(result.get("review_text", ""))
        action = str(result.get("action", "request_changes"))
        tokens_in = int(result.get("tokens_in", 0))
        tokens_out = int(result.get("tokens_out", 0))

        payload_in = guess_bytes(req.source) + guess_bytes(req.lint_summary) + \
            guess_bytes(req.sec_summary)
        payload_out = guess_bytes({
            "review_text": review_text,
            "action": action,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        })

        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.summarize",
            node_id=node_id,
            args_hash=src_id,
            inputs_meta={
                "source": {"id": src_id, "bytes": guess_bytes(req.source), "type": "str"},
                "lint_summary": {"id": lint_id, "bytes": guess_bytes(req.lint_summary),
                                  "type": "dict"},
                "sec_summary": {"id": sec_id, "bytes": guess_bytes(req.sec_summary),
                                 "type": "dict"},
            },
            outputs_meta={
                "review_text": {
                    "id": obj_id_from_str(review_text, kind="txt"),
                    "bytes": guess_bytes(review_text),
                    "type": "str",
                },
                "action": {
                    "id": f"const:{action}",
                    "bytes": len(action),
                    "type": "str",
                },
            },
            t_start_ms=t_start,
            t_end_ms=t_end,
            payload_in_bytes=payload_in,
            payload_out_bytes=payload_out,
            stage_ms={
                "prompt_build": t_pb1 - t_pb0,
                "generate": t_g1 - t_g0,
            },
            status_code=200,
            extra={
                "backend": BACKEND_NAME,
                "action": action,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )
        print(f"  [summarizer] {node_id} backend={BACKEND_NAME} action={action} "
              f"tokens_out={tokens_out}")

        return SumResp(
            review_text=review_text,
            action=action,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    except HTTPException:
        raise
    except Exception as exc:
        t_end = now_ms()
        emit(
            trace_id=trace_id,
            op="tool.summarize",
            node_id=node_id,
            t_start_ms=t_start,
            t_end_ms=t_end,
            error=repr(exc),
            status_code=500,
            extra={"backend": BACKEND_NAME},
        )
        raise HTTPException(status_code=500, detail=repr(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global BACKEND, BACKEND_NAME
    parser = argparse.ArgumentParser(description="code_review summarizer server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8104)
    parser.add_argument(
        "--backend",
        choices=["template", "local"],
        default="template",
        help="Review backend. 'template' is deterministic and has no ML deps; "
             "'local' is a stub for a small local LLM (see llm_backend.py).",
    )
    args = parser.parse_args()

    BACKEND_NAME = args.backend
    BACKEND = get_backend(args.backend)

    os.makedirs(os.path.dirname(EXEC_OPS_LOG), exist_ok=True)
    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Backend:     {BACKEND_NAME}")
    print(f"Starting summarizer at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
