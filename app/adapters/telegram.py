"""Telegram bot adapter for polling/webhook and outgoing messages."""

from __future__ import annotations

import logging
import mimetypes
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.schemas import TelegramInboundMessage

logger = logging.getLogger(__name__)
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_CODE_FENCE_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)```", flags=re.DOTALL)
_INLINE_TOKEN_RE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]]+\]\([^)]+\))")
_NUMBERED_RE = re.compile(r"^(?P<num>\d+)\.\s+(?P<body>.+)$")
_BULLET_RE = re.compile(r"^[-*•]\s+(?P<body>.+)$")
_LOCAL_LINK_LINE_RE = re.compile(r"L(?P<line>\d+)(?:C(?P<col>\d+))?$")
_LOCAL_LINK_TOKEN_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<target>/[^)]+)\)")
_CODE_LANG_RE = re.compile(r"[^A-Za-z0-9_+#.-]+")


def _mask_token(token: str) -> str:
    """Mask token for safe logging: show first 4 and last 4 chars."""
    if len(token) <= 10:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


class TelegramAdapter:
    def __init__(
        self,
        bot_token: str,
        chunk_size: int = 3500,
        debug: bool = False,
        proxy_url: str | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chunk_size = chunk_size
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.debug = debug
        self.proxy_url = self._normalize_proxy_url(proxy_url)

    def _disable_proxy(self, reason: str) -> None:
        if not self.proxy_url:
            return
        self._log_step("PROXY", f"disabling proxy for Telegram adapter: {reason}", logging.WARNING)
        self.proxy_url = None

    def _normalize_proxy_url(self, proxy_url: str | None) -> str | None:
        if not proxy_url:
            return None
        parts = urlsplit(proxy_url)
        if parts.scheme.lower() != "socks5h":
            return proxy_url
        normalized = urlunsplit(("socks5", parts.netloc, parts.path, parts.query, parts.fragment))
        self._log_step("PROXY", f"normalized proxy scheme for httpx: {proxy_url!r} -> {normalized!r}")
        return normalized

    def _client(self, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, proxy=self.proxy_url or None)

    def _log_step(self, step: str, message: str, level: int = logging.DEBUG, **kwargs: Any) -> None:
        """Log a debug step with a labelled prefix for easy grep/filtering."""
        if self.debug or level >= logging.WARNING:
            logger.log(level, "[%s] %s", step, message, extra={"step": step, **kwargs})

    # ------------------------------------------------------------------
    # Startup connection test
    # ------------------------------------------------------------------
    async def startup_connection_test(self, auto_clear_webhook: bool = False) -> dict[str, Any]:
        """Run a full step-by-step connection test. Call once at startup."""
        report: dict[str, Any] = {}

        # Step 1: Token format check
        self._log_step("TOKEN", f"token = {_mask_token(self.bot_token)}")
        parts = self.bot_token.split(":")
        if len(parts) != 2 or not parts[0].isdigit():
            self._log_step("TOKEN", "FAIL - token format invalid (expected <bot_id>:<hash>)", logging.ERROR)
            report["token_format"] = False
            return report
        self._log_step("TOKEN", f"OK - bot_id={parts[0]}, hash_len={len(parts[1])}")
        report["token_format"] = True

        # Step 2: DNS resolution
        self._log_step("DNS", "resolving api.telegram.org ...")
        try:
            t0 = time.perf_counter()
            resolved = socket.getaddrinfo("api.telegram.org", 443, proto=socket.IPPROTO_TCP)
            elapsed = int((time.perf_counter() - t0) * 1000)
            ips = sorted({item[4][0] for item in resolved if item and len(item) >= 5})
            self._log_step("DNS", f"OK - {len(ips)} addresses resolved in {elapsed}ms: {ips[:4]}")
            report["dns"] = True
        except OSError as exc:
            self._log_step("DNS", f"FAIL - {exc}", logging.ERROR)
            report["dns"] = False
            return report

        # Step 3: HTTPS connectivity + getMe
        self._log_step("GETME", "calling getMe to validate token ...")
        async with self._client(timeout=15) as client:
            try:
                t0 = time.perf_counter()
                resp = await client.get(f"{self.base_url}/getMe")
                elapsed = int((time.perf_counter() - t0) * 1000)
                self._log_step("GETME", f"HTTP {resp.status_code} in {elapsed}ms")
                data = resp.json()
                if data.get("ok"):
                    bot = data["result"]
                    self._log_step(
                        "GETME",
                        f"OK - @{bot.get('username')} (id={bot.get('id')}, "
                        f"first_name={bot.get('first_name')!r}, "
                        f"can_join_groups={bot.get('can_join_groups')}, "
                        f"can_read_all_group_messages={bot.get('can_read_all_group_messages')})",
                        logging.INFO,
                    )
                    report["get_me"] = True
                    report["bot_info"] = bot
                else:
                    desc = data.get("description", "unknown error")
                    self._log_step("GETME", f"FAIL - API returned ok=false: {desc}", logging.ERROR)
                    report["get_me"] = False
                    return report
            except Exception as exc:  # noqa: BLE001
                if self.proxy_url:
                    self._disable_proxy(f"startup getMe failed through proxy: {exc}")
                    async with self._client(timeout=15) as direct_client:
                        try:
                            t0 = time.perf_counter()
                            resp = await direct_client.get(f"{self.base_url}/getMe")
                            elapsed = int((time.perf_counter() - t0) * 1000)
                            self._log_step("GETME", f"HTTP {resp.status_code} in {elapsed}ms (direct fallback)")
                            data = resp.json()
                            if data.get("ok"):
                                bot = data["result"]
                                self._log_step(
                                    "GETME",
                                    f"OK - @{bot.get('username')} (id={bot.get('id')}, direct fallback)",
                                    logging.WARNING,
                                )
                                report["get_me"] = True
                                report["bot_info"] = bot
                            else:
                                desc = data.get("description", "unknown error")
                                self._log_step("GETME", f"FAIL - API returned ok=false: {desc}", logging.ERROR)
                                report["get_me"] = False
                                return report
                        except Exception as fallback_exc:  # noqa: BLE001
                            self._log_step("GETME", f"FAIL - HTTP error after direct fallback: {fallback_exc}", logging.ERROR)
                            report["get_me"] = False
                            return report
                else:
                    self._log_step("GETME", f"FAIL - HTTP error: {exc}", logging.ERROR)
                    report["get_me"] = False
                    return report

            # Step 4: Webhook status
            self._log_step("WEBHOOK", "checking webhook info ...")
            try:
                t0 = time.perf_counter()
                resp = await client.get(f"{self.base_url}/getWebhookInfo")
                elapsed = int((time.perf_counter() - t0) * 1000)
                wh_data = resp.json()
                wh = wh_data.get("result", {})
                wh_url = wh.get("url", "")
                pending = wh.get("pending_update_count", 0)
                last_err = wh.get("last_error_message", "")
                last_err_date = wh.get("last_error_date", "")
                self._log_step(
                    "WEBHOOK",
                    f"HTTP {resp.status_code} in {elapsed}ms - "
                    f"url={wh_url!r}, pending={pending}, "
                    f"last_error={last_err!r}, last_error_date={last_err_date}",
                )
                report["webhook_url"] = wh_url
                report["webhook_pending"] = pending

                # Step 4b: Auto-clear webhook if set and URL is not empty
                if wh_url and auto_clear_webhook:
                    self._log_step(
                        "WEBHOOK",
                        f"webhook URL is set ({wh_url!r}) but mode=polling, auto-clearing ...",
                        logging.WARNING,
                    )
                    try:
                        clear_resp = await client.get(f"{self.base_url}/deleteWebhook")
                        clear_data = clear_resp.json()
                        if clear_data.get("ok"):
                            self._log_step("WEBHOOK", "OK - webhook cleared successfully", logging.INFO)
                            report["webhook_cleared"] = True
                        else:
                            self._log_step(
                                "WEBHOOK",
                                f"FAIL - could not clear webhook: {clear_data}",
                                logging.ERROR,
                            )
                            report["webhook_cleared"] = False
                    except httpx.HTTPError as exc:
                        self._log_step("WEBHOOK", f"FAIL - clear webhook HTTP error: {exc}", logging.ERROR)
                        report["webhook_cleared"] = False
                elif wh_url and not auto_clear_webhook:
                    self._log_step(
                        "WEBHOOK",
                        "WARNING: webhook URL is set but TELEGRAM_AUTO_CLEAR_WEBHOOK=false. "
                        "Polling may not receive updates while a webhook is active!",
                        logging.WARNING,
                    )
            except httpx.HTTPError as exc:
                self._log_step("WEBHOOK", f"FAIL - HTTP error: {exc}", logging.ERROR)

            # Step 5: Try getUpdates to see if polling works
            self._log_step("POLL_TEST", "testing getUpdates (timeout=1) ...")
            try:
                t0 = time.perf_counter()
                resp = await client.get(
                    f"{self.base_url}/getUpdates",
                    params={"timeout": 1, "limit": 1},
                    timeout=10,
                )
                elapsed = int((time.perf_counter() - t0) * 1000)
                poll_data = resp.json()
                if poll_data.get("ok"):
                    count = len(poll_data.get("result", []))
                    self._log_step(
                        "POLL_TEST",
                        f"OK - HTTP {resp.status_code} in {elapsed}ms, "
                        f"pending_updates={count}",
                        logging.INFO,
                    )
                    report["poll_test"] = True
                else:
                    desc = poll_data.get("description", "unknown")
                    error_code = poll_data.get("error_code", "?")
                    self._log_step(
                        "POLL_TEST",
                        f"FAIL - API returned ok=false (code={error_code}): {desc}",
                        logging.ERROR,
                    )
                    report["poll_test"] = False
            except httpx.HTTPError as exc:
                self._log_step("POLL_TEST", f"FAIL - HTTP error: {exc}", logging.ERROR)
                report["poll_test"] = False

        self._log_step("STARTUP", "connection test complete", logging.INFO)
        return report

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        self._log_step("GET_UPDATES", f"request offset={offset} timeout={timeout}")
        t0 = time.perf_counter()
        try:
            async with self._client(timeout=timeout + 5) as client:
                response = await client.get(f"{self.base_url}/getUpdates", params=payload)
                elapsed = int((time.perf_counter() - t0) * 1000)
                self._log_step("GET_UPDATES", f"HTTP {response.status_code} in {elapsed}ms")
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    desc = data.get("description", "unknown")
                    error_code = data.get("error_code", "?")
                    self._log_step(
                        "GET_UPDATES",
                        f"FAIL - API ok=false (code={error_code}): {desc}",
                        logging.ERROR,
                    )
                    raise RuntimeError(f"Telegram getUpdates failed: {data}")
                result = data.get("result", [])
                self._log_step("GET_UPDATES", f"OK - {len(result)} update(s) received")
                for upd in result:
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    text = (msg.get("text") or "")[:80]
                    chat = msg.get("chat", {})
                    self._log_step(
                        "GET_UPDATES",
                        f"  update_id={upd.get('update_id')} "
                        f"chat_id={chat.get('id')} "
                        f"from={msg.get('from', {}).get('username', '?')} "
                        f"text={text!r}",
                    )
                return result
        except httpx.HTTPError as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            self._log_step("GET_UPDATES", f"FAIL after {elapsed}ms - {exc.__class__.__name__}: {exc}", logging.ERROR)
            raise

    # ------------------------------------------------------------------
    # Diagnostics (used by /debug command)
    # ------------------------------------------------------------------
    async def run_diagnostics(self) -> dict[str, Any]:
        """Run lightweight connectivity/config checks against Telegram API."""
        report: dict[str, Any] = {
            "base_url": self.base_url,
            "chunk_size": self.chunk_size,
            "debug": self.debug,
            "proxy_enabled": bool(self.proxy_url),
        }

        try:
            resolved = socket.getaddrinfo("api.telegram.org", 443, proto=socket.IPPROTO_TCP)
            ips = sorted({item[4][0] for item in resolved if item and len(item) >= 5})
            report["dns"] = {"ok": True, "addresses": ips[:6], "count": len(ips)}
        except OSError as exc:
            report["dns"] = {"ok": False, "error": str(exc)}

        async with self._client(timeout=15) as client:
            started = time.perf_counter()
            try:
                response = await client.get(f"{self.base_url}/getMe")
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                payload = response.json()
                report["get_me"] = {
                    "ok": bool(payload.get("ok")),
                    "http_status": response.status_code,
                    "latency_ms": elapsed_ms,
                    "description": payload.get("description"),
                    "result": payload.get("result"),
                }
            except Exception as exc:  # noqa: BLE001
                report["get_me"] = {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }

            started = time.perf_counter()
            try:
                response = await client.get(f"{self.base_url}/getWebhookInfo")
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                payload = response.json()
                report["get_webhook_info"] = {
                    "ok": bool(payload.get("ok")),
                    "http_status": response.status_code,
                    "latency_ms": elapsed_ms,
                    "description": payload.get("description"),
                    "result": payload.get("result"),
                }
            except Exception as exc:  # noqa: BLE001
                report["get_webhook_info"] = {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }

        return report

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------
    async def send_text(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        chunks = self._chunk_text(text)
        self._log_step("SEND", f"sending {len(chunks)} chunk(s) to chat_id={chat_id} (total {len(text)} chars)")
        async with self._client(timeout=20) as client:
            for i, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                self._log_step(
                    "SEND",
                    f"  chunk {i + 1}/{len(chunks)}: {len(chunk)} chars, parse_mode={parse_mode}",
                )
                t0 = time.perf_counter()
                try:
                    response = await client.post(f"{self.base_url}/sendMessage", json=payload)
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    if response.status_code != 200:
                        body = response.text[:500]
                        self._log_step(
                            "SEND",
                            f"  FAIL - HTTP {response.status_code} in {elapsed}ms: {body}",
                            logging.ERROR,
                        )
                    else:
                        self._log_step("SEND", f"  OK - HTTP 200 in {elapsed}ms")
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    self._log_step(
                        "SEND",
                        f"  FAIL after {elapsed}ms - {exc.__class__.__name__}: {exc}",
                        logging.ERROR,
                    )
                    raise

    async def send_markdown_block(self, chat_id: int, title: str, body: str) -> None:
        title_text = self._escape_markdown_v2(title.strip() or "Message")
        body_chunks = self._chunk_code_block(body or "(empty)")
        total = len(body_chunks)
        for idx, chunk in enumerate(body_chunks, start=1):
            chunk_title = title_text
            if total > 1:
                chunk_title = f"{title_text} \\({idx}/{total}\\)"
            message = f"*{chunk_title}*\n```text\n{self._escape_code_block(chunk)}\n```"
            await self.send_text(chat_id, message, parse_mode="MarkdownV2")

    async def send_markdown_card(self, chat_id: int, title: str, body: str) -> None:
        messages = self._render_markdown_card_messages(title, body)
        for message in messages:
            await self.send_text(chat_id, message, parse_mode="MarkdownV2")

    async def send_markdown(self, chat_id: int, markdown_text: str) -> None:
        await self.send_text(chat_id, markdown_text, parse_mode="MarkdownV2")

    async def send_codex_reply(self, chat_id: int, body: str) -> None:
        messages = self._render_codex_reply_messages(body)
        for message in messages:
            await self.send_text(chat_id, message, parse_mode="MarkdownV2")

    async def send_photo(self, chat_id: int, path: str | Path, caption: str | None = None) -> None:
        file_path = Path(path)
        payload: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            payload["caption"] = caption[:1024]
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self._log_step("SEND_PHOTO", f"sending photo {file_path} to chat_id={chat_id}")
        async with self._client(timeout=60) as client:
            with file_path.open("rb") as fh:
                files = {"photo": (file_path.name, fh, mime_type)}
                response = await client.post(f"{self.base_url}/sendPhoto", data=payload, files=files)
                if response.status_code != 200:
                    body = response.text[:500]
                    self._log_step("SEND_PHOTO", f"FAIL - HTTP {response.status_code}: {body}", logging.ERROR)
                response.raise_for_status()

    async def send_document(self, chat_id: int, path: str | Path, caption: str | None = None) -> None:
        file_path = Path(path)
        payload: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            payload["caption"] = caption[:1024]
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self._log_step("SEND_DOCUMENT", f"sending document {file_path} to chat_id={chat_id}")
        async with self._client(timeout=120) as client:
            with file_path.open("rb") as fh:
                files = {"document": (file_path.name, fh, mime_type)}
                response = await client.post(f"{self.base_url}/sendDocument", data=payload, files=files)
                if response.status_code != 200:
                    body = response.text[:500]
                    self._log_step("SEND_DOCUMENT", f"FAIL - HTTP {response.status_code}: {body}", logging.ERROR)
                response.raise_for_status()

    async def get_me(self) -> dict[str, Any]:
        async with self._client(timeout=15) as client:
            response = await client.get(f"{self.base_url}/getMe")
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram getMe failed: {data}")
            return data.get("result", {})

    async def get_webhook_info(self) -> dict[str, Any]:
        async with self._client(timeout=15) as client:
            response = await client.get(f"{self.base_url}/getWebhookInfo")
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram getWebhookInfo failed: {data}")
            return data.get("result", {})

    def normalize_update(self, update: dict[str, Any]) -> TelegramInboundMessage | None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            self._log_step("NORMALIZE", f"skip update {update.get('update_id')}: no message payload")
            return None
        text = message.get("text")
        if not text:
            self._log_step("NORMALIZE", f"skip update {update.get('update_id')}: non-text message (keys={list(message.keys())})")
            return None
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        self._log_step(
            "NORMALIZE",
            f"update_id={update['update_id']} -> chat_id={chat['id']} "
            f"user=@{from_user.get('username', '?')} text={text.strip()[:60]!r}",
        )
        return TelegramInboundMessage(
            update_id=update["update_id"],
            chat_id=chat["id"],
            message_id=message["message_id"],
            text=text.strip(),
            username=from_user.get("username"),
        )

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= self.chunk_size:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            piece = remaining[: self.chunk_size]
            split_idx = piece.rfind("\n")
            if split_idx > self.chunk_size // 2:
                piece = piece[:split_idx]
            chunks.append(piece)
            remaining = remaining[len(piece):]
        return chunks

    def _chunk_code_block(self, text: str) -> list[str]:
        max_chunk = max(200, self.chunk_size - 80)
        lines = text.splitlines() or [text]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            remaining = line
            while remaining:
                escaped_line = self._escape_code_block(remaining)
                if len(escaped_line) > max_chunk:
                    take = max_chunk // 2
                    piece = remaining[:take]
                    remaining = remaining[take:]
                else:
                    piece = remaining
                    remaining = ""
                projected = current_len + len(self._escape_code_block(piece)) + 1
                if current and projected > max_chunk:
                    chunks.append("\n".join(current))
                    current = []
                    current_len = 0
                current.append(piece)
                current_len += len(self._escape_code_block(piece)) + 1
            if line == "":
                if current and current_len + 1 > max_chunk:
                    chunks.append("\n".join(current))
                    current = []
                    current_len = 0
                current.append("")
                current_len += 1
        if current:
            chunks.append("\n".join(current))
        return chunks or [text]

    def _render_markdown_card_messages(self, title: str, body: str) -> list[str]:
        title_text = self._escape_markdown_v2(title.strip() or "Message")
        lines = (body or "").splitlines()
        kv_lines: list[str] = []
        code_lines: list[str] = []
        text_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if code_lines and code_lines[-1] != "":
                    code_lines.append("")
                elif text_lines and text_lines[-1] != "":
                    text_lines.append("")
                continue
            if ": " in line and not line.startswith("cmd: "):
                key, value = line.split(": ", 1)
                if key and "\n" not in value:
                    kv_lines.append(f"• *{self._escape_markdown_v2(key)}:* `{self._escape_inline_code(value)}`")
                    continue
            if stripped.startswith("/"):
                text_lines.append(f"`{self._escape_inline_code(stripped)}`")
                continue
            if line.startswith("cmd: ") or line.startswith("diff ") or line.startswith("## ") or line.startswith("#"):
                code_lines.append(line)
                continue
            if len(line) > 120 or "/" in line or "\\" in line:
                code_lines.append(line)
                continue
            text_lines.append(self._escape_markdown_v2(line))

        blocks: list[str] = [f"*{title_text}*"]
        if kv_lines:
            blocks.extend(kv_lines)
        if text_lines:
            blocks.append("\n".join(text_lines))
        rendered = "\n".join(block for block in blocks if block.strip())

        messages: list[str] = []
        if rendered:
            messages.extend(self._chunk_markdown_message(rendered))
        if code_lines:
            code_chunks = self._chunk_code_block("\n".join(code_lines))
            total = len(code_chunks)
            for idx, chunk in enumerate(code_chunks, start=1):
                suffix = f" \\({idx}/{total}\\)" if total > 1 else ""
                messages.append(f"*{title_text} details{suffix}*\n```text\n{self._escape_code_block(chunk)}\n```")
        if not messages:
            messages.append(f"*{title_text}*\n`(empty)`")
        return messages

    def _render_codex_reply_messages(self, body: str) -> list[str]:
        body = (body or "").strip()
        if not body:
            return ["*Codex Reply*\n`(empty)`"]

        blocks: list[str] = []
        cursor = 0
        for match in _CODE_FENCE_RE.finditer(body):
            prose = body[cursor : match.start()]
            blocks.extend(self._render_codex_prose_blocks(prose))
            lang = self._normalize_code_language(match.group("lang"))
            code = match.group("body").strip("\n")
            if code:
                blocks.append(self._render_code_block(code, lang))
            cursor = match.end()
        blocks.extend(self._render_codex_prose_blocks(body[cursor:]))

        messages: list[str] = []
        current = "*Codex Reply*"
        for block in blocks:
            if self._is_section_boundary(block) and current != "*Codex Reply*":
                messages.append(current)
                current = "*Codex Reply*"
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) <= self.chunk_size:
                current = candidate
                continue
            if current and current != "*Codex Reply*":
                messages.append(current)
            current = block
            if len(current) > self.chunk_size and current.startswith("```"):
                header, _, rest = current.partition("\n")
                code_body = rest.removesuffix("\n```")
                lang = self._normalize_code_language(header.removeprefix("```"))
                for idx, chunk in enumerate(self._chunk_code_block(code_body), start=1):
                    prefix = "*Codex Reply*\n\n" if idx == 1 else "*Codex Reply \\(cont\\.\\)*\n\n"
                    messages.append(f"{prefix}{self._render_code_block(chunk, lang)}")
                current = ""
        if current:
            messages.append(current)
        return messages or ["*Codex Reply*\n`(empty)`"]

    def _render_codex_prose_blocks(self, prose: str) -> list[str]:
        lines = prose.splitlines()
        blocks: list[str] = []
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph
            if not paragraph:
                return
            text = " ".join(item.strip() for item in paragraph if item.strip())
            if text:
                reference_blocks = self._maybe_render_reference_blocks(text)
                if reference_blocks is not None:
                    blocks.extend(reference_blocks)
                else:
                    blocks.append(self._render_codex_inline(text))
            paragraph = []

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                flush_paragraph()
                continue
            if stripped in {"---", "——", "___"}:
                flush_paragraph()
                continue

            heading_text = self._normalize_heading_line(stripped)
            if heading_text is not None:
                flush_paragraph()
                blocks.append(f"*{self._escape_markdown_v2(heading_text)}*")
                continue

            bullet_match = _BULLET_RE.match(stripped)
            if bullet_match:
                flush_paragraph()
                blocks.append(f"• {self._render_codex_inline(bullet_match.group('body'))}")
                continue

            numbered_match = _NUMBERED_RE.match(stripped)
            if numbered_match:
                flush_paragraph()
                number = self._escape_markdown_v2(numbered_match.group("num"))
                blocks.append(f"{number}\\. {self._render_codex_inline(numbered_match.group('body'))}")
                continue

            if stripped.endswith(":") and len(stripped) <= 40:
                flush_paragraph()
                blocks.append(f"*{self._escape_markdown_v2(stripped[:-1])}*")
                continue

            paragraph.append(stripped)

        flush_paragraph()
        return blocks

    def _normalize_heading_line(self, text: str) -> str | None:
        if text.startswith("#"):
            return text.lstrip("#").strip() or None
        if text.startswith("**") and text.endswith("**") and text.count("**") == 2:
            return text[2:-2].strip() or None
        return None

    def _render_codex_inline(self, text: str) -> str:
        parts: list[str] = []
        cursor = 0
        for match in _INLINE_TOKEN_RE.finditer(text):
            if match.start() > cursor:
                parts.append(self._escape_markdown_v2(text[cursor : match.start()]))
            token = match.group(0)
            if token.startswith("`") and token.endswith("`"):
                parts.append(f"`{self._escape_inline_code(token[1:-1])}`")
            elif token.startswith("**") and token.endswith("**"):
                parts.append(f"*{self._escape_markdown_v2(token[2:-2])}*")
            elif token.startswith("["):
                parts.append(self._render_codex_link_token(token))
            cursor = match.end()
        if cursor < len(text):
            parts.append(self._escape_markdown_v2(text[cursor:]))
        return "".join(parts)

    def _render_codex_link_token(self, token: str) -> str:
        label, target = token[1:].split("](", 1)
        target = target[:-1]
        suffix = ""
        if "#" in target:
            _, frag = target.split("#", 1)
            line_match = _LOCAL_LINK_LINE_RE.fullmatch(frag)
            if line_match:
                suffix = f" `L{line_match.group('line')}`"
        return f"*{self._escape_markdown_v2(label)}*{suffix}"

    def _maybe_render_reference_blocks(self, text: str) -> list[str] | None:
        matches = list(_LOCAL_LINK_TOKEN_RE.finditer(text))
        if not matches:
            return None
        connector_text = _LOCAL_LINK_TOKEN_RE.sub(" ", text)
        normalized = re.sub(r"[，。、“”‘’：:;；,.!！?？()\s]+", "", connector_text)
        normalized = (
            normalized.replace("见", "")
            .replace("參见", "")
            .replace("参见", "")
            .replace("参考", "")
            .replace("另见", "")
            .replace("和", "")
            .replace("与", "")
            .replace("及", "")
            .replace("以及", "")
            .replace("and", "")
            .replace("or", "")
        )
        if normalized:
            return None
        blocks = ["*References*"]
        for match in matches:
            blocks.append(f"• {self._render_codex_link_token(match.group(0))}")
        return blocks

    @staticmethod
    def _is_heading_block(block: str) -> bool:
        return block.startswith("*") and block.endswith("*") and "\n" not in block and not block.startswith("• ")

    @classmethod
    def _is_section_boundary(cls, block: str) -> bool:
        return cls._is_heading_block(block) and block != "*References*"

    def _render_code_block(self, text: str, language: str | None = None) -> str:
        lang = self._normalize_code_language(language)
        return f"```{lang}\n{self._escape_code_block(text)}\n```"

    def _normalize_code_language(self, language: str | None) -> str:
        raw = (language or "").strip()
        cleaned = _CODE_LANG_RE.sub("", raw)
        return cleaned or "text"

    def _chunk_markdown_message(self, text: str) -> list[str]:
        if len(text) <= self.chunk_size:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            piece = remaining[: self.chunk_size]
            split_idx = piece.rfind("\n")
            if split_idx > self.chunk_size // 2:
                piece = piece[:split_idx]
            chunks.append(piece)
            remaining = remaining[len(piece):]
        return chunks

    @staticmethod
    def _escape_markdown_v2(text: str) -> str:
        escaped = text.replace("\\", "\\\\")
        for ch in _MARKDOWN_V2_SPECIALS:
            escaped = escaped.replace(ch, f"\\{ch}")
        return escaped

    @staticmethod
    def _escape_code_block(text: str) -> str:
        return text.replace("\\", "\\\\").replace("`", "\\`")

    @staticmethod
    def _escape_inline_code(text: str) -> str:
        return text.replace("\\", "\\\\").replace("`", "\\`")

    def escape_markdown_v2(self, text: str) -> str:
        return self._escape_markdown_v2(text)

    def escape_inline_code(self, text: str) -> str:
        return self._escape_inline_code(text)
