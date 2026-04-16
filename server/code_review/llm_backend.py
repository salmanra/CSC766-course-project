# -*- coding: utf-8 -*-
"""llm_backend.py — pluggable review backends for the summarizer server.

Two backends are provided:

  * TemplateBackend  — deterministic, zero-dependency. Default. Decides
                       approve/request_changes from the structured lint and
                       security summaries and renders a human-readable review.

  * LocalLLMBackend  — stub. Intended to load a small local model (e.g.,
                       Qwen2.5-Coder-0.5B-Instruct via transformers) and
                       generate a natural-language review. Left as a TODO so
                       students can fill it in without touching any other file.

Selection is via get_backend(name) where name is "template" or "local".
The summarizer server exposes a --backend CLI flag.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Protocol


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
    """Stub backend for a small local LLM (e.g., Qwen2.5-Coder-0.5B-Instruct).

    Fill this in when you want a real model in the loop. The rest of the
    benchmark (trace schema, optimizations, analyzer) does not depend on which
    backend is selected, so this is the only file you need to edit.

    Sketch of the intended implementation:

        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch

        class LocalLLMBackend:
            _model = None
            _tokenizer = None
            MODEL_ID = "Qwen/Qwen2.5-Coder-0.5B-Instruct"

            def _ensure_loaded(self):
                if LocalLLMBackend._model is None:
                    LocalLLMBackend._tokenizer = AutoTokenizer.from_pretrained(
                        self.MODEL_ID
                    )
                    LocalLLMBackend._model = AutoModelForCausalLM.from_pretrained(
                        self.MODEL_ID,
                        torch_dtype=torch.float16 if torch.cuda.is_available()
                            else torch.float32,
                        device_map="auto",
                    )

            def generate_review(self, source, lint_summary, sec_summary):
                self._ensure_loaded()
                prompt = build_prompt(source, lint_summary, sec_summary)
                # ...tokenize, model.generate(...), decode...
                # return {"review_text": ..., "action": _decide_action(...),
                #         "tokens_in": ..., "tokens_out": ...}
    """

    name = "local"

    def __init__(self) -> None:
        # TODO: load tokenizer + model here; cache as class-level singletons
        # so repeated requests amortize the load cost.
        pass

    def generate_review(
        self,
        source: str,
        lint_summary: Dict[str, Any],
        sec_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            "LocalLLMBackend is a stub. Install transformers + torch and fill in "
            "the body of generate_review in server/code_review/llm_backend.py."
        )


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
