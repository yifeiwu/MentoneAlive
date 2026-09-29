"""Infer concrete dates for dateless events from their description text.

Every event needs a real date before it can be placed on a calendar. Several
sources publish recurring programs without per-occurrence dates: CCC term
classes state "Wednesdays. 2:00pm - 3:30pm", archived council listings state
"on the fourth Saturday of every month". This module parses that prose into a
recurrence spec and expands it into concrete dated occurrences.

Unparseable listings are reported as unresolvable and dropped by the caller.
Inference never overrides a date a source actually supplied.
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta

MAX_OCCURRENCES = 12
WEEKLY_HORIZON_DAYS = 126
FORTNIGHTLY_HORIZON_DAYS = 252
MONTHLY_HORIZON_MONTHS = 20

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday")
DAY_ALT = "|".join(d.capitalize() for d in DAYS)
DAY_IDX = {d: i for i, d in enumerate(DAYS)}
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")
MONTH_ALT = "|".join(m.capitalize() for m in MONTHS) + \
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
MONTH_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}
for _abbr, _i in (("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("jun", 6),
                   ("jul", 7), ("aug", 8), ("sept", 9), ("sep", 9), ("oct", 10),
                   ("nov", 11), ("dec", 12)):
    MONTH_IDX[_abbr] = _i

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}

# A four-digit year must never be read as a time-of-day.
_FOUR_DIGIT_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Language that marks a listing as genuinely ongoing. Required before a bare
# weekday in a title is trusted as a recurring pattern.
_ONGOING_HINT_RE = re.compile(
    r"\bevery\s+\w+day\b|\beach\s+\w+day\b|\bweekly\b|\bfortnightly\b"
    r"|\bterm\b|\bongoing\b|\brecurring\b|\bevery\s+week\b|\bevery\s+month\b"
    r"|\b\d+\s*weeks?\b|\bclasses?\s+each\b|\bsessions?\s+each\b", re.I)

_DAY_RE = rf"({DAY_ALT})"
_DAY_NC = rf"(?:{DAY_ALT})"
# "noon" and "midday" are written instead of "12pm", and a bare "12" means
# twelve o'clock. Without these, "Thursday 12noon - 1.30pm" matched no time
# range at all, so the whole schedule read as a bare weekday and the venue
# was dropped as undatable.
_NOON_ALT = r"noon|midday|mid-day"
# Note: a bare "12" is deliberately NOT a token. Schedule text is full of
# bare numbers ("Monday 28 September", "5:30pm") and matching digits alone
# turns day-of-month into an hour. A bare hour only appears as one end of a
# range, which _TIME_RANGE_RE handles via the other alternatives.
# Every alternative is anchored with \b so "afternoon" is not read as "noon".
_TIME_TOKEN = (
    r"(?:"
    r"\d{1,2}[:.]\d{2}\s*[ap]\.?\s*m\.?"
    # "12noon" / "12 midday" is one time, not a bare hour plus a word.
    rf"|\b\d{{1,2}}\s*(?:{_NOON_ALT})\b"
    rf"|\b(?:{_NOON_ALT})\b"
    r"|\b\d{1,2}\s*[ap]\.?\s*m\.?"
    r"|\b\d{1,2}[:.]\d{2}\b"
    r")")
_TIME_RANGE_RE = re.compile(
    rf"({_TIME_TOKEN})\s*(?:-|–|—|to|until|till)\s*({_TIME_TOKEN})", re.I)
# "12 - 1.30pm": a bare hour is only meaningful as one end of a range, so it
# gets its own pattern rather than joining _TIME_TOKEN (where it would also
# match day numbers in dates).
_BARE_HOUR_RANGE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2}[:.]\d{2}\s*[ap]\.?\s*m\.?)", re.I)
_TIME_POINT_RE = re.compile(_TIME_TOKEN, re.I)

_MONTHLY_RE = re.compile(
    rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+"
    rf"{_DAY_RE}s?\s+(?:of|each|in|every)\s+"
    rf"(?:(?:the|every|each)\s+)?(?:month|week)", re.I)
_FORTNIGHTLY_RE = re.compile(
    rf"\bevery\s+(?:second|2nd|alternating|other)\s+({DAY_ALT})s?\b", re.I)
_WEEKEND_RE = re.compile(r"\bweekends?\b", re.I)

_DAY_SPAN_RE = re.compile(
    rf"\b{_DAY_RE}\s*(?:s\.\s*)?(?:to|through|thru|[-–])\s*{_DAY_RE}\b", re.I)
_DAY_TOKEN_RE = re.compile(rf"\b{_DAY_RE}s?\b", re.I)

_DATE_RANGE_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALT})\.?\s*"
    rf"(?:-|–|—|to|until)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALT})\b"
    rf"(?:\s*,?\s*(\d{{4}}))?", re.I)
_MONTH_WINDOW_RE = re.compile(
    rf"\bfrom\s+({MONTH_ALT})\s+to\s+({MONTH_ALT})\b", re.I)
_DMY_RE = re.compile(
    rf"\b(?:{_DAY_NC}[,\s]+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALT})\.?\s*,?\s*"
    rf"(\d{{4}})\b", re.I)
_MDY_RE = re.compile(
    rf"\b(?:{_DAY_NC}[,\s]+)?({MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*"
    rf"(\d{{4}})\b", re.I)
_WEEKS_RE = re.compile(r"\b(\d{1,2})\s*weeks?\b|\bweeks?\s*:?\s*(\d{1,2})\b",
                       re.I)


def _default_today():
    """Today, overridable via SOURCE_DATE_EPOCH for reproducible runs.

    Every expansion is anchored on this, so two runs on different days
    otherwise shift whole inferred series forward.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return date.fromtimestamp(int(epoch))
        except (ValueError, OverflowError, OSError):
            pass
    return date.today()


def _to_hhmm(token):
    t = (token or "").strip()
    if not t:
        return None
    # "noon" / "12noon" / "midday" are 12:00; "midnight" is 00:00.
    m = re.fullmatch(r"(\d{1,2})?\s*(noon|midday|mid-day)", t, re.I)
    if m:
        return "12:00"
    if re.fullmatch(r"midnight", t, re.I):
        return "00:00"
    m = re.match(r"(\d{1,2})[:.](\d{2})\s*([ap])\.?\s*m", t, re.I)
    if m:
        hour, minute, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    else:
        m = re.match(r"(\d{1,2})\s*([ap])\.?\s*m", t, re.I)
        if m:
            hour, minute, ap = int(m.group(1)), 0, m.group(2).lower()
        else:
            # 24-hour form, e.g. "14:00" or "18.30".
            m = re.match(r"(\d{1,2})[:.](\d{2})\s*$", t)
            if m:
                hour, minute, ap = int(m.group(1)), int(m.group(2)), None
            else:
                # Bare hour ("12" in "12 - 1.30pm"): twelve o'clock. Reject
                # 4-digit years, which would otherwise read as 20:26.
                m = re.fullmatch(r"(\d{1,2})", t)
                if not m or _FOUR_DIGIT_YEAR_RE.match(t):
                    return None
                hour, minute, ap = int(m.group(1)), 0, None
    if ap == "p":
        # 12pm is noon, not midnight. Adding 12 to 12 would make 24, which
        # `hour %= 24` silently wraps round to 00:00.
        if 1 <= hour < 12:
            hour += 12
    elif ap == "a" and hour == 12:
        hour = 0
    hour %= 24
    minute = min(max(minute, 0), 59)
    return f"{hour:02d}:{minute:02d}"


def _time_ranges(text):
    """(start_pos, start_hhmm, end_hhmm) for each explicit time range.

    Only ranges, not lone times: _weekday_slots pairs those separately.
    """
    out = []
    for m in _TIME_RANGE_RE.finditer(text):
        start, end = _to_hhmm(m.group(1)), _to_hhmm(m.group(2))
        if start:
            out.append((m.start(), m.end(), start, end or start))
    # Bare-hour start ("12 - 1.30pm"), tried second so the explicit forms win.
    for m in _BARE_HOUR_RANGE_RE.finditer(text):
        start, end = _to_hhmm(m.group(1)), _to_hhmm(m.group(2))
        if start and not any(s <= m.start() < e for s, e, _, _ in out):
            out.append((m.start(), m.end(), start, end or start))
    out.sort(key=lambda t: t[0])
    return out


def _first_time_range(text):
    """Start/end of the first session time. Falls back to a lone time point
    ('on the 1st Wednesday of every month at 1:15pm')."""
    ranges = _time_ranges(text)
    if ranges:
        return ranges[0][2], ranges[0][3]
    m = _TIME_POINT_RE.search(text or "")
    if m:
        single = _to_hhmm(m.group(0))
        if single:
            return single, single
    return None, None


def _weekday_tokens(text):
    """Weekday mentions in reading order; ranges like 'Monday to Friday' are
    returned as one token holding the whole span."""
    spans, tokens = [], []
    for m in _DAY_SPAN_RE.finditer(text):
        a = DAY_IDX[m.group(1).lower()]
        b = DAY_IDX[m.group(2).lower()]
        # "Friday to Monday" wraps the week-end. range(min, max) would turn
        # that into the whole Mon-Fri working week, inventing 3 extra days.
        if a <= b:
            days = set(range(a, b + 1))
        else:
            days = set(range(a, 7)) | set(range(0, b + 1))
        tokens.append((m.start(), days))
        spans.append((m.start(), m.end()))
    for m in _DAY_TOKEN_RE.finditer(text):
        if any(s < m.end() and m.start() < e for s, e in spans):
            continue
        tokens.append((m.start(), {DAY_IDX[m.group(1).lower()]}))
        spans.append((m.start(), m.end()))
    tokens.sort()
    return tokens


def weekday_slots(text):
    """Pair weekday mentions with the times that follow them.

    Weekdays seen since the last time range all share that time range, so
    'Mondays and Thursdays 9am - 12pm' puts both on 9am while
    'Mondays 9am - 12pm | Fridays 1pm - 2pm' assigns one time each.

    A weekday with no time and no recurrence marker is a single dated
    occurrence, not a pattern: "Friday 2 October, 11:00am" is one event, and
    treating it as weekly would fabricate 12 of them.
    """
    events = [(pos, "day", days) for pos, days in _weekday_tokens(text)]
    ranges = _time_ranges(text)
    events += [(pos, "time", (start, end))
               for pos, _end_pos, start, end in ranges]
    # A lone time with no range ("Every Thursday 11.30am") still belongs to the
    # weekdays that precede it. Without this the slot stayed untimed and the
    # occurrence was published at midnight.
    for m in _TIME_POINT_RE.finditer(text or ""):
        single = _to_hhmm(m.group(0))
        if not single:
            continue
        # Skip any time already consumed as part of a range.
        if any(start <= m.start() < end for start, end, _, _ in ranges):
            continue
        events.append((m.start(), "time", (single, single)))
    events.sort(key=lambda ev: ev[0])
    recurring = bool(_ONGOING_HINT_RE.search(text or ""))
    slots, pending, last_days = [], [], []
    for _, kind, val in events:
        if kind == "day":
            pending.extend(val)
            continue
        if pending:
            last_days = list(pending)
        for day in (pending or last_days):
            slots.append((day, val[0], val[1]))
        pending = []
    for day in pending:
        # Untimed weekday with nothing marking it as recurring: a dated
        # occurrence, not a pattern. Leave it out so the caller can date it
        # from the explicit date instead of inventing a weekly series.
        if recurring:
            slots.append((day, None, None))
    return _dedupe_slots(slots)


def _dedupe_slots(slots):
    out, seen = [], set()
    for day, start, end in sorted(slots, key=lambda s: (s[0], s[1] or "")):
        key = (day, start)
        if key in seen:
            continue
        seen.add(key)
        out.append((day, start, end))
    return out


@dataclass
class Spec:
    kind: str
    slots: list = field(default_factory=list)
    nth: int = 0
    start_date: date = None
    end_date: date = None
    month_window: tuple = None
    max_periods: int = None
    label: str = ""


def _day_names(weekdays):
    names = [DAYS[d].capitalize() for d in sorted(weekdays)]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _extract_date_range(text, today):
    """Explicit '17th October - 5th December' window, resolved to the current
    year. Returns None when absent, and (None, 'stale') when the window has
    already finished."""
    m = _DATE_RANGE_RE.search(text)
    if not m:
        return None
    d1, mon1, d2, mon2, year = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    m1, m2 = MONTH_IDX.get(mon1.lower()), MONTH_IDX.get(mon2.lower())
    if m1 is None or m2 is None:
        return None
    try:
        y = int(year) if year else today.year
    except (TypeError, ValueError):
        return None
    try:
        start = date(y, m1, int(d1))
    except ValueError:
        return None
    if m2 < m1:
        y += 1
    try:
        end = date(y, m2, int(d2))
    except ValueError:
        return None
    if end < today:
        return (None, "stale")
    return (start, end)


def _extract_month_window(text):
    m = _MONTH_WINDOW_RE.search(text)
    if not m:
        return None
    a = MONTH_IDX.get(m.group(1).lower())
    b = MONTH_IDX.get(m.group(2).lower())
    if a is None or b is None:
        return None
    # A window given as "December to February" wraps the year end. Swapping it
    # would instead describe the 11-month stretch Feb..Dec.
    if a > b:
        return None
    return (a, b)


def _extract_week_count(text):
    m = _WEEKS_RE.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _extract_single_date(text, today, require_year):
    for rx, order in ((_DMY_RE, "dmy"), (_MDY_RE, "mdy")):
        m = rx.search(text)
        if not m:
            continue
        try:
            if order == "dmy":
                day, mon, year = int(m.group(1)), m.group(2), int(m.group(3))
            else:
                mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
            return date(year, MONTH_IDX[mon.lower()], day)
        except (ValueError, KeyError):
            continue
    if require_year:
        return None
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_ALT})\b", text, re.I)
    if m:
        mon = MONTH_IDX.get(m.group(2).lower())
        day = int(m.group(1))
        if mon is None:
            return None
        # A year-less date could belong to this year or the next. Take the
        # first that is not already in the past, so "17 October" read in
        # December rolls forward instead of silently vanishing.
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, mon, day)
            except ValueError:
                return None
            if candidate >= today:
                return candidate
        return None
    return None


def _is_stale(span):
    """True when an explicit date range in the text has already finished.

    The sentinel is the (None, "stale") tuple, which would otherwise be
    mistaken for real bounds -- start_date=None, end_date="stale".
    """
    return isinstance(span, tuple) and len(span) == 2 and span[0] is None \
        and span[1] == "stale"


def _parse_text(text, today, allow_loose_single=False):
    m = _MONTHLY_RE.search(text)
    if m:
        nth, day = ORDINALS[m.group(1).lower()], DAY_IDX[m.group(2).lower()]
        start, end = _first_time_range(text)
        span = _extract_date_range(text, today)
        if _is_stale(span):
            return None, "explicit date range has already finished"
        bounds = span if span else (None, None)
        label = f"{m.group(1).capitalize()} {DAYS[day].capitalize()} of every month"
        return Spec("monthly", [(day, start, end)], nth=nth,
                    start_date=bounds[0], end_date=bounds[1],
                    month_window=_extract_month_window(text),
                    max_periods=_extract_week_count(text), label=label), None

    m = _FORTNIGHTLY_RE.search(text)
    if m:
        day = DAY_IDX[m.group(1).lower()]
        start, end = _first_time_range(text)
        span = _extract_date_range(text, today)
        if _is_stale(span):
            return None, "explicit date range has already finished"
        bounds = span if span else (None, None)
        return Spec("fortnightly", [(day, start, end)],
                    start_date=bounds[0], end_date=bounds[1],
                    max_periods=_extract_week_count(text),
                    label=f"Every second {DAYS[day].capitalize()}"), None

    # A recurring pattern wins over a bare date: "Fortnightly chess on
    # Tuesdays from 15 July 2026" carries a real start date *and* a weekly
    # pattern, and must not collapse to a single occurrence.
    slots = weekday_slots(text)
    if slots:
        span = _extract_date_range(text, today)
        if _is_stale(span):
            return None, "explicit date range has already finished"
        bounds = span if span else (None, None)
        weekdays = {s[0] for s in slots}
        return Spec("weekly", slots, start_date=bounds[0], end_date=bounds[1],
                    max_periods=_extract_week_count(text),
                    label=f"Every {_day_names(weekdays)}"), None

    single = _extract_single_date(text, today, require_year=True)
    if single is not None:
        start, end = _first_time_range(text)
        return Spec("once", [(0, start, end)], start_date=single,
                    end_date=single,
                    label=single.strftime("%d %b %Y")), None

    if allow_loose_single:
        single = _extract_single_date(text, today, require_year=False)
        if single is not None:
            start, end = _first_time_range(text)
            return Spec("once", [(0, start, end)], start_date=single,
                        end_date=single, label=str(single)), None

    return None, None


def _name_hint_spec(name, text=""):
    """Weekday stated only in the event title ('Friday After School STEAM
    session'). Accepted as a last-resort hint, all-day.

    Requires corroborating language in the surrounding text. Without it, a
    one-off titled "Friday Night Quiz" or a listing whose blurb merely says
    "Friday 2 October" would be expanded into 12 fabricated weekly events.
    """
    if not name:
        return None
    if not _ONGOING_HINT_RE.search(text or ""):
        return None
    if _WEEKEND_RE.search(name):
        return Spec("weekly", [(5, None, None)], label="Every Saturday")
    m = _DAY_TOKEN_RE.search(name)
    if not m:
        return None
    day = DAY_IDX[m.group(1).lower()]
    return Spec("weekly", [(day, None, None)],
                label=f"Every {DAYS[day].capitalize()}")


def build_spec(row, today=None):
    """Return (Spec, reason). reason is set when a schedule was recognised but
    cannot produce a future date; reason is None when nothing was found."""
    today = today or _default_today()
    texts = []
    for field_name in ("description", "datetime_text"):
        value = (row.get(field_name) or "").strip()
        if value and value not in texts:
            texts.append(value)
    for index, text in enumerate(texts):
        spec, reason = _parse_text(text, today,
                                    allow_loose_single=index == len(texts) - 1)
        if spec is not None:
            return spec, None
        if reason:
            return None, reason
    spec = _name_hint_spec(row.get("name") or "",
                           " ".join(texts))
    if spec is not None:
        return spec, None
    return None, None


def _nth_weekday(year, month, weekday, nth):
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (nth - 1) * 7
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _at(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour,
                                                                 minutes=minute)


def _in_bounds(day, spec):
    if spec.start_date and day < spec.start_date:
        return False
    if spec.end_date and day > spec.end_date:
        return False
    if spec.month_window and not (spec.month_window[0] <= day.month
                                  <= spec.month_window[1]):
        return False
    return True


def expand(spec, today=None, max_occurrences=MAX_OCCURRENCES):
    """Concrete datetimes for the spec, earliest first, capped."""
    today = today or _default_today()
    if spec is None or not spec.slots:
        return []
    found = []

    if spec.kind == "once":
        day = spec.start_date
        if day and day >= today:
            found.append((day, spec.slots[0][1]))
        return found

    slots_by_day = {}
    for day, start, end in spec.slots:
        slots_by_day.setdefault(day, []).append((start, end))

    if spec.kind == "weekly":
        for offset in range(WEEKLY_HORIZON_DAYS + 1):
            day = today + timedelta(days=offset)
            if day.weekday() not in slots_by_day or not _in_bounds(day, spec):
                continue
            for start, _end in slots_by_day[day.weekday()]:
                found.append((day, start))
    elif spec.kind == "fortnightly":
        for offset in range(FORTNIGHTLY_HORIZON_DAYS + 1):
            anchor = today + timedelta(days=offset)
            if anchor.weekday() not in slots_by_day:
                continue
            # Do not bounds-check the anchor: it only picks the first matching
            # weekday to align the fortnight to. Checking it here would skip a
            # series whose first matching weekday precedes start_date, and the
            # loop would then align to a fortnight phase weeks out of step.
            for week in range(0, 26, 2):
                day = anchor + timedelta(weeks=week)
                if day > today + timedelta(days=FORTNIGHTLY_HORIZON_DAYS):
                    break
                if _in_bounds(day, spec):
                    for start, _end in slots_by_day[day.weekday()]:
                        found.append((day, start))
            break
    elif spec.kind == "monthly":
        anchor_day = next(iter(slots_by_day))
        year, month = today.year, today.month
        for _ in range(MONTHLY_HORIZON_MONTHS):
            occ = _nth_weekday(year, month, anchor_day, spec.nth or 1)
            if occ and occ >= today and _in_bounds(occ, spec):
                for start, _end in slots_by_day[anchor_day]:
                    found.append((occ, start))
            month += 1
            if month > 12:
                month, year = 1, year + 1

    # `start` is None for an all-day slot, so the default tuple order raises
    # TypeError comparing None with str. Sort on the day first, then the
    # start time with None normalised to "" (untimed sorts first).
    found = sorted(set(found), key=lambda f: (f[0], f[1] or ""))
    if spec.max_periods:
        found = found[:spec.max_periods]
    return found[:max_occurrences]


def _row_with_date(row, day, start, label):
    # deepcopy: a shallow dict() copy leaves every generated occurrence
    # aliasing the input's mutable `sources` list, so a later merge writing
    # to one occurrence would write to all 12 and to the caller's row.
    out = deepcopy(row)
    stamp = _at(day, start) if start else datetime.combine(day, dt_time.min)
    out["datetime_iso"] = stamp.isoformat(timespec="seconds")
    out["datetime_text"] = label
    out["datetime_display"] = stamp.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    out["has_real_date"] = True
    out["date_inferred"] = True
    out["recurrence"] = label
    out.pop("date", None)
    return out


def _norm(value):
    return " ".join((value or "").lower().strip().split())


def infer_event(row, today=None, max_occurrences=MAX_OCCURRENCES):
    """Return (rows, reason). rows is empty when no date can be inferred."""
    today = today or _default_today()
    spec, reason = build_spec(row, today)
    if spec is None:
        return [], reason or "no schedule found in description"
    occurrences = expand(spec, today, max_occurrences)
    if not occurrences:
        return [], f"schedule '{spec.label}' has no upcoming occurrences"
    label = spec.label
    if spec.end_date:
        label = f"{label} (to {spec.end_date.strftime('%d %b %Y')})"
    elif spec.max_periods:
        label = f"{label} ({spec.max_periods} sessions)"
    return [_row_with_date(row, day, start, label)
            for day, start in occurrences], label


def refresh_inferred(rows, today=None, max_occurrences=MAX_OCCURRENCES):
    """Re-derive inferred rows whose stored time no longer matches the text.

    Inferred dates are written back into the canonical store, so a row dated
    by an older, buggier build of this module keeps its old time forever --
    nothing re-derives it. Two real cases: midnight copies of time-stated
    series (every Chatty Cafe venue whose schedule says "10.30am"), and rows
    whose time was mis-parsed ("15pm" once read as 03:00).

    A row is re-derived when it is `date_inferred` and the text states a start
    time for that row's own weekday which differs from what is stored. Rows
    whose text gives no time for that weekday are left alone.
    """
    today = today or _default_today()
    kept, refreshed, unresolvable = [], 0, 0
    for r in rows:
        iso = str(r.get("datetime_iso") or "")
        if not (r.get("date_inferred") and len(iso) >= 16):
            kept.append(r)
            continue
        text = r.get("description") or ""
        # Only timed slots count: a weekday can appear both untimed and timed
        # ("...an afternoon... 1st Wednesday at 1:15pm") and the untimed
        # mention is not a competing time.
        slots = [(d, s) for d, s, _e in weekday_slots(text) if s]
        try:
            weekday = date.fromisoformat(iso[:10]).weekday()
        except ValueError:
            weekday = None
        stated = [s for d, s in slots if weekday is not None and d == weekday]
        # With two times for one weekday (a morning and an afternoon session)
        # either stored value may be correct, so only act when unambiguous.
        if len(stated) == 1 and stated[0] != iso[11:16]:
            made, reason = infer_event(r, today, max_occurrences)
            if made:
                kept.extend(made)
                refreshed += 1
                continue
            unresolvable += 1
        kept.append(r)
    if refreshed:
        print(f"  Refreshed {refreshed} stale inferred rows (stored time "
              f"disagreed with the text)")
    if unresolvable:
        print(f"  {unresolvable} stale inferred rows could not be refreshed")
    return kept


def resolve_dateless(rows, today=None, max_occurrences=MAX_OCCURRENCES):
    """Give every dateless row a date, or drop it.

    A row whose name+location already has a *source-supplied* dated sibling is
    dropped in favour of that real date, because a stated date always beats an
    inferred one. Previously inferred siblings do not count — they came from
    this same function, and suppressing against them would make a re-run lose
    occurrences the first run had wrongly collapsed.
    """
    today = today or _default_today()
    dated_keys = set()
    for r in rows:
        if r.get("datetime_iso") and not r.get("date_inferred"):
            dated_keys.add((_norm(r.get("name")), _norm(r.get("location"))))
    kept, expanded, dropped = [], 0, 0
    reasons = {}
    for r in rows:
        if r.get("datetime_iso"):
            kept.append(r)
            continue
        # Key every reason on name *and* source, so two same-named events from
        # different sources do not collapse into a single report entry.
        reason_key = f"{r.get('name')} [{r.get('source_label')}]"
        key = (_norm(r.get("name")), _norm(r.get("location")))
        if key in dated_keys:
            dropped += 1
            reasons[reason_key] = "real date already present"
            continue
        made, reason = infer_event(r, today, max_occurrences)
        if not made:
            dropped += 1
            reasons[reason_key] = reason
            continue
        kept.extend(made)
        expanded += 1
    return kept, {"expanded": expanded, "dropped": dropped, "reasons": reasons}
