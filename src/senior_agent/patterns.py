from __future__ import annotations

import re

CODE_FENCE_PATTERN = re.compile(
    r"```(?:[A-Za-z0-9_+-]+)?\n(?P<code>[\s\S]*?)```",
    re.MULTILINE,
)

__all__ = ["CODE_FENCE_PATTERN"]
