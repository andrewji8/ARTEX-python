"""MITM Proxy integration — traffic capture addon for ARTEX evidence gathering."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Headers to strip for security
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
    "set-cookie",
    "authentication",
}

# Internal webhook URL for ingesting traffic data
DEFAULT_INGEST_URL = "http://127.0.0.1:8000/api/traffic/ingest"


class TrafficCaptureAddon:
    """
    mitmproxy addon that captures HTTP/HTTPS traffic metadata.
    Strips sensitive headers, computes body hash, and pushes to backend webhook.
    """

    def __init__(
        self,
        ingest_url: str = DEFAULT_INGEST_URL,
        task_id: Optional[str] = None,
        api_key: Optional[str] = None,
        max_body_size: int = 10 * 1024 * 1024,  # 10 MB
    ):
        self.ingest_url = ingest_url
        self.task_id = task_id
        self.api_key = api_key
        self.max_body_size = max_body_size
        self._session: Optional[httpx.AsyncClient] = None
        self._stats = {"requests_captured": 0, "errors": 0}

    def _get_client(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=10.0),
                limits=httpx.Limits(max_connections=50),
            )
        return self._session

    @staticmethod
    def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
        """Remove sensitive headers, lowercase keys for consistency."""
        return {
            k.lower(): v
            for k, v in headers.items()
            if k.lower() not in SENSITIVE_HEADERS
        }

    @staticmethod
    def compute_body_hash(body: Optional[bytes]) -> str:
        if not body:
            return ""
        return hashlib.sha256(body).hexdigest()

    async def _ingest(self, flow_data: Dict[str, Any]) -> None:
        """Push traffic metadata to the backend webhook."""
        try:
            client = self._get_client()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            resp = await client.post(self.ingest_url, json=flow_data, headers=headers)
            resp.raise_for_status()
            self._stats["requests_captured"] += 1
        except Exception as exc:
            self._stats["errors"] += 1
            logger.warning("Traffic ingest failed: %s", exc)

    # ---- mitmproxy hook methods ----

    def request(self, flow):
        """Called when a request is received."""
        try:
            headers = dict(flow.request.headers) if flow.request.headers else {}
            body = flow.request.content or b""
            if len(body) > self.max_body_size:
                body = body[: self.max_body_size]

            flow_data = {
                "event": "request",
                "task_id": self.task_id,
                "timestamp": flow.request.timestamp_start.isoformat() if flow.request.timestamp_start else None,
                "method": flow.request.method,
                "url": str(flow.request.url),
                "headers": self.sanitize_headers(headers),
                "body_hash": self.compute_body_hash(body),
                "body_size": len(body),
                "is_https": flow.request.scheme == "https",
            }
            # Non-blocking fire-and-forget ingest
            threading.Thread(
                target=lambda: _run_async(self._ingest(flow_data)),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.debug("request hook error: %s", exc)

    def response(self, flow):
        """Called when a response is received."""
        try:
            headers = dict(flow.response.headers) if flow.response.headers else {}
            body = flow.response.content or b""
            if len(body) > self.max_body_size:
                body = body[: self.max_body_size]

            flow_data = {
                "event": "response",
                "task_id": self.task_id,
                "timestamp": flow.response.timestamp_end.isoformat() if flow.response.timestamp_end else None,
                "request_method": flow.request.method,
                "request_url": str(flow.request.url),
                "status_code": flow.response.status_code,
                "headers": self.sanitize_headers(headers),
                "body_hash": self.compute_body_hash(body),
                "body_size": len(body),
                "content_type": flow.response.headers.get("content-type", ""),
            }
            threading.Thread(
                target=lambda: _run_async(self._ingest(flow_data)),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.debug("response hook error: %s", exc)

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)


def _run_async(coro):
    """Run an async coroutine in a new event loop (for threading context)."""
    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        asyncio.run(coro)


# ---------------------------------------------------------------------------
# Programmatic proxy launcher
# ---------------------------------------------------------------------------

def start_proxy(
    listen_host: str = "127.0.0.1",
    listen_port: int = 8788,
    addon: Optional[TrafficCaptureAddon] = None,
    mode: str = "regular",
    ssl_insecure: bool = True,
) -> None:
    """
    Launch mitmproxy programmatically with the given addon.
    Runs in a separate thread so the main event loop is not blocked.
    """
    try:
        import mitmproxy.addons as core_addons
        import mitmproxy.options as opts
        import mitmproxy.proxy.server as proxy_server
    except ImportError as exc:
        logger.error("mitmproxy not installed: %s", exc)
        raise ImportError("Install mitmproxy: pip install mitmproxy") from exc

    addons = list(core_addons.get())
    if addon:
        addons.append(addon)

    options = opts.Options(
        listen_addr=(listen_host, listen_port),
        mode=[mode],
        ssl_insecure=ssl_insecure,
        addons=addons,
        pretty_print=False,
        verbosity=1,
    )

    server = proxy_server.ProxyServer(options)
    logger.info("mitmproxy starting on %s:%d (mode=%s)", listen_host, listen_port, mode)

    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("mitmproxy stopped")
    finally:
        server.shutdown()
        logger.info("mitmproxy cleanup complete")
