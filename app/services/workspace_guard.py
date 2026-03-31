"""Workspace normalization and safety checks."""

from __future__ import annotations

from pathlib import Path


class WorkspaceGuard:
    def __init__(self, allowed_roots: list[Path], default_workspace: Path) -> None:
        self.allowed_roots = [root.resolve() for root in allowed_roots]
        self.default_workspace = default_workspace.resolve()
        self.ensure_allowed(self.default_workspace)

    def normalize(self, workspace: str | Path | None) -> Path:
        candidate = self.default_workspace if workspace is None else Path(workspace).expanduser().resolve()
        self.ensure_allowed(candidate)
        return candidate

    def ensure_allowed(self, path: Path) -> None:
        if not any(self._is_subpath(path, allowed) for allowed in self.allowed_roots):
            allowed = ", ".join(str(p) for p in self.allowed_roots)
            raise ValueError(f"Workspace '{path}' is outside allowed roots: {allowed}")

    @staticmethod
    def _is_subpath(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
