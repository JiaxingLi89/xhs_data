from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import SetupError


@dataclass
class Config:
    keywords: list = field(default_factory=lambda: ["母乳喂养"])
    sort: str = "general"
    timezone: str = "Asia/Shanghai"
    stage: str = "pilot"
    target_notes: int = 1500
    pilot_notes: int = 50
    reviewed_notes: int = 0
    daily_notes_pilot: int = 5
    daily_notes_formal: int = 10
    daily_requests_pilot: int = 300
    daily_requests_formal: int = 300
    quota_mode: str = "daily"
    continuous_daily: bool = False
    requests_per_run: int = 30
    rolling_requests_limit: int = 3000
    notes_per_run: int = 5
    request_interval_seconds: list = field(default_factory=lambda: [120, 300])
    comments_per_note: int | None = None
    max_comment_pages: int | None = None
    candidate_low_watermark: int = 20
    network_retry_seconds: int = 900
    login_wait_seconds: int = 600
    run_timeout_seconds: int = 14400
    browser_channel: str = "chrome"
    data_dir: str = "data"
    export_dir: str = "exports"
    total_research_path: str | None = None
    root: Path = field(default_factory=Path.cwd, repr=False)

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            allowed = {f.name for f in fields(cls)} - {"root"}
            if not isinstance(data, dict) or set(data) - allowed:
                raise ValueError("unknown config keys")
            obj = cls(**data, root=path.parent)
            obj.validate()
            return obj
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SetupError("配置文件无效，请检查 config.json 的字段和值。") from exc

    def validate(self):
        if type(self.continuous_daily) is not bool or (self.continuous_daily and self.quota_mode != "daily"):
            raise ValueError("continuous_daily requires daily quota mode")
        if self.quota_mode not in {"daily", "rolling"}:
            raise ValueError("quota_mode")
        if self.stage not in {"pilot", "formal"}:
            raise ValueError("stage")
        if self.sort not in {"general", "time_descending", "popularity_descending"}:
            raise ValueError("sort")
        if not isinstance(self.keywords, list) or not self.keywords or any(
            not isinstance(k, str) or not k.strip() or len(k) > 200 for k in self.keywords
        ):
            raise ValueError("keywords")
        self.keywords = list(dict.fromkeys(k.strip() for k in self.keywords))
        ZoneInfo(self.timezone)
        for name in ["target_notes", "pilot_notes", "daily_notes_pilot", "daily_notes_formal",
                     "daily_requests_pilot", "daily_requests_formal", "notes_per_run",
                     "requests_per_run", "rolling_requests_limit",
                     "comments_per_note", "max_comment_pages", "candidate_low_watermark",
                     "network_retry_seconds", "login_wait_seconds", "run_timeout_seconds"]:
            value = getattr(self, name)
            if name in {"daily_notes_pilot", "daily_notes_formal", "comments_per_note", "max_comment_pages"} and value is None:
                continue
            if type(value) is not int or value < 1:
                raise ValueError(name)
        if type(self.reviewed_notes) is not int or self.reviewed_notes < 0:
            raise ValueError("reviewed_notes")
        bounds = self.request_interval_seconds
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int for v in bounds):
            raise ValueError("interval")
        if not 120 <= bounds[0] <= bounds[1] or self.network_retry_seconds < 900:
            raise ValueError("minimum delay")
        if self.browser_channel not in {"chrome", "chromium", "msedge"}:
            raise ValueError("browser_channel")
        for name in ["data_dir", "export_dir"]:
            value = getattr(self, name)
            if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError(name)
        if self.total_research_path is not None:
            value = self.total_research_path
            if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("total_research_path")
        if self.state_dir == self.root or self.output_dir == self.root or self.state_dir == self.output_dir:
            raise ValueError("data/export directories must be separate")

    @property
    def state_dir(self):
        return (self.root / self.data_dir).resolve()

    @property
    def output_dir(self):
        return (self.root / self.export_dir).resolve()

    @property
    def total_research_file(self):
        return (self.root / self.total_research_path).resolve() if self.total_research_path else None

    @property
    def daily_notes(self):
        return self.daily_notes_pilot if self.stage == "pilot" else self.daily_notes_formal

    @property
    def daily_requests(self):
        return self.daily_requests_pilot if self.stage == "pilot" else self.daily_requests_formal

    @property
    def collection_limit(self):
        return min(self.target_notes, self.pilot_notes) if self.stage == "pilot" else self.target_notes

    @property
    def sampling_version(self):
        data = json.dumps([self.keywords, self.sort, self.comments_per_note, self.max_comment_pages], ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

