"""Provider-agnostic model interface. No paid inference; the default does nothing.

A model may only *suggest* wording. Its output never decides eligibility, facts or submissions.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from careerpilot.config import Settings
from careerpilot.cost_guard import CostPolicyViolation, FREE_LLM_PROVIDERS


class TextModel(Protocol):
    name: str

    def suggest(self, prompt: str) -> str | None: ...


class NullModel:
    name = "none"

    def suggest(self, prompt: str) -> str | None:
        return None


class OllamaModel:
    """A model running locally through Ollama (free, on the user's own hardware)."""

    name = "ollama"

    def __init__(self, base_url: str, model: str = "llama3.1:8b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def suggest(self, prompt: str) -> str | None:
        try:
            resp = httpx.post(f"{self.base_url}/api/generate",
                              json={"model": self.model, "prompt": prompt, "stream": False}, timeout=120)
            resp.raise_for_status()
            return resp.json().get("response")
        except httpx.HTTPError:
            return None


def get_model(settings: Settings) -> TextModel:
    if settings.llm_provider not in FREE_LLM_PROVIDERS:
        raise CostPolicyViolation(f"Model provider {settings.llm_provider!r} is not allowed")
    if settings.llm_provider == "ollama":
        return OllamaModel(settings.llm_base_url)
    return NullModel()
