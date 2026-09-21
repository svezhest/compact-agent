"""Configuration loaded from environment (.env) plus runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # LLM endpoint (OpenAI-compatible) — the "model" config layer
    api_base: str | None
    api_key: str | None
    api_model: str          # MODEL_NAME (API_MODEL kept as a back-compat alias)
    max_context_tokens: int = 32000  # MAX_CONTEXT_TOKENS — the one knob that shapes a run

    # Vision endpoint for `describe` (image OCR + caption). Separate so a vision-capable
    # model can serve images while the summariser runs on a different one. Each falls
    # back to the text endpoint's value when unset.
    vision_base: str | None = None
    vision_key: str | None = None
    vision_model: str | None = None

    @staticmethod
    def load() -> "Config":
        api_base = os.getenv("API_BASE")
        api_key = os.getenv("API_KEY")
        # MODEL_NAME is the new name; API_MODEL stays as a fallback for old .env files.
        model = os.getenv("MODEL_NAME") or os.getenv("API_MODEL", "gpt-4o-mini")
        return Config(
            api_base=api_base,
            api_key=api_key,
            api_model=model,
            max_context_tokens=int(os.getenv("MAX_CONTEXT_TOKENS", "32000")),
            vision_base=os.getenv("VISION_BASE") or api_base,
            vision_key=os.getenv("VISION_KEY") or api_key,
            vision_model=os.getenv("VISION_MODEL") or model,
        )

    def require_llm(self) -> None:
        if not self.api_base or not self.api_key:
            raise SystemExit("Missing API_BASE / API_KEY. See .env.example.")
