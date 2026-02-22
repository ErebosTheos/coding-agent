from __future__ import annotations

from pathlib import Path


def is_within_workspace(workspace: Path, candidate: Path) -> bool:
    """Ensures a candidate path is securely contained within the workspace root."""
    workspace_resolved = workspace.resolve()
    if candidate.is_absolute():
        candidate_resolved = candidate.resolve()
    else:
        candidate_resolved = (workspace_resolved / candidate).resolve()
    try:
        candidate_resolved.relative_to(workspace_resolved)
    except ValueError:
        return False
    return True
