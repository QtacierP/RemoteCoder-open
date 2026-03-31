from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex.cli_session import CodexCliSessionBackend


def test_build_process_env_uses_clean_proxy_values(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:45159")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:45159")
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:44025")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:44025")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:44025")
    monkeypatch.setenv("all_proxy", "socks5h://127.0.0.1:44025")
    monkeypatch.setenv("UNRELATED_ENV", "should-not-pass-through")

    backend = CodexCliSessionBackend("codex", "--search", proxy_url="socks5h://127.0.0.1:45159")

    env = backend._build_process_env()

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[key] == "socks5h://127.0.0.1:45159"
    assert env["HOME"] == "/tmp/home"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert "UNRELATED_ENV" not in env
