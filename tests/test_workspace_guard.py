from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.workspace_guard import WorkspaceGuard


def test_normalize_relative_to_current_workspace(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    current = root / "project-a"
    target = current / "subdir"
    target.mkdir(parents=True)

    guard = WorkspaceGuard(allowed_roots=[root], default_workspace=root)

    resolved = guard.normalize("subdir", base_workspace=current)

    assert resolved == target.resolve()


def test_normalize_rejects_relative_escape(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    current = root / "project-a"
    current.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)

    guard = WorkspaceGuard(allowed_roots=[root], default_workspace=root)

    with pytest.raises(ValueError):
        guard.normalize("../../outside", base_workspace=current)
