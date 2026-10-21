"""Ask Gemini to turn page text into strict JSON. Code validates it afterwards (validate.py)."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date

from .config import PROMPT_FILE, Source

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 120_000
PAUSE_BETWEEN_CALLS = 5.0
MAX_TRIES = 3

_NULLABLE_STR = {"type": ["string", "null"]}
_DATE = {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD, or null"}

EDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "year": {"type": "string", "description": "Year the conference takes place, e.g. 2027"},
        "region": {**_NULLABLE_STR, "description": "Regional chapter name, only for multi-region sites"},
        "name": {"type": "string", "description": "The edition's name as the page gives it"},
        "start_date": _DATE,
        "end_date": _DATE,
        "location": _NULLABLE_STR,
        "format": {**_NULLABLE_STR, "description": "in-person, virtual or hybrid; null if not stated"},
        "cfp_deadline": _DATE,
        "cfp_deadline_time": {**_NULLABLE_STR, "description": "e.g. 11:59 PM ET, as stated"},
        "cfp_kind": {**_NULLABLE_STR,
                     "description": "presentation, abstract, paper, poster, proposal or other"},
        "cfp_note": {**_NULLABLE_STR, "description": "Other deadlines, or the old date of an extended deadline"},
        "cfp_url": _NULLABLE_STR,
        "info_url": _NULLABLE_STR,
        "evidence": {
            "type": "object",
            "properties": {
                "start_date": _NULLABLE_STR,
                "end_date": _NULLABLE_STR,
                "cfp_deadline": _NULLABLE_STR,
            },
            "required": ["start_date", "end_date", "cfp_deadline"],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["year", "region", "name", "start_date", "end_date", "location", "format",
                 "cfp_deadline", "cfp_deadline_time", "cfp_kind", "cfp_note", "cfp_url",
                 "info_url", "evidence", "confidence"],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"editions": {"type": "array", "items": EDITION_SCHEMA}},
    "required": ["editions"],
}


class ExtractError(Exception):
    """Gemini could not be used this run (no key, outage, quota, bad output)."""


def build_prompt(source: Source, today: date) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    return template.format(name=source.name, today=today.isoformat(),
                           hints=source.hints or "none")


class Extractor:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL")
        self._client = None
        self._last_call = 0.0
        self.calls = 0

    def available(self) -> str | None:
        """None if ready, otherwise the reason it isn't."""
        if not self.api_key:
            return "GEMINI_API_KEY is not set"
        if not self.model:
            return "GEMINI_MODEL is not set"
        return None

    def _client_or_raise(self):
        problem = self.available()
        if problem:
            raise ExtractError(problem)
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def extract(self, source: Source, page_text: str, today: date) -> list[dict]:
        from google.genai import errors, types

        client = self._client_or_raise()
        prompt = build_prompt(source, today)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
            temperature=0,
        )
        contents = [prompt, page_text[:MAX_INPUT_CHARS]]
        last_error = None
        for attempt in range(MAX_TRIES):
            wait = PAUSE_BETWEEN_CALLS - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                resp = client.models.generate_content(model=self.model, contents=contents,
                                                      config=config)
            except errors.APIError as e:
                last_error = f"Gemini HTTP {e.code}: {e.message}"
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_TRIES - 1:
                    backoff = 15 * (2 ** attempt)
                    log.warning("%s; retrying in %ss", last_error, backoff)
                    time.sleep(backoff)
                    continue
                raise ExtractError(last_error) from e
            except Exception as e:  # network errors etc.
                last_error = f"Gemini call failed: {type(e).__name__}: {e}"
                if attempt < MAX_TRIES - 1:
                    time.sleep(15 * (2 ** attempt))
                    continue
                raise ExtractError(last_error) from e
            try:
                data = json.loads(resp.text or "")
                editions = data["editions"]
                if not isinstance(editions, list):
                    raise TypeError("editions is not a list")
                return editions
            except (ValueError, KeyError, TypeError) as e:
                last_error = f"Gemini returned unusable JSON: {e}"
                log.warning(last_error)
        raise ExtractError(last_error or "Gemini failed")
