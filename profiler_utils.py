# -*- coding: utf-8 -*-
"""profiler_utils.py

Low-overhead profiler helpers for emitting structured EXEC_OP JSONL records.

EXEC_OP records are the core logging primitive for ToolIR trace analysis.
Each record captures one tool invocation: timing, payload sizes, input/output
object IDs (content-addressed, never the raw payload), and stage breakdowns.

Key design rules:
  - NEVER log large base64 payloads; only content-addressed object IDs.
  - Object IDs are sha256 hashes of content, prefixed by kind:
      obj:b64img:<hash16>  — base64-encoded image
      obj:txt:<hash16>     — text string (caption, etc.)
  - append_jsonl() is thread-safe (global lock).
  - guess_bytes() estimates sizes without large copies.

Usage:
    from profiler_utils import (
        build_exec_op_record, append_jsonl,
        obj_id_from_str, now_ms, guess_bytes,
    )
"""

from __future__ import annotations

import json
import os
import threading
import time
import hashlib
import uuid
from typing import Any, Dict, Optional


_JSONL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def now_ms() -> int:
    """Return the current time in milliseconds (integer)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Object ID helpers
# ---------------------------------------------------------------------------

def _sha256_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def obj_id_from_str(s: str, kind: str = "obj") -> str:
    """Return a content-addressed object ID for a string value.

    Examples:
        obj_id_from_str(caption, kind="txt")      -> "obj:txt:a3f9c12d..."
        obj_id_from_str(image_b64, kind="b64img") -> "obj:b64img:7ec3a..."
    """
    return f"obj:{kind}:{_sha256_str(s)[:16]}"


# ---------------------------------------------------------------------------
# Byte estimation
# ---------------------------------------------------------------------------

def guess_bytes(x: Any) -> int:
    """Best-effort byte estimation without large copies."""
    if x is None:
        return 0
    if isinstance(x, (bytes, bytearray)):
        return len(x)
    if isinstance(x, str):
        return len(x.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(x, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON record as a line to a JSONL file (thread-safe)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    with _JSONL_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# EXEC_OP record builder
# ---------------------------------------------------------------------------

def build_exec_op_record(
    *,
    trace_id: str,
    op: str,
    node_id: Optional[str] = None,
    args_hash: Optional[str] = None,
    inputs_meta: Optional[Dict[str, Any]] = None,
    outputs_meta: Optional[Dict[str, Any]] = None,
    t_start_ms: int,
    t_end_ms: int,
    payload_in_bytes: Optional[int] = None,
    payload_out_bytes: Optional[int] = None,
    stage_ms: Optional[Dict[str, int]] = None,
    error: Optional[str] = None,
    status_code: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured EXEC_OP record dict.

    Fields:
        trace_id        — unique per client run (e.g. "tr_abc123")
        op              — tool name (e.g. "tool.caption_interrogate")
        node_id         — stable node name within trace (e.g. "interrogate_1")
        args_hash       — sha256 of sanitized input args (for memoization detection)
        inputs_meta     — {field: {"id": "obj:...", "bytes": N, "type": "..."}}
        outputs_meta    — {port: {"id": "obj:...", "bytes": N, "type": "..."}}
        t_start_ms      — wall-clock start (ms)
        t_end_ms        — wall-clock end (ms)
        payload_in_bytes  — request payload size
        payload_out_bytes — response payload size
        stage_ms        — optional breakdown {"decode": X, "compute": X, "encode": X}
        error           — error string if call failed, else None
        status_code     — HTTP status code
        extra           — freeform dict for additional metadata
    """
    return {
        "kind": "EXEC_OP",
        "op": op,
        "trace_id": trace_id or "tr_unknown",
        "event_id": f"ev_{uuid.uuid4().hex[:12]}",
        "node_id": node_id,
        "args_hash": args_hash,
        "inputs_meta": inputs_meta or {},
        "outputs_meta": outputs_meta or {},
        "t_start_ms": int(t_start_ms),
        "t_end_ms": int(t_end_ms),
        "latency_ms": max(0, int(t_end_ms) - int(t_start_ms)),
        "payload_in_bytes": payload_in_bytes,
        "payload_out_bytes": payload_out_bytes,
        "stage_ms": stage_ms or {},
        "error": error,
        "status_code": status_code,
        "extra": extra,
    }
