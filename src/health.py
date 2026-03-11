"""Lightweight HTTP health check endpoint using only the Python standard library.

Exposes /health and /ready endpoints over a raw asyncio TCP server,
parsing HTTP manually to avoid pulling in any third-party web framework.

Usage::

    provider = async def _checks() -> dict: ...
    server = HealthCheckServer(config=HealthCheckConfig(enabled=True, port=8080),
                               health_provider=provider)
    await server.start()
    ...
    await server.stop()

The health provider callback should return a dict mapping check names to
their results.  Each result should be a dict with at least an ``ok`` key::

    {
        "database": {"ok": True},
        "discord": {"ok": True, "latency_ms": 42},
        "orchestrator": {"ok": True, "paused": False},
        "agents": {"ok": True, "active": 2, "idle": 1},
        "tasks": {"ok": True, "in_progress": 3, "ready": 5},
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckConfig:
    """Configuration for the health check HTTP server."""

    enabled: bool = False
    port: int = 8080


# Type alias for the callback the orchestrator supplies.
HealthProvider = Callable[[], Awaitable[dict[str, Any]]]


class HealthCheckServer:
    """Minimal async HTTP server that exposes ``/health`` and ``/ready``.

    Built on ``asyncio.start_server`` with manual HTTP parsing so we
    don't need any third-party web framework.  The server listens on all
    interfaces (0.0.0.0) and responds to:

    - ``GET /health`` — full health status with all checks
    - ``GET /ready`` — readiness probe (database + Discord connectivity)
    - Everything else — 404
    """

    def __init__(
        self,
        config: HealthCheckConfig,
        health_provider: HealthProvider | None = None,
    ) -> None:
        self._config = config
        self._health_provider = health_provider
        self._server: asyncio.AbstractServer | None = None
        self._started_at: float | None = None

    async def start(self) -> None:
        """Start listening for HTTP connections."""
        if not self._config.enabled:
            logger.info("Health check server is disabled, not starting.")
            return

        self._started_at = time.monotonic()
        self._server = await asyncio.start_server(
            self._handle_connection,
            host="0.0.0.0",
            port=self._config.port,
        )
        logger.info("Health check server listening on port %s", self._config.port)

    async def stop(self) -> None:
        """Gracefully shut down the server."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Health check server stopped.")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single inbound TCP connection (one HTTP request)."""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            request_line = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
            parts = request_line.split()
            if len(parts) < 2:
                await self._send_response(writer, 400, {"error": "bad request"})
                return

            method, path = parts[0], parts[1]

            if method != "GET":
                await self._send_response(writer, 405, {"error": "method not allowed"})
                return

            if path == "/health":
                await self._handle_health(writer)
            elif path == "/ready":
                await self._handle_ready(writer)
            else:
                await self._send_response(writer, 404, {"error": "not found"})
        except asyncio.TimeoutError:
            await self._send_response(writer, 408, {"error": "request timeout"})
        except Exception:
            logger.exception("Error handling health check request")
            try:
                await self._send_response(
                    writer, 500, {"error": "internal server error"}
                )
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_health(self, writer: asyncio.StreamWriter) -> None:
        """Respond to GET /health with full system health status.

        Returns 200 when all checks pass, 503 when any check indicates
        a problem (``ok: False``).  The response body always includes
        ``status``, ``uptime_seconds``, ``timestamp``, and ``checks``.
        """
        checks = await self._get_checks()

        all_ok = all(
            (c.get("ok", False) if isinstance(c, dict) else bool(c))
            for c in checks.values()
        )
        status = "healthy" if all_ok else "degraded"

        uptime = (
            round(time.monotonic() - self._started_at, 2)
            if self._started_at is not None
            else 0
        )

        body = {
            "status": status,
            "uptime_seconds": uptime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        }

        http_status = 200 if all_ok else 503
        await self._send_response(writer, http_status, body)

    async def _handle_ready(self, writer: asyncio.StreamWriter) -> None:
        """Respond to GET /ready with readiness probe.

        Readiness requires both Discord and database connectivity.
        Returns 200 when ready, 503 when not.
        """
        checks = await self._get_checks()

        discord_ok = self._check_ok(checks.get("discord"))
        database_ok = self._check_ok(checks.get("database"))
        ready = discord_ok and database_ok

        body = {
            "ready": ready,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {
                "discord": checks.get("discord", {"ok": False}),
                "database": checks.get("database", {"ok": False}),
            },
        }

        http_status = 200 if ready else 503
        await self._send_response(writer, http_status, body)

    async def _get_checks(self) -> dict[str, Any]:
        """Invoke the health provider, returning an empty dict on failure."""
        if self._health_provider is None:
            return {}
        try:
            return await self._health_provider()
        except Exception:
            logger.exception("Health provider raised an exception")
            return {"_provider_error": {"ok": False, "error": "provider failed"}}

    @staticmethod
    def _check_ok(value: Any) -> bool:
        """Return True when a single check result indicates success."""
        if value is None:
            return False
        if isinstance(value, dict):
            return bool(value.get("ok", False))
        return bool(value)

    @staticmethod
    async def _send_response(
        writer: asyncio.StreamWriter,
        status_code: int,
        body: dict,
    ) -> None:
        """Serialise *body* as JSON and send a minimal HTTP/1.1 response."""
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            408: "Request Timeout",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status_code, "Unknown")

        payload = json.dumps(body).encode("utf-8")

        lines = [
            f"HTTP/1.1 {status_code} {reason}",
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Connection: close",
            "",
            "",
        ]
        header = "\r\n".join(lines).encode("utf-8")

        writer.write(header + payload)
        await writer.drain()
