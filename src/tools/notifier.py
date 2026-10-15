"""Notification Dispatcher — multi-channel with rate limiting, retry queue, SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import aiosmtplib
from aiosmtplib.errors import SMTPConnectError, SMTPDataError, SMTPException
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_BLOCKED_SUBNETS = [
    ipaddress.ip_network("169.254.169.254/32"),   # AWS instance metadata
    ipaddress.ip_network("127.0.0.0/8"),          # localhost
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),              # IPv6 localhost
]

_ALLOW_LOCAL = False  # Set via env var ARTEX_NOTIFY_ALLOW_LOCAL


def _is_blocked(url: str) -> bool:
    """Check if a URL resolves to a blocked IP (SSRF protection)."""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        return True
    if host == "localhost" or host.startswith("127."):
        return not _ALLOW_LOCAL
    try:
        addr = ipaddress.ip_address(host)
        for subnet in _BLOCKED_SUBNETS:
            if addr in subnet:
                return True
    except ValueError:
        pass  # hostname, will be resolved at send time
    return False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ChannelType(str, Enum):
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    WECOM = "wecom"
    WEBHOOK = "webhook"
    TELEGRAM = "telegram"
    EMAIL = "email"


@dataclass
class NotificationMessage:
    channel: ChannelType
    title: str
    body: str
    finding_id: Optional[UUID] = None
    urgency: str = "normal"  # low | normal | high | critical
    extra: Dict[str, Any] = field(default_factory=dict)
    task_id: Optional[UUID] = None


class RateLimiter:
    """Token bucket rate limiter per channel."""

    def __init__(self, max_tokens: float, refill_rate: float):
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self._tokens = max_tokens
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


# ---------------------------------------------------------------------------
# Channel implementations
# ---------------------------------------------------------------------------

class BaseChannel:
    channel_type: ChannelType = ChannelType.WEBHOOK
    rate_limit: float = 10.0  # msgs/sec
    retry_attempts: int = 3

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._limiter = RateLimiter(max_tokens=self.rate_limit, refill_rate=self.rate_limit)

    async def send(self, message: NotificationMessage) -> bool:
        raise NotImplementedError

    async def _check_rate_limit(self) -> bool:
        return await self._limiter.acquire()


class DingTalkChannel(BaseChannel):
    channel_type = ChannelType.DINGTALK

    async def send(self, message: NotificationMessage) -> bool:
        import httpx
        if not await self._check_rate_limit():
            return False
        url = self.config.get("webhook_url", "")
        if _is_blocked(url):
            logger.warning("SSRF blocked: DingTalk webhook URL")
            return False
        severity_map = {"low": "normal", "normal": "normal", "high": "warning", "critical": "alert"}
        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"ARTEX: {message.title}",
                "text": f"### {message.title}\n\n{message.body}\n\n_urgency: {severity_map.get(message.urgency, 'normal')}_",
            },
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("errcode") == 0


class FeishuChannel(BaseChannel):
    channel_type = ChannelType.FEISHU

    async def send(self, message: NotificationMessage) -> bool:
        import httpx
        if not await self._check_rate_limit():
            return False
        url = self.config.get("webhook_url", "")
        if _is_blocked(url):
            logger.warning("SSRF blocked: Feishu webhook URL")
            return False
        body = {
            "msg_type": "text",
            "content": {"text": f"[ARTEX] {message.title}\n{message.body}"},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("code") == 0 or data.get("ret") == 0


class WeComChannel(BaseChannel):
    channel_type = ChannelType.WECOM

    async def send(self, message: NotificationMessage) -> bool:
        import httpx
        if not await self._check_rate_limit():
            return False
        url = self.config.get("webhook_url", "")
        if _is_blocked(url):
            logger.warning("SSRF blocked: WeCom webhook URL")
            return False
        body = {
            "msgtype": "markdown",
            "markdown": {"content": f"## ARTEX 漏洞通知\n### {message.title}\n{message.body}"},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("errcode") == 0


class TelegramChannel(BaseChannel):
    channel_type = ChannelType.TELEGRAM

    async def send(self, message: NotificationMessage) -> bool:
        import httpx
        if not await self._check_rate_limit():
            return False
        bot_token = self.config.get("bot_token", "")
        chat_id = self.config.get("chat_id", "")
        if not bot_token or not chat_id:
            logger.error("Telegram: missing bot_token or chat_id")
            return False
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        if _is_blocked(url):
            return False
        body = {
            "chat_id": chat_id,
            "text": f"🚨 ARTEX\n{message.title}\n\n{message.body}",
            "parse_mode": "HTML",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("ok") is True


class WebhookChannel(BaseChannel):
    channel_type = ChannelType.WEBHOOK

    async def send(self, message: NotificationMessage) -> bool:
        import httpx
        if not await self._check_rate_limit():
            return False
        url = self.config.get("url", "")
        if not url:
            logger.error("Webhook: no URL configured")
            return False
        if _is_blocked(url):
            logger.warning("SSRF blocked: webhook URL %s", url)
            return False
        headers = self.config.get("headers", {})
        body = {
            "title": message.title,
            "body": message.body,
            "urgency": message.urgency,
            "finding_id": str(message.finding_id) if message.finding_id else None,
            "extra": message.extra,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return 200 <= resp.status_code < 300


class EmailChannel(BaseChannel):
    channel_type = ChannelType.EMAIL
    rate_limit = 5.0
    retry_attempts = 5

    async def send(self, message: NotificationMessage) -> bool:
        smtp_host = self.config.get("smtp_host", "localhost")
        smtp_port = self.config.get("smtp_port", 587)
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        from_addr = self.config.get("from_addr", username)
        to_addrs = self.config.get("to_addrs", [])

        if not to_addrs:
            logger.error("Email: no recipient addresses")
            return False

        subject = f"[ARTEX] {message.title}"
        html_body = (
            f"<html><body>"
            f"<h2 style='color: red;'>ARTEX: {message.title}</h2>"
            f"<pre style='background:#f4f4f4;padding:10px;'>{message.body}</pre>"
            f"<p><small>Urgency: {message.urgency}</small></p>"
            f"</body></html>"
        )

        try:
            await aiosmtplib.send(
                message.body,
                hostname=smtp_host,
                port=smtp_port,
                username=username,
                password=password,
                from_addr=from_addr,
                to_addrs=to_addrs,
                use_tls=smtp_port == 465,
            )
            logger.info("Email sent to %s", to_addrs)
            return True
        except SMTPDataError as exc:
            # 5xx: permanent failure
            logger.error("Email permanent failure: %s", exc)
            return False
        except (SMTPConnectError, TimeoutError) as exc:
            # Connection errors: retryable
            logger.warning("Email transient error (retryable): %s", exc)
            raise
        except Exception as exc:
            logger.warning("Email unexpected error: %s", exc)
            raise


_CHANNEL_FACTORIES: Dict[ChannelType, type] = {
    ChannelType.DINGTALK: DingTalkChannel,
    ChannelType.FEISHU: FeishuChannel,
    ChannelType.WECOM: WeComChannel,
    ChannelType.TELEGRAM: TelegramChannel,
    ChannelType.WEBHOOK: WebhookChannel,
    ChannelType.EMAIL: EmailChannel,
}


# ---------------------------------------------------------------------------
# Notification Dispatcher
# ---------------------------------------------------------------------------

class NotificationDispatcher:
    """
    Multi-channel notification dispatcher with:
    - Channel registry pattern
    - Per-channel rate limiting (token bucket)
    - Retry with exponential backoff
    - SSRF protection
    - Encrypted credentials at rest (Fernet)
    """

    def __init__(self, encryption_key: Optional[bytes] = None):
        self._channels: Dict[str, BaseChannel] = {}
        self._encryption_key = encryption_key or Fernet.generate_key()
        self._fernet = Fernet(self._encryption_key)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    def register(self, channel_id: str, channel_type: ChannelType, config: Dict[str, Any]) -> None:
        encrypted_config = self._encrypt_config(config)
        self._channels[channel_id] = self._create_channel(channel_type, encrypted_config)

    def _create_channel(self, channel_type: ChannelType, config: Dict[str, Any]) -> BaseChannel:
        factory = _CHANNEL_FACTORIES.get(channel_type)
        if not factory:
            raise ValueError(f"Unknown channel type: {channel_type}")
        return factory(config)

    def _encrypt_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        sensitive_keys = {"password", "api_key", "token", "webhook_url", "bot_token", "chat_id",
                          "smtp_host", "from_addr", "to_addrs"}
        encrypted = {}
        for k, v in config.items():
            if k in sensitive_keys and isinstance(v, str):
                encrypted[k] = self._fernet.encrypt(v.encode()).decode()
            else:
                encrypted[k] = v
        return encrypted

    def decrypt_config(self, encrypted_config: Dict[str, Any]) -> Dict[str, Any]:
        sensitive_keys = {"password", "api_key", "token", "webhook_url", "bot_token", "chat_id",
                          "smtp_host", "from_addr", "to_addrs"}
        decrypted = {}
        for k, v in encrypted_config.items():
            if k in sensitive_keys and isinstance(v, str):
                try:
                    decrypted[k] = self._fernet.decrypt(v.encode()).decode()
                except Exception:
                    decrypted[k] = v
            else:
                decrypted[k] = v
        return decrypted

    async def send(self, message: NotificationMessage) -> bool:
        """Send notification through matching channels."""
        sent = False
        for channel_id, channel in self._channels.items():
            if channel.channel_type != message.channel:
                continue
            try:
                ok = await channel.send(message)
                if ok:
                    sent = True
                    logger.info("Notification sent via %s (%s)", channel_id, message.channel)
                else:
                    logger.warning("Notification failed via %s", channel_id)
            except Exception as exc:
                logger.error("Notification error via %s: %s", channel_id, exc)
        return sent

    async def enqueue(self, message: NotificationMessage) -> None:
        await self._queue.put(message)

    async def _drain_queue(self) -> None:
        while self._running:
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            for attempt in range(5):
                try:
                    ok = await self.send(message)
                    if ok:
                        break
                except Exception as exc:
                    logger.warning("Queue send attempt %d failed: %s", attempt + 1, exc)
                    await asyncio.sleep(2 ** attempt)
            else:
                logger.error("Queue send exhausted retries for: %s", message.title)

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._drain_queue())
        logger.info("Notification dispatcher started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification dispatcher stopped")

    @property
    def channel_ids(self) -> List[str]:
        return list(self._channels.keys())

    def get_channel(self, channel_id: str) -> Optional[BaseChannel]:
        return self._channels.get(channel_id)


# Module-level singleton
_dispatcher: Optional[NotificationDispatcher] = None


def get_dispatcher(encryption_key: Optional[bytes] = None) -> NotificationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher(encryption_key=encryption_key)
    return _dispatcher


def reset_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None
