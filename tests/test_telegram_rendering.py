from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.telegram import TelegramAdapter


def test_render_codex_reply_formats_headings_lists_and_code() -> None:
    adapter = TelegramAdapter("123456:dummy-token")
    body = """**项目概览**

这是一个 `demo`。

- 第一项
1. 第二项

```bash
python train.py
```
"""

    messages = adapter._render_codex_reply_messages(body)

    assert len(messages) == 1
    message = messages[0]
    assert "*Codex Reply*" in message
    assert "*项目概览*" in message
    assert "• 第一项" in message
    assert "1\\. 第二项" in message
    assert "```bash\npython train.py\n```" in message


def test_render_codex_reply_simplifies_local_file_links() -> None:
    adapter = TelegramAdapter("123456:dummy-token")

    rendered = adapter._render_codex_inline(
        "见 [README.md](/tmp/project/README.md#L12) 和 [train](/tmp/project/train.py#L34)。"
    )

    assert "*README\\.md* `L12`" in rendered
    assert "*train* `L34`" in rendered


def test_render_codex_reply_extracts_reference_block() -> None:
    adapter = TelegramAdapter("123456:dummy-token")

    messages = adapter._render_codex_reply_messages(
        "见 [README.md](/tmp/project/README.md#L12) 和 [train.py](/tmp/project/train.py#L34)。"
    )

    assert len(messages) == 1
    message = messages[0]
    assert "*References*" in message
    assert "• *README\\.md* `L12`" in message
    assert "• *train\\.py* `L34`" in message


def test_render_codex_reply_splits_on_new_heading() -> None:
    adapter = TelegramAdapter("123456:dummy-token")

    messages = adapter._render_codex_reply_messages(
        "**第一部分**\n\n说明一。\n\n**第二部分**\n\n说明二。"
    )

    assert len(messages) == 2
    assert "*第一部分*" in messages[0]
    assert "说明一" in messages[0]
    assert "*第二部分*" in messages[1]
    assert "说明二" in messages[1]


def test_render_codex_reply_keeps_code_language_on_split() -> None:
    adapter = TelegramAdapter("123456:dummy-token", chunk_size=80)
    code = "\n".join(f"print({i})" for i in range(60))

    messages = adapter._render_codex_reply_messages(f"```python\n{code}\n```")

    assert len(messages) >= 2
    for message in messages:
        assert "```python\n" in message
