"""Source-clock date arithmetic reused conceptually from TraceWeave."""
from __future__ import annotations

import re
import calendar
from datetime import datetime, timedelta, timezone


RELATIVE_DAYS = {"today": 0, "yesterday": -1, "tomorrow": 1, "tonight": 0, "last night": -1,
                 "this morning": 0, "this afternoon": 0, "this evening": 0, "今天": 0, "昨天": -1, "明天": 1}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def message_time(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=timestamp)).isoformat()


def resolve_time(expression: str | None, text: str, anchor: str | None) -> str | None:
    """Resolve a source-bound date; unsupported or ambiguous expressions stay unknown."""
    if not expression:
        return None
    expression = expression.strip()
    base = parse_time(anchor) if anchor else None
    offsets = RELATIVE_DAYS
    if expression.casefold() in offsets and expression.casefold() in text.casefold() and base:
        day = (base + timedelta(days=offsets[expression.casefold()])).date()
        return day.isoformat() + "T00:00:00+00:00"
    try:
        iso = re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", expression)
        if iso:
            resolved = parse_time(expression)
            if expression in text:
                return resolved.isoformat()
            # A normalized value may be equivalent to a grounded relative or calendar date.
            for phrase in offsets:
                if phrase in text.casefold() and base:
                    candidate = resolve_time(phrase, text, anchor)
                    if candidate and parse_time(candidate).date() == resolved.date():
                        return candidate
            for match in re.finditer(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text):
                year = int(match[1]) if match[1] else base.year if base else None
                if year and (year, int(match[2]), int(match[3])) == (resolved.year, resolved.month, resolved.day):
                    return datetime(year, int(match[2]), int(match[3]), tzinfo=timezone.utc).isoformat()
            for date in calendar_dates(text, base):
                if date.date() == resolved.date():
                    return date.isoformat()
            return None
        match = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", expression)
        if match and expression in text:
            year = int(match[1]) if match[1] else base.year if base else None
            return datetime(year, int(match[2]), int(match[3]), tzinfo=timezone.utc).isoformat() if year else None
        if expression.casefold() in text.casefold():
            dates = list(calendar_dates(expression, base))
            if len(dates) == 1:
                return dates[0].isoformat()
    except (ValueError, OverflowError):
        return None
    return None


def calendar_dates(text, base):
    """Resolve explicit English month/day expressions using the same source-year policy."""
    names = {name.casefold(): number for number in range(1, 13)
             for name in (calendar.month_name[number], calendar.month_abbr[number])}
    names["sept"] = 9
    months = "|".join(sorted(names, key=len, reverse=True))
    patterns = [rf"\b(?P<month>{months})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?\b",
                rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{months})\.?(?:,?\s+(?P<year>\d{{4}}))?\b"]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            year = int(match["year"]) if match["year"] else base.year if base else None
            if year:
                try:
                    yield datetime(year, names[match["month"].casefold()], int(match["day"]), tzinfo=timezone.utc)
                except ValueError:
                    continue


def absolute_content(content: str, source_text: str, anchor: str | None) -> str:
    """Normalize supported relative day references in this single memory's content."""
    english = [re.escape(phrase) for phrase in RELATIVE_DAYS if phrase.isascii()]
    pattern = r"\b(?:" + "|".join(sorted(english, key=len, reverse=True)) + r")\b|今天|昨天|明天"
    def substitute(match):
        resolved = resolve_time(match[0], source_text, anchor)
        return resolved[:10] if resolved else match[0]
    return re.sub(pattern, substitute, content, flags=re.IGNORECASE)


def in_view(revision, as_of: str) -> bool:
    clock = parse_time(as_of)
    return (parse_time(revision.observed_at) <= clock and revision.status not in {"retracted", "cancelled"}
            and (not revision.valid_from or parse_time(revision.valid_from) <= clock)
            and (not revision.valid_to or clock < parse_time(revision.valid_to)))
