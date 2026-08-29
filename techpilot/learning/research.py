"""Evidence-first source reading and local text ingestion for S1-C.

Network access is deliberately performed only by the Runtime-owned
``research_url`` Tool.  This module is also usable with fake connectors in
tests, but it never turns a failed fetch into a source, trend, or task.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from .contracts import DocumentRecord, ExtractedText, LearningGoal, LearningPlan, LearningTask, SourceRecord, TrendBrief
from .store import LearningStore

_MAX_SOURCE_BYTES = 1_000_000
_MAX_EXTRACTED_CHARACTERS = 100_000
_TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".rst", ".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h",
    ".cpp", ".cs", ".rb", ".php", ".sh", ".ps1", ".html", ".htm", ".json", ".csv", ".yaml", ".yml", ".toml",
})
_UNSUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".gif"})


class ResearchError(ValueError):
    """A source could not be read without inventing a replacement fact."""


@dataclass(frozen=True)
class FetchedSource:
    uri: str
    title: str
    text: str
    content_type: str | None = None
    version: str | None = None
    published_at: str | None = None
    uncertainty: str | None = None
    source_type: str = "web"


@dataclass(frozen=True)
class ResearchResult:
    source: SourceRecord | None
    task: LearningTask | None
    trend: TrendBrief | None
    updated: bool = False
    document: DocumentRecord | None = None
    extracted: ExtractedText | None = None

    @property
    def message(self) -> str:
        if self.document is not None and self.document.extraction_status == "unsupported":
            return f"暂不支持提取 {self.document.filename}；S1 不读取扫描件、图片或 Office/PDF 二进制文档。"
        if self.source is None:
            return "资料没有成功读取，因此没有创建来源、趋势或学习任务。"
        lines = [f"已记录资料：{self.source.title}", f"来源：{self.source.uri}"]
        if self.source.version:
            lines.append(f"版本/发布日期：{self.source.version}")
        if self.task is not None:
            lines.append(f"已加入学习任务：{self.task.title}")
        if self.updated:
            lines.append("此来源内容已更新，已覆盖同一链接的旧记录。")
        if self.source.uncertainty:
            lines.append(f"注意：{self.source.uncertainty}")
        return "\n".join(lines)


class ResearchConnector(Protocol):
    def read(self, uri: str, *, timeout: int = 15) -> FetchedSource:
        """Return successfully retrieved source text or raise ``ResearchError``."""


class UrlResearchConnector:
    """Read a public HTTP(S) document with bounded, dependency-free parsing."""

    def read(self, uri: str, *, timeout: int = 15) -> FetchedSource:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResearchError("只支持可公开读取的 http(s) 来源。")
        if parsed.netloc.casefold() in {"github.com", "www.github.com"} and _github_repository_path(parsed.path):
            return GitHubPublicConnector().read(uri, timeout=timeout)
        return self._read_http(uri, timeout=timeout)

    @staticmethod
    def _read_http(uri: str, *, timeout: int) -> FetchedSource:
        request = urllib.request.Request(uri, headers={"User-Agent": "TechPilot/0.1 source-reader"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_SOURCE_BYTES + 1)
                content_type = response.headers.get_content_type() or None
                charset = response.headers.get_content_charset() or "utf-8"
                final_uri = response.geturl()
        except urllib.error.HTTPError as error:
            raise ResearchError(f"来源返回 HTTP {error.code}，未创建学习资料。") from error
        except (OSError, ValueError) as error:
            raise ResearchError(f"无法读取来源：{error}") from error
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ResearchError("来源超过 1 MB 读取上限，未创建学习资料。")
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        title, text, published_at = _extract_web_text(body, content_type)
        if not text.strip():
            raise ResearchError("来源没有可提取的文字内容，未创建学习资料。")
        return FetchedSource(
            uri=final_uri,
            title=title or _title_from_uri(final_uri),
            text=_limit_text(text),
            content_type=content_type,
            published_at=published_at,
            uncertainty=(
                "未从页面元数据识别发布日期；请以来源页面为准。"
                if published_at is None
                else None
            ),
        )


class GitHubPublicConnector:
    """Read public repository metadata and README without a GitHub credential."""

    def read(self, uri: str, *, timeout: int = 15) -> FetchedSource:
        parsed = urllib.parse.urlparse(uri)
        owner_repo = _github_repository_path(parsed.path)
        if owner_repo is None:
            raise ResearchError("请提供公开 GitHub 仓库链接。")
        owner, repository = owner_repo
        api_uri = f"https://api.github.com/repos/{owner}/{repository}"
        request = urllib.request.Request(api_uri, headers={"Accept": "application/vnd.github+json", "User-Agent": "TechPilot/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(_MAX_SOURCE_BYTES).decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ResearchError(f"无法读取公开 GitHub 仓库：{error}") from error
        if not isinstance(payload, dict):
            raise ResearchError("GitHub 返回了无效仓库资料。")
        branch = payload.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ResearchError("GitHub 仓库没有可读取的默认分支。")
        readme_uri = f"https://raw.githubusercontent.com/{owner}/{repository}/{branch}/README.md"
        try:
            readme = UrlResearchConnector._read_http(readme_uri, timeout=timeout)
            text = readme.text
        except ResearchError:
            text = str(payload.get("description") or "该仓库没有可读取的 README。")
        version = payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None
        title = str(payload.get("full_name") or f"{owner}/{repository}")
        description = str(payload.get("description") or "").strip()
        combined = f"{description}\n\n{text}".strip()
        return FetchedSource(
            uri=f"https://github.com/{owner}/{repository}",
            title=title,
            text=_limit_text(combined),
            content_type="text/markdown",
            version=version,
            published_at=version,
            source_type="github",
            uncertainty="读取的是公开仓库元数据与默认分支 README；Release 需单独指定其链接。",
        )


class DocumentIngestion:
    """Extract user-selected, locally readable text; never claim OCR support."""

    def ingest(self, path: Path, *, goal: LearningGoal | None, store: LearningStore) -> ResearchResult:
        resolved = path.expanduser().resolve()
        suffix = resolved.suffix.casefold()
        if suffix in _UNSUPPORTED_SUFFIXES or suffix not in _TEXT_SUFFIXES:
            record = DocumentRecord(
                filename=resolved.name,
                mime_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                content_hash="unsupported",
                goal_id=goal.id if goal is not None else None,
                extraction_status="unsupported",
            )
            store.save_record(record)
            return ResearchResult(None, None, None, document=record)
        try:
            raw = resolved.read_bytes()
        except OSError as error:
            raise ResearchError(f"无法读取本地资料：{error}") from error
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ResearchError("本地资料超过 1 MB 读取上限，未创建学习资料。")
        text = _decode_local_text(raw, suffix)
        if not text.strip():
            raise ResearchError("本地资料没有可提取的文字内容，未创建学习资料。")
        digest = hashlib.sha256(raw).hexdigest()
        record = DocumentRecord(
            filename=resolved.name,
            mime_type=mimetypes.guess_type(resolved.name)[0] or "text/plain",
            content_hash=digest,
            goal_id=goal.id if goal is not None else None,
            extraction_status="extracted",
        )
        extract = ExtractedText(document_id=record.id, text=_limit_text(text), locator=str(resolved))
        store.save_record(record)
        store.save_record(extract)
        fetched = FetchedSource(
            uri=resolved.as_uri(),
            title=resolved.name,
            text=extract.text,
            content_type=record.mime_type,
            source_type="document",
            uncertainty="仅提取原生文字；复杂排版、图片和扫描内容未解析。",
        )
        outcome = ResearchService(store=store).record_fetched(fetched, goal=goal)
        return ResearchResult(
            outcome.source,
            outcome.task,
            outcome.trend,
            outcome.updated,
            document=record,
            extracted=extract,
        )


class ResearchService:
    """Persist retrieved evidence and derive conservative, recoverable study work."""

    def __init__(self, store: LearningStore | None = None, connector: ResearchConnector | None = None) -> None:
        self.store = store or LearningStore()
        self.connector = connector or UrlResearchConnector()

    def active_goal(self) -> LearningGoal | None:
        active = [goal for goal in self.store.list_goals() if goal.status == "active"]
        if len(active) > 1:
            raise ResearchError("存在多条进行中的学习路径，无法确定资料应保存到哪里。")
        return active[0] if active else None

    def research_url(self, uri: str, *, timeout: int = 15) -> ResearchResult:
        goal = self.active_goal()
        if goal is None:
            raise ResearchError("请先开始一条学习路径，再把资料加入学习计划。")
        return self.record_fetched(self.connector.read(uri, timeout=timeout), goal=goal)

    def ingest_document(self, path: str | Path) -> ResearchResult:
        return DocumentIngestion().ingest(Path(path), goal=self.active_goal(), store=self.store)

    def record_fetched(self, fetched: FetchedSource, *, goal: LearningGoal | None) -> ResearchResult:
        if goal is None:
            raise ResearchError("资料必须关联到一条已确认的学习路径。")
        content_hash = hashlib.sha256(fetched.text.encode("utf-8")).hexdigest()
        existing = next(
            (source for source in self.store.list_records(SourceRecord) if source.goal_id == goal.id and source.uri == fetched.uri),
            None,
        )
        source_payload: dict[str, object] = {
            "uri": fetched.uri,
            "title": fetched.title,
            "goal_id": goal.id,
            "source_type": fetched.source_type if fetched.source_type in {"web", "github", "document"} else "web",
            "content_type": fetched.content_type,
            "content_hash": content_hash,
            "summary": _summary(fetched.text),
            "version": fetched.version,
            "published_at": fetched.published_at,
            "uncertainty": fetched.uncertainty,
        }
        if existing is not None:
            source_payload["id"] = existing.id
        source = SourceRecord(**source_payload)
        changed = existing is None or existing.content_hash != content_hash
        self.store.save_record(source)
        task = self._ensure_task(goal, source)
        trend = self._ensure_trend(goal, source)
        return ResearchResult(source, task, trend, updated=existing is not None and changed)

    def _ensure_task(self, goal: LearningGoal, source: SourceRecord) -> LearningTask | None:
        plan = self._plan_for(goal)
        if plan is None or not plan.steps:
            return None
        existing = next(
            (task for task in self.store.list_records(LearningTask) if task.plan_id == plan.id and source.id in task.source_ids),
            None,
        )
        if existing is not None:
            return existing
        step = next((item for item in plan.steps if item.status in {"active", "pending"}), plan.steps[0])
        task = LearningTask(
            plan_id=plan.id,
            step_id=step.id,
            title=f"阅读并整理：{source.title}",
            status="active" if step.status == "active" else "pending",
            estimated_minutes=20,
            source_ids=(source.id,),
            practice="用自己的话写出三个关键概念和一个适用场景。",
            acceptance_criteria=("能引用该来源说明一个关键概念或版本变化。",),
        )
        self.store.save_record(task)
        return task

    def _ensure_trend(self, goal: LearningGoal, source: SourceRecord) -> TrendBrief | None:
        if source.version is None and source.published_at is None:
            return None
        existing = next(
            (trend for trend in self.store.list_records(TrendBrief) if trend.goal_id == goal.id and source.id in trend.source_ids),
            None,
        )
        if existing is not None:
            return existing
        trend = TrendBrief(
            goal_id=goal.id,
            category="watchlist",
            title=f"持续关注：{source.title}",
            source_ids=(source.id,),
            rationale="该来源包含版本或更新时间信息；尚未将其自动判定为必须学习内容。",
            valid_as_of=source.version or source.published_at,
            skip_if="当前学习目标不使用该项目或版本。",
        )
        self.store.save_record(trend)
        return trend

    def _plan_for(self, goal: LearningGoal) -> LearningPlan | None:
        plans = [plan for plan in self.store.list_records(LearningPlan) if plan.goal_id == goal.id]
        return plans[-1] if plans else None


class _ReadableHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.published_at: str | None = None
        self.parts: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attributes.get("property") in {"article:published_time", "og:updated_time"}:
            self.published_at = attributes.get("content") or self.published_at

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)


def _extract_web_text(body: str, content_type: str | None) -> tuple[str, str, str | None]:
    if content_type in {"application/json", "text/json"}:
        try:
            parsed = json.loads(body)
            return "JSON source", json.dumps(parsed, ensure_ascii=False, indent=2), None
        except json.JSONDecodeError:
            return "JSON source", body, None
    if content_type == "text/csv":
        rows = list(csv.reader(body.splitlines()))
        return "CSV source", "\n".join(" | ".join(row) for row in rows), None
    if content_type in {"text/html", "application/xhtml+xml"} or "<html" in body.casefold():
        parser = _ReadableHtml()
        parser.feed(body)
        return html.unescape(parser.title).strip(), _normalize_text(" ".join(parser.parts)), parser.published_at
    return "", body, None


def _decode_local_text(raw: bytes, suffix: str) -> str:
    text = raw.decode("utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        _title, text, _published_at = _extract_web_text(text, "text/html")
    elif suffix == ".csv":
        text = _extract_web_text(text, "text/csv")[1]
    return _limit_text(text)


def _github_repository_path(path: str) -> tuple[str, str] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repository = parts[:2]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        return None
    return owner, repository.removesuffix(".git")


def _title_from_uri(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri)
    return parsed.netloc + parsed.path or uri


def _summary(text: str) -> str:
    return _normalize_text(text)[:1_000]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _limit_text(text: str) -> str:
    return text[:_MAX_EXTRACTED_CHARACTERS]
