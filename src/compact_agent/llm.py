"""Shared OpenAI-compatible LLM client + retry helper.

Both `compact` and `read_photos` talk to the same endpoint, so the agent build
(with a configurable request timeout) and the transient-5xx retry loop live here
once instead of being copied into each command.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich.console import Console

from .config import Config

console = Console()

# Per-request READ cap on a single LLM call: how long to wait for the model's
# response. Generous enough for a large batch / slow vision pass, but NOT 20 min —
# the upstream proxy occasionally accepts a connection then hangs silently (no 5xx,
# no bytes). A huge read timeout turns one such silent hang into a 20-min stall,
# multiplied by the retry loop (~100 min on a single request). A bounded read makes
# a hung request fail fast and retry against the usually-healthy endpoint.
DEFAULT_TIMEOUT = 4 * 60  # seconds (read)
# Connecting is quick or never — fail fast so a dead route doesn't eat the read budget.
_CONNECT_TIMEOUT = 15  # seconds

# The nginx proxy in front of the model returns transient 5xx (notably 504
# Gateway Time-out) on a slow upstream. Retry with exponential backoff rather
# than letting one timeout abort an otherwise-resumable run.
_MAX_RETRIES = 5
_RETRY_BACKOFF = 5  # seconds; doubled each attempt
# A single batch makes several tool-call round-trips (one per file edit, plus reads
# and retries). pydantic-ai re-sends the ENTIRE growing conversation on every round,
# so an unbounded run can overflow the context window mid-pass (a real run died at
# ~262k tokens after a ~100-call "whole-picture" pass). Cap the rounds per run to a
# sane default; callers pass a tighter, budget-derived limit when a window is known.
_REQUEST_LIMIT = 40


class ContextOverflowError(Exception):
    """A single request exceeded the model's context window (HTTP 400, context-length).

    This is NOT a generic 4xx and NOT a transient 5xx: retrying the identical request
    is futile. It is a RECOVERABLE signal — the caller should ensure the latest good
    state is checkpointed, skip/abort just this pass, and continue the run rather than
    crashing a multi-hour compaction."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# Substrings (lower-cased) that mark a 400 as a context-length overflow across the
# various OpenAI-compatible gateways/wordings we have seen.
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "longer than",
    "maximum context",
    "maximum length",
    "too many tokens",
    "reduce the length",
    "exceeds the model",
)


def _is_context_overflow(e: ModelHTTPError) -> bool:
    """True if a ModelHTTPError is a 400 caused by exceeding the context window."""
    if e.status_code != 400:
        return False
    blob = str(e).lower()
    body = getattr(e, "body", None)
    if body is not None:
        blob += " " + str(body).lower()
    return any(m in blob for m in _CONTEXT_OVERFLOW_MARKERS)


# Connection-layer failures are transient infrastructure hiccups (a real run also died
# on `[Errno -2] Name or service not known`), so retry them with backoff like a 5xx.
try:  # openai wraps socket/DNS errors in APIConnectionError
    from openai import APIConnectionError as _OpenAIConnError  # type: ignore
    _CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
        httpx.TransportError, _OpenAIConnError, ConnectionError,
    )
except Exception:  # pragma: no cover - openai layout drift
    _CONNECTION_ERRORS = (httpx.TransportError, ConnectionError)


def _build_model(cfg: Config, timeout: float | None, *, vision: bool = False):
    read = DEFAULT_TIMEOUT if timeout is None else timeout
    # Bound connect/pool tightly; let read carry the generation time. A silent upstream
    # hang now fails in `read` seconds and retries, instead of blocking for 20 minutes.
    http_timeout = httpx.Timeout(read, connect=_CONNECT_TIMEOUT, pool=_CONNECT_TIMEOUT)
    base, key, model = (
        (cfg.vision_base, cfg.vision_key, cfg.vision_model)
        if vision
        else (cfg.api_base, cfg.api_key, cfg.api_model)
    )
    return OpenAIModel(
        model,
        provider=OpenAIProvider(
            base_url=base, api_key=key, http_client=httpx.AsyncClient(timeout=http_timeout)
        ),
    )


def build_agent(
    cfg: Config, *, output_type, system_prompt: str, timeout: float | None = None, vision: bool = False
):
    """Build a pydantic-ai Agent against the configured endpoint (LLM, or the vision one)."""
    return Agent(
        _build_model(cfg, timeout, vision=vision), output_type=output_type, system_prompt=system_prompt
    )


# Fallback if tool-schema introspection ever breaks across a pydantic-ai upgrade:
# the writer tools' JSON schemas measured ~1.6k chars (~408 tok) at 1.105.0.
_TOOL_SCHEMA_TOKENS_FALLBACK = 408


def measure_tool_schema_tokens() -> int:
    """Tokens the writer's tool-call JSON schemas occupy in the request.

    Measured EMPIRICALLY from the actually-registered tools (name + description +
    parameter JSON schema) rather than guessed, so the fixed-overhead budget stays
    honest. Builds a throwaway engine writer agent (no network) over an empty store and
    inspects its function toolset; falls back to a measured constant if the internal
    layout changes under us. Lazy imports avoid an engine<->llm import cycle."""
    import json

    from .render import estimate_tokens

    try:
        from .engine import build_writer_agent
        from .sections.store import VStore

        stub = Config(
            api_id=None, api_hash=None, session="s",
            api_base="http://x", api_key="k", api_model="m",
        )
        agent = build_writer_agent(
            stub, VStore(sections=[], known_keys=set()), system_prompt=""
        )
        blob = "".join(
            json.dumps({
                "name": t.tool_def.name,
                "description": t.tool_def.description,
                "parameters": t.tool_def.parameters_json_schema,
            })
            for t in agent._function_toolset.tools.values()
        )
        return estimate_tokens(blob)
    except Exception:  # pragma: no cover - defensive against pydantic-ai drift
        return _TOOL_SCHEMA_TOKENS_FALLBACK


async def run_with_retries(agent: Agent, prompt, *, request_limit: int | None = None):
    """Run an agent, classifying failures so one bad call can't kill a long run.

    * transient 5xx — retry with exponential backoff.
    * connection/DNS errors — retry with backoff (transient infra hiccups).
    * context-length 400 — raise ``ContextOverflowError`` (recoverable; the caller
      checkpoints and skips this pass, never crashes the whole run).
    * any other 4xx — raise immediately (our fault; retrying won't help).

    ``request_limit`` bounds the tool-call rounds in this single run so the resent
    conversation can't balloon past the window; defaults to ``_REQUEST_LIMIT``.

    Returns ``None`` if the run hit ``request_limit`` — pydantic-ai signals that by
    RAISING ``UsageLimitExceeded`` rather than stopping cleanly, but the tool calls
    made before the cap already mutated the store, so reaching the cap is a graceful
    stop (partial work done + persisted), NOT a crash. Callers must tolerate ``None``."""
    request_limit = _REQUEST_LIMIT if request_limit is None else request_limit
    delay = _RETRY_BACKOFF
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await agent.run(
                prompt, usage_limits=UsageLimits(request_limit=request_limit)
            )
        except UsageLimitExceeded:
            # Hit the per-run tool-call cap. The edits up to here already landed in the
            # store; bounding the run is the POINT, so stop gracefully instead of
            # crashing the whole compaction.
            console.print(
                f"[yellow]Reached the per-run tool-call cap ({request_limit})[/yellow] "
                "— stopping this pass; work so far is kept."
            )
            return None
        except ModelHTTPError as e:
            if _is_context_overflow(e):
                raise ContextOverflowError(str(e), status_code=e.status_code) from e
            if e.status_code < 500 or attempt == _MAX_RETRIES:
                raise  # 4xx is our fault, or we're out of attempts
            console.print(
                f"[yellow]Model returned {e.status_code}[/yellow] "
                f"(attempt {attempt}/{_MAX_RETRIES}) — retrying in {delay}s ..."
            )
            await asyncio.sleep(delay)
            delay *= 2
        except _CONNECTION_ERRORS as e:
            if attempt == _MAX_RETRIES:
                raise
            console.print(
                f"[yellow]Connection error[/yellow] ({e!r}) "
                f"(attempt {attempt}/{_MAX_RETRIES}) — retrying in {delay}s ..."
            )
            await asyncio.sleep(delay)
            delay *= 2
