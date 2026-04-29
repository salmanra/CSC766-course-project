# ToolIR Benchmark: `code_review`

A four-tool Python code-review pipeline built to exercise every ToolIR
inefficiency pattern. Basic and Optimized versions share the same servers and
input corpus, so the delta between them isolates the effect of each
optimization.

## Hardware setup

| Component       | Spec                                                                 |
|-----------------|----------------------------------------------------------------------|
| **CPU**         | 13th Gen Intel Core i9-13900F — 24 cores / 32 threads, max 5.6 GHz, 36 MiB L3 |
| **GPU**         | NVIDIA GeForce RTX 4090 — 24 GB GDDR6X, compute capability 8.9       |
| **GPU driver**  | 555.42.02                                                            |
| **CUDA**        | 12.5 (runtime + `nvcc` 12.5.82)                                      |
| **RAM**         | 62 GiB DDR5                                                          |
| **OS / kernel** | Ubuntu 22.04.5 LTS, Linux 6.8.0-85-generic (x86_64)                  |
| **Python**      | 3.10.12                                                              |

### Python package versions (project venv at `env/`)

| Package         | Version  | Source                  | Used for                                  |
|-----------------|----------|-------------------------|-------------------------------------------|
| `fastapi`       | 0.136.1  | `requirements.txt`      | All four tool servers                     |
| `uvicorn`       | 0.46.0   | `requirements.txt`      | ASGI server for FastAPI                   |
| `pydantic`      | 2.13.3   | `requirements.txt` (transitive) | Request models in `_shared.py`    |
| `requests`      | 2.33.1   | `requirements.txt`      | Client → server HTTP                      |
| `Pillow`        | 12.2.0   | `requirements.txt`      | Image dep carried over from base profiler |
| `ruff`          | 0.15.12  | `requirements.txt`      | Linter subprocess (`tool.lint`)           |
| `bandit`        | 1.9.4    | `requirements.txt`      | Security scanner subprocess (`tool.scan`) |
| `torch`         | optional | `requirements-llm.txt`  | LocalLLMBackend (`--backend local`) only  |
| `transformers`  | optional | `requirements-llm.txt`  | LocalLLMBackend tokenizer + model         |
| `accelerate`    | optional | `requirements-llm.txt`  | LocalLLMBackend device placement          |

```bash
python3.10 -m venv env
env/bin/pip install -r requirements.txt
# Optional, for the CUDA LLM backend only:
env/bin/pip install -r requirements-llm.txt
```

## LLM model (LocalLLMBackend)

| Field             | Value                                                              |
|-------------------|--------------------------------------------------------------------|
| Model ID          | `Qwen/Qwen2.5-Coder-0.5B-Instruct`                                 |
| Family / size     | Qwen 2.5 Coder, 0.5 B parameters                                   |
| License           | Apache 2.0                                                         |
| Loaded as         | `torch.float16` via `transformers.AutoModelForCausalLM`            |
| Device placement  | `device_map="cuda"` (single 4090; ~1 GB VRAM at fp16)              |
| Generation config | `max_new_tokens=256`, `do_sample=False`                            |
| Override          | `CODE_REVIEW_LLM_MODEL=...` env var                                |

Lazy-loaded on the first `/summarize` request, then resident for the
process lifetime. On any failure (no CUDA, missing dependency, OOM,
download error) `LocalLLMBackend` falls back to `TemplateBackend` and
tags the EXEC_OP record with `extra.llm_fallback_reason`. The headline
numbers in *Measured performance* below were produced with
`TemplateBackend`.

## Workflow

```
                          ┌────────► tool.lint   (ruff subprocess)
                          │
 client ─► tool.parse ────┤          ┌──►  tool.summarize
   │         (ast.py)     │          │        (LLM backend: template|local)
   │                      └────────► tool.scan  (bandit subprocess)
   │
   └───► (basic only)  second tool.parse "pre-summarize validation"
```

Four independently-deployed FastAPI services:

| Service       | Port | Backed by                                            |
|---------------|------|------------------------------------------------------|
| parser        | 8101 | Python stdlib `ast`                                  |
| linter        | 8102 | `ruff check --output-format=json`                    |
| scanner       | 8103 | `bandit -f json` (via tempfile)                      |
| summarizer    | 8104 | `TemplateBackend` (default) or `LocalLLMBackend`     |

Every service emits `EXEC_OP` records to
`profiler_logs/code_review_exec_ops.jsonl` via
`profiler_utils.build_exec_op_record`.

## The Basic version

A sequential pipeline that exhibits four inefficiency patterns:

1. **Redundant invocation** — parser called twice per request (once at
   the start, once as a pre-summarize validation pass).
2. **Dead output** — linter's `full_report_text` and scanner's
   `cwe_refs` transmitted on every call but unused downstream.
3. **Unnecessary round-trip** — full serialized AST shuttled from parser
   to linter and scanner via the client on every hop.
4. **Control-flow branching** — summarizer serialized behind lint + scan
   even when the action is predictable from the source.

## The Optimized version

| #  | Optimization                        | Pattern fixed                    |
|----|-------------------------------------|----------------------------------|
| O1 | `ast_id` only; fetch via parser     | Unnecessary round-trip           |
| O2 | `?fields=` trims lint / scan output | Dead output                      |
| O3 | Client-side `parse` memo by SHA-256 | Redundant invocation             |
| O4 | Cheap guard + speculative summarize | Control-flow branching (bonus)   |

- **O1.** Parser response carries a content-addressed `ast_id`. Linter
  and scanner accept `{source, ast_id}` and fetch the AST directly via
  `GET /ast/{ast_id}`.
- **O2.** Linter and scanner honor `?fields=...` so callers can skip
  `full_report_text` and `cwe_refs`.
- **O3.** Optimized client caches parser responses keyed by
  `sha256(source)`. Hits emit a client-side `EXEC_OP` with
  `cache_hit=true`.
- **O4.** A regex / lines-of-code guard predicts the action before lint
  and scan finish; summarize fires in parallel against placeholder
  summaries. On match, the speculative review is kept; on mismatch, the
  client re-issues `summarize` with real findings, logging
  `speculation_hit` or `rollback`.

## Repository layout

```
server/code_review/
  _shared.py            — EXEC_OP emitter, AST cache, subprocess helper
  parser_server.py      — :8101  POST /parse, GET /ast/{id}
  linter_server.py      — :8102  POST /lint?fields=...
  scanner_server.py     — :8103  POST /scan?fields=...
  summarizer_server.py  — :8104  POST /summarize
  llm_backend.py        — TemplateBackend (default) + LocalLLMBackend
client/code_review/
  basic_client.py       — unoptimized sequential pipeline
  optimized_client.py   — O1 + O2 + O3 + O4
analysis/code_review/
  parse_and_compare.py  — trace analyzer + speedup / speculation breakdown
inputs/code_review/     — 8 curated Python files
inputs/code_review/large/ — 6 cpython stdlib files, ~11k LOC
scripts/code_review/
  run_all_servers.sh    — launch all four services with trap cleanup
profiler_utils.py       — shared EXEC_OP schema
profiler_logs/code_review_exec_ops.jsonl  — appended by every run
```

## Installation

Requires Python 3.10+ (CPU only).

```bash
pip install -r requirements.txt
# Installs: fastapi, uvicorn, requests, Pillow, ruff, bandit
```

Optional CUDA backend dependencies:

```bash
pip install -r requirements-llm.txt   # transformers, torch, accelerate
```

## How to run

### Start all four servers

```bash
bash scripts/code_review/run_all_servers.sh
# Ctrl+C stops them all.
```

Override any port with `PARSER_PORT=... LINTER_PORT=... ... bash scripts/...`.
Use `--backend local` (or `SUMMARIZER_BACKEND=local`) to switch the
summarizer to `LocalLLMBackend`.

### Run the Basic version

```bash
python client/code_review/basic_client.py --input inputs/code_review/mixed.py
python client/code_review/basic_client.py --all --runs 5
# Large corpus
python client/code_review/basic_client.py --all --runs 5 \
  --corpus-dir inputs/code_review/large
```

### Run the Optimized version

```bash
python client/code_review/optimized_client.py --input inputs/code_review/mixed.py
python client/code_review/optimized_client.py --all --runs 5
# Large corpus
python client/code_review/optimized_client.py --all --runs 5 \
  --corpus-dir inputs/code_review/large
```

`--corpus-dir <path>` lets `--all` glob any directory; the default is the
small curated corpus. To keep the small- and large-corpus traces in
separate logs, set `CODE_REVIEW_EXEC_OPS_LOG` on both servers and clients.

### Analyze traces

```bash
python analysis/code_review/parse_and_compare.py
python analysis/code_review/parse_and_compare.py \
  --log profiler_logs/code_review_exec_ops_large.jsonl
```

Traces are appended (not overwritten); override the log path with
`CODE_REVIEW_EXEC_OPS_LOG`.

## Inputs corpus

### Small curated corpus (`inputs/code_review/`)

Eight Python files curated so the cheap guard has a 50 % hit rate over a
mix of hits, misses, and rollbacks:

| File                        | Real outcome      | Guard prediction  | Outcome   |
|-----------------------------|-------------------|-------------------|-----------|
| `clean_small.py`            | approve           | approve           | hit       |
| `lint_only.py`              | request_changes   | approve           | rollback  |
| `security_only.py`          | request_changes   | request_changes   | hit       |
| `mixed.py`                  | request_changes   | request_changes   | hit       |
| `branchy_guard_wrong.py`    | approve           | request_changes   | rollback  |
| `large_clean.py`            | approve           | request_changes   | rollback  |
| `subtle_security.py`        | request_changes   | approve           | rollback  |
| `empty.py`                  | approve           | approve           | hit       |

### Large corpus (`inputs/code_review/large/`)

Six unmodified files copied from the CPython 3.10 standard library,
totaling 10 988 lines of code:

| File             | Lines | Source path in CPython 3.10  |
|------------------|------:|------------------------------|
| `functools.py`   |   992 | `/usr/lib/python3.10/functools.py`   |
| `dataclasses.py` |  1453 | `/usr/lib/python3.10/dataclasses.py` |
| `pathlib.py`     |  1461 | `/usr/lib/python3.10/pathlib.py`     |
| `ast.py`         |  1709 | `/usr/lib/python3.10/ast.py`         |
| `difflib.py`     |  2056 | `/usr/lib/python3.10/difflib.py`     |
| `inspect.py`     |  3317 | `/usr/lib/python3.10/inspect.py`     |

Provenance and a regeneration command are recorded in
`inputs/code_review/large/NOTICE.txt`.

## Measured performance

```bash
# Small curated corpus
rm -f profiler_logs/code_review_exec_ops.jsonl
python client/code_review/basic_client.py     --all --runs 5
python client/code_review/optimized_client.py --all --runs 5
python analysis/code_review/parse_and_compare.py

# Large corpus (cpython subset)
rm -f profiler_logs/code_review_exec_ops_large.jsonl
CODE_REVIEW_EXEC_OPS_LOG=profiler_logs/code_review_exec_ops_large.jsonl \
  python client/code_review/basic_client.py     --all --runs 5 --corpus-dir inputs/code_review/large
CODE_REVIEW_EXEC_OPS_LOG=profiler_logs/code_review_exec_ops_large.jsonl \
  python client/code_review/optimized_client.py --all --runs 5 --corpus-dir inputs/code_review/large
python analysis/code_review/parse_and_compare.py --log profiler_logs/code_review_exec_ops_large.jsonl
```

### Headline — Small curated corpus (5 runs × 8 inputs = 40 traces / mode)

| Metric                          | Basic       | Optimized | Reduction |
|---------------------------------|-------------|-----------|-----------|
| End-to-end latency (mean ± std) | 86 ± 10 ms  | 76 ± 6 ms | 11.3 %    |
| Client RPC calls (total)        | 200         | 148       | 26.0 %    |
| Bytes transferred (mean / trace)| 29.4 KB     | 6.8 KB    | 76.7 %    |

End-to-end speedup: **1.13×** (10 ms absolute).

#### Per-optimization breakdown (small corpus)

| Optimization  | Before    | After    | Effect                        |
|---------------|-----------|----------|-------------------------------|
| O1 round-trip (lint + scan request bytes)   | 533.4 KB | 94.1 KB | 82.4 % fewer request bytes |
| O2 dead-output (lint + scan response bytes) | 23.5 KB  | 14.8 KB | 37.1 % fewer response bytes |
| O3 redundant invocations                    | 40 duplicate (op, args_hash) pairs | 72 cache hits | second parse + cross-run repeats served locally |
| O4 speculation                              | —        | 50 % hit rate | observed E[C] = 76 ms |

### Headline — Large corpus (5 runs × 6 cpython files = 30 traces / mode, 10 988 lines total)

| Metric                          | Basic         | Optimized     | Reduction |
|---------------------------------|---------------|---------------|-----------|
| End-to-end latency (mean ± std) | 173 ± 45 ms   | 142 ± 31 ms   | 17.8 %    |
| Client RPC calls (total)        | 150           | 96            | 36.0 %    |
| Bytes transferred (mean / trace)| 1015.8 KB     | 251.6 KB      | 75.2 %    |

End-to-end speedup: **1.22×** (31 ms absolute).

#### Per-optimization breakdown (large corpus)

| Optimization  | Before    | After    | Effect                        |
|---------------|-----------|----------|-------------------------------|
| O1 round-trip (lint + scan request bytes)   | 13.75 MB | 3.93 MB | 71.4 % fewer request bytes (~9.8 MB removed across the run) |
| O2 dead-output (lint + scan response bytes) | 135.1 KB | 85.2 KB | 36.9 % fewer response bytes |
| O3 redundant invocations                    | 30 duplicate (op, args_hash) pairs | 54 cache hits | within-run + within-trace repeats served locally |
| O4 speculation                              | —        | 100 % hit rate | every large file trips the guard's 200-line threshold and bandit always finds an issue, so guard and real action agree on every trace |

### Speculation analysis (bonus, small corpus)

| Policy                                | Latency (mean) | Notes |
|---------------------------------------|---------------:|-------|
| Basic (sequential wait-then-execute)  | 86 ms          | summarize serialized after lint + scan |
| Optimized (speculation, observed E[C])| **76 ms**      | guard predicts; rollback only on miss |
| Always-expensive (synthetic, every speculation rolls back) | 77 ms | Optimized + measured rollback cost applied to every hit |

- Guard hit rate (`p`): 50.0 %
- Guard cost (`g`): < 1 ms (regex over source)
- Rollback cost (measured per-trace, N=20): mean **1.8 ms**, median 2.0 ms, interquartile range 0.8 ms — wall-clock of the redo `summarize` call recorded as `rollback_cost_ms` on each rollback event.
- Speculative tokens wasted across all rollbacks: 460
- Speculation savings vs always-expensive: ~1 ms (1.2 %)
- Speculation savings vs basic: ~10 ms (11.3 %)

Speculation is beneficial when
`guard_cost + (1 − p) · rollback_cost < serial_summarize_latency`.

## Reproducibility notes

- Runs end-to-end on any CPU-only host with Python 3.10+.
- All four tools (AST, ruff, bandit, TemplateBackend) are deterministic for the same input; only `trace_id` varies per run.
- Logs are appended, not overwritten. Delete or rotate between experiments.
- On hosts without CUDA — including Apple Silicon (M1–M4) — `LocalLLMBackend` falls back to `TemplateBackend`.

## EXEC_OP extensions used in this benchmark

Every record follows the `profiler_utils.build_exec_op_record` schema.
This benchmark adds these `extra` fields:

- `client_mode: "basic" | "optimized"` — emitted once per workflow by the client.
- `cache_hit: true` — client-side parse memo hit.
- `speculation_hit: true` / `rollback: true` — guard outcome.
- `guard_ms`, `guard_action`, `real_action`, `speculative_tokens_wasted` — reported on every speculation event.
- `rollback_cost_ms` — wall-clock of the redo `summarize` call, emitted only on `rollback` events.
- `redundant_parse`, `dropped_full_report`, `dropped_cwe_refs` — emitted server-side by linter / scanner.
