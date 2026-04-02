# ToolIR Benchmark Example: Caption Repeat

## Overview

This benchmark demonstrates a common tool-based workflow inefficiency: a client
calls a captioning service N times on the **same image**, performing identical
computation repeatedly. The basic version wastes both network bandwidth and server
compute on every repeat. The optimized version applies **client-side result
memoization** keyed by `(image_hash, model)`, reducing N server calls to 1 and
delivering a proportional speedup. This is a clean, self-contained example of the
**memoization / result caching** optimization class in ToolIR.

---

## Workflow Description

### Basic Version

```
Client → POST /caption/interrogate → Server   (call 1: image + model)
Client → POST /caption/interrogate → Server   (call 2: SAME image!)
Client → POST /caption/interrogate → Server   (call 3: SAME image!)
Client → POST /caption/interrogate → Server   (call 4: SAME image!)
Client → POST /caption/interrogate → Server   (call 5: SAME image!)

Inefficiency: identical computation repeated N times.
              Full image transferred over the network on every call.
              Server runs the model N times for the same input.
```

### Optimized Version

```
Client → POST /caption/interrogate → Server   (call 1: compute + cache)
Client → [CACHE HIT, no RPC]                  (call 2: served locally)
Client → [CACHE HIT, no RPC]                  (call 3: served locally)
Client → [CACHE HIT, no RPC]                  (call 4: served locally)
Client → [CACHE HIT, no RPC]                  (call 5: served locally)

Optimization: result memoization keyed by (image_hash, model).
              Only 1 server call. Remaining 4 served from in-process cache.
```

---

## Optimizations Applied

- **Result memoization / deduplication**: cache the server response keyed by
  `sha256(image_bytes):model`. On a cache hit, return the stored result without
  making an RPC call or sending any data over the network.

This is safe because the caption function is **deterministic and side-effect-free**:
for the same image and model, the server always returns the same caption.

---

## Installation

```bash
pip install -r requirements.txt
```

No GPU or ML framework is required. The server uses a fake caption model that
returns deterministic captions based on image content hash.

---

## How to Run

### Step 1: Start the server

```bash
python server/caption_server.py
```

The server listens on `http://127.0.0.1:8765` by default.
EXEC_OP records are written to `profiler_logs/caption_exec_ops.jsonl`.

### Step 2: Run the basic (unoptimized) version

```bash
python client/basic_client.py --image <path/to/image.jpg> --repeats 5
```

Every call sends the full image to the server. All 5 calls are real RPCs.

### Step 3: Run the optimized version

```bash
python client/optimized_client.py --image <path/to/image.jpg> --repeats 5
```

Only the first call hits the server. Calls 2–5 are served from the local cache.

### Step 4: Analyze the traces

```bash
python analysis/parse_and_compare.py
```

Reads `profiler_logs/caption_exec_ops.jsonl` and prints a structured comparison
of the two traces, including detected memoization opportunities.

---

## Performance Results (Reference Machine)

> Run with `--repeats 5` on a standard development laptop (no GPU).
> The fake model sleeps for ~215ms to simulate realistic compute time.

| Version   | Latency  | RPC Calls | Data Transferred |
|-----------|----------|-----------|------------------|
| Basic     | ~1100ms  | 5         | ~5.2MB           |
| Optimized | ~220ms   | 1         | ~1.0MB           |
| Speedup   | ~5.0×    | 5.0×      | 5.0×             |

Hardware: Laptop CPU, no GPU required (fake model)
Input: any JPEG/PNG image file

*Exact numbers depend on image size and system load.*

---

## EXEC_OP Record Format

Each tool invocation emits one EXEC_OP record to the JSONL log:

```json
{
  "kind": "EXEC_OP",
  "op": "tool.caption_interrogate",
  "trace_id": "tr_abc123def456",
  "event_id": "ev_0011223344aa",
  "node_id": "interrogate_1",
  "args_hash": "obj:b64img:a3f9c12d45e6f789",
  "inputs_meta": {
    "image": {"id": "obj:b64img:a3f9c12d45e6f789", "bytes": 102400, "type": "base64_image"},
    "model": {"id": "const:clip", "bytes": 4, "type": "str"}
  },
  "outputs_meta": {
    "caption": {"id": "obj:txt:bc12de34f5678901", "bytes": 48, "type": "str"}
  },
  "t_start_ms": 1711900000000,
  "t_end_ms":   1711900000215,
  "latency_ms": 215,
  "payload_in_bytes": 102400,
  "payload_out_bytes": 48,
  "stage_ms": {"decode": 10, "compute": 200, "encode": 5},
  "error": null,
  "status_code": 200,
  "extra": {"cache_hit": false}
}
```

Key fields:
- `trace_id` — unique per client run; shared across all nodes in one workflow
- `args_hash` — content-addressed hash of inputs (never the raw payload)
- `inputs_meta` / `outputs_meta` — object IDs and sizes (no raw data)
- `stage_ms` — server-side breakdown: decode → compute → encode
- `extra.cache_hit` — `true` for client-side cache hits (latency_ms = 0)

See Appendix A in the course project description for the complete schema.

---

## How to Adapt This for Your Own Benchmark

1. **Replace `server/caption_server.py`** with your own tool server.
   Keep the `build_exec_op_record` / `append_jsonl` calls exactly as shown.

2. **Replace `client/basic_client.py`** with your unoptimized workflow.
   Pick a `trace_id` at the start of each run and thread it through all calls.

3. **Keep the EXEC_OP logging pattern** exactly as shown — same field names,
   same object ID format (`obj:<kind>:<sha256_prefix>`).

4. **Add your own `optimized_client.py`** that applies one or more of:
   - Result memoization (shown here)
   - Operator fusion (merge two sequential API calls into one)
   - Dead output elimination (suppress unused server outputs)

5. **Run `analysis/parse_and_compare.py`** — it works on any EXEC_OP log that
   follows this schema. Add your own analysis logic as needed.
