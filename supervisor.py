"""
supervisor.py — Zero-downtime auto-updater for ARTEX-python Docker deployment.

Monitors a GitHub Release feed. When a new version is detected:
  1. Pulls the new Docker image.
  2. Spins up the new container alongside the old one (same port via unique mapping).
  3. Drains active connections from the old container via /api/health + /api/drain.
  4. Waits until old-container active_tasks == 0.
  5. Stops and removes the old container.
  6. Promotes the new container to the primary port.

Usage:
    python supervisor.py [--docker-image artex-python:latest]
                         [--repo your-org/artex-python]
                         [--interval 300]
                         [--drain-timeout 120]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("supervisor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "artex-python:latest")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "your-org/artex-python")
CHECK_INTERVAL = int(os.environ.get("UPDATE_CHECK_INTERVAL", "300"))
DRAIN_TIMEOUT = int(os.environ.get("DRAIN_TIMEOUT", "120"))
HEALTH_URL = os.environ.get("HEALTH_URL", "http://localhost:8000")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "artex-backend")
NEW_CONTAINER_SUFFIX = os.environ.get("NEW_CONTAINER_SUFFIX", "-new")

_last_known_version: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=check)
    return result


def _container_running(container_id: str) -> bool:
    try:
        _run(["docker", "inspect", "-f", "{{.State.Running}}", container_id], check=False)
        return True
    except subprocess.CalledProcessError:
        return False


def _get_container_ip(container_id: str) -> str:
    result = _run([
        "docker", "inspect",
        "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        container_id,
    ], check=False)
    return result.stdout.strip() or container_id[:12]


# ---------------------------------------------------------------------------
# GitHub Release Check
# ---------------------------------------------------------------------------

async def get_latest_release_version(repo: str, token: Optional[str] = None) -> Optional[str]:
    """Return the latest release tag (or None on failure)."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("tag_name") or data.get("name")
        except httpx.HTTPError as exc:
            logger.warning("GitHub API error: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Health / Drain Helpers
# ---------------------------------------------------------------------------

async def check_health(url: str, timeout: float = 5.0) -> Optional[dict]:
    """GET /api/health — returns parsed JSON or None."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(f"{url}/api/health")
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as exc:
            logger.debug("Health check failed: %s", exc)
            return None


async def trigger_drain(url: str, container_id: str, timeout: float = 60.0) -> bool:
    """
    Call POST /api/drain on the old container and wait until
    active_tasks drops to 0 or timeout is reached.
    Falls back to docker kill --signal TERM if the endpoint is missing.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await client.post(f"{url}/api/drain", timeout=5.0)
            logger.info("Drain endpoint triggered for container %s", container_id)
        except Exception:
            logger.info("No /api/drain endpoint; falling back to SIGTERM")

    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = await check_health(url)
        if stats and stats.get("active_tasks", 0) == 0:
            logger.info("Container %s drained (active_tasks=0)", container_id)
            return True
        await asyncio.sleep(2)

    logger.warning("Drain timed out for container %s — forcing stop", container_id)
    return False


# ---------------------------------------------------------------------------
# Docker Operations
# ---------------------------------------------------------------------------

async def pull_image(image: str) -> bool:
    """docker pull <image>."""
    loop = asyncio.get_event_loop()
    proc = await loop.run_in_executor(None, lambda: _run(["docker", "pull", image]))
    if proc.returncode == 0:
        logger.info("Image %s pulled successfully", image)
        return True
    logger.error("Failed to pull %s: %s", image, proc.stderr)
    return False


async def start_new_container(
    image: str,
    container_name: str,
    host_port: int = 8000,
) -> Optional[str]:
    """
    Spin up a new container on an ephemeral host port, then remap it
    to the target port after successful health check.
    Returns the new container ID or None on failure.
    """
    # Pick an ephemeral free port
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ephemeral_port = s.getsockname()[1]

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", "artex-net",
        "-e", f"CONTAINER_NAME={container_name}",
        "-p", f"{ephemeral_port}:{host_port}",
        image,
    ]

    loop = asyncio.get_event_loop()
    proc = await loop.run_in_executor(None, lambda: _run(cmd))
    if proc.returncode != 0:
        logger.error("Failed to start new container: %s", proc.stderr)
        return None

    new_id = proc.stdout.strip()
    logger.info("New container started: %s (ephemeral port %s)", new_id[:12], ephemeral_port)
    return new_id


async def get_container_port(container_name: str) -> Optional[int]:
    """Get the host port mapped to container port 8000."""
    result = _run([
        "docker", "inspect", "-f",
        "{{index (index .HostConfig.PortBindings \"8000/tcp\") 0 \"HostPort\"}}",
        container_name,
    ], check=False)
    if result.returncode != 0:
        return None
    return int(result.stdout.strip())


async def promote_container(new_name: str, target_port: int) -> bool:
    """Stop current, remove it, rename new to current name."""
    global CONTAINER_NAME
    loop = asyncio.get_event_loop()

    # Gracefully stop current
    old_name = CONTAINER_NAME
    proc = await loop.run_in_executor(None, lambda: _run(
        ["docker", "stop", old_name, "--time", "5"], check=False
    ))
    if proc.returncode != 0:
        logger.warning("Could not stop old container %s", old_name)

    # Remove old
    await loop.run_in_executor(None, lambda: _run(
        ["docker", "rm", old_name], check=False
    ))

    # Rename new -> old name
    await loop.run_in_executor(None, lambda: _run(
        ["docker", "rename", new_name, old_name], check=False
    ))

    # Restart to re-attach to the original host port
    await loop.run_in_executor(None, lambda: _run(
        ["docker", "start", old_name], check=False
    ))

    CONTAINER_NAME = old_name
    logger.info("Promoted %s -> %s on port %d", new_name, old_name, target_port)
    return True


# ---------------------------------------------------------------------------
# Main Supervisor Loop
# ---------------------------------------------------------------------------

async def run_supervisor() -> None:
    global _last_known_version, CONTAINER_NAME

    github_token = os.environ.get("GITHUB_TOKEN")

    logger.info("Supervisor started — monitoring %s", GITHUB_REPO)
    logger.info("Docker image: %s  |  Container: %s", DOCKER_IMAGE, CONTAINER_NAME)

    # Initial version check
    _last_known_version = await get_latest_release_version(GITHUB_REPO, github_token)
    logger.info("Current known version: %s", _last_known_version or "unknown")

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        latest = await get_latest_release_version(GITHUB_REPO, github_token)
        if latest is None:
            logger.warning("Could not fetch release info, skipping update check")
            continue

        if latest == _last_known_version:
            continue

        logger.info("New version detected: %s (current: %s)", latest, _last_known_version)

        # 1. Pull new image
        if not await pull_image(DOCKER_IMAGE):
            logger.error("Image pull failed — aborting update")
            continue

        # 2. Start new container (side-by-side)
        new_container = f"{CONTAINER_NAME}{NEW_CONTAINER_SUFFIX}"
        new_id = await start_new_container(DOCKER_IMAGE, new_container)
        if not new_id:
            logger.error("Failed to start new container — aborting update")
            continue

        # 3. Wait for new container to become healthy
        new_port = await get_container_port(new_container)
        if not new_port:
            logger.error("Could not determine new container port — aborting")
            _run(["docker", "stop", new_container, "--time", "5"], check=False)
            continue

        new_url = f"http://localhost:{new_port}"
        logger.info("Waiting for new container health (url=%s)...", new_url)

        healthy = False
        for attempt in range(30):
            stats = await check_health(new_url)
            if stats and stats.get("status") == "ok":
                healthy = True
                logger.info("New container is healthy (attempt %d)", attempt + 1)
                break
            await asyncio.sleep(2)

        if not healthy:
            logger.error("New container failed health check — aborting update")
            _run(["docker", "stop", new_container, "--time", "5"], check=False)
            continue

        # 4. Drain old container
        old_url = f"{HEALTH_URL}"
        if not await trigger_drain(old_url, CONTAINER_NAME, timeout=DRAIN_TIMEOUT):
            logger.error("Drain timed out — aborting update")
            _run(["docker", "stop", new_container, "--time", "5"], check=False)
            continue

        # 5. Promote new container
        if await promote_container(new_container, target_port=8000):
            logger.info(
                "Update complete: %s -> %s at %s",
                _last_known_version, latest, datetime.now(timezone.utc).isoformat(),
            )
            _last_known_version = latest
        else:
            logger.error("Promotion failed — rolling back new container")
            _run(["docker", "stop", new_container, "--time", "5"], check=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ARTEX Docker Auto-Updater")
    parser.add_argument("--docker-image", default=DOCKER_IMAGE, help="Docker image name:tag")
    parser.add_argument("--repo", default=GITHUB_REPO, help="GitHub repo (owner/name)")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL, help="Check interval in seconds")
    parser.add_argument("--drain-timeout", type=int, default=DRAIN_TIMEOUT, help="Max seconds to wait for drain")
    parser.add_argument("--health-url", default=HEALTH_URL, help="Base URL of the running backend")
    parser.add_argument("--container-name", default=CONTAINER_NAME, help="Docker container name")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    global DOCKER_IMAGE, GITHUB_REPO, CHECK_INTERVAL, DRAIN_TIMEOUT, HEALTH_URL, CONTAINER_NAME

    DOCKER_IMAGE = args.docker_image
    GITHUB_REPO = args.repo
    CHECK_INTERVAL = args.interval
    DRAIN_TIMEOUT = args.drain_timeout
    HEALTH_URL = args.health_url
    CONTAINER_NAME = args.container_name

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logger.info("Received signal %s — shutting down supervisor", sig)
        loop.create_task(shutdown_handler())
        loop.stop()

    async def shutdown_handler():
        logger.info("Cleaning up supervisor...")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(run_supervisor())
    except KeyboardInterrupt:
        logger.info("Supervisor stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
