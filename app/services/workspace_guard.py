"""Workspace normalization and safety checks."""

from __future__ import annotations

from pathlib import Path


class WorkspaceGuard:
    def __init__(self, allowed_roots: list[Path], default_workspace: Path) -> None:
        self.allowed_roots = [root.resolve() for root in allowed_roots]
        self.default_workspace = default_workspace.resolve()
        self.ensure_allowed(self.default_workspace)

    def normalize(self, workspace: str | Path | None, base_workspace: str | Path | None = None) -> Path:
        if workspace is None:
            candidate = self.default_workspace
        else:
            raw = Path(workspace).expanduser()
            if raw.is_absolute():
                candidate = raw.resolve()
            else:
                base = Path(base_workspace).expanduser().resolve() if base_workspace is not None else self.default_workspace
                candidate = (base / raw).resolve()
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
