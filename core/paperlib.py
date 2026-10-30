"""Paper-library evidence helpers for B/C/E.

The design follows PaperCode's MaterialIndex + evidence-gate discipline:
each specified PDF is indexed by content hash and page count, given a title and
abstract, and reduced to query-relevant evidence sentences. Retrieval uses BM25
over the local corpus instead of raw term counts.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from pypdf import PdfReader

TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+", re.IGNORECASE)
SENTENCE_RE = re.compile(r"[^。！？!?\.]+[。！？!?\.]?")
INLINE_CITATION = r"\[\s*\d+(?:\s*[,，]\s*\d+)*(?:\s*,\s*p\.?\s*\d+)?\s*\]"
INLINE_CITATION_RE = re.compile(INLINE_CITATION)
LEADING_CITATIONS_RE = re.compile(r"^[ \t]*(?:" + INLINE_CITATION + r"[ \t]*)+")
ABSTRACT_RE = re.compile(r"(?:^|\n)\s*(?:abstract|摘\s*要)\s*[:：]?\s*", re.IGNORECASE)
FACT_HINT_RE = re.compile(
    r"\d|%|percent|研究|实验|数据|指标|结果|方法|模型|提出|表明|发现|证明|"
    r"reported|propos(?:e|ed)|show(?:s|ed)?|found|achiev(?:e|ed)|dataset|metric|"
    r"baseline|accuracy|recall|mrr|f1|bm25",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Paper:
    path: Path
    filename: str
    text: str


@dataclass(frozen=True)
class MaterialRecord:
    """Indexed evidence for one specified paper (PaperCode MaterialIndex.v1 port)."""

    path: Path
    filename: str
    sha256: str
    bytes: int
    pages: Optional[int]
    title: str
    abstract: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "pages": self.pages,
            "title": self.title,
            "abstract": self.abstract[:500],
        }


def list_pdfs(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.rglob("*") if path.suffix.lower() == ".pdf")


def read_pdf(path: Path, max_pages: int = 4, max_chars: int = 12000) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)[:max_chars]


def read_pdf_evidence(
    path: Path,
    *,
    head_pages: int = 8,
    tail_pages: int = 4,
    max_chars: int = 48000,
) -> str:
    """Read the head and tail of a paper: abstract/intro plus results/conclusion.

    Reading only the first N pages misses the results and conclusion that C/E
    fact checks usually target; reading the whole PDF wastes time and tokens.
    """
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    pages = list(reader.pages)
    if len(pages) > head_pages + tail_pages:
        selected = pages[:head_pages] + pages[-tail_pages:]
    else:
        selected = pages
    chunks: list[str] = []
    for page in selected:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)[:max_chars]


def probe_pdf(path: Path) -> dict[str, Any]:
    """Lightweight PDF diagnostic, adapted from PaperCode research-tools.

    It answers "is this actually a PDF and how many pages?" without trusting the
    filename alone. A corrupt file is reported, not silently treated as text.
    """
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "looks_like_pdf": False,
        "pages": None,
        "error": None,
    }
    if not path.exists():
        result["error"] = "file not found"
        return result
    try:
        with path.open("rb") as handle:
            result["looks_like_pdf"] = handle.read(5) == b"%PDF-"
    except OSError as exc:
        result["error"] = str(exc)
        return result
    try:
        result["pages"] = len(PdfReader(str(path)).pages)
    except Exception as exc:
        result["error"] = str(exc)[:200]
    return result


def load_papers(paths: Iterable[Path], max_pages: int = 4) -> list[Paper]:
    papers: list[Paper] = []
    for path in paths:
        text = read_pdf(path, max_pages=max_pages)
        papers.append(Paper(path=path, filename=path.name, text=text))
    return papers


def terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def paper_title(text: str, filename: str) -> str:
    """First plausible title line; PDF text usually starts with title + authors."""
    for line in (text or "").splitlines():
        value = " ".join(line.split())
        if 5 <= len(value) <= 300 and not re.match(
            r"^(abstract|摘要|keywords|关键词|arxiv|doi|http|www\.)", value, re.IGNORECASE
        ):
            return value
    return Path(filename).stem.replace("_", " ")


def paper_abstract(text: str, limit: int = 900) -> str:
    flat = " ".join((text or "").split())
    match = ABSTRACT_RE.search(flat)
    if match:
        return flat[match.end() : match.end() + limit].strip()
    return flat[:limit].strip()


def sentence_split(text: str) -> list[str]:
    # A page reference's abbreviation is not a sentence ending. Protect the
    # whole citation before normal decimal/filename dot handling.
    protected = INLINE_CITATION_RE.sub(lambda match: match.group(0).replace(".", "․"), text or "")
    # Protect non-space dots (decimals like 0.9, filenames like paper_2024.pdf)
    # so the sentence splitter does not cut them in half. A real English
    # sentence boundary is ". " and is still split below.
    protected = re.sub(r"\.(?=[^\s\[])", "․", protected)
    sentences: list[str] = []
    for fragment in SENTENCE_RE.findall(protected):
        value = fragment.replace("․", ".")
        # The specification also places citations after sentence punctuation.
        # Move only a leading citation group, never the following sentence's
        # text or an inline citation belonging to that following sentence.
        leading = LEADING_CITATIONS_RE.match(value)
        if leading and sentences:
            sentences[-1] += leading.group(0).strip()
            value = value[leading.end():]
        if value.strip():
            sentences.append(value.strip())
    return sentences


def evidence_excerpt(paper: Paper, query: str = "", max_chars: int = 1800) -> str:
    """Title + abstract + query-relevant sentences, bounded for the prompt."""
    flat = " ".join((paper.text or "").split())
    if not flat:
        return f"标题：{paper_title('', paper.filename)}\n（无法提取正文）"
    title = paper_title(flat, paper.filename)
    abstract = paper_abstract(flat)
    query_terms = set(terms(query))
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentence_split(flat)):
        if len(sentence) < 24:
            continue
        sentence_terms = set(terms(sentence))
        overlap = len(query_terms & sentence_terms)
        numeric = 1.0 if re.search(r"\d", sentence) else 0.0
        method = 1.0 if FACT_HINT_RE.search(sentence) else 0.0
        scored.append((overlap * 3.0 + numeric + method, index, sentence))
    chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:8]
    chosen.sort(key=lambda item: item[1])
    key_sentences = " ".join(item[2] for item in chosen)
    block = f"标题：{title}\n摘要：{abstract}\n关键句：{key_sentences}"
    return block[:max_chars]


def index_materials(paths: Iterable[Path], max_pages: int = 6, max_chars: int = 48000) -> list[MaterialRecord]:
    """Build a MaterialIndex-style record list, one per specified PDF."""
    records: list[MaterialRecord] = []
    for path in paths:
        diagnostic = probe_pdf(path)
        text = read_pdf_evidence(
            path,
            head_pages=max(1, min(int(max_pages), 8)),
            tail_pages=4,
            max_chars=max_chars,
        )
        records.append(
            MaterialRecord(
                path=path,
                filename=path.name,
                sha256=file_sha256(path),
                bytes=int(diagnostic.get("size_bytes") or 0),
                pages=diagnostic.get("pages"),
                title=paper_title(text, path.name),
                abstract=paper_abstract(text),
                text=text,
            )
        )
    return records


def score(query: str, paper: Paper) -> float:
    query_terms = terms(query)
    if not query_terms:
        return 0.0
    haystack = (paper.filename + "\n" + paper.text).lower()
    return float(sum(haystack.count(term) for term in query_terms))


def bm25_rank(
    query: str,
    papers: list[Paper],
    top_k: int = 12,
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[Paper, float]]:
    """BM25 over filename + body. Title is duplicated to give it more weight."""
    query_terms = list(dict.fromkeys(terms(query)))
    if not query_terms or not papers:
        return []
    documents = [terms(paper.filename + " " + paper.filename + " " + paper.text) for paper in papers]
    count = len(documents)
    average_length = sum(len(document) for document in documents) / count or 1.0
    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))
    scored: list[tuple[Paper, float]] = []
    for paper, document in zip(papers, documents):
        frequencies = Counter(document)
        value = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency <= 0:
                continue
            idf = math.log(1.0 + (count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * len(document) / average_length)
            value += idf * frequency * (k1 + 1.0) / denominator
        scored.append((paper, value))
    scored.sort(key=lambda item: (-item[1], item[0].filename.lower()))
    return scored[:top_k]


def rank(query: str, papers: list[Paper], top_k: int = 8) -> list[tuple[Paper, float]]:
    return bm25_rank(query, papers, top_k=top_k)


def snippet(paper: Paper, query: str, limit: int = 1200) -> str:
    text = paper.text.strip()
    if not text:
        return ""
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms(query) if lowered.find(term) >= 0]
    start = max(0, min(positions) - 200) if positions else 0
    return text[start : start + limit]


def _paper_roots(settings_data_dir: Path) -> list[Path]:
    roots = [settings_data_dir / "论文库", settings_data_dir / "papers"]
    for value in (os.environ.get("DATA_PATH"), "/data"):
        if not value:
            continue
        base = Path(value)
        roots.extend([base / "论文库", base / "papers", base])
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def resolve_materials(materials: object, settings_data_dir: Path) -> list[Path]:
    """Resolve the question's `materials` into actual PDF paths.

    Accepts a list of filenames, a directory, or a missing value. Searches the
    contest workspace as well as the base image's read-only DATA_PATH (/data).
    """
    roots = _paper_roots(settings_data_dir)
    candidates: list[Path] = []
    if isinstance(materials, str):
        path = Path(materials)
        if path.is_dir():
            candidates.extend(list_pdfs(path))
        elif path.exists():
            candidates.append(path)
    elif isinstance(materials, list):
        for item in materials:
            if not isinstance(item, str):
                continue
            path = Path(item)
            if path.is_dir():
                candidates.extend(list_pdfs(path))
            elif path.exists() and path.suffix.lower() == ".pdf":
                candidates.append(path)
            else:
                for root in roots:
                    found = list(root.rglob(path.name)) if root.exists() else []
                    if found:
                        candidates.append(found[0])
                        break
    if not candidates:
        for root in roots:
            if root.exists():
                found = list_pdfs(root)
                if found:
                    candidates.extend(found)
                    break
    # de-duplicate by filename, keep first
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        if path.name not in seen:
            seen.add(path.name)
            unique.append(path)
    return unique
