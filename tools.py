"""Custom tools exposed to the AgentHack SDK.

Every function that performs a real tool operation (PDF read, local search,
file write, platform experiment client, source save) is wrapped with @tool so
the official SDK records its name and execution. Keep arguments and return
values JSON-friendly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from agenthack import tool
except ImportError:  # local development without the official base image
    from agenthack_compat import tool

from core.paperlib import list_pdfs, load_papers, probe_pdf, rank, read_pdf
from core.execution import check_active, publication
from core.files import atomic_write_text


@tool(name="pdf_probe")
def pdf_probe(path: str) -> dict[str, Any]:
    return probe_pdf(Path(path))


@tool(name="list_pdf_files")
def list_pdf_files(directory: str) -> list[str]:
    return [str(path) for path in list_pdfs(Path(directory))]


@tool(name="read_pdf_text")
def read_pdf_text(path: str, max_pages: int = 4) -> str:
    return read_pdf(Path(path), max_pages=max_pages)


@tool(name="read_materials")
def read_materials(paths: list[str], max_pages: int = 4, max_chars: int = 8000) -> list[dict[str, Any]]:
    papers = load_papers([Path(path) for path in paths], max_pages=max_pages)
    return [
        {
            "filename": paper.filename,
            "path": str(paper.path),
            # Keep tool telemetry bounded; the handlers only need excerpts.
            "text": paper.text[:max_chars],
        }
        for paper in papers
    ]


@tool(name="search_local_papers")
def search_local_papers(query: str, directory: str, top_k: int = 8) -> list[dict[str, Any]]:
    papers = load_papers(list_pdfs(Path(directory)))
    return [
        {
            "filename": paper.filename,
            "path": str(paper.path),
            "score": value,
            "snippet": paper.text[:900],
        }
        for paper, value in rank(query, papers, top_k=top_k)
    ]


@tool(name="write_text_file")
def write_text_file(path: str, text: str) -> str:
    target = Path(path)
    atomic_write_text(target, text)
    return str(target)


@tool(name="run_experiment_client")
def run_experiment_client(case_id: str, implementation: str, timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Call the platform-mounted D client. It owns the source-credential exchange."""
    from core.config import load_settings
    from d_adapter.runner import run_platform

    check_active()
    return run_platform(
        case_id,
        Path(implementation),
        load_settings(),
        timeout=timeout_seconds,
    )


@tool(name="save_generated_source")
def save_generated_source(response: dict[str, Any], destination: str) -> dict[str, str]:
    """Save the model's complete source verbatim (newline normalization only)."""
    from d_adapter.source import save_source

    with publication():
        sha = save_source(response, Path(destination))
    return {"path": destination, "sha256": sha}
