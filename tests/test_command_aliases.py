from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import normalize_command_alias


def test_normalize_command_alias_keeps_simple_status_command() -> None:
    assert normalize_command_alias("/status") == "/status"


def test_normalize_command_alias_supports_restart_service_aliases() -> None:
    assert normalize_command_alias("/restart service") == "/restart_service"
    assert normalize_command_alias("/service restart") == "/restart_service"


def test_normalize_command_alias_supports_git_subcommands_without_list_hash_crash() -> None:
    assert normalize_command_alias("/git status") == "/git_status"
    assert normalize_command_alias("/git diff HEAD") == "/git_diff HEAD"


def test_normalize_command_alias_supports_codex_api_aliases() -> None:
    assert normalize_command_alias("/codex api add relay :: model :: https://x :: key") == (
        "/codex_api_add relay :: model :: https://x :: key"
    )


def test_normalize_command_alias_supports_proxy_aliases() -> None:
    assert normalize_command_alias("/codex proxy on") == "/codex_proxy on"
    assert normalize_command_alias("/claude proxy off") == "/claude_code_proxy off"


def test_normalize_command_alias_supports_context_handoff_alias() -> None:
    assert normalize_command_alias("/context handoff light") == "/context_handoff light"
