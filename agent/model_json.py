"""Robust, dependency-free decoding of a local model's JSON object reply.

This is not general-purpose agent output: Director/Operator/Reviewer must return
one JSON object. Malformed/empty output must fail with actionable diagnostics.
"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_model_json(message: Any) -> dict[str, Any]:
    content = getattr(message, "content", None)
    if isinstance(content, list):
        content = "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, (str, dict))
        )
    if not isinstance(content, str):
        raise ValueError("Ollama returned no textual content for the expected JSON object.")

    # Defensive: older model/library versions can embed thinking in main text.
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    if not cleaned:
        meta = getattr(message, "response_metadata", {}) or {}
        reason = meta.get("done_reason", "unknown")
        tokens = meta.get("eval_count", "unknown")
        raise ValueError(
            f"Ollama returned an empty JSON response (done_reason={reason}, output_tokens={tokens}). "
            "For Qwen 3.5, keep reasoning=False and increase num_predict if done_reason=length. "
            "Make sure your local Ollama and langchain-ollama versions are up to date."
        )

    # Plain object first (no extra parsing required).
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        # Some models wrap the single JSON object in a markdown block or add a
        # short preamble. Walk to a valid object rather than blindly taking the
        # first/last braces (which breaks on nested objects and quoted braces).
        decoder = json.JSONDecoder()
        value = None
        for match in re.finditer(r"\{", cleaned):
            try:
                candidate, _end = decoder.raw_decode(cleaned, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            excerpt = cleaned[:280].replace("\n", " ")
            raise ValueError(
                f"Ollama response was not valid JSON. First 280 chars: {excerpt!r}. "
                "The scene was not modified."
            ) from None

    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object, got {type(value).__name__}.")
    return value
