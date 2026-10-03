"""Infer concrete dates for dateless events from their description text.

Every event needs a real date before it can be placed on a calendar. Several
sources publish recurring programs without per-occurrence dates: CCC term
classes state "Wednesdays. 2:00pm - 3:30pm", archived council listings state
"on the fourth Saturday of every month". This module parses that prose into a
recurrence spec and expands it into concrete dated occurrences.

Unparseable listings are reported as unresolvable and dropped by the caller.
Inference never overrides a date a source actually supplied.
"""
import hashlib
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta

MAX_OCCURRENCES = 12

# Loop bounds for expand(). Each is deliberately slack above MAX_OCCURRENCES, so
# the cap is always what bounds an expansion and these only ever stop a runaway
# loop on a malformed spec: 126 days is 18 weekly candidates, 252 days is 18
# fortnightly periods, 20 months is 20 monthly candidates, against a cap of 12.
# Tightening any of them to 12 would let a bound, not the cap, decide the last
# occurrence.
WEEKLY_HORIZON_DAYS = 126
FORTNIGHTLY_HORIZON_DAYS = 252
MONTHLY_HORIZON_MONTHS = 20

# Which fortnight a date falls in, for a series that states no start date.
# A Monday, and arbitrary: "every second Friday" does not say which fortnight
# it means, so there is no correct answer -- only one that does not change
# between runs. Anchoring on the run date instead makes the phase depend on
# the weekday of the run, so the same text infers one fortnight on a Friday and
# a different one on the Saturday, and the stored series stops matching what its
# own text yields. See the fortnightly branch of expand().
_PHASE_EPOCH = date(2024, 1, 1)

# How stale a year-less date may be before it stops being "this year, just
# gone" and starts being "next year". Greater Dandenong states dates without a
# year ("28 Sep Drop-In Casual Basketball Monday 28 September, 5:30pm"), so a
# listing read the day after it happened looked like next year's and was
# published twelve months out. Two weeks is long enough to cover a genuine
# "we are publishing next year's programme already" listing read in December,
# and short enough that a one-day-old listing is treated as stale.
YEAR_ROLL_GRACE_DAYS = 14

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
    r"|\bterm\b|\bongoing\b|\brecurring\b"
    r"|\b\d+\s*weeks?\b|\bclasses?\s+each\b|\bsessions?\s+each\b"
    # "First Tuesday each week", "every other week". "each <weekday>" was
    # already here but "each week" was not, so a weekly class written that
    # way had no recurring marker at all.
    r"|\b(?:each|every)\s+(?:other\s+)?(?:week|month|fortnight)\b", re.I)

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
    rf"|\b(?:{_NOON_ALT}|midnight)\b"
    r"|\b\d{1,2}\s*[ap]\.?\s*m\.?"
    r"|\b\d{1,2}[:.]\d{2}\b"
    r")")
_TIME_RANGE_RE = re.compile(
    rf"({_TIME_TOKEN})\s*(?:-|–|—|to|until|till)\s*({_TIME_TOKEN})", re.I)
# "12 - 1.30pm": a bare hour is only meaningful as one end of a range, so it
# gets its own pattern rather than joining _TIME_TOKEN (where it would also
# match day numbers in dates).
#
# Both ends accept a bare hour, and the meridiem is inherited across the
# range. The second group used to require ":MM", so "Tuesdays 6 - 8pm" matched
# no range at all: the bare "6" is not a _TIME_TOKEN, and "8pm" alone cannot
# satisfy ":MM". The end survived as a lone time point and became the *start*,
# publishing a 6-8pm class at 20:00.
#
# The start may borrow the end's am/pm ("9 - 11am"), but only when the text
# states no meridiem of its own: "9am - 11" is 9 in the morning to 11 at
# night, and reading that as 09:00-11:00 would be the reverse error.
_BARE_HOUR_RANGE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2}(?:[:.]\d{2})?\s*[ap]\.?\s*m\.?)",
    re.I)
_BARE_START_RE = re.compile(r"^\d{1,2}$")
_TIME_POINT_RE = re.compile(_TIME_TOKEN, re.I)

# "of the month" only. The alternation used to read "week" as well, so
# "First Tuesday each week, 7pm" parsed as the first Tuesday of each *month*
# -- twelve dates twelve months apart, four of every five wrong. "Every second
# Tuesday" without "of the month" is caught by _FORTNIGHTLY_RE instead.
_MONTHLY_RE = re.compile(
    rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+"
    rf"{_DAY_RE}s?\s+(?:of|each|in|every)\s+"
    rf"(?:(?:the|every|each)\s+)?month", re.I)
_FORTNIGHTLY_RE = re.compile(
    rf"\bevery\s+(?:second|2nd|alternating|other)\s+({DAY_ALT})s?\b", re.I)
# The same cadence written as a property of the weekday rather than of the
# repetition: "Friday (fortnightly) 10.30am-11.30am" (a Chatty Cafe venue
# listing) and "Monday 10.30am (every second week)". Without these the listing
# fell through to the weekly branch and published a session on the weeks the
# venue does not open.
_FORTNIGHTLY_PROP_RE = re.compile(
    r"\(\s*fortnightly\s*\)|\bevery\s+(?:second|2nd|alternating|other)\s+week\b",
    re.I)
_WEEKEND_RE = re.compile(r"\bweekends?\b", re.I)

_DAY_SPAN_RE = re.compile(
    rf"\b{_DAY_RE}\s*(?:s\.\s*)?(?:to|through|thru|[-–])\s*{_DAY_RE}\b", re.I)
_DAY_TOKEN_RE = re.compile(rf"\b{_DAY_RE}s?\b", re.I)
# A sentence break between a weekday and a later bare time. See weekday_slots().
_SENTENCE_END_RE = re.compile(r"[.!?;]\s|\n")

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

    Anchoring every expansion on this is what keeps two runs on different days
    from shifting a whole inferred series forward. Where a listing states its
    own start date that date wins for the *phase* (see expand()); a bad
    SOURCE_DATE_EPOCH is reported rather than silently ignored, because a
    reproducible run that is not reproducible is worse than one that failed.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return date.fromtimestamp(int(epoch))
        except (ValueError, OverflowError, OSError) as e:
            print(f"  WARN: SOURCE_DATE_EPOCH={epoch!r} is unusable ({e}); "
                  f"falling back to today, so this run is not reproducible")
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
        start_token, end_token = m.group(1), m.group(2)
        if _BARE_START_RE.match(start_token):
            # "9 - 11am": the start states no meridiem, so it inherits the
            # end's. Read as a clock time it would be 09:00 anyway for an am
            # end, but "9 - 8pm" is 9am to 8pm, and a bare "9" means neither.
            ap = re.search(r"([ap])\.?\s*m?\.?$", end_token, re.I)
            if ap:
                start_token += ap.group(1) + "m"
        start, end = _to_hhmm(start_token), _to_hhmm(end_token)
        # A bare start inherits the end's meridiem, which is what makes
        # "9 - 11am" 09:00-11:00. When the two are then out of order -- "9 -
        # 8pm", "10 - 12am" -- the inherited reading is wrong, and the pair is
        # refused rather than published as a class that ends before it starts.
        # The times are left unpaired, so the caller reports the listing as
        # undatable instead of inventing a session.
        if start and end and end < start:
            continue
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
    # A backwards bare-hour range ("9 - 8pm") is refused in _time_ranges, but
    # its end time would otherwise return here as a lone time point and be
    # published as a session at 20:00. Exclude those positions so a refused
    # range stays refused instead of re-entering through the fallback.
    refused_spans = []
    for m in _BARE_HOUR_RANGE_RE.finditer(text or ""):
        start_token, end_token = m.group(1), m.group(2)
        if _BARE_START_RE.match(start_token):
            ap = re.search(r"([ap])\.?\s*m?\.?$", end_token, re.I)
            if ap:
                start_token += ap.group(1) + "m"
        start, end = _to_hhmm(start_token), _to_hhmm(end_token)
        if start and end and end < start:
            refused_spans.append((m.start(), m.end()))
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
        if any(rs <= m.start() < re_ for rs, re_ in refused_spans):
            continue
        events.append((m.start(), "time", (single, single)))
    events.sort(key=lambda ev: ev[0])
    recurring = bool(_ONGOING_HINT_RE.search(text or ""))
    slots, pending, last_days, last_days_at = [], [], [], -1
    for pos, kind, val in events:
        if kind == "day":
            pending.extend(val)
            continue
        # A time with no weekday of its own belongs to the last weekday seen.
        # That carry-over must not cross a sentence boundary: "Mondays and
        # Wednesdays 9am - 12pm. Bookings close 4pm." is one class on two
        # days and a booking deadline, but the deadline's time was re-applied
        # to both days, publishing a phantom 4pm session each. "Mondays 9am |
        # Fridays 1pm" has no sentence break and still works.
        days = list(pending)
        if not days and last_days and not _SENTENCE_END_RE.search(
                text[last_days_at:pos]):
            days = list(last_days)
        if pending:
            last_days, last_days_at = list(pending), pos
        for day in days:
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
    for day, start, end in sorted(slots, key=lambda s: (s[0], s[1] or "", s[2] or "")):
        key = (day, start, end)
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
    # The single calendar year a month_window belongs to. "from February to
    # November" states months, never a year, so the window alone is satisfied
    # again by the same months of the following year -- and a 20-month
    # expansion horizon reaches into it. "Music at McClelland" (third Sunday,
    # February to November) published 12 rows, 10 of them next season.
    window_year: int = None
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
        # December rolls forward instead of silently vanishing -- but only
        # when the current-year reading is *clearly* gone. Rolling on the
        # very next day turned every just-past listing into a twelve-months-
        # ahead phantom, which then sat in the store until it aged out.
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, mon, day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
            if (today - candidate).days <= YEAR_ROLL_GRACE_DAYS:
                # Only just past: this year's occurrence, now over.
                return None
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
        window = _extract_month_window(text)
        return Spec("monthly", [(day, start, end)], nth=nth,
                    start_date=bounds[0], end_date=bounds[1],
                    month_window=window,
                    window_year=_window_year(today, window),
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
    #
    # weekday_slots() returns a slot whenever a time follows a weekday, so a
    # listing that names one date and one time -- "Drop-In Casual Basketball
    # Monday 28 September, 5:30pm" -- produced slots too, and this branch
    # returned a weekly series for a single afternoon: twelve Tuesdays. The
    # test is the text, not the slots: a pattern needs recurring language
    # ("every week", "term", "10 weeks") or must carry no date at all, as
    # "Tuesdays and Thursdays 9am" does. A weekday plus a time plus one
    # explicit date is that one session.
    slots = weekday_slots(text)
    # Only look for a competing single date when the text does not claim to
    # recur; otherwise "Fortnightly chess from 15 July" is read as one session.
    # The loose (year-less) reading is allowed here for the same reason it is
    # for a dateless row: "Monday 12 October, 5:30pm" is that one afternoon.
    #
    # When that single date is *stale* -- read after the day it names, inside
    # the grace window -- the listing is over rather than recurring, so it
    # must not fall through to the weekly branch and be republished as twelve
    # future Mondays. That is what turned one finished afternoon into a
    # twelve-week run.
    single = stale_single = None
    if slots and not _ONGOING_HINT_RE.search(text):
        single = _extract_single_date(text, today, require_year=True)
        loose = None
        if single is None:
            loose = _extract_single_date(text, today, require_year=False)
            if allow_loose_single:
                single = loose
            if loose is None and re.search(rf"\b\d{{1,2}}\s+({MONTH_ALT})\b",
                                            text, re.I):
                stale_single = True
    # A weekday and a time with no date anywhere is a pattern. That is how
    # these sources actually write a weekly program -- a Chatty Cafe venue
    # states "Tuesday 11am - 1pm" for a session it runs every week of term,
    # and 15 of the published series are written exactly that way.
    #
    # What separates that from a single undated session is not the plural
    # ("Tuesday" and "Tuesdays" are both used) but an explicit date. When the
    # text names a day, that day is the session: "Drop-In Casual Basketball
    # Monday 12 October, 5:30pm" is one afternoon, and reading it as weekly
    # published twelve of them. So the date decides, and the absence of one
    # leaves the weekday as a pattern.
    #
    # `stale_single` is the exception: a date that has just passed means the
    # listing is over, not that it recurs.
    if slots and single is None and not stale_single:
        span = _extract_date_range(text, today)
        if _is_stale(span):
            return None, "explicit date range has already finished"
        bounds = span if span else (None, None)
        weekdays = {s[0] for s in slots}
        # "(fortnightly)" states the cadence for every weekday it is attached
        # to, and a fortnightly listing names one weekday, so all slots take
        # the fortnight. Guarded on a single weekday because the fortnight
        # expansion walks one weekday per period -- see expand().
        if len(weekdays) == 1 and _FORTNIGHTLY_PROP_RE.search(text):
            day = weekdays.pop()
            return Spec("fortnightly", slots, start_date=bounds[0],
                        end_date=bounds[1], max_periods=_extract_week_count(text),
                        label=f"Every second {DAYS[day].capitalize()}"), None
        return Spec("weekly", slots, start_date=bounds[0], end_date=bounds[1],
                    max_periods=_extract_week_count(text),
                    label=f"Every {_day_names(weekdays)}"), None

    if single is not None:
        start, end = _first_time_range(text)
        return Spec("once", [(0, start, end)], start_date=single,
                    end_date=single,
                    label=single.strftime("%d %b %Y")), None
    if stale_single:
        return None, "the date this listing states has already passed"

    # An explicit range that has finished settles the listing, even when the
    # range carries no year. Without this the loose-single path below runs
    # first and re-reads the range's opening day as next year: "from 1 June
    # to 31 August" read in September became a phantom 2027 event.
    span = _extract_date_range(text, today)
    if _is_stale(span):
        return None, "explicit date range has already finished"

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
        return Spec("weekly", [(5, None, None), (6, None, None)], label="Every weekend")
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
    first_reason = None
    for index, text in enumerate(texts):
        spec, reason = _parse_text(text, today,
                                    allow_loose_single=index == len(texts) - 1)
        if spec is not None:
            return spec, None
        if reason and first_reason is None:
            first_reason = reason
    if first_reason:
        return None, first_reason
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
    if spec.window_year and day.year != spec.window_year:
        return False
    return True


def _window_year(today, month_window):
    """The calendar year a month_window describes, or None.

    A window given as bare months is a *season*, and a season belongs to the
    year that contains the day it is read on: read in September, "from February
    to November" is this year's Feb-Nov, with the remaining months still ahead.
    Read after it has closed -- December, for the same window -- the season
    being described is next year's, the same rolling treatment a year-less
    single date already gets.
    """
    if not month_window or today is None:
        return None
    first, last = month_window
    if first > last:
        # A wrapping window ("December to February"): pick the year that
        # contains the current month, whatever part of the window it is.
        return today.year
    return today.year if today.month <= last else today.year + 1


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
        # A fortnight divides the week in two, so the phase of "every second
        # Saturday" is arbitrary, and it has to come from somewhere that does
        # not move between runs. Two anchors, in order of authority:
        #
        # * A stated `start_date` is the venue's own phase. "Fortnightly chess
        #   from 15 July" means the 15th, not whichever fortnight today is in.
        #   Anchoring on `today` shifted the published dates by a week every
        #   time the pipeline ran, and when the stated start fell outside
        #   today's phase the whole series moved and the last named session
        #   fell off the end.
        # * With no stated start there is nothing to anchor to except the run
        #   date, and `today` is the one anchor that cannot be used. Which
        #   fortnight comes "first" depends on the weekday of the run: the same
        #   "Friday (fortnightly)" text inferred on a Friday starts that Friday,
        #   and on the Saturday it starts a week later -- a whole period out of
        #   step, not a day. A Chatty Cafe venue states no start date at all,
        #   so its series was inferred one way on Friday 2 October and
        #   re-inferred the other way on Saturday the 3rd, and health_check
        #   failed the build on the stored rows the morning after.
        #
        #   ISO week parity against a fixed epoch is arbitrary but stable, and
        #   arbitrary is the best available: the source does not say which
        #   fortnight it means, so no choice is more correct than another --
        #   it only has to be the *same* choice every run.
        if spec.start_date is not None:
            anchor = spec.start_date
            while anchor.weekday() not in slots_by_day:
                anchor += timedelta(days=1)
            # Do not bounds-check the anchor: it only picks the first matching
            # weekday to align the fortnight to. Checking it would skip a series
            # whose first matching weekday precedes start_date, and the series
            # would then align to a fortnight phase weeks out of step.
            candidates = [anchor + timedelta(weeks=week)
                          for week in range(0, 26, 2)]
        else:
            candidates = [today + timedelta(days=offset)
                          for offset in range(FORTNIGHTLY_HORIZON_DAYS + 1)
                          if (today + timedelta(days=offset)).weekday()
                          in slots_by_day
                          and (today + timedelta(days=offset)
                               - _PHASE_EPOCH).days // 7 % 2 == 0]
        for day in candidates:
            if day > today + timedelta(days=FORTNIGHTLY_HORIZON_DAYS):
                break
            # A phase_origin may be a past start_date, which is what puts the
            # series in the right fortnight; the occurrence still has to be
            # ahead of the reader.
            if day < today:
                continue
            if _in_bounds(day, spec):
                for start, _end in slots_by_day[day.weekday()]:
                    found.append((day, start))
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
        # `_extract_week_count` reads "\bN weeks?\b", but `found` is a flat
        # list of *occurrences*, so N was truncating at N sessions. A 10-week
        # Monday course lost its final Monday; a twice-weekly 6-week course
        # published 3 weeks. An explicit date range is the more specific
        # statement of the same thing and already bounds `found`, so the
        # count only applies where there is no range, and is converted to
        # occurrences by how many sessions the week actually holds.
        if not (spec.start_date or spec.end_date):
            per_week = max(1, len(spec.slots))
            found = found[:spec.max_periods * per_week]
    return found[:max_occurrences]


def series_id_for(row):
    """A stable identity for the recurring series this row belongs to.

    A series is materialised into up to MAX_OCCURRENCES independent rows, one
    per occurrence, and until they carried an identity there was no way to ask
    "is this series still published?" of any of them individually. dedupe.py
    had to re-derive the series from its prose and compare timestamps instead,
    and because the expansion window is anchored on the run date, that
    comparison slid forward every run and declared correct rows unjustified
    (97 rows 8 days after a build, 382 two months after).

    Keyed on the source, the programme name and the venue head -- the three
    fields the store carries identically on the dateless listing and on every
    occurrence materialised from it, so both sides compute the same value.

    Deliberately NOT keyed on `source`. Chatty Cafe re-slugged a venue page
    once; a URL key would have re-identified every series that venue owns on
    the next run, which is the opposite of what an identity is for. Nor is it
    folded for accents, because the id is per-source by construction: it exists
    to recognise one listing's own occurrences, never to merge two sources'
    listings. Cross-source identity is dedupe.py's name/location match.

    A fetcher that publishes its own series identity should use that instead of
    this: webfetch_everi.py stamps the site's own `eventIdentifier` GUID, which
    is authoritative where ours is inferred.
    """
    key = "|".join((
        str(row.get("source_id") or "unknown"),
        _norm(row.get("name")),
        _norm((row.get("location") or "").split(",")[0]),
    ))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _row_with_date(row, day, start, label):
    # deepcopy: a shallow dict() copy leaves every generated occurrence
    # aliasing the input's mutable `sources` list, so a later merge writing
    # to one occurrence would write to all 12 and to the caller's row.
    out = deepcopy(row)
    stamp = _at(day, start) if start else datetime.combine(day, dt_time.min)
    out["datetime_iso"] = stamp.isoformat(timespec="seconds")
    out["datetime_text"] = label
    out["date_inferred"] = True
    out["recurrence"] = label
    out["series_id"] = series_id_for(row)
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
        label = f"{label} ({spec.max_periods} weeks)"
    return [_row_with_date(row, day, start, label)
            for day, start in occurrences], label


def materialise(row, schedule, today=None,
                max_occurrences=MAX_OCCURRENCES):
    """Expand an explicit schedule string into dated rows for `row`.

    For a source that states a schedule in a structured form of its own rather
    than in prose: a per-weekday hours table, say. The fetcher normalises that
    table to the same wording the parser already understands
    ("Every Sunday 7:00am - 10:00am, every Wednesday 6:00am - 8:00am") and hands
    it here, so the occurrence maths stays in this module and a fetcher never
    has to reimplement it -- the same reason webfetch_ccc._ccc_weekly_term()
    exists, and the reason this is a function rather than a Spec the caller
    assembles.

    Returns (rows, spec, reason). `rows` is empty when the wording produced no
    dated occurrence, which is the caller's signal to drop the listing.

    The rows carry `series_id` like any inferred row, so reconcile_store() can
    withdraw the whole series at once when the source stops publishing it. That
    matters more for these than for prose-derived ones: a group's hours table is
    the only statement of the schedule there is, so a series that is withdrawn
    leaves nothing behind to justify its remaining occurrences.
    """
    today = today or _default_today()
    text = (schedule or "").strip()
    if not text:
        return [], None, "no schedule supplied"
    probe = dict(row)
    probe["datetime_text"] = text
    probe["description"] = text
    spec, reason = build_spec(probe, today)
    if spec is None:
        return [], None, reason or f"schedule {text!r} is not a recognisable pattern"
    # A weekday with no clock time is not a meeting a reader can turn up to, and
    # an occurrence at midnight is a date with a time the source never stated.
    # Callers that need a time (a group you join has to say when it meets) test
    # for this rather than accepting the slot.
    timed = [s for s in spec.slots if s[1] is not None]
    if not timed:
        return [], spec, f"schedule {text!r} names days but no start time"
    occurrences = expand(spec, today, max_occurrences)
    if not occurrences:
        return [], spec, (reason or
                          f"schedule '{spec.label}' has no upcoming occurrences")
    label = spec.label
    if spec.end_date:
        label = f"{label} (to {spec.end_date.strftime('%d %b %Y')})"
    return [_row_with_date(row, day, start, label)
            for day, start in occurrences], spec, None


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

    It is *withdrawn* when the text no longer produces the row's date at all --
    an inferred date is what the text yields, not an independent fact, so a
    date the text cannot produce is a session that cannot exist. Only dates
    from `today` onwards are tested, because an expansion runs forward from
    `today` and past rows are `dedupe.prune_old`'s business at 90 days.
    """
    today = today or _default_today()
    kept, refreshed, unresolvable, withdrawn = [], 0, 0, 0
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
            stored_day = date.fromisoformat(iso[:10])
        except ValueError:
            stored_day = None
        weekday = stored_day.weekday() if stored_day else None
        stated = [s for d, s in slots if weekday is not None and d == weekday]

        # An inferred row's date is not a fact about the world, it is what this
        # row's own text yields. So when the text no longer yields it, the row
        # is not a session that might have moved -- it is a session that cannot
        # exist, and keeping it publishes a date the source never stated. This
        # is the accumulated damage of the fortnightly phase being anchored on
        # the run date: a class that met every second Friday was inferred one
        # fortnight on a Friday and a fortnight off on the Saturday, the two
        # sets merged in an append-only store, and the result read as a *weekly*
        # class -- 23 sessions where there are 12.
        #
        # Only rows dated today or later are withdrawn. An expansion runs
        # forward from `today`, so no past date can appear in one, and past rows
        # are `prune_old`'s business at 90 days; testing them here would
        # shorten the published history instead of repairing it.
        if stored_day is not None and stored_day >= today:
            made, _reason = infer_event(r, today, max_occurrences)
            if made and not any(m.get("datetime_iso", "")[:10] == iso[:10]
                                for m in made):
                withdrawn += 1
                continue

        # With two times for one weekday (a morning and an afternoon session)
        # either stored value may be correct, so only act when unambiguous.
        if len(stated) == 1 and stated[0] != iso[11:16]:
            made, reason = infer_event(r, today, max_occurrences)
            # The re-expansion replaces this row, so it has to contain the
            # row's own date as well as the right time. Checking only the time
            # let a stored date be silently moved to another day: the row was
            # dropped and the replacement kept, and a phase change in the
            # pattern could do the same for a row whose time happened to
            # agree. Reconciling on the date makes the two consistent.
            if made and stored_day and any(
                    m.get("datetime_iso", "")[:10] == iso[:10] for m in made):
                for m in made:
                    if m.get("datetime_iso", "")[:10] == iso[:10]:
                        kept.append(m)
                        refreshed += 1
                        break
                continue
            unresolvable += 1
        kept.append(r)
    if withdrawn:
        print(f"  Withdrew {withdrawn} inferred row(s) whose own text no "
              f"longer produces their date (a stale series phase)")
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
        reason_key = f"{r.get('name')} [{r.get('source_id')}]"
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


# ---------------------------------------------------------------------------
# Self-tests. Run by `python scripts/recurrence.py` and by the GHA workflow.
#
# This module decides the day a reader turns up, so a wrong date is worse than
# a missing event: the row still renders, still looks bookable, and is simply
# wrong. Every case below is a real listing form, and each was a live defect
# that shipped a correct-looking wrong date. The trigger text is quoted from
# the listing as published.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # A fixed "today" so the expected dates below are literal rather than
    # relative to whenever the suite runs.
    TODAY = date(2026, 9, 30)

    def dates_for(text, today=TODAY):
        spec, _reason = _parse_text(text, today, allow_loose_single=True)
        if spec is None:
            return None
        return [d.isoformat() for d, _s in expand(spec, today)]

    def slots_for(text):
        spec, _reason = _parse_text(text, TODAY, allow_loose_single=True)
        return spec.slots if spec else None

    # (label, actual, expected). Every one of these is a date the pipeline
    # published or would have published, so a failure names a wrong day a
    # reader would have been sent to.
    TESTS = [
        # --- a month window is a season, and a season has one year ---------
        # 10 of these 12 rows were dated 2027, a programme that is not held
        # then. The window is bound to the year containing the read date.
        ("month_window stays in its own year",
         dates_for("Music at McClelland is held on the third Sunday of the "
                   "month, 2.30pm to 4pm, from February to November"),
         ["2026-10-18", "2026-11-15"]),

        # --- an explicit range outranks a stated week count ---------------
        # "Weeks: 10" was truncating a list of *occurrences*, so the 11th
        # Monday -- the one the term actually ends on -- was dropped.
        ("date range wins over week count",
         dates_for("Practice your netball at home! Monday. 3pm - 5pm. "
                   "Term 4 : 5th October - 14th December Weeks : 10"),
         ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02",
          "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30", "2026-12-07",
          "2026-12-14"]),

        # With no range to defer to, a week count is still a bound -- but on
        # occurrences, so a twice-weekly course gets its full six weeks.
        ("week count multiplies by sessions per week",
         len(dates_for("Circuit class Tuesdays and Thursdays 9am for "
                       "6 weeks") or []),
         12),

        # --- a fortnightly series has a phase, and it is the start date ----
        # Anchored on `today`, the same text published different dates on
        # different days, and the last named session fell off the end.
        ("fortnightly phase is stable across run dates",
         [dates_for("Every second Saturday 10:30am - 12:30pm "
                    "Term 4 (17th October - 5th December)", d)
          for d in (date(2026, 9, 30), date(2026, 10, 6))],
         [["2026-10-17", "2026-10-31", "2026-11-14", "2026-11-28"]] * 2),

        # The same stability with NO stated start date, which is the case the
        # anchor above was missing. A Chatty Cafe venue states only "Friday
        # (fortnightly) 10.30am-11.30am", so `today` was the only anchor, and
        # the weekday of the run then decided which fortnight came first: the
        # same text gave Friday 2 Oct -> 2, 16, 30 October and Saturday 3 Oct
        # -> 9, 23 October, one whole period apart. Eleven stored rows stopped
        # matching their own text and health_check failed the build the morning
        # after a Friday run.
        ("a dateless fortnightly phase is stable across run dates",
         [dates_for("Every second Friday", d)
          for d in (date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5))],
         [dates_for("Every second Friday", date(2026, 10, 2))] * 3),
        # ...and it is a fortnight, not a week: consecutive occurrences are
        # exactly 14 days apart.
        ("a dateless fortnightly series is fortnightly",
         [(b - a).days for a, b in zip(
             [date.fromisoformat(x) for x in
              dates_for("Every second Friday", date(2026, 10, 3))],
             [date.fromisoformat(x) for x in
              dates_for("Every second Friday", date(2026, 10, 3))][1:])],
         [14] * (len(dates_for("Every second Friday", date(2026, 10, 3))) - 1)),

        # --- one named date is one session, not a pattern ------------------
        # weekday_slots() returns a slot for a weekday followed by a time, so
        # a single afternoon parsed as weekly and published twelve of them.
        ("a dated single occurrence stays single",
         dates_for("Drop-In Casual Basketball Monday 12 October, 5:30pm"),
         ["2026-10-12"]),

        ("a dated single occurrence with a year stays single",
         dates_for("Storytime on Tuesday 6 October 2026, 10:00am"),
         ["2026-10-06"]),

        # A finished listing is not a recurring one. Falling through to the
        # weekly branch republished one past afternoon as twelve future ones.
        ("a stale single date is not expanded",
         dates_for("Drop-In Casual Basketball Monday 28 September, 5:30pm"),
         None),

        # Genuine recurrence must still expand.
        ("'every Tuesday' still expands",
         len(dates_for("Every Tuesday 7pm - 8pm") or []),
         12),
        # A weekday and a time with no date is a weekly program, singular
        # weekday or not: 15 published Chatty Cafe series are written exactly
        # this way ("Tuesday 11am - 1pm" for a session open every week), and
        # the plural is used interchangeably.
        ("a singular weekday with a time and no date is a pattern",
         len(dates_for("Tuesday 9am - 12pm") or []),
         12),
        ("a plural weekday with a time is a pattern",
         len(dates_for("Tuesdays 9am - 12pm") or []),
         12),
        # A real venue listing in that same shape, so the case above cannot
        # regress to dropping every Chatty Cafe venue.
        ("a Chatty Cafe weekly listing expands",
         len(dates_for("Chatty Cafe at Timbuktu Cafe. Tuesday 11am - 12:30pm. "
                       "A welcoming space for conversation.") or []),
         12),

        # --- a range's end is not its start -------------------------------
        # The bare "6" is not a time token and "8pm" alone could not satisfy
        # the minutes group, so no range matched and the end became the start:
        # a 6-8pm class published at 20:00.
        ("bare-hour range keeps its start",
         slots_for("Community Kitchen Tuesdays 6 - 8pm"),
         [(1, "18:00", "20:00")]),
        ("bare start inherits the end's meridiem",
         slots_for("Every Thursday 9 to 11am"),
         [(3, "09:00", "11:00")]),
        ("an explicit range is unaffected",
         slots_for("Saturdays 6pm - 8pm"),
         [(5, "18:00", "20:00")]),
        ("a backwards range is refused, not published",
         slots_for("Mondays 9 - 8pm"),
         None),

        # --- "each week" is not "of the month" -----------------------------
        # The alternation read "week" as a monthly period, so a weekly class
        # published on the first Tuesday of each month: four of five dates wrong.
        ("'First Tuesday each week' is weekly",
         len(dates_for("First Tuesday each week, 7pm") or []),
         12),
        ("'of the month' is still monthly",
         dates_for("First Tuesday of every month, 7pm")[:3],
         ["2026-10-06", "2026-11-03", "2026-12-01"]),

        # --- a deadline in the prose is not a class ------------------------
        # The carry-over re-applied a bare time to the last weekday set across
        # a sentence boundary, publishing a 4pm session on both days.
        ("a booking deadline is not a session",
         slots_for("Mondays and Wednesdays 9am - 12pm. Bookings close 4pm."),
         [(0, "09:00", "12:00"), (2, "09:00", "12:00")]),
        ("a pipe-separated second session still pairs",
         slots_for("Mondays 9am - 12pm | Fridays 1pm - 2pm"),
         [(0, "09:00", "12:00"), (4, "13:00", "14:00")]),
    ]

    failures = []
    for label, actual, expected in TESTS:
        if actual == expected:
            print(f"ok   {label}")
        else:
            print(f"FAIL {label}\n       actual:   {actual}\n       expected: {expected}")
            failures.append(label)

    # refresh_inferred must not multiply rows: it replaces a stale row with
    # its corrected form, so N stored rows stay N rows. It used to re-expand
    # each one to a whole series -- 3 stored rows became 36 -- and to move a
    # row to a date its own text did not justify.
    _txt = "Every Tuesday 10:30am at the library"
    _stored = [dict(name=_txt, description=_txt, location="Library",
                    date_inferred=True, datetime_iso=d + "T00:00:00")
               for d in ("2026-10-06", "2026-10-13", "2026-10-20")]
    _out = refresh_inferred(_stored, TODAY)
    for _label, _actual, _expected in (
            ("refresh does not multiply rows", len(_out), 3),
            ("refresh keeps each row's own date",
             sorted(m["datetime_iso"][:10] for m in _out),
             ["2026-10-06", "2026-10-13", "2026-10-20"]),
            ("refresh applies the stated time",
             sorted({m["datetime_iso"][11:16] for m in _out}), ["10:30"])):
        if _actual == _expected:
            print(f"ok   {_label}")
        else:
            print(f"FAIL {_label}\n       actual:   {_actual}"
                  f"\n       expected: {_expected}")
            failures.append(_label)

    # ...but it must not *keep* a date its own text cannot produce. The
    # fortnightly phase was once anchored on the run date, so "Every second
    # Friday" inferred one fortnight on a Friday and another on the Saturday;
    # both sets landed in an append-only store and the class published as a
    # weekly one. Rows in the retired phase have to be withdrawable, or the
    # store keeps both readings forever.
    _ft = "Every second Friday"
    _good = {d[:10] for d in dates_for(_ft, TODAY)}
    _rows = [dict(name=_ft, description=_ft, location="Aspendale Gardens",
                  date_inferred=True, datetime_iso=d + "T10:30:00")
             for d in ("2026-10-09", "2026-10-16", "2026-10-23")]
    _kept = refresh_inferred(_rows, TODAY)
    for _label, _actual, _expected in (
            ("a row the text still produces is kept", len(_kept), 2),
            ("a row in the retired phase is withdrawn",
             sorted(m["datetime_iso"][:10] for m in _kept),
             sorted(d for d in _good if d in
                    {"2026-10-09", "2026-10-16", "2026-10-23"})),
            ("and the text's fortnight is the one that survives",
             sorted(m["datetime_iso"][:10] for m in _kept)[:1], ["2026-10-09"])):
        if _actual == _expected:
            print(f"ok   {_label}")
        else:
            print(f"FAIL {_label}\n       actual:   {_actual}"
                  f"\n       expected: {_expected}")
            failures.append(_label)

    if failures:
        print(f"\nrecurrence: {len(failures)}/{len(TESTS) + 6} cases FAILED")
        raise SystemExit(1)
    print(f"\nall {len(TESTS) + 6} recurrence cases as expected")
