"""Publish UTF-8 files atomically while honoring case cancellation."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from core.execution import publication


def atomic_write_text(path: Path, text: str) -> None:
    with publication():
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(text.encode("utf-8"))
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()
