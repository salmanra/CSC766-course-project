# -*- coding: utf-8 -*-
"""llm_backend.py — pluggable review backends for the summarizer server.

Two backends are provided:

  * TemplateBackend  — deterministic, zero-dependency default. Decides
                       approve / request_changes from the structured lint
                       and security summaries and renders a human-readable
                       review.

  * LocalLLMBackend  — runs a small open-source instruct model (default:
                       Qwen/Qwen2.5-Coder-0.5B-Instruct) on a CUDA device
                       when one is available. Detects CUDA at construction
                       time via has_cuda() and on first use; on any failure
                       (no GPU, missing torch/transformers, model load
                       error, generation OOM) it transparently delegates to
                       TemplateBackend. The model ID is overridable via
                       the CODE_REVIEW_LLM_MODEL env var.

Selection is via get_backend(name) where name is "template" or "local".
The summarizer server exposes a --backend CLI flag.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Protocol


def has_cuda() -> bool:
    """True iff `torch` is importable AND a CUDA device is visible.

    Defensive in two layers: catches ImportError when torch isn't installed
    at all, and catches any other exception from torch.cuda probing
    (driver mismatch, etc.) so callers always get a clean bool.
    """
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


class ReviewBackend(Protocol):
    def generate_review(
        self,
        source: str,
        lint_summary: Dict[str, Any],
        sec_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...


_SEVERITY_OK = {"LOW", "NONE", "UNDEFINED", ""}


def _decide_action(lint_summary: Dict[str, Any], sec_summary: Dict[str, Any]) -> str:
    """Shared decision rule so the two backends agree on the approve/reject label."""
    counts = (lint_summary or {}).get("counts", {}) or {}
    errors = int(counts.get("error", 0) or 0)
    sev_max = str((sec_summary or {}).get("severity_max", "") or "").upper()
    if errors == 0 and sev_max in _SEVERITY_OK:
        return "approve"
    return "request_changes"


class TemplateBackend:
    """Deterministic review formatter. No model dependencies."""

    name = "template"

    def generate_review(
        self,
        source: str,
        lint_summary: Dict[str, Any],
        sec_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        action = _decide_action(lint_summary, sec_summary)
        counts = (lint_summary or {}).get("counts", {}) or {}
        top = (lint_summary or {}).get("top3", []) or (
            lint_summary.get("diagnostics", []) if lint_summary else []
        )
        top = top[:3] if top else []
        issues = (sec_summary or {}).get("issues", []) or []
        sev_max = (sec_summary or {}).get("severity_max", "NONE")

        lines = [
            f"### Review ({action})",
            f"- Lint: {counts.get('error', 0)} error(s), "
            f"{counts.get('warning', 0)} warning(s), {counts.get('info', 0)} info",
            f"- Security severity_max: {sev_max} ({len(issues)} issue(s))",
        ]
        if top:
            lines.append("- Top lint findings:")
            for d in top:
                code = d.get("code") or d.get("rule") or "?"
                msg = d.get("message", "")
                line_no = d.get("line") or d.get("location", {}).get("row", "?")
                lines.append(f"    * L{line_no} [{code}] {msg}")
        if issues:
            lines.append("- Security findings:")
            for sec in issues[:3]:
                test = sec.get("test_id") or sec.get("code") or "?"
                msg = sec.get("issue_text") or sec.get("message", "")
                line_no = sec.get("line_number") or sec.get("line", "?")
                lines.append(f"    * L{line_no} [{test}] {msg}")

        # Deterministic token accounting so the benchmark is reproducible.
        review_text = "\n".join(lines)
        tokens_out = len(review_text.split())
        tokens_in = len(source.split()) + len(str(lint_summary).split())

        return {
            "review_text": review_text,
            "action": action,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }


class LocalLLMBackend:
    """Local LLM-backed reviewer with automatic CUDA detection and fallback.

    On a host that has both ``torch`` (with CUDA support) installed *and* a
    visible CUDA device, this backend lazily loads a small open-source
    instruct model on first use and answers ``/summarize`` requests with
    real generated text.

    On any other host — no CUDA, ``torch`` not installed, model download
    failure, generation OOM — it transparently delegates to
    :class:`TemplateBackend` so the rest of the benchmark keeps running
    unmodified. The fallback decision is made at construction time
    (``has_cuda()`` is cheap) and at first-use time (HF model load), and is
    latched once it has happened so we don't re-pay the failure cost on
    every request.

    The model ID can be overridden without editing this file:

        export CODE_REVIEW_LLM_MODEL="Qwen/Qwen2.5-Coder-1.5B-Instruct"
    """

    name = "local"

    MODEL_ID = os.environ.get(
        "CODE_REVIEW_LLM_MODEL",
        "Qwen/Qwen2.5-Coder-0.5B-Instruct",
    )

    # Class-level singletons so the model loads at most once per process.
    _tokenizer = None
    _model = None
    # Latched on first failure so we don't keep retrying an unrecoverable load.
    _load_failed = False

    def __init__(self) -> None:
        self._cuda_available = has_cuda()
        # Eager-instantiate the fallback so request-time can never re-cross
        # the import boundary if the LLM path goes sideways.
        self._fallback = TemplateBackend()
        if not self._cuda_available:
            print("[llm_backend] no CUDA device visible; "
                  "LocalLLMBackend will fall back to TemplateBackend.")

    # ------------------------------------------------------------------
    # Lazy load
    # ------------------------------------------------------------------

    @classmethod
    def _ensure_loaded(cls) -> bool:
        """Return True iff the tokenizer + model are ready for inference."""
        if cls._load_failed:
            return False
        if cls._tokenizer is not None and cls._model is not None:
            return True
        try:
            import torch  # noqa: F401 — used below
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            print(f"[llm_backend] transformers/torch not installed ({exc}); "
                  "falling back to template. Install with: "
                  "pip install -r requirements-llm.txt")
            cls._load_failed = True
            return False
        try:
            import torch
            print(f"[llm_backend] loading model {cls.MODEL_ID} on CUDA…")
            cls._tokenizer = AutoTokenizer.from_pretrained(cls.MODEL_ID)
            cls._model = AutoModelForCausalLM.from_pretrained(
                cls.MODEL_ID,
                torch_dtype=torch.float16,
                device_map="cuda",
            )
            cls._model.eval()
            print(f"[llm_backend] model {cls.MODEL_ID} ready.")
            return True
        except Exception as exc:
            print(f"[llm_backend] model load failed ({exc!r}); "
                  "falling back to template for the rest of this run.")
            cls._tokenizer = None
            cls._model = None
            cls._load_failed = True
            return False

    # ------------------------------------------------------------------
    # Prompt + verdict parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        source: str,
        lint_summary: Dict[str, Any],
        sec_summary: Dict[str, Any],
    ) -> str:
        recommended = _decide_action(lint_summary, sec_summary)
        counts = (lint_summary or {}).get("counts", {}) or {}
        sev_max = (sec_summary or {}).get("severity_max", "NONE")
        return (
            "You are reviewing a Python file. Write a brief code review and "
            "end with exactly one of these tokens on its own line: "
            "VERDICT=approve or VERDICT=request_changes.\n\n"
            f"Lint: {counts.get('error', 0)} error(s), "
            f"{counts.get('warning', 0)} warning(s), "
            f"{counts.get('info', 0)} info.\n"
            f"Security severity_max: {sev_max}.\n"
            f"Recommended verdict (override if you disagree): {recommended}.\n\n"
            "Source:\n```python\n"
            + source
            + "\n```\n\nReview:\n"
        )

    @staticmethod
    def _parse_verdict(text: str, fallback: str) -> str:
        """Last ``VERDICT=...`` line wins. Falls back to ``fallback`` if absent."""
        verdict = fallback
        for line in text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("verdict="):
                tok = stripped.split("=", 1)[1].strip()
                if tok in {"approve", "request_changes"}:
                    verdict = tok
        return verdict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_review(
        self,
        source: str,
        lint_summary: Dict[str, Any],
        sec_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Cold path: no CUDA → straight to template.
        if not self._cuda_available:
            out = self._fallback.generate_review(source, lint_summary, sec_summary)
            out["backend_used"] = "template"
            out["llm_fallback_reason"] = "no_cuda"
            return out

        if not LocalLLMBackend._ensure_loaded():
            out = self._fallback.generate_review(source, lint_summary, sec_summary)
            out["backend_used"] = "template"
            out["llm_fallback_reason"] = "load_failed"
            return out

        try:
            import torch
            prompt = self._build_prompt(source, lint_summary, sec_summary)
            inputs = LocalLLMBackend._tokenizer(prompt, return_tensors="pt").to("cuda")
            with torch.inference_mode():
                output_ids = LocalLLMBackend._model.generate(
                    **inputs,
                    max_new_tokens=256,
                    do_sample=False,
                )
            full = LocalLLMBackend._tokenizer.decode(
                output_ids[0], skip_special_tokens=True,
            )
            review_text = (
                full[len(prompt):].strip() if full.startswith(prompt) else full.strip()
            )
            recommended = _decide_action(lint_summary, sec_summary)
            action = self._parse_verdict(review_text, fallback=recommended)
            tokens_in = int(inputs.input_ids.shape[-1])
            tokens_out = int(output_ids.shape[-1] - tokens_in)
            return {
                "review_text": review_text,
                "action": action,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "backend_used": "local",
            }
        except Exception as exc:
            print(f"[llm_backend] generate_review failed ({exc!r}); "
                  "falling back to template.")
            LocalLLMBackend._load_failed = True
            out = self._fallback.generate_review(source, lint_summary, sec_summary)
            out["backend_used"] = "template"
            out["llm_fallback_reason"] = "generate_failed"
            return out


def get_backend(name: str) -> ReviewBackend:
    name = (name or "template").lower()
    if name == "template":
        return TemplateBackend()
    if name == "local":
        return LocalLLMBackend()
    raise ValueError(f"Unknown backend: {name!r}. Expected 'template' or 'local'.")


def prompt_hash(source: str, lint_summary: Dict[str, Any], sec_summary: Dict[str, Any]) -> str:
    """Stable hash over the inputs used to produce a review (useful for caching)."""
    blob = f"{source}|{lint_summary}|{sec_summary}"
    return hashlib.sha256(blob.encode("utf-8", errors="ignore")).hexdigest()[:16]
