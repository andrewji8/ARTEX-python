"""Tests for the LLM Gateway (src/tools/llm_gateway.py)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.tools.llm_gateway import (
    CircuitBreaker,
    EndpointConfig,
    LLMEmptyResponseError,
    LLMPool,
    LLMProvider,
    LLMProviderError,
    ProviderType,
    TokenUsage,
    create_anthropic_pool,
    create_failover_pool,
    create_openai_pool,
)


def _openai_response(content: str = "ok", *, usage: dict | None = None) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _anthropic_response(content: str = "ok") -> dict:
    return {
        "content": [{"type": "text", "text": content}],
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestTokenUsage:
    def test_total_tokens(self):
        u = TokenUsage(input_tokens=10, output_tokens=5, cache_read_tokens=2, cache_write_tokens=1)
        assert u.total_tokens == 18

    def test_defaults_zero(self):
        assert TokenUsage().total_tokens == 0


class TestProviderShaping:
    def test_openai_request_shape(self):
        provider = LLMProvider(
            EndpointConfig(base_url="https://api.openai.com/v1", api_key="sk-x", model="gpt-4o")
        )
        url, headers, body = provider._build_request([{"role": "user", "content": "hi"}])
        assert url == "https://api.openai.com/v1/chat/completions"
        assert headers["authorization"] == "Bearer sk-x"
        assert body["model"] == "gpt-4o"
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_openai_base_url_already_completions(self):
        provider = LLMProvider(
            EndpointConfig(base_url="https://x.com/chat/completions", api_key="k", model="m")
        )
        url, _, _ = provider._build_request([{"role": "user", "content": "hi"}])
        assert url == "https://x.com/chat/completions"

    def test_anthropic_request_shape_splits_system(self):
        provider = LLMProvider(
            EndpointConfig(
                base_url="https://api.anthropic.com",
                api_key="sk-a",
                model="claude",
                provider=ProviderType.ANTHROPIC,
            )
        )
        msgs = [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hi"},
        ]
        url, headers, body = provider._build_request(msgs)
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "sk-a"
        assert headers["anthropic-version"] == "2023-06-01"
        assert body["system"] == "be nice"
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_parse_openai(self):
        provider = LLMProvider(EndpointConfig(base_url="http://x", api_key="k", model="m"))
        content, usage = provider._parse_response(
            _openai_response("  hello  ", usage={"prompt_tokens": 3, "completion_tokens": 4})
        )
        assert content == "hello"
        assert usage.input_tokens == 3
        assert usage.output_tokens == 4

    def test_parse_openai_cached_tokens(self):
        provider = LLMProvider(EndpointConfig(base_url="http://x", api_key="k", model="m"))
        _, usage = provider._parse_response(
            _openai_response("x", usage={
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 6},
            })
        )
        assert usage.cache_read_tokens == 6

    def test_parse_anthropic(self):
        provider = LLMProvider(
            EndpointConfig(base_url="http://x", api_key="k", model="m", provider=ProviderType.ANTHROPIC)
        )
        content, usage = provider._parse_response(_anthropic_response("hi there"))
        assert content == "hi there"
        assert usage.input_tokens == 12
        assert usage.output_tokens == 7

class TestProviderCall:
    @pytest.mark.asyncio
    async def test_success(self):
        def handler(request):
            return httpx.Response(200, json=_openai_response("analysis done"))

        provider = LLMProvider(
            EndpointConfig(base_url="http://x/v1", api_key="k", model="m"),
            client=_mock_client(handler),
        )
        result = await provider.call([{"role": "user", "content": "hi"}])
        assert result.success
        assert result.content == "analysis done"
        assert result.provider == "openai"
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_http_error_raises_provider_error(self):
        def handler(request):
            return httpx.Response(500, json={"error": "boom"})

        provider = LLMProvider(
            EndpointConfig(base_url="http://x/v1", api_key="k", model="m"),
            client=_mock_client(handler),
        )
        with pytest.raises(LLMProviderError):
            await provider.call([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_connection_error_retried_then_raises(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ConnectError("no route", request=request)

        provider = LLMProvider(
            EndpointConfig(base_url="http://x/v1", api_key="k", model="m"),
            client=_mock_client(handler),
            max_connection_attempts=3,
        )
        with pytest.raises(httpx.ConnectError):
            await provider.call([{"role": "user", "content": "hi"}])
        assert calls["n"] == 3  # Layer 1 retried to exhaustion

    @pytest.mark.asyncio
    async def test_empty_response_then_success(self):
        state = {"n": 0}

        def handler(request):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(200, json=_openai_response("   "))
            return httpx.Response(200, json=_openai_response("recovered"))

        provider = LLMProvider(
            EndpointConfig(base_url="http://x/v1", api_key="k", model="m"),
            client=_mock_client(handler),
            max_empty_attempts=2,
        )
        result = await provider.call([{"role": "user", "content": "hi"}])
        assert result.success
        assert result.content == "recovered"
        assert state["n"] == 2

    @pytest.mark.asyncio
    async def test_persistent_empty_raises(self):
        def handler(request):
            return httpx.Response(200, json=_openai_response(""))

        provider = LLMProvider(
            EndpointConfig(base_url="http://x/v1", api_key="k", model="m"),
            client=_mock_client(handler),
            max_empty_attempts=2,
        )
        with pytest.raises(LLMEmptyResponseError):
            await provider.call([{"role": "user", "content": "hi"}])


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_starts_closed(self):
        cb = CircuitBreaker()
        assert not cb.is_open
        assert await cb.allow_request()

    @pytest.mark.asyncio
    async def test_opens_after_error_rate(self):
        cb = CircuitBreaker(min_calls=4, error_rate_threshold=0.5, recovery_seconds=30.0)
        for _ in range(4):
            await cb.record_failure()  # 4/4 failures -> rate 1.0 >= 0.5 -> open
        assert cb.is_open
        assert not await cb.allow_request()

    @pytest.mark.asyncio
    async def test_below_min_calls_stays_closed(self):
        cb = CircuitBreaker(min_calls=5, error_rate_threshold=0.5)
        for _ in range(3):
            await cb.record_failure()
        assert not cb.is_open  # not enough calls yet

    @pytest.mark.asyncio
    async def test_half_open_after_recovery(self):
        cb = CircuitBreaker(min_calls=2, error_rate_threshold=0.5, recovery_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.is_open
        assert not await cb.allow_request()
        import asyncio
        await asyncio.sleep(0.06)  # wait past recovery window -> half-open
        assert await cb.allow_request()

    @pytest.mark.asyncio
    async def test_success_resets_open_state(self):
        cb = CircuitBreaker(min_calls=2, error_rate_threshold=0.5, recovery_seconds=0.05)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.is_open
        import asyncio
        await asyncio.sleep(0.06)
        assert await cb.allow_request()  # half-open
        await cb.record_success()  # closes
        assert not cb.is_open

    def test_error_rate_property(self):
        cb = CircuitBreaker(window_seconds=1000)
        now = time.monotonic()
        cb._events = [(now, False), (now, True)]
        assert cb.error_rate == 0.5


class TestPool:
    @pytest.mark.asyncio
    async def test_single_provider_success(self):
        def handler(request):
            return httpx.Response(200, json=_openai_response("done"))

        pool = create_openai_pool("http://x/v1", "k", "m", client=_mock_client(handler))
        result = await pool.complete("hello")
        assert result.success
        assert result.content == "done"

    @pytest.mark.asyncio
    async def test_failover_to_backup(self):
        def handler(request):
            if request.url.host == "primary":
                return httpx.Response(500, json={"e": 1})
            return httpx.Response(200, json=_openai_response("from-backup"))

        client = _mock_client(handler)
        pool = create_failover_pool(
            [
                EndpointConfig(base_url="http://primary/v1", api_key="k", model="p"),
                EndpointConfig(base_url="http://backup/v1", api_key="k", model="b"),
            ],
            client=client,
        )
        result = await pool.complete("hi")
        assert result.success
        assert result.content == "from-backup"

    @pytest.mark.asyncio
    async def test_all_fail_retriable(self):
        def handler(request):
            return httpx.Response(500, json={"e": 1})

        client = _mock_client(handler)
        pool = create_failover_pool(
            [
                EndpointConfig(base_url="http://a/v1", api_key="k", model="a"),
                EndpointConfig(base_url="http://b/v1", api_key="k", model="b"),
            ],
            client=client,
        )
        result = await pool.complete("hi")
        assert not result.success
        assert result.retriable  # Layer 5
        assert "all providers failed" in result.error

    @pytest.mark.asyncio
    async def test_system_prompt_prepended(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_openai_response("ok"))

        pool = create_openai_pool("http://x/v1", "k", "m", client=_mock_client(handler))
        await pool.call([{"role": "user", "content": "hi"}], system="sys prompt")
        assert seen["body"]["messages"][0] == {"role": "system", "content": "sys prompt"}

    @pytest.mark.asyncio
    async def test_round_robin_advances(self):
        def handler(request):
            return httpx.Response(200, json=_openai_response("ok"))

        client = _mock_client(handler)
        pool = create_failover_pool(
            [
                EndpointConfig(base_url="http://a/v1", api_key="k", model="a"),
                EndpointConfig(base_url="http://b/v1", api_key="k", model="b"),
            ],
            client=client,
        )
        assert pool._rr_index == 0
        await pool.complete("hi")
        assert pool._rr_index == 1

    def test_pool_requires_provider(self):
        with pytest.raises(ValueError):
            LLMPool([])

    def test_pool_breakers_length_mismatch(self):
        pool = create_openai_pool("http://x/v1", "k", "m")
        with pytest.raises(ValueError):
            LLMPool(pool.providers, breakers=[CircuitBreaker(), CircuitBreaker()])

    @pytest.mark.asyncio
    async def test_anthropic_factory(self):
        def handler(request):
            return httpx.Response(200, json=_anthropic_response("claude says hi"))

        pool = create_anthropic_pool("http://anthropic", "k", "claude-3", client=_mock_client(handler))
        result = await pool.complete("hi")
        assert result.success
        assert result.content == "claude says hi"
        assert result.provider == "anthropic"

