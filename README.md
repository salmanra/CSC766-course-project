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
```

### Run the Optimized version

```bash
python client/code_review/optimized_client.py --input inputs/code_review/mixed.py
python client/code_review/optimized_client.py --all --runs 5
```

### Analyze traces

```bash
python analysis/code_review/parse_and_compare.py
```

Traces are appended (not overwritten) to
`profiler_logs/code_review_exec_ops.jsonl`.

## Inputs corpus

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

## Measured performance

TODO

## Hardware and reproducibility

- CPU-only; tested on macOS with Python 3.10+. No GPU required.
- Deterministic: all four tools (AST, ruff, bandit, TemplateBackend) are
  deterministic for the same input; the only non-deterministic value per run
  is the generated `trace_id`.
- Logs are appended, not overwritten, so multiple runs combine in the same
  JSONL. Delete the log or rotate it between experiments.

## EXEC_OP extensions used in this benchmark

Every record follows the shared `profiler_utils.build_exec_op_record`
schema. This benchmark also uses these `extra` fields:

- `client_mode: "basic" | "optimized"` — emitted once per workflow by the
  client so the analyzer can classify the trace.
- `cache_hit: true` — client-side parse memo hit.
- `speculation_hit: true` / `rollback: true` — outcome of the guard.
- `guard_ms`, `guard_action`, `real_action`, `speculative_tokens_wasted` —
  reported on every speculation event.
- `redundant_parse`, `dropped_full_report`, `dropped_cwe_refs` — emitted
  server-side by linter / scanner so the analyzer can attribute bytes saved
  to O1 and O2.

## Filling in the LLM

`server/code_review/llm_backend.py` houses both backends.
`TemplateBackend` is the deterministic default; `LocalLLMBackend` contains a
TODO-marked sketch (model load, tokenize, `model.generate(...)`) for a small
local model such as `Qwen/Qwen2.5-Coder-0.5B-Instruct`. No other file needs
to change to swap backends — select via
`summarizer_server.py --backend local`.
