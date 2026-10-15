"""Real tool execution layer for ARTEX (Route B: real tools, not mocks).

Bridges a tool decision ``{tool, target, args}`` to actual execution:

- **Builtin tools** (``http_probe`` via httpx) run in-process.
- **External CLI tools** (``nmap`` / ``masscan`` / ``sqlmap`` ...) run via
  ``subprocess`` when the binary is on ``PATH``; otherwise they degrade to a
  clear ``unavailable`` result so the engine never hard-crashes.

Safety guarantees:
- Only tools present in the registry may run (whitelist).
- No ``shell=True``; arguments are passed as an argv list.
- Per-tool timeout + stdout truncation.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MAX_OUTPUT = 5000
DEFAULT_HTTP_TARGET = "http://127.0.0.1:8000/api/health"


@dataclass
class ToolPlan:
    """A concrete, executable tool invocation."""

    tool: str
    target: str
    args: str = ""
    source: str = "rule"  # rule | llm | user


class ToolExecutor:
    """Registry + runner for builtin and external security tools."""

    BUILTIN = {"http_probe"}
    # external CLI: registry name -> executable searched on PATH
    EXTERNAL = {"nmap": "nmap", "masscan": "masscan", "sqlmap": "sqlmap"}
    DEFAULT_TIMEOUT = 120

    # ---- registry / availability ----
    def available(self, tool: str) -> bool:
        if tool in self.BUILTIN:
            return True
        exe = self.EXTERNAL.get(tool)
        return bool(exe and shutil.which(exe))

    def status(self) -> Dict[str, Any]:
        """Report availability of every registered tool (for /api tooling UI)."""
        out: Dict[str, Any] = {}
        for tool in sorted(self.BUILTIN):
            out[tool] = "builtin"
        for tool in sorted(self.EXTERNAL):
            out[tool] = "available" if self.available(tool) else "unavailable"
        return out

    # ---- dispatch ----
    async def run(
        self,
        tool: str,
        target: str,
        args: str = "",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        start = time.monotonic()
        timeout = timeout or self.DEFAULT_TIMEOUT
        if tool in self.BUILTIN:
            result = await self._run_builtin(tool, target, args, timeout)
        elif tool in self.EXTERNAL:
            result = self._run_external(tool, target, args, timeout)
        else:
            result = {"status": "error", "error": f"unknown tool: {tool}"}

        result["tool"] = tool
        result["target"] = target
        result["args"] = args
        result["duration_ms"] = int((time.monotonic() - start) * 1000)
        return result

    # ---- builtin ----
    async def _run_builtin(self, tool: str, target: str, args: str, timeout: int) -> Dict[str, Any]:
        if tool == "http_probe":
            return await self._http_probe(target, args, timeout)
        return {"status": "error", "error": f"unsupported builtin tool: {tool}"}

    async def _http_probe(self, url: str, args: str, timeout: int) -> Dict[str, Any]:
        import httpx

        method = "GET"
        tokens = (args or "").split()
        if "-m" in tokens:
            i = tokens.index("-m")
            if i + 1 < len(tokens):
                method = tokens[i + 1].upper()
        try:
            async with httpx.AsyncClient(timeout=min(float(timeout), 30.0), follow_redirects=False) as client:
                if method == "HEAD":
                    resp = await client.request("HEAD", url)
                    body = b""
                else:
                    resp = await client.request(method, url)
                    body = resp.content[:2000]
                return {
                    "status": "ok",
                    "http_status": resp.status_code,
                    "final_url": str(resp.url),
                    "headers": dict(list(resp.headers.items())[:20]),
                    "content_length": len(body),
                    "body_preview": body[:200].decode("utf-8", errors="replace"),
                }
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # ---- external ----
    def _run_external(self, tool: str, target: str, args: str, timeout: int) -> Dict[str, Any]:
        exe = self.EXTERNAL[tool]
        found = shutil.which(exe)
        if not found:
            return {
                "status": "unavailable",
                "error": f"{exe} not found on PATH; install it or run under a Kali Docker image",
            }
        argv = [found]
        argv += [t for t in (args or "").split() if t]
        if target:
            argv.append(target)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            combined = (proc.stdout or "") + (proc.stderr or "")
            return {
                "status": "ok" if proc.returncode == 0 else "nonzero",
                "returncode": proc.returncode,
                "command": " ".join(argv),
                "output": combined[-_MAX_OUTPUT:],
            }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "error": f"exceeded {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}


__all__ = ["ToolExecutor", "ToolPlan", "DEFAULT_HTTP_TARGET"]
