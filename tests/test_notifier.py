"""Tests for the Notification Dispatcher (src/tools/notifier.py).

Focus on pure/unit-testable pieces: SSRF protection, rate limiter, dispatcher
registry. Network send paths are exercised via a mock httpx handler.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.tools.notifier import (
    ChannelType,
    NotificationDispatcher,
    NotificationMessage,
    RateLimiter,
    get_dispatcher,
    reset_dispatcher,
)


class TestSSRF:
    def test_blocks_metadata_ip(self):
        # Patch the module flag to ensure localhost handling is deterministic.
        import src.tools.notifier as n
        old = n._ALLOW_LOCAL
        n._ALLOW_LOCAL = False
        try:
            assert n._is_blocked("http://169.254.169.254/latest/meta-data/")
            assert n._is_blocked("http://127.0.0.1:8000/x")
            assert n._is_blocked("http://localhost:8000/x")
            assert n._is_blocked("http://10.0.0.5/x")
            assert n._is_blocked("http://192.168.1.1/x")
            assert not n._is_blocked("https://example.com/webhook")
        finally:
            n._ALLOW_LOCAL = old

    def test_blocks_empty_host(self):
        import src.tools.notifier as n
        assert n._is_blocked("not-a-url")

    def test_allows_public_host(self):
        import src.tools.notifier as n
        assert not n._is_blocked("https://hooks.example.com/abc")


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_up_to_capacity(self):
        rl = RateLimiter(max_tokens=3, refill_rate=0.0)
        assert await rl.acquire()
        assert await rl.acquire()
        assert await rl.acquire()
        assert not await rl.acquire()  # exhausted

    @pytest.mark.asyncio
    async def test_refills_over_time(self):
        rl = RateLimiter(max_tokens=1, refill_rate=100.0)
        assert await rl.acquire()
        assert not await rl.acquire()
        await asyncio.sleep(0.02)  # refill
        assert await rl.acquire()


class TestDispatcher:
    def test_singleton_get_reset(self):
        reset_dispatcher()
        a = get_dispatcher()
        b = get_dispatcher()
        assert a is b
        reset_dispatcher()
        c = get_dispatcher()
        assert c is not a

    def test_register_and_get_channel(self):
        d = NotificationDispatcher()
        d.register("hook-1", ChannelType.WEBHOOK, {"webhook_url": "https://example.com/x"})
        assert "hook-1" in d.channel_ids
        assert d.get_channel("hook-1") is not None

    def test_encrypt_decrypt_roundtrip(self):
        d = NotificationDispatcher()
        cfg = {"webhook_url": "https://secret.example.com/token", "plain": "visible"}
        enc = d._encrypt_config(cfg)
        assert enc["webhook_url"] != cfg["webhook_url"]  # encrypted
        assert enc["plain"] == "visible"  # non-sensitive untouched
        dec = d.decrypt_config(enc)
        assert dec["webhook_url"] == cfg["webhook_url"]

    @pytest.mark.asyncio
    async def test_webhook_send_with_mock_transport(self):
        d = NotificationDispatcher()
        # WebhookChannel reads config["url"] / config["headers"].
        d.register("hook-1", ChannelType.WEBHOOK, {"url": "https://example.com/x"})

        def handler(request):
            return httpx.Response(200, json={"ok": True})

        real_client = httpx.AsyncClient

        class _PatchedAsyncClient(real_client):
            def __init__(self, *a, **k):
                k.pop("transport", None)
                super().__init__(*a, transport=httpx.MockTransport(handler), **k)

        httpx.AsyncClient = _PatchedAsyncClient
        try:
            msg = NotificationMessage(channel=ChannelType.WEBHOOK, title="t", body="b")
            ok = await d.send(msg)
            assert ok is True
        finally:
            httpx.AsyncClient = real_client

    @pytest.mark.asyncio
    async def test_send_no_matching_channel_returns_false(self):
        d = NotificationDispatcher()
        msg = NotificationMessage(channel=ChannelType.TELEGRAM, title="t", body="b")
        ok = await d.send(msg)
        assert ok is False

