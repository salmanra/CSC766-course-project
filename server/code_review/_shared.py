# -*- coding: utf-8 -*-
"""_shared.py — shared helpers for the code_review benchmark servers.

Provides:
  * EXEC_OPS_LOG        — path to the benchmark-wide JSONL log
  * REPO_ROOT           — absolute path to the repo root (for profiler_utils import)
  * emit(...)           — thin wrapper around build_exec_op_record + append_jsonl
  * AST_CACHE           — process-local AST store owned by the parser server
  * BaseReq             — pydantic base for every server's request model
  * run_subprocess_ms() — subprocess helper that also reports elapsed ms
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel

# Repo root is two levels up from this file.
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from profiler_utils import append_jsonl, build_exec_op_record  # noqa: E402

EXEC_OPS_LOG = os.environ.get("CODE_REVIEW_EXEC_OPS_LOG") or os.path.normpath(
    os.path.join(REPO_ROOT, "profiler_logs", "code_review_exec_ops.jsonl")
)


# Process-local AST store owned by the parser_server process.
# Keyed by ast_id (content-addressed), values are the serialized ast_json string.
# Linter/scanner do NOT read this dict — they fetch via HTTP GET /ast/{id} on the
# parser server so the "independently deployed" property stays honest.
AST_CACHE: Dict[str, str] = {}


class BaseReq(BaseModel):
    """Fields every tool request carries so the client can thread a trace_id."""

    trace_id: Optional[str] = None
    node_id: Optional[str] = None


def emit(
    *,
    trace_id: str,
    op: str,
    node_id: Optional[str],
    t_start_ms: int,
    t_end_ms: int,
    inputs_meta: Optional[Dict[str, Any]] = None,
    outputs_meta: Optional[Dict[str, Any]] = None,
    args_hash: Optional[str] = None,
    payload_in_bytes: Optional[int] = None,
    payload_out_bytes: Optional[int] = None,
    stage_ms: Optional[Dict[str, int]] = None,
    status_code: Optional[int] = 200,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Build one EXEC_OP record and append it to the shared JSONL log."""
    record = build_exec_op_record(
        trace_id=trace_id or "tr_unknown",
        op=op,
        node_id=node_id,
        args_hash=args_hash,
        inputs_meta=inputs_meta or {},
        outputs_meta=outputs_meta or {},
        t_start_ms=t_start_ms,
        t_end_ms=t_end_ms,
        payload_in_bytes=payload_in_bytes,
        payload_out_bytes=payload_out_bytes,
        stage_ms=stage_ms,
        status_code=status_code,
        error=error,
        extra=extra,
    )
    append_jsonl(EXEC_OPS_LOG, record)


def find_tool(name: str) -> str:
    """Locate a CLI tool, preferring the venv that is running this process.

    Tools installed via ``pip install`` into a virtualenv land in the same
    ``bin`` directory as the Python interpreter but are not necessarily on the
    global PATH when a server is launched from a bare ``python3 server.py``
    invocation. Probe that location first, then fall back to ``shutil.which``.
    """
    venv_candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(venv_candidate) and os.access(venv_candidate, os.X_OK):
        return venv_candidate
    found = shutil.which(name)
    return found or name


def run_subprocess_ms(
    cmd: list[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[str, str, int, int]:
    """Run a subprocess and return (stdout, stderr, returncode, elapsed_ms)."""
    from profiler_utils import now_ms  # local import to keep import order simple

    t0 = now_ms()
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    t1 = now_ms()
    return proc.stdout, proc.stderr, proc.returncode, t1 - t0
