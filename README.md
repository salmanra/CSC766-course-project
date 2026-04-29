# ToolIR Benchmark: `code_review`

A four-tool Python code-review pipeline built to exercise every ToolIR
inefficiency pattern. Basic and Optimized versions share the same servers and
input corpus, so the delta between them isolates the effect of each
optimization.

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

| Service       | Port | Backed by                          |
|---------------|------|------------------------------------|
| parser        | 8101 | Python stdlib `ast`                |
| linter        | 8102 | `ruff check --output-format=json`  |
| scanner       | 8103 | `bandit -f json` (via tempfile)    |
| summarizer    | 8104 | `TemplateBackend` (default) or `LocalLLMBackend` (stub) |

All four services emit `EXEC_OP` records to
`profiler_logs/code_review_exec_ops.jsonl` via the shared
`profiler_utils.build_exec_op_record` helper.

## The Basic version

The Basic client implements the workflow as a straightforward sequential
pipeline. The client calls the parser, hands the
resulting AST on to the linter and scanner, reparses the source before
summarizing so the downstream call sees a freshly validated tree, and waits
for all results before invoking the summarizer. The linter and scanner
return both structured diagnostics and pretty human-readable renderings;
the client keeps the structured fields and uses them to build the summary.

Here are some ways this workflow leaves performance on the table:

1. **Redundant invocation** — the parser is called twice per request, once
   at the start of the workflow and once as a pre-summarize validation
   pass, performing identical work for the same source.
2. **Dead output** — the linter's `full_report_text` and the scanner's
   `cwe_refs` are computed and transmitted on every call but are never used
   downstream.
3. **Unnecessary round-trip** — the full serialized AST returned by the
   parser is uploaded again to the linter and the scanner on the next hop,
   inflating request bodies by several KB per call.
4. **Control-flow branching** — the approve / request_changes decision
   depends on lint + scan results, so the summarizer is serialized behind
   both even though the decision usually points in a predictable direction
   from a quick look at the source.

## The Optimized version

The Optimized client applies four ToolIR-style optimizations that together
address every pattern above:

| #  | Optimization                        | Pattern fixed                    |
|----|-------------------------------------|----------------------------------|
| O1 | `ast_id` only; fetch via parser     | Unnecessary round-trip           |
| O2 | `?fields=` trims lint / scan output | Dead output                      |
| O3 | Client-side `parse` memo by SHA-256 | Redundant invocation             |
| O4 | Cheap guard + speculative summarize | Control-flow branching (bonus)   |

- **O1 — AST by reference, not by value.** The parser's response carries a
  content-addressed `ast_id`. The linter and scanner accept `{source,
  ast_id}` and, when no inline `ast_json` is supplied, fetch the AST
  directly from the parser via `GET /ast/{ast_id}` instead of receiving it
  through the client.
- **O2 — trim responses to what callers actually use.** Both linter and
  scanner honor a `?fields=...` query parameter so callers can opt out of
  the verbose human-readable outputs (`full_report_text`, `cwe_refs`) they
  don't consume.
- **O3 — client-side parse memo.** The optimized client caches parser
  responses keyed by `sha256(source)`. The pre-summarize validation call
  becomes a cache hit and emits a client-side `EXEC_OP` with
  `cache_hit=true`.
- **O4 — speculative execution (bonus).** A cheap guard runs a regex / LOC
  heuristic on the source and predicts the likely `action` before lint and
  scan finish. The summarizer is fired in parallel with lint + scan using
  placeholder summaries. When the guard and the real outputs agree, the
  speculative review is kept; when they disagree, the client rolls back and
  re-issues `summarize` with the real findings, logging the outcome as a
  `speculation_hit` or `rollback`.

## Repository layout

```
server/code_review/
  _shared.py            — EXEC_OP emitter, AST cache, subprocess helper
  parser_server.py      — :8101  POST /parse, GET /ast/{id}
  linter_server.py      — :8102  POST /lint?fields=...
  scanner_server.py     — :8103  POST /scan?fields=...
  summarizer_server.py  — :8104  POST /summarize
  llm_backend.py        — TemplateBackend (default) + LocalLLMBackend (stub)
client/code_review/
  basic_client.py       — unoptimized sequential pipeline
  optimized_client.py   — O1 + O2 + O3 + O4
analysis/code_review/
  parse_and_compare.py  — trace analyzer + speedup / speculation breakdown
inputs/code_review/     — 8 curated Python files (hits, misses, rollbacks)
inputs/code_review/large/ — 6 cpython stdlib files, ~11k LOC, latency-scaling corpus
scripts/code_review/
  run_all_servers.sh    — launch all four services with trap cleanup
profiler_utils.py       — shared EXEC_OP schema (do not duplicate)
profiler_logs/code_review_exec_ops.jsonl  — appended by every run
```

## Installation

Requires Python 3.10+ (CPU only).

```bash
pip install -r requirements.txt
# Installs: fastapi, uvicorn, requests, Pillow, ruff, bandit
```

Optional: to experiment with a local LLM backend, install the extras and fill
in `LocalLLMBackend.generate_review` in `server/code_review/llm_backend.py`:

```bash
pip install transformers torch accelerate
```

The stub raises `NotImplementedError` until you implement it; no other file
needs to change to swap backends.

## How to run

### Start all four servers

```bash
bash scripts/code_review/run_all_servers.sh
# Ctrl+C stops them all.
```

Override any port with `PARSER_PORT=... LINTER_PORT=... ... bash scripts/...`.
Use `--backend local` (or `SUMMARIZER_BACKEND=local`) once the LLM stub is
filled in.

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
separate logs, set the `CODE_REVIEW_EXEC_OPS_LOG` environment variable
on both servers and clients (see "Measured performance" below).

### Analyze traces

```bash
python analysis/code_review/parse_and_compare.py
python analysis/code_review/parse_and_compare.py \
  --log profiler_logs/code_review_exec_ops_large.jsonl
```

Traces are appended (not overwritten) to
`profiler_logs/code_review_exec_ops.jsonl` by default; override with the
`CODE_REVIEW_EXEC_OPS_LOG` env var.

## Inputs corpus

Two corpora ship with the benchmark:

### Small curated corpus (`inputs/code_review/`)

Eight small Python files curated so the cheap guard has a visible hit / miss
/ rollback profile:

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

The rollback cases are what the course project's speculation analysis
("under what conditions is speculation beneficial?") needs to be meaningful.

### Large corpus (`inputs/code_review/large/`)

Six unmodified files copied from the CPython 3.10 standard library,
totaling 10 988 LOC, used to make the latency-speedup story defensible.
At this scale `ruff` and `bandit` each take 100 + ms per file and the
serialized AST exceeds 100 KB, so the savings from O1 (round-trip) and
O3 (parse memo) translate into wall-clock latency reductions instead of
just byte-traffic reductions.

| File             | LOC   | Source path in CPython 3.10  |
|------------------|------:|------------------------------|
| `functools.py`   |   992 | `/usr/lib/python3.10/functools.py`   |
| `dataclasses.py` |  1453 | `/usr/lib/python3.10/dataclasses.py` |
| `pathlib.py`     |  1461 | `/usr/lib/python3.10/pathlib.py`     |
| `ast.py`         |  1709 | `/usr/lib/python3.10/ast.py`         |
| `difflib.py`     |  2056 | `/usr/lib/python3.10/difflib.py`     |
| `inspect.py`     |  3317 | `/usr/lib/python3.10/inspect.py`     |

These files are PSF-licensed; provenance and a regeneration command are
recorded in `inputs/code_review/large/NOTICE.txt`.

## Measured performance

Two corpora, each with its own purpose:

- **Small curated corpus** (`inputs/code_review/`, 8 files, 350 LOC) — designed for the speculation analysis. The 8 files give the cheap guard a 50 % hit rate with a mix of hits, misses, and rollbacks, so the speculation E[C] math (below) has meaningful data.
- **Large corpus** (`inputs/code_review/large/`, 6 cpython stdlib files, ~11 000 LOC) — designed to make the latency-speedup story defensible. Real-sized inputs push baseline ruff + bandit cost into the 100–300 ms range, where the byte-traffic savings from O1 / O2 actually translate into wall-clock latency wins.

Reproduce both with:

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

`CODE_REVIEW_EXEC_OPS_LOG` reroutes every server- and client-emitted EXEC_OP record to a separate JSONL so the two corpora's traces don't mix.

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

On this corpus `ruff` and `bandit` subprocesses dominate wall-clock (~60–80 ms per invocation) and the input files are tiny, so O1 and O2 show up primarily as byte-traffic savings rather than latency savings. O3 and O4 drive the latency win.

### Headline — Large corpus (5 runs × 6 cpython files = 30 traces / mode, 10 988 LOC total)

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
| O4 speculation                              | —        | 100 % hit rate | every large file trips the LOC>200 guard and bandit always finds something, so guard and real action agree on every trace |

On this corpus the AST round-trip alone removes ~9.8 MB of duplicated payload across 30 traces — that's the dominant absolute saving — and the latency speedup grows from 1.13× to 1.22× as ruff and bandit each take 100 + ms to chew on real-sized files. The 100 % speculation hit rate on the large corpus is *not* a sign the guard is unusually accurate; it's a corpus property. Every file is over the 200 LOC threshold (so the guard always predicts `request_changes`), and bandit always finds at least one issue in real-world stdlib code (so the real action also resolves to `request_changes`). Speculation diversity is studied on the small corpus, which is purpose-built for it.

### Speculation analysis (bonus, small corpus)

The speculation analysis runs on the small curated corpus, where the
guard hit rate is 50 % by design (see the input table above). On the
large corpus every trace is a hit — fine for latency comparisons,
useless for studying when speculation pays off.

Three-way comparison of summarize-path policies, all measured on the same
optimized run (40 traces, 50 % guard hit rate):

| Policy                                | Latency (mean) | Notes |
|---------------------------------------|---------------:|-------|
| Basic (sequential wait-then-execute)  | 86 ms          | summarize serialized after lint + scan |
| Optimized (speculation, observed E[C])| **76 ms**      | guard predicts; rollback only on miss |
| Always-expensive (synthetic, every speculation rolls back) | 77 ms | Optimized + measured rollback cost applied to every hit |

- Guard hit rate (`p`): 50.0 %
- Guard cost (`g`): < 1 ms (regex over source)
- Rollback cost (measured per-trace, N=20): mean **1.8 ms**, median 2.0 ms, IQR 0.8 ms — wall-clock of the redo `summarize` call emitted as `rollback_cost_ms` on each rollback event, not a cross-trace mean of total walls.
- Speculative tokens wasted across all rollbacks: 460
- Speculation savings vs always-expensive: ~1 ms (1.2 %)
- Speculation savings vs basic: ~10 ms (11.3 %)

Speculation is beneficial when
`guard_cost + (1 − p) · rollback_cost < serial_summarize_latency`. With
the deterministic template backend, summarize cost is tiny (~2 ms) so the
50 % guard barely moves the needle vs. always-expensive (~1 % win) but
*does* still beat the sequential basic policy (~11 % win) because
speculation also lifts summarize off the critical path. Under a real LLM
backend the serial summarize cost is much larger (typically 100s of ms
even for a 0.5 B model), so the same hit rate becomes a substantial
end-to-end win.

#### How rollback cost is measured

Each rollback in the optimized client records the wall-clock duration of
the *redo* `summarize` call directly into the EXEC_OP record's
`extra.rollback_cost_ms`. The analyzer reports mean / median / IQR over
those per-trace values and uses the mean to synthesize the
always-expensive baseline. This avoids an earlier methodology — comparing
aggregate wall-time between rollback-tagged and hit-tagged traces — that
was confounded by per-input variance and floored to zero whenever
optimized happened to win, hiding the real rollback penalty.

## Hardware and reproducibility

Performance numbers in this README were measured on:

- **OS**: macOS 15.6 (Darwin 24.x)
- **CPU**: Apple M4 (arm64, 10 cores)
- **RAM**: 16 GB unified memory
- **Python**: 3.14.4
- **Tool versions**: `ruff` 0.15.11, `bandit` 1.9.4, FastAPI / uvicorn from
  `requirements.txt`

The benchmark runs end-to-end on any CPU-only host with Python 3.10+.

The summarizer's `--backend local` path requires a **CUDA-enabled GPU**
plus the optional dependencies in `requirements-llm.txt`. On any host
without CUDA — including Apple Silicon (M1–M4), where PyTorch uses MPS
rather than CUDA — `LocalLLMBackend` transparently falls back to
`TemplateBackend` and the EXEC_OP record carries
`extra.llm_fallback_reason`. The deterministic `template` backend is the
default, so no GPU is needed to reproduce the headline numbers.

Determinism notes:

- All four tools (AST, ruff, bandit, TemplateBackend) are deterministic
  for the same input; the only non-deterministic value per run is the
  generated `trace_id`.
- Logs are appended, not overwritten, so multiple runs combine in the
  same JSONL. Delete the log or rotate it between experiments.

## EXEC_OP extensions used in this benchmark

Every record follows the shared `profiler_utils.build_exec_op_record`
schema. This benchmark also uses these `extra` fields:

- `client_mode: "basic" | "optimized"` — emitted once per workflow by the
  client so the analyzer can classify the trace.
- `cache_hit: true` — client-side parse memo hit.
- `speculation_hit: true` / `rollback: true` — outcome of the guard.
- `guard_ms`, `guard_action`, `real_action`, `speculative_tokens_wasted` —
  reported on every speculation event.
- `rollback_cost_ms` — per-trace wall-clock of the redo `summarize` call,
  emitted only on `rollback` events. Used by the analyzer to compute the
  honest rollback cost and the always-expensive synthetic baseline.
- `redundant_parse`, `dropped_full_report`, `dropped_cwe_refs` — emitted
  server-side by linter / scanner so the analyzer can attribute bytes saved
  to O1 and O2.

## LLM backend (optional)

`server/code_review/llm_backend.py` provides two backends behind the same
`generate_review(...)` interface:

- **`TemplateBackend`** — deterministic, zero-dependency default. Runs
  everywhere, no GPU required.
- **`LocalLLMBackend`** — runs a small open-source instruct model
  (`Qwen/Qwen2.5-Coder-0.5B-Instruct` by default) on a CUDA device.
  Detects CUDA at construction time via `has_cuda()`, lazy-loads the
  model on first use, and on any failure (no CUDA, missing torch /
  transformers, model download error, generation OOM) silently delegates
  to `TemplateBackend`. The model ID is overridable without editing the
  file:

      export CODE_REVIEW_LLM_MODEL="Qwen/Qwen2.5-Coder-1.5B-Instruct"

Select the backend at startup:

```bash
python server/code_review/summarizer_server.py --port 8104 --backend local
# or, for the deterministic default
python server/code_review/summarizer_server.py --port 8104 --backend template
```

To enable real model inference on a CUDA host:

```bash
pip install -r requirements-llm.txt   # transformers, torch, accelerate
python server/code_review/summarizer_server.py --port 8104 --backend local
```

`requirements-llm.txt` is intentionally separate from base
`requirements.txt` so the benchmark stays installable on hosts without
GPU support.
