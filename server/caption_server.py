# -*- coding: utf-8 -*-
"""caption_server.py

Simple FastAPI caption server for the ToolIR benchmark example.

Endpoint:
    POST /caption/interrogate
    Input:  {"image": "<base64-encoded image bytes>", "model": "clip"}
    Output: {"caption": "<text>", "model": "clip"}

The server uses a FAKE caption model by default: it returns a deterministic
string derived from the image content hash. This lets students run the full
benchmark without a GPU or any ML dependencies.

To use a real model (SD WebUI backend), set --real-model and point
--sdapi-url at your A1111 instance. The real-model path is provided as a
reference; students are NOT expected to run it for the CSC766 project.

EXEC_OP records are written to:
    ../profiler_logs/caption_exec_ops.jsonl
(one level up from this file, into the profiler_logs/ directory)

Usage:
    python server/caption_server.py
    python server/caption_server.py --port 8765
    # python server/caption_server.py --real-model --sdapi-url http://127.0.0.1:7860
"""

import argparse
import base64
import hashlib
import os
import sys
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Make profiler_utils importable from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from profiler_utils import (
    append_jsonl,
    build_exec_op_record,
    guess_bytes,
    now_ms,
    obj_id_from_str,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXEC_OPS_LOG = os.path.join(
    os.path.dirname(__file__), "..", "profiler_logs", "caption_exec_ops.jsonl"
)
EXEC_OPS_LOG = os.path.normpath(EXEC_OPS_LOG)

app = FastAPI(title="ToolIR Caption Server (Example)")

# ---------------------------------------------------------------------------
# Fake model
# ---------------------------------------------------------------------------

_FAKE_CAPTIONS = [
    "a scenic landscape with mountains in the background",
    "a close-up photograph of a domestic cat",
    "an urban street scene at golden hour",
    "a bowl of colorful fresh fruit on a wooden table",
    "abstract geometric shapes in primary colors",
    "a group of people gathered in a sunlit park",
    "a vintage automobile parked on a cobblestone road",
    "a serene beach with calm turquoise water",
]


def fake_caption(image_b64: str, model: str) -> str:
    """Return a deterministic caption based on image content hash.

    Same image always produces the same caption, regardless of how many
    times you call the server. Different images produce different captions.
    This determinism is what makes client-side memoization provably correct
    in the example benchmark.
    """
    digest = hashlib.sha256(image_b64.encode("utf-8", errors="ignore")).digest()
    index = digest[0] % len(_FAKE_CAPTIONS)
    return f"[{model}] {_FAKE_CAPTIONS[index]}"


def fake_stage_ms(image_b64: str) -> dict:
    """Simulate realistic stage timing using fake sleep.

    decode  ~10ms : base64 → PIL
    compute ~200ms: model forward pass (simulated)
    encode  ~5ms  : dict serialisation
    """
    # Deterministic seed so runs are reproducible
    digest = hashlib.sha256(image_b64.encode("utf-8", errors="ignore")).digest()
    base = digest[1] % 50  # 0-49 ms variance

    t_decode_start = now_ms()
    time.sleep(0.010)  # 10ms decode
    t_decode_end = now_ms()

    t_compute_start = now_ms()
    time.sleep(0.200 + base / 1000)  # 200-250ms compute
    t_compute_end = now_ms()

    t_encode_start = now_ms()
    time.sleep(0.005)  # 5ms encode
    t_encode_end = now_ms()

    return {
        "decode": t_decode_end - t_decode_start,
        "compute": t_compute_end - t_compute_start,
        "encode": t_encode_end - t_encode_start,
    }


# ---------------------------------------------------------------------------
# Real model (commented out — for students who want to use SD WebUI)
# ---------------------------------------------------------------------------
#
# To use the real SD WebUI interrogate endpoint instead of the fake model:
#
#   1. Start A1111 WebUI with --api flag
#   2. Run: python server/caption_server.py --real-model --sdapi-url http://127.0.0.1:7860
#   3. The server will proxy /caption/interrogate → /sdapi/v1/interrogate
#
# import requests as _requests
#
# def real_caption(image_b64: str, model: str, sdapi_url: str) -> tuple[str, dict]:
#     t0 = now_ms()
#     resp = _requests.post(
#         f"{sdapi_url}/sdapi/v1/interrogate",
#         json={"image": image_b64, "model": model},
#         timeout=120,
#     )
#     resp.raise_for_status()
#     caption = resp.json().get("caption", "")
#     latency = now_ms() - t0
#     stage = {"decode": 0, "compute": latency, "encode": 0}
#     return caption, stage

# ---------------------------------------------------------------------------
# Request / response schema
# ---------------------------------------------------------------------------

class CaptionRequest(BaseModel):
    image: str   # base64-encoded image bytes (no data-URI prefix needed)
    model: str = "clip"
    # Optional: trace_id from client lets server and client logs share the same ID
    trace_id: str | None = None
    node_id: str | None = None


class CaptionResponse(BaseModel):
    caption: str
    model: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/caption/interrogate", response_model=CaptionResponse)
def caption_interrogate(req: CaptionRequest) -> CaptionResponse:
    trace_id = req.trace_id or "tr_server_unknown"
    node_id = req.node_id or "interrogate_server"

    # --- timing start ---
    t_start = now_ms()

    try:
        # Validate that image is decodable base64
        try:
            raw_bytes = base64.b64decode(req.image)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}")

        # Compute object IDs for inputs and outputs (NEVER log the raw payload)
        image_obj_id = obj_id_from_str(req.image, kind="b64img")
        payload_in_bytes = guess_bytes(req.image)

        # Run the fake (or real) model
        stage = fake_stage_ms(req.image)
        caption = fake_caption(req.image, req.model)

        caption_obj_id = obj_id_from_str(caption, kind="txt")
        payload_out_bytes = guess_bytes(caption)

        t_end = now_ms()

        # --- emit EXEC_OP record ---
        record = build_exec_op_record(
            trace_id=trace_id,
            op="tool.caption_interrogate",
            node_id=node_id,
            args_hash=image_obj_id,  # hash of primary input
            inputs_meta={
                "image": {
                    "id": image_obj_id,
                    "bytes": payload_in_bytes,
                    "type": "base64_image",
                },
                "model": {
                    "id": f"const:{req.model}",
                    "bytes": len(req.model),
                    "type": "str",
                },
            },
            outputs_meta={
                "caption": {
                    "id": caption_obj_id,
                    "bytes": payload_out_bytes,
                    "type": "str",
                }
            },
            t_start_ms=t_start,
            t_end_ms=t_end,
            payload_in_bytes=payload_in_bytes,
            payload_out_bytes=payload_out_bytes,
            stage_ms=stage,
            status_code=200,
        )
        append_jsonl(EXEC_OPS_LOG, record)
        print(f"  [server] {node_id} latency={t_end - t_start}ms "
              f"stage={stage} caption='{caption[:40]}...'")

        return CaptionResponse(caption=caption, model=req.model)

    except HTTPException:
        raise
    except Exception as exc:
        t_end = now_ms()
        record = build_exec_op_record(
            trace_id=trace_id,
            op="tool.caption_interrogate",
            node_id=node_id,
            t_start_ms=t_start,
            t_end_ms=t_end,
            error=repr(exc),
            status_code=500,
        )
        append_jsonl(EXEC_OPS_LOG, record)
        raise HTTPException(status_code=500, detail=repr(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ToolIR Caption Server (Example)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    # --real-model: described in comments above; not implemented here
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="(Not implemented in example) Proxy to SD WebUI interrogate endpoint.",
    )
    parser.add_argument(
        "--sdapi-url",
        default="http://127.0.0.1:7860",
        help="SD WebUI base URL (only used with --real-model).",
    )
    args = parser.parse_args()

    if args.real_model:
        print("NOTE: --real-model is documented in comments but not implemented "
              "in this example. Using fake model instead.")

    os.makedirs(os.path.dirname(EXEC_OPS_LOG), exist_ok=True)
    print(f"EXEC_OP log: {EXEC_OPS_LOG}")
    print(f"Starting server at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
