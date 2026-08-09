"""Explainable relevance ranking for repository files."""

import re
from dataclasses import dataclass, field

from .index import RepositoryIndex


@dataclass(slots=True)
class CandidateFile:
    path: str
    score: int
    reasons: list[str] = field(default_factory=list)


class ContextSelector:
    """Rank files using paths, symbols, content and file roles."""

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index

    def select(self, query: str, limit: int = 10) -> list[CandidateFile]:
        terms = {term for term in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query.lower()) if len(term) > 1}
        candidates: list[CandidateFile] = []
        candidate_by_path: dict[str, CandidateFile] = {}
        for path in self.index.files:
            lower_path = path.lower()
            lower_text = self.index.file_texts.get(path, "").lower()
            symbols = {
                symbol.name.lower()
                for symbol in self.index.python_modules.get(path, []).symbols
            } if path in self.index.python_modules else set()
            score = 0
            reasons: list[str] = []
            for term in sorted(terms):
                if term in lower_path:
                    score += 5
                    reasons.append(f"path contains '{term}'")
                if term in symbols:
                    score += 6
                    reasons.append(f"symbol matches '{term}'")
                if term in lower_text:
                    score += 2
                    reasons.append(f"content contains '{term}'")
            if "test" in terms and ("test" in lower_path or "/tests/" in f"/{lower_path}/"):
                score += 4
                reasons.append("test file role")
            if "readme" in terms and lower_path.endswith("readme.md"):
                score += 4
                reasons.append("README role")
            if score:
                candidate = CandidateFile(path=path, score=score, reasons=reasons)
                candidates.append(candidate)
                candidate_by_path[path] = candidate

        for source, targets in self.index.import_graph().items():
            source_candidate = candidate_by_path.get(source)
            if source_candidate is None:
                continue
            for target in targets:
                candidate = candidate_by_path.get(target)
                if candidate is None:
                    candidate = CandidateFile(path=target, score=0, reasons=[])
                    candidates.append(candidate)
                    candidate_by_path[target] = candidate
                candidate.score += 1
                candidate.reasons.append(f"imported by '{source}'")
        return sorted(candidates, key=lambda item: (-item.score, item.path))[:limit]
