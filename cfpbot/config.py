"""Load sources.yaml and the settings that go with it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.yaml"
PROMPT_FILE = ROOT / "prompts" / "extract.txt"
STATE_FILE = ROOT / "data" / "state.json"
PAGES_DIR = ROOT / "data" / "pages"
DOCS_DIR = ROOT / "docs"
FIXTURES_DIR = ROOT / "tests" / "fixtures"

DEFAULT_SETTINGS = {
    "feed_url": "",
    "calendar_name": "Conferences & CFPs",
    "reminder_days": [30, 7],
    "keep_after_end_days": 365,
    "failure_alert_after": 3,
}


@dataclass
class Source:
    id: str
    name: str
    short: str
    urls: list[str]
    hints: str = ""
    regions: list[str] = field(default_factory=list)
    link_keywords: list[str] | None = None
    max_links: int = 5
    manual: list[dict] = field(default_factory=list)


@dataclass
class Config:
    settings: dict
    sources: list[Source]


def load_config(path: Path = SOURCES_FILE) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw.get("sources") or []:
        sid = str(entry["id"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", sid):
            raise ValueError(f"sources.yaml: id {sid!r} must be lowercase letters, digits and dashes")
        if sid in seen:
            raise ValueError(f"sources.yaml: id {sid!r} is listed twice")
        seen.add(sid)
        urls = entry.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        manual = entry.get("manual") or []
        if not urls and not manual:
            raise ValueError(f"sources.yaml: {sid} needs at least one url or a manual: block")
        sources.append(Source(
            id=sid,
            name=entry["name"],
            short=entry.get("short") or entry["name"],
            urls=list(urls),
            hints=(entry.get("hints") or "").strip(),
            regions=list(entry.get("regions") or []),
            link_keywords=entry.get("link_keywords"),
            max_links=int(entry.get("max_links", 5)),
            manual=list(manual),
        ))
    return Config(settings=settings, sources=sources)
