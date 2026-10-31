"""Small text and date helpers shared across Ghosted."""
import re
from datetime import datetime, timezone

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def truncate(text, limit):
    """Cut text to at most `limit` characters, adding an ellipsis when something was cut."""
    text = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clean_markdown(text, limit):
    """Drop HTML comments (issue templates are full of them), tidy whitespace, truncate."""
    if not text:
        return ""
    out = _HTML_COMMENT.sub("", str(text))
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _SPACES.sub(" ", out)
    out = _BLANK_LINES.sub("\n\n", out).strip()
    return truncate(out, limit)


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def one_line(text, limit):
    """Collapse all whitespace to single spaces, then truncate."""
    return truncate(" ".join(str(text or "").split()), limit)


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """Parse an ISO-8601 timestamp (GitHub style, 'Z' suffix) into an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def day_str(value):
    parsed = parse_ts(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def age_in_days(value, now=None):
    parsed = parse_ts(value)
    if parsed is None:
        return 0
    return max(0, ((now or now_utc()) - parsed).days)


def span_phrase(days):
    """Prose for a time span, e.g. 'about 3 years and 2 months'."""
    days = int(days)
    if days < 1:
        return "less than a day"
    if days < 45:
        return "%d day%s" % (days, "" if days == 1 else "s")
    months = int(round(days / 30.44))
    if months < 12:
        return "about %d month%s" % (months, "" if months == 1 else "s")
    years, rest = divmod(months, 12)
    text = "%d year%s" % (years, "" if years == 1 else "s")
    if rest:
        text += " and %d month%s" % (rest, "" if rest == 1 else "s")
    return "about " + text


def span_short(days):
    """Compact time span for badges, e.g. '3y 2mo'."""
    days = int(days)
    if days < 31:
        return "%dd" % days
    months = max(1, int(round(days / 30.44)))
    if months < 12:
        return "%dmo" % months
    years, rest = divmod(months, 12)
    return "%dy %dmo" % (years, rest) if rest else "%dy" % years
