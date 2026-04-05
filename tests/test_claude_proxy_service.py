from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.claude_proxy_service import ClaudeCodeProxyService


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_claude_proxy_service_forwards_messages() -> None:
    upstream_port = _free_port()
    proxy_port = _free_port()
    captured: dict[str, object] = {}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003,N802
            return

        def do_POST(self):  # noqa: N802
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("Authorization")
            captured["anthropic_version"] = self.headers.get("anthropic-version")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            captured["body"] = json.loads(body.decode("utf-8"))
            payload = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "doubao-seed-code-preview-latest",
                "content": [{"type": "text", "text": "4"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), UpstreamHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    service = ClaudeCodeProxyService(
        enabled=True,
        listen_host="127.0.0.1",
        listen_port=proxy_port,
        upstream_base=f"http://127.0.0.1:{upstream_port}",
        upstream_key="secret-token",
        proxy_url=None,
    )

    try:
        assert service.start() is True
        req = Request(
            f"http://127.0.0.1:{proxy_port}/v1/messages?beta=true",
            data=json.dumps(
                {
                    "model": "doubao-seed-code-preview-latest",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "2+2?"}],
                }
            ).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": "dummy",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"][0]["text"] == "4"
        assert captured["path"] == "/v1/messages"
        assert captured["authorization"] == "Bearer secret-token"
        assert captured["anthropic_version"] == "2023-06-01"
        assert captured["body"] == {
            "model": "doubao-seed-code-preview-latest",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "2+2?"}],
        }
    finally:
        service.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
