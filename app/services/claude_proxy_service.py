"""Local Anthropic-compatible proxy for Claude Code providers."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


def _normalize_httpx_proxy(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    if parts.scheme.lower() != "socks5h":
        return proxy_url
    return proxy_url.replace("socks5h://", "socks5://", 1)


class ClaudeCodeProxyService:
    def __init__(
        self,
        *,
        enabled: bool,
        listen_host: str,
        listen_port: int,
        upstream_base: str,
        upstream_key: str,
        proxy_url: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_base = upstream_base.rstrip("/")
        self.upstream_key = upstream_key.strip()
        self.proxy_url = _normalize_httpx_proxy(proxy_url)
        self._client: httpx.Client | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def public_base_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    def is_configured(self) -> bool:
        return self.enabled and bool(self.upstream_key)

    def start(self) -> bool:
        if not self.enabled:
            logger.info("claude code proxy disabled")
            return False
        if not self.upstream_key:
            logger.warning("claude code proxy enabled but upstream key is empty")
            return False
        if self._server is not None:
            return True

        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("claude proxy: " + fmt, *args)

            def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler signature
                path = self.path.split("?", 1)[0]
                if path != "/v1/messages":
                    self._send_json(404, {"error": {"message": f"unsupported path: {self.path}"}})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                response = service.forward_messages(body)
                self.send_response(response.status_code)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        self._client = httpx.Client(timeout=60, proxy=self.proxy_url)
        self._server = ThreadingHTTPServer((self.listen_host, self.listen_port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="claude-code-proxy", daemon=True)
        self._thread.start()
        logger.info("claude code proxy listening at %s", self.public_base_url)
        return True

    def forward_messages(self, body: bytes) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("Claude Code proxy client is not running")
        try:
            return self._client.post(
                f"{self.upstream_base}/v1/messages",
                content=body,
                headers={
                    "Authorization": f"Bearer {self.upstream_key}",
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
            )
        except httpx.HTTPError as exc:
            payload = {"error": {"message": f"upstream request failed: {exc}"}}
            request = httpx.Request("POST", f"{self.upstream_base}/v1/messages")
            return httpx.Response(status_code=502, json=payload, request=request)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        client = self._client
        self._server = None
        self._thread = None
        self._client = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if client is not None:
            client.close()
