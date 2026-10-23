"""Knowledge base for the project assistant: markdown docs split into sections + TF-IDF search.

Sources (missing files are skipped, so the API container only needs what it copies):
* app/assistant/faq.md      - curated answers, one per `##` heading (preferred on ties)
* README.md, docs/DEPLOY.md - project and deployment docs
* data/risk/EVALUATION.md   - real risk-model metrics
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from app.config import PROJECT_ROOT

FAQ_PATH = Path(__file__).with_name("faq.md")
DOC_SOURCES: list[tuple[str, Path]] = [
    ("FAQ", FAQ_PATH),
    ("README", PROJECT_ROOT / "README.md"),
    ("Deployment guide", PROJECT_ROOT / "docs" / "DEPLOY.md"),
    ("Risk model evaluation", PROJECT_ROOT / "data" / "risk" / "EVALUATION.md"),
]
FAQ_BOOST = 1.25  # curated answers win close calls against raw doc sections
HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)
STOP_WORDS = ENGLISH_STOP_WORDS | {"does", "did", "doing", "using", "ok"}
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
# Question filler that says nothing about the topic; ignored when checking coverage.
FILLER = {"explain", "tell", "describe", "show", "mean", "work", "happen", "use", "used", "need",
          "know", "want", "like", "please", "thing", "way", "ok"}
SUFFIXES = ("ations", "ation", "ings", "ing", "ies", "ied", "es", "ed", "ly", "s")


def _stem(word: str) -> str:
    """Tiny suffix stripper so 'approve/approving/approved' and 'test/tests' share a term."""
    for suf in SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            word = word[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
            break
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def analyze(text: str) -> list[str]:
    """Unigrams + bigrams of stemmed, non-stop-word tokens. Digits are kept ('9 tests', 'p85')."""
    words = [_stem(w) for w in TOKEN_RE.findall(text.lower()) if w not in STOP_WORDS]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


@dataclass(frozen=True)
class Section:
    source: str
    title: str
    body: str

    @property
    def label(self) -> str:
        return self.title if self.source == "FAQ" else f"{self.source} › {self.title}"


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float


def split_sections(source: str, text: str) -> list[Section]:
    """One section per `##`/`###` heading; text before the first heading is dropped (titles, intros)."""
    matches = list(HEADING_RE.finditer(text))
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            sections.append(Section(source, m.group(2).strip("` "), body))
    return sections


class KnowledgeBase:
    def __init__(self, sections: list[Section]) -> None:
        if not sections:
            raise ValueError("knowledge base is empty")
        self.sections = sections

    @classmethod
    def load(cls, sources: list[tuple[str, Path]] | None = None) -> KnowledgeBase:
        sections: list[Section] = []
        for name, path in sources or DOC_SOURCES:
            if path.exists():
                sections += split_sections(name, path.read_text(encoding="utf-8"))
        return cls(sections)

    @cached_property
    def _index(self):
        # Titles are repeated so a question that matches a heading ranks that section first.
        docs = [f"{s.title} {s.title} {s.body}" for s in self.sections]
        vec = TfidfVectorizer(analyzer=analyze, sublinear_tf=True)
        return vec, vec.fit_transform(docs)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        vec, matrix = self._index
        scores = linear_kernel(vec.transform([query]), matrix).ravel()
        boosted = [(s * (FAQ_BOOST if sec.source == "FAQ" else 1.0), sec)
                   for s, sec in zip(scores, self.sections)]
        boosted.sort(key=lambda t: t[0], reverse=True)
        return [Hit(sec, round(float(s), 4)) for s, sec in boosted[:k] if s > 0]

    def coverage(self, query: str, section: Section) -> float:
        """Share of the question's topic words that appear in the section (0-1)."""
        terms = {t for t in analyze(query) if " " not in t and t not in FILLER}
        if not terms:
            return 0.0
        present = set(analyze(f"{section.title} {section.body}"))
        return len(terms & present) / len(terms)

    def render(self) -> str:
        """The whole knowledge base as one document (the LLM's cached grounding context)."""
        return "\n\n".join(f"### {s.label}\n{s.body}" for s in self.sections)
