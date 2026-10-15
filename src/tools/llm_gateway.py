"""
LLM Gateway — multi-provider async LLM client with layered resilience.

5-layer retry architecture:
    Layer 1: Connection Retry      (tenacity, N attempts, exponential backoff)
    Layer 2: Empty-Response Retry  (transient empty/truncated completions)
    Layer 3: Provider Failover     (rotate to backup endpoint)
    Layer 4: Circuit Breaker       (open after error-rate threshold, half-open recovery)
    Layer 5: Intent-level Re-run   (caller marks an intent retriable)

Providers supported: OpenAI-compatible (/v1/chat/completions) and Anthropic
(/v1/messages). Any OpenAI-compatible base_url works for custom endpoints.

All HTTP is done through ``httpx.AsyncClient``; a client can be injected for
testing via :class:`LLMProvider` / :class:`LLMPool`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ProviderType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"


@dataclass
class EndpointConfig:
    """Configuration for a single LLM endpoint."""
    base_url: str
    api_key: str
    model: str
    provider: ProviderType = ProviderType.OPENAI
    timeout: float = 60.0
    max_tokens: int = 4096
    temperature: float = 0.2
    extra_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass
class LLMCallResult:
    """Outcome of a single logical LLM call (after all retry layers)."""
    success: bool
    content: str = ""
    error: Optional[str] = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    attempts: int = 0
    provider: Optional[str] = None
    model: Optional[str] = None
    retriable: bool = False  # Layer 5 hint: caller may re-run the intent


class LLMError(Exception):
    """Base error raised by the LLM gateway."""


class LLMEmptyResponseError(LLMError):
    """Provider returned a 200 but an empty/whitespace completion."""


class LLMProviderError(LLMError):
    """Provider returned a non-2xx or an unrecoverable error."""


# ---------------------------------------------------------------------------
# Circuit Breaker (Layer 4)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Sliding-window error-rate circuit breaker.

    States:
        closed   → calls pass through; failures recorded
        open     → calls short-circuit until ``recovery_seconds`` elapse
        half_open→ a single trial call is allowed; success closes, failure re-opens
    """

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        error_rate_threshold: float = 0.5,
        min_calls: int = 5,
        recovery_seconds: float = 30.0,
    ) -> None:
        self.window_seconds = window_seconds
        self.error_rate_threshold = error_rate_threshold
        self.min_calls = min_calls
        self.recovery_seconds = recovery_seconds
        self._events: List[tuple[float, bool]] = []  # (timestamp, ok)
        self._opened_at: Optional[float] = None
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._events = [(t, ok) for (t, ok) in self._events if t >= cutoff]

    async def allow_request(self) -> bool:
        """Return True if a call is permitted right now."""
        async with self._lock:
            now = time.monotonic()
            if self._opened_at is not None:
                if now - self._opened_at >= self.recovery_seconds:
                    return True  # half-open: allow one trial call
                return False
            return True

    async def record_success(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._events.append((now, True))
            self._prune(now)
            if self._opened_at is not None:
                self._opened_at = None  # success while half-open closes breaker

    async def record_failure(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._events.append((now, False))
            self._prune(now)
            if len(self._events) < self.min_calls:
                return
            failures = sum(1 for _, ok in self._events if not ok)
            rate = failures / len(self._events)
            if rate >= self.error_rate_threshold and self._opened_at is None:
                self._opened_at = now
                logger.warning(
                    "CircuitBreaker OPEN — error_rate=%.2f over %d calls",
                    rate, len(self._events),
                )

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (time.monotonic() - self._opened_at) < self.recovery_seconds

    @property
    def error_rate(self) -> float:
        self._prune(time.monotonic())
        if not self._events:
            return 0.0
        return sum(1 for _, ok in self._events if not ok) / len(self._events)


# ---------------------------------------------------------------------------
# Provider (single endpoint) — Layers 1 & 2
# ---------------------------------------------------------------------------

class LLMProvider:
    """A single LLM endpoint with connection + empty-response retries."""

    def __init__(
        self,
        config: EndpointConfig,
        *,
        client: Optional[httpx.AsyncClient] = None,
        max_connection_attempts: int = 3,
        max_empty_attempts: int = 2,
    ) -> None:
        self.config = config
        self._max_connection_attempts = max_connection_attempts
        self._max_empty_attempts = max_empty_attempts
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_request(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        cfg = self.config
        if cfg.provider == ProviderType.ANTHROPIC:
            url = f"{cfg.base_url.rstrip('/')}/v1/messages"
            headers = {
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                **cfg.extra_headers,
            }
            system_parts = [m["content"] for m in messages if m.get("role") == "system"]
            body: Dict[str, Any] = {
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "temperature": cfg.temperature,
                "messages": [m for m in messages if m.get("role") != "system"],
            }
            if system_parts:
                body["system"] = "\n\n".join(system_parts)
            return url, headers, body

        base = cfg.base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers = {
            "authorization": f"Bearer {cfg.api_key}",
            "content-type": "application/json",
            **cfg.extra_headers,
        }
        body = {
            "model": cfg.model,
            "messages": list(messages),
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        return url, headers, body

    def _parse_response(self, data: Dict[str, Any]) -> tuple[str, TokenUsage]:
        cfg = self.config
        if cfg.provider == ProviderType.ANTHROPIC:
            blocks = data.get("content", [])
            content = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            usage = data.get("usage", {})
            return content.strip(), TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            )
        choices = data.get("choices", [])
        content = ""
        if choices:
            message = choices[0].get("message", {})
            content = (message.get("content") or "").strip()
        usage = data.get("usage", {})
        cache_read = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_read = details.get("cached_tokens", 0)
        return content, TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cache_read_tokens=cache_read,
        )


    async def _raw_call(self, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        url, headers, body = self._build_request(messages)
        client = self._get_client()
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise LLMProviderError(f"provider HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def call(self, messages: Sequence[Mapping[str, Any]]) -> LLMCallResult:
        """
        Execute a chat completion with Layer-1 (connection) and
        Layer-2 (empty response) retries. Raises on terminal failure so the
        pool (Layer 3) can decide whether to fail over.
        """
        started = time.monotonic()
        attempts = 0
        last_error: Optional[Exception] = None

        # Layer 2 loop: retry a couple of times on empty completions.
        for _ in range(self._max_empty_attempts):
            data: Dict[str, Any] = {}
            # Layer 1 loop: connection/transient retries with exponential backoff.
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self._max_connection_attempts),
                    wait=wait_exponential(multiplier=1, min=1, max=10),
                    retry=retry_if_exception_type(
                        (httpx.TransportError, httpx.TimeoutException)
                    ),
                    reraise=True,
                ):
                    with attempt:
                        attempts += 1
                        data = await self._raw_call(messages)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning("LLM connection retries exhausted: %s", exc)
                break
            except LLMProviderError as exc:
                last_error = exc
                logger.warning("LLM provider error: %s", exc)
                break

            content, usage = self._parse_response(data)
            if content:
                return LLMCallResult(
                    success=True,
                    content=content,
                    token_usage=usage,
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempts=attempts,
                    provider=self.config.provider.value,
                    model=self.config.model,
                )
            logger.info("LLM returned empty completion (attempt %d) — retrying", attempts)

        # Terminal failure: propagate the underlying error so the pool's
        # failover logic sees the true cause (connection vs provider vs empty).
        if last_error is not None:
            raise last_error
        raise LLMEmptyResponseError(
            f"empty completion after {attempts} attempts: no content"
        )


# ---------------------------------------------------------------------------
# Pool (multi-provider) — Layers 3, 4, 5
# ---------------------------------------------------------------------------

class LLMPool:
    """
    Multi-provider pool with failover + circuit breakers.

    call() rotates through healthy providers; the circuit breaker (Layer 4)
    skips providers that are erroring. If all providers fail, the result is
    marked ``retriable=True`` (Layer 5) so the caller can re-run the intent.
    """

    def __init__(
        self,
        providers: List[LLMProvider],
        *,
        breakers: Optional[List[CircuitBreaker]] = None,
    ) -> None:
        if not providers:
            raise ValueError("LLMPool requires at least one provider")
        self._providers = providers
        self._breakers = breakers or [CircuitBreaker() for _ in providers]
        if len(self._breakers) != len(providers):
            raise ValueError("breakers length must match providers length")
        self._rr_index = 0  # round-robin cursor

    @property
    def providers(self) -> List[LLMProvider]:
        return list(self._providers)

    @property
    def breakers(self) -> List[CircuitBreaker]:
        return list(self._breakers)

    async def aclose(self) -> None:
        for p in self._providers:
            await p.aclose()

    async def call(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        system: Optional[str] = None,
    ) -> LLMCallResult:
        """Failover chat call across healthy providers."""
        if system is not None:
            messages = [{"role": "system", "content": system}, *messages]

        started = time.monotonic()
        n = len(self._providers)
        errors: List[str] = []

        for offset in range(n):
            idx = (self._rr_index + offset) % n
            provider = self._providers[idx]
            breaker = self._breakers[idx]

            if not await breaker.allow_request():
                errors.append(f"{provider.config.model}: circuit open")
                continue

            try:
                result = await provider.call(messages)
            except Exception as exc:  # noqa: BLE001 — failover must be defensive
                await breaker.record_failure()
                errors.append(f"{provider.config.model}: {exc}")
                continue

            await breaker.record_success()
            self._rr_index = (idx + 1) % n
            result.latency_ms = (time.monotonic() - started) * 1000
            return result

        return LLMCallResult(
            success=False,
            error="all providers failed: " + " | ".join(errors),
            latency_ms=(time.monotonic() - started) * 1000,
            retriable=True,
        )

    async def complete(self, prompt: str, *, system: Optional[str] = None) -> LLMCallResult:
        return await self.call([{"role": "user", "content": prompt}], system=system)


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def create_openai_pool(
    base_url: str,
    api_key: str,
    model: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 60.0,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> LLMPool:
    provider = LLMProvider(
        EndpointConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider=ProviderType.OPENAI,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        ),
        client=client,
    )
    return LLMPool([provider])


def create_anthropic_pool(
    base_url: str,
    api_key: str,
    model: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 60.0,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> LLMPool:
    provider = LLMProvider(
        EndpointConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider=ProviderType.ANTHROPIC,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        ),
        client=client,
    )
    return LLMPool([provider])


def create_failover_pool(
    endpoints: Sequence[EndpointConfig],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> LLMPool:
    """Build a multi-provider pool for primary + backup endpoints."""
    providers = [LLMProvider(cfg, client=client) for cfg in endpoints]
    return LLMPool(providers)

