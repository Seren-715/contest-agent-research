"""Paper-library evidence helpers for B/C/E.

The design follows PaperCode's MaterialIndex + evidence-gate discipline:
each specified PDF is indexed by content hash and page count, given a title and
abstract, and reduced to query-relevant evidence sentences. Retrieval uses BM25
over the local corpus instead of raw term counts.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from pypdf import PdfReader

from core.budget import Deadline
from core.execution import check_active, current_execution

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
class EvidencePassage:
    """One exact span in a PDF page's pypdf extraction, before normalization."""

    page: int
    start: int
    end: int
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
    passages: tuple[EvidencePassage, ...] = ()
    indexed_pages: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "pages": self.pages,
            "title": self.title,
            "abstract": self.abstract[:500],
            "indexed_pages": list(self.indexed_pages),
            "warnings": list(self.warnings),
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
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text or ""):
        value = match.group(0).lower()
        tokens.append(value)
        # No external dictionary is needed to match a phrase inside a longer
        # Chinese sentence. Whole runs alone made almost every such match fail.
        if len(value) > 2 and "一" <= value[0] <= "鿿":
            tokens.extend(value[index:index + 2] for index in range(len(value) - 1))
    return tokens


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


def evidence_excerpt(paper: Paper | MaterialRecord, query: str = "", max_chars: int = 1800) -> str:
    """Title + abstract + query-relevant sentences, bounded for the prompt."""
    if getattr(paper, "passages", ()):
        ranked = rank_passages(query, paper)
        header = "标题：" + paper.title[:min(160, max_chars // 5)] + "\n"
        blocks: list[str] = []
        remaining = max(0, max_chars - len(header))
        for passage, _ in ranked:
            label = f"[p.{passage.page}, chars {passage.start}:{passage.end}] "
            value = label + passage.text
            if len(value) + 1 > remaining:
                length = remaining - len(label) - 1
                if length >= 24:
                    lowered = passage.text.lower()
                    keywords = {term for term in terms(query) if len(term) > 1}
                    offsets = {0}
                    for term in keywords:
                        position = lowered.find(term)
                        if position >= 0:
                            offsets.add(max(0, min(position - length // 3, len(lowered) - length)))
                    start = max(sorted(offsets), key=lambda offset: (
                        sum(term in lowered[offset:offset + length] for term in keywords),
                        bool(re.search(r"\d", lowered[offset:offset + length])), -offset,
                    ))
                    source_start = passage.start + start
                    # Updating a locator can add digits; fit the exact label,
                    # quote and end offset together, never relabel old text.
                    for _ in range(4):
                        quote = passage.text[start:start + length]
                        label = f"[p.{passage.page}, chars {source_start}:{source_start + len(quote)}] "
                        excess = len(label) + len(quote) + 1 - remaining
                        if excess <= 0:
                            break
                        length -= excess
                    value = label + quote
                else:
                    continue
            if len(value) + 1 <= remaining:
                blocks.append(value)
                remaining -= len(value) + 1
            if remaining < 100:
                break
        return (header + "\n".join(blocks))[:max_chars]
    flat = " ".join((paper.text or "").split())
    if not flat:
        return f"标题：{paper_title('', paper.filename)}\n（无法提取正文）"
    title = paper_title(flat, paper.filename)
    abstract = paper_abstract(flat, limit=max(0, min(350, max_chars // 4)))
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
    # Preserve relevance order so a long early sentence cannot push a more
    # relevant results sentence out of the prompt.
    key_sentences = " ".join(item[2] for item in chosen)
    block = f"标题：{title}\n摘要：{abstract}\n关键句：{key_sentences}"
    return block[:max_chars]


def _index_limit(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _passages(text: str, page: int, size: int = 1200, overlap: int = 200) -> list[EvidencePassage]:
    values: list[EvidencePassage] = []
    for start in range(0, len(text), size - overlap):
        end = min(len(text), start + size)
        if text[start:end].strip():
            values.append(EvidencePassage(page, start, end, text[start:end]))
        if end == len(text):
            break
    return values


def rank_passages(query: str, record: MaterialRecord) -> list[tuple[EvidencePassage, float]]:
    """Rank exact page spans, with deterministic page/offset tie breaking."""
    passages = list(record.passages)
    if not terms(query):
        return [(passage, 0.0) for passage in passages]
    documents = [Paper(record.path, f"{item.page:08d}:{item.start:08d}", item.text) for item in passages]
    ranked = bm25_rank(query, documents, top_k=len(documents))
    lookup = {document.filename: passage for document, passage in zip(documents, passages)}
    return [(lookup[document.filename], value) for document, value in ranked]


def index_materials(
    paths: Iterable[Path], max_pages: Optional[int] = None, max_chars: Optional[int] = None,
    *, deadline: Optional[Deadline] = None, page_selection: str = "spread",
) -> list[MaterialRecord]:
    """Index all authorized files, retaining exact page spans and coverage gaps.

    Limits bound work rather than silently selecting the first few papers.
    Per-page text budgets stop a long introduction from erasing later pages.
    No process-wide cache can mix evidence between differently scoped tasks.
    """
    page_limit = max_pages if max_pages is not None else _index_limit("CONTEST_PDF_MAX_PAGES", 256)
    char_limit = max_chars if max_chars is not None else _index_limit("CONTEST_PDF_MAX_CHARS", 256000)
    byte_limit = _index_limit("CONTEST_PDF_MAX_BYTES", 64 * 1024 * 1024)
    total_chars = _index_limit("CONTEST_INDEX_MAX_CHARS", 8000000)
    seconds = _index_limit("CONTEST_INDEX_SECONDS", 120)
    control = current_execution()
    # Parsing must leave time for generation and publication. Fallback parsing
    # must not reset the outer case budget or consume its final ten seconds.
    seconds = min(seconds, max(0.0, control.remaining() - 15)) if control else seconds
    if deadline is not None:
        seconds = min(seconds, max(0.0, deadline.remaining() - 30))
    if page_limit <= 0 or char_limit <= 0:
        raise ValueError("PDF page and character limits must be positive")
    if page_selection not in ("spread", "head_tail"):
        raise ValueError("unknown PDF page selection")
    started = time.monotonic()
    records: list[MaterialRecord] = []
    paths = list(paths)
    for position, path in enumerate(paths):
        check_active()
        # Fair share of the remaining time: a slow early file cannot starve
        # every later file (paths are in filename order). Unused time carries
        # forward, and the last file may use whatever remains.
        file_started = time.monotonic()
        file_seconds = max(0.0, seconds - (file_started - started)) / (len(paths) - position)
        warnings: list[str] = []
        spans: list[EvidencePassage] = []
        indexed: list[int] = []
        attempted_pages = 0
        pages: Optional[int] = None
        size = path.stat().st_size if path.is_file() else 0
        digest = ""
        if size > byte_limit:
            warnings.append("pdf_byte_limit")
        elif total_chars <= 0 or time.monotonic() - started >= seconds:
            warnings.append("index_budget_exhausted")
        else:
            digest = file_sha256(path)
            try:
                reader = PdfReader(str(path))
                pages = len(reader.pages)
                # If a huge PDF exceeds the page budget, distribute selected
                # pages across it and report incomplete coverage explicitly.
                if pages > page_limit:
                    if page_selection == "head_tail":
                        tail = min(4, page_limit)
                        selected = list(range(page_limit - tail)) + list(range(pages - tail, pages))
                    else:
                        selected = sorted({round(i * (pages - 1) / max(1, page_limit - 1))
                                           for i in range(page_limit)})
                    warnings.append("pdf_page_limit")
                else:
                    selected = list(range(pages))
                per_page = min(12000, min(char_limit, total_chars) // max(1, len(selected)))
                for number in selected:
                    check_active()
                    now = time.monotonic()
                    # The per-file share starts limiting after one attempt:
                    # hashing and PdfReader setup can exceed the share on their
                    # own, which used to leave a readable file with no text.
                    # Individual parsing calls cannot be interrupted here.
                    if now - started >= seconds or (attempted_pages and now - file_started >= file_seconds):
                        warnings.append("index_time_limit")
                        break
                    attempted_pages += 1
                    try:
                        raw = reader.pages[number].extract_text() or ""
                    except Exception:
                        warnings.append(f"page_extract_failed:{number + 1}")
                        continue
                    indexed.append(number + 1)
                    if len(raw) > per_page:
                        warnings.append(f"page_char_limit:{number + 1}")
                    retained = raw[:per_page]
                    spans.extend(_passages(retained, number + 1))
                    total_chars -= len(retained)
            except Exception as exc:
                # Cancellation is not a PDF parse error and must propagate.
                check_active()
                warnings.append("pdf_parse_failed:" + type(exc).__name__)
        # Overlapping passages are NOT concatenated to avoid duplicate facts.
        page_text: dict[int, str] = {}
        for span in spans:
            previous = page_text.get(span.page, "")
            page_text[span.page] = previous + span.text[max(0, len(previous) - span.start):]
        text = "\n".join(page_text.values())
        if not text.strip():
            warnings.append("no_extractable_text")
        if warnings:
            logging.getLogger(__name__).warning("material_index filename=%r warnings=%s", path.name, warnings)
        records.append(
            MaterialRecord(
                path=path,
                filename=path.name,
                sha256=digest,
                bytes=size,
                pages=pages,
                title=paper_title(text, path.name),
                abstract=paper_abstract(text),
                text=text,
                passages=tuple(spans),
                indexed_pages=tuple(indexed),
                warnings=tuple(warnings),
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


class MaterialResolutionError(RuntimeError):
    def __init__(self, message: str, *, skipped: Optional[dict] = None, shapes: Optional[dict] = None):
        logging.getLogger(__name__).warning("material_resolution_failed reason=%s", message.split(":", 1)[0])
        super().__init__(message)
        # Diagnostic counts only (never file names): skip reasons and the
        # shapes of the declared entries, see core/failure_code.py.
        self.skipped = dict(skipped or {})
        self.shapes = dict(shapes or {})


def material_shape(value: object) -> str:
    """Coarse form of a declared material, for diagnostics without names."""
    if not isinstance(value, str) or not value.strip():
        return "other"
    text = value.strip()
    path = Path(text)
    if path.is_absolute():
        return "absolute"
    if path.parts and path.parts[0] == "data" and len(path.parts) > 1:
        return "data_relative"
    if len(path.parts) > 1:
        return "relative"
    if path.suffix.lower() == ".pdf":
        return "pdf_name"
    if path.suffix:
        return "other_suffix"
    return "cjk_name" if re.search(r"[\u4e00-\u9fff]", text) else "ascii_name"


def _team_name() -> Optional[str]:
    """This team's directory name, from the same variables as core.config."""
    for key in ("RESEARCH_WORKSPACE_TEAM", "TEAM_ID", "CONTEST_TEAM"):
        value = os.environ.get(key, "").strip()
        if value and value not in (".", "..") and "/" not in value and "\\" not in value:
            return value
    return None


def _is_team_dir(directory: Path) -> bool:
    """Any team's workspace (it holds assigned/ or dispatch.json)."""
    try:
        return (directory / "assigned").is_dir() or (directory / "dispatch.json").is_file()
    except OSError:
        return False


def _walk_dirs(start: Path, depth: int, own_team: Optional[Path]) -> list[Path]:
    """Directories below ``start`` up to ``depth`` levels, never entering task
    or output trees or another team's workspace."""
    found: list[Path] = []
    frontier = [(start, 0)]
    while frontier:
        directory, level = frontier.pop()
        try:
            children = sorted(child for child in directory.iterdir() if child.is_dir())
        except OSError:
            continue
        for child in children:
            if child.name.casefold() in _NOT_A_LIBRARY:
                continue
            if _is_team_dir(child) and (own_team is None or _lexical(child) != _lexical(own_team)):
                continue
            found.append(child)
            if level + 1 < depth:
                frontier.append((child, level + 1))
    return found


def _suffix_matches(path: Path, team_dirs: list[Path], depth: int = 2) -> list[Path]:
    """The declared relative path inside this team's directory tree.

    "data/论文库" may be mounted at /data/<team>/论文库. Only this team's
    workspace is searched (never another team's), and only the complete
    declared suffix matches. Callers still require a unique match.
    """
    parts = path.parts[1:] if path.parts and path.parts[0] == "data" and len(path.parts) > 1 else path.parts
    if not parts:
        return []
    suffix = Path(*parts)
    found: list[Path] = []
    for team in dict.fromkeys(team_dirs):
        if not team.is_dir():
            continue
        for directory in [team] + _walk_dirs(team, depth, team):
            candidate = directory / suffix
            if candidate.exists():
                found.append(candidate)
    return list(dict.fromkeys(found))

def is_material_description(value: object) -> bool:
    """Identify a natural-language paper scope, never a missing concrete path.

    Official examples describe a local/assigned paper collection instead of
    naming files. Such entries require task-specific selection after discovery.
    A PDF filename, explicit path or glob is not a description.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or re.search(r"[\\/:*?]", text) or re.search(r"\.[a-zA-Z0-9]{1,12}(?:\s|$)", text):
        return False
    chinese_scope = re.search(r"本地|本题|指定|允许|提供", text)
    chinese_papers = re.search(r"论文|文献|PDF", text, re.IGNORECASE)
    if chinese_scope and chinese_papers:
        return True
    return bool(re.search(r"\b(?:local|specified|provided|permitted|allowed)\b.*\b(?:papers?|pdfs?|literature)\b",
                          text, re.IGNORECASE))


def is_scope_prose(value: object) -> bool:
    """Chinese prose describing a whole collection of papers.

    Platform B cases (eval 2026-11-01 06:21, timing code 230) declare their
    search scope in Chinese without the scope words is_material_description()
    requires, e.g. "论文库中的全部论文". Only whole-library retrieval opts in.
    A bare name such as "2024论文资料" or "论文库" is a name that was not
    found, never a description: it needs a collective or locative word
    (全部/所有/中的/内的/范围...) together with a paper word, and no path
    separator, glob or extension.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or re.search(r"[\\/:*?]", text) or re.search(r"\.[a-zA-Z0-9]{1,12}(?:\s|$)", text):
        return False
    collective = re.search(r"全部|所有|全体|整个|中的|内的|里的|之中|范围|以内", text)
    papers = re.search(r"论文|文献|PDF|资料|材料", text, re.IGNORECASE)
    return bool(collective and papers)


def describe_material_scope(materials: object) -> list[str]:
    """Return declared descriptions for a caller's explicit selection stage."""
    values = [materials] if isinstance(materials, str) else materials
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if is_material_description(value)]


def material_exists(value: object, settings_data_dir: Path) -> bool:
    """Whether a declaration names an existing file or directory.

    Existing paths take precedence over description wording: a folder called
    "指定论文" is a path. Uses the same lookup as resolve_materials().
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return bool(resolve_materials([value], settings_data_dir))
    except MaterialResolutionError:
        return False


def has_explicit_material_scope(materials: object) -> bool:
    """Recognize ordinary PDF/directory declarations, after strict resolution.

    This is a routing hint, not path authorization. Callers can resolve the
    non-description subset separately when directory names contain extensions.
    """
    values = [materials] if isinstance(materials, str) else materials
    if not isinstance(values, list):
        return False
    for value in values:
        if not isinstance(value, str) or not value.strip() or is_material_description(value):
            continue
        path = Path(value)
        if path.suffix.lower() == ".pdf" or not path.suffix or path.is_dir():
            return True
    return False


def _identical_materials(paths: list[Path]) -> bool:
    """Separate mount copies are equivalent only after exact content hashing."""
    if not paths or any(not path.is_file() for path in paths):
        return False
    if len({path.stat().st_size for path in paths}) != 1:
        return False
    hashes = [file_sha256(path) for path in paths]
    return bool(hashes[0]) and len(set(hashes)) == 1


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _on_disk_case(path: Path) -> Path:
    """On case-insensitive file systems a path "exists" in any letter case;
    report the file name exactly as the directory stores it."""
    try:
        names = os.listdir(path.parent)
    except OSError:
        return path
    if path.name in names:
        return path
    matches = [name for name in names if _fold_name(name) == _fold_name(path.name)]
    return path.parent / matches[0] if len(matches) == 1 else path


def _fold_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


_NOT_A_LIBRARY = {"experiments", "assigned", "expert", "figure", "work", "diagnostics"}


def _library_like_dirs(bases: list[Path], team_dirs: list[Path], depth: int = 3) -> list[Path]:
    """Library-like directories in the shared data roots (top level only) or
    inside this team's workspace; never another team's workspace."""
    pattern = re.compile(r"论文|文献|paper|pdf|literature", re.IGNORECASE)
    candidates: list[Path] = []
    for base in dict.fromkeys(bases):
        if base.is_dir():
            candidates.extend(_walk_dirs(base, 1, None))
    for team in dict.fromkeys(team_dirs):
        if team.is_dir():
            candidates.extend(_walk_dirs(team, depth, team))
    return list(dict.fromkeys(directory for directory in candidates if pattern.search(directory.name)))

def resolve_materials(
    materials: object, settings_data_dir: Path, *, allow_library: bool = False,
    allow_descriptions: bool = False, lenient: bool = False, prose_scope: bool = False,
) -> list[Path]:
    """Resolve declared materials; description discovery needs explicit opt-in.

    Missing declarations never broaden scope. Ambiguous basenames are errors,
    not an invitation to pick whichever root happened to be searched first.
    Description-only discovery returns candidates, not a selected C/E corpus.
    ``lenient`` (retrieval over a whole library) skips a missing or ambiguous
    entry instead of failing the case; it never substitutes another file.
    ``prose_scope`` (B only) also reads unmatched Chinese prose without any
    path or extension as a description (see is_scope_prose()).
    """
    skipped: list[str] = []
    roots = _paper_roots(settings_data_dir)
    data_bases = [settings_data_dir, Path("/data")]
    if os.environ.get("DATA_PATH"):
        data_bases.append(Path(os.environ["DATA_PATH"]))
    allowed = list(dict.fromkeys([_lexical(base) for base in data_bases] + [base.resolve() for base in data_bases]))

    resolved_bases = list(dict.fromkeys(base.resolve() for base in data_bases))
    team = _team_name()
    team_dirs = [base / team for base in dict.fromkeys(data_bases) if team and (base / team).is_dir()]
    # Searched by name: the shared libraries and this team's workspace only.
    # A whole data root may hold other teams' directories and is never walked.
    search_roots = [root for root in roots if root.name in ("论文库", "papers")] + team_dirs

    def within(path: Path) -> bool:
        # Both the declared location and the real target (after symlinks) must
        # lie inside a data root: the spec forbids symlinks that leave it.
        lexical, resolved = _lexical(path), path.resolve()
        return (any(lexical == base or base in lexical.parents for base in allowed)
                and any(resolved == base or base in resolved.parents for base in resolved_bases))

    def checked(path: Path) -> Path:
        if not within(path):
            raise MaterialResolutionError(f"material outside allowed data roots: {path.name}")
        # Keep the library's own (link) name, spelled as it is on disk.
        return _on_disk_case(_lexical(path))

    def accept(paths: Iterable[Path]) -> list[Path]:
        """checked() for each path; whole-library retrieval skips escaping links."""
        result: list[Path] = []
        for value in paths:
            if lenient and not within(value):
                skipped.append("outside_roots")
                continue
            result.append(checked(value))
        return result

    folded_index: dict[str, list[Path]] = {}

    def folded_matches(name: str) -> list[Path]:
        """Case- and Unicode-insensitive filename lookup in the paper roots.

        The spec treats names differing only in letter case as the same paper
        and says to read the library's actual file name.
        """
        if not folded_index:
            for root in search_roots:
                if root.is_dir():
                    for value in root.rglob("*"):
                        if value.is_file():
                            folded_index.setdefault(_fold_name(value.name), []).append(value)
            folded_index.setdefault("", [])
        # A same-named file whose real target leaves the data roots is not a match.
        return [checked(value) for value in folded_index.get(_fold_name(name), []) if within(value)]

    def folded_path_matches(path: Path) -> list[Path]:
        # A declared directory part is part of the identity: compare the whole
        # declared suffix (minus a leading "data/"), never just the basename.
        parts = list(path.parts[1:] if path.parts and path.parts[0] == "data" and len(path.parts) > 1 else path.parts)
        wanted = [_fold_name(part) for part in parts]
        return [value for value in folded_matches(path.name)
                if [_fold_name(part) for part in value.parts[-len(wanted):]] == wanted]

    def discover_library(*, descriptions_only: bool = False) -> list[Path]:
        declared: list[Path] = []
        libraries = [root for root in roots if root.name in ("论文库", "papers") and root.is_dir()]
        for root in (libraries if descriptions_only else (libraries or team_dirs)):
            if root.exists():
                declared.extend(list_pdfs(root))
        if not declared:
            # The library may sit under the team directory or use another name.
            for directory in _library_like_dirs(data_bases, team_dirs):
                declared.extend(list_pdfs(directory))
            if declared:
                logging.getLogger(__name__).warning("material_library_fallback pdfs=%d", len(declared))
        result: list[Path] = []
        for path in declared:
            if lenient and not within(path):
                skipped.append("outside_roots")
                continue
            result.append(checked(path))
        return result

    def identical_pdf_scopes(directories: list[Path]) -> bool:
        if not directories or any(not directory.is_dir() for directory in directories):
            return False
        # Compare only PDF scope. Auxiliary files are neither read nor claimed
        # to be equivalent. Never union distinct directory scopes implicitly.
        scopes: list[dict[str, Path]] = []
        for directory in directories:
            check_active()
            scopes.append({path.relative_to(directory).as_posix(): checked(path)
                           for path in list_pdfs(directory)})
        keys = set(scopes[0])
        if any(set(scope) != keys for scope in scopes[1:]):
            return False
        for key in keys:
            check_active()
            if not _identical_materials([scope[key] for scope in scopes]):
                return False
        return True

    missing = materials is None or materials == []
    if missing:
        if not allow_library:
            return []
        candidates = discover_library()
    else:
        values = [materials] if isinstance(materials, str) else materials
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise MaterialResolutionError("materials must contain nonempty path strings")
        candidates = []
        descriptions: list[str] = []
        explicit_paper_scope = False
        # A declared entry skipped in lenient mode was still a declaration:
        # it must not leave an accompanying description free to widen.
        explicit_skipped = False
        for item in values:
            check_active()
            path = Path(item)
            if ".." in path.parts:
                raise MaterialResolutionError("material path contains parent traversal")
            if path.is_absolute():
                found = accept([path]) if path.exists() else []
            else:
                exact = [base / path for base in [settings_data_dir, settings_data_dir.parent] + roots]
                # Questions use paths relative to the contest package, while
                # deployment mounts its data separately from writable /output.
                # Preserve the complete suffix; never reduce an explicit path
                # to a basename that could select another paper.
                if path.parts and path.parts[0] == "data" and len(path.parts) > 1:
                    suffix = Path(*path.parts[1:])
                    data_bases = [settings_data_dir, Path("/data")]
                    if os.environ.get("DATA_PATH"):
                        data_bases.append(Path(os.environ["DATA_PATH"]))
                    exact.extend(base / suffix for base in data_bases)
                    if suffix.parts[0] in ("论文库", "papers") and len(suffix.parts) > 1:
                        exact.extend(root / Path(*suffix.parts[1:]) for root in roots
                                     if root.name == suffix.parts[0])
                found = accept(value for value in exact if value.exists())
                if len(path.parts) == 1:
                    found.extend(accept(value for root in search_roots if root.exists()
                                        for value in root.rglob("*") if value.name == path.name))
            found = list(dict.fromkeys(found))
            if not found and not path.is_absolute():
                found = list(dict.fromkeys(accept(_suffix_matches(path, team_dirs))))
                if found:
                    logging.getLogger(__name__).warning("material_suffix_found matches=%d", len(found))
            if not found and path.suffix:
                found = list(dict.fromkeys(folded_path_matches(path)))
                if found:
                    logging.getLogger(__name__).warning("material_name_folded matches=%d", len(found))
            # An existing file or directory always wins: a folder named
            # "指定论文" is a path, not prose. Only an entry that matches
            # nothing on disk may be a description, and only if it carries the
            # explicit scope wording is_material_description() requires; any
            # other missing entry stays not-found and never widens the scope.
            if not found and (is_material_description(item) or (prose_scope and is_scope_prose(item))):
                if not allow_descriptions:
                    raise MaterialResolutionError("descriptive material scope requires explicit selection")
                descriptions.append(item)
                continue
            if not found:
                if lenient:
                    skipped.append("not_found")
                    explicit_skipped = True
                    continue
                raise MaterialResolutionError(f"declared material not found: {path.name}")
            if len(found) != 1 and not (_identical_materials(found) or identical_pdf_scopes(found)):
                if lenient:
                    skipped.append("ambiguous")
                    explicit_skipped = True
                    continue
                raise MaterialResolutionError(f"ambiguous declared material: {path.name}")
            value = found[0]
            if value.is_dir():
                explicit_paper_scope = True
                candidates.extend(accept(list_pdfs(value)))
            elif value.is_file() and value.suffix.lower() == ".pdf":
                explicit_paper_scope = True
                candidates.append(value)
            elif value.is_file():
                # It remains an authorized declared input, but this helper
                # only indexes PDFs and must not claim to have read the file.
                logging.getLogger(__name__).info("material_auxiliary_not_indexed suffix=%s", value.suffix.lower())
            else:
                raise MaterialResolutionError(f"declared material is not a regular file or directory: {path.name}")
        if descriptions:
            if explicit_skipped and not explicit_paper_scope:
                logging.getLogger(__name__).warning(
                    "material_description_scope descriptions=%d explicit_skipped=true no_scope_expansion=true",
                    len(descriptions))
            elif not explicit_paper_scope:
                candidates = discover_library(descriptions_only=True)
                logging.getLogger(__name__).warning(
                    "material_description_scope descriptions=%d candidates=%d requires_selection=true",
                    len(descriptions), len(candidates),
                )
            else:
                logging.getLogger(__name__).info(
                    "material_description_scope descriptions=%d explicit_pdfs=%d no_scope_expansion=true",
                    len(descriptions), len(candidates),
                )
    names: dict[str, Path] = {}
    unique: list[Path] = []
    for path in candidates:
        key = path.name.lower()
        if key in names:
            if names[key] != path and not _identical_materials([names[key], path]):
                if not lenient:
                    raise MaterialResolutionError(f"ambiguous PDF filename: {path.name}")
                # Answers name papers by file name only; keep the first copy.
                skipped.append("duplicate_name")
            continue
        names[key] = path
        unique.append(path)
    if skipped:
        logging.getLogger(__name__).warning("material_resolution_skipped %s", dict(Counter(skipped)))
        if not unique:
            raw = [materials] if isinstance(materials, str) else (materials if isinstance(materials, list) else [])
            raise MaterialResolutionError(f"no declared material could be resolved: {dict(Counter(skipped))}",
                                          skipped=dict(Counter(skipped)),
                                          shapes=dict(Counter(material_shape(value) for value in raw)))
    return unique
