"""Commercial-event detector. Kept in data, hidden by default in UI.

is_commercial=True when a pub/meal deal promo is detected:
- strong food/deal keywords always count
- weak venue terms (hotel/pub/RSL/...) need corroboration
- trivia counts ONLY when priced (user rule: drop trivia unless priced),
  or when tied to a denylisted venue (pub trivia driving drink sales).

Never auto-flag council/library/community-centre sources on their own.
"""
import re

STRONG = [
    r"\bparmas?\b", r"\bparmi\b", r"\bschnitzel\b", r"\bsteak\b", r"\bwagyu\b",
    r"\bcounter\s*meal\b", r"\bmeat\s*raffle\b", r"\bmembers?\s*draw\b",
    r"\bhappy\s*hour\b", r"\blunch\s*special\b", r"\bdinner\s*special\b",
    r"\bseniors?\s*meal\s*deal\b", r"\broast\s*special\b", r"\bfish\s*n\s*chips\s*special\b",
]
STRONG_RE = [re.compile(p, re.I) for p in STRONG]

# Venue denylist applied to location + address only (not name/description,
# to avoid false positives like "Pubs with a past" heritage talk).
# Words that are unambiguously commercial. The community allowlist may never
# override these, so "The Bank Bar" / "Parkview Tavern" stay flagged.
HARD_VENUE = [r"\bpub\b", r"\btavern\b", r"\bbistro\b", r"\bbrewery\b",
              r"\bpokie\b", r"\bhotel\b", r"\bgaming\s*lounge\b"]
# Weaker, more ambiguous words that a genuine community venue may contain.
SOFT_VENUE = [r"\binn\b", r"\bsports?\s*bar\b", r"\btab\b.*\bbet\b"]

HARD_VENUE_RE = [re.compile(p, re.I) for p in HARD_VENUE]
SOFT_VENUE_RE = [re.compile(p, re.I) for p in SOFT_VENUE]

# Word-bounded: without \b, "park" matched "Parker", "hall" matched
# "Shall we dance", "bank" matched "Bank Bar" and "beach" matched "Beaches",
# each exempting a real commercial venue from the denylist below.
ALLOWLIST_RE = re.compile(
    r"\blibrar\w*|\bcommunity\s*cent\w*|\bneighbou?rhood\s*house\b"
    r"|\bactivity\s*hub\b|\bchurch\w*|\bhall\b|\breserve\b|\bbeach\b|\bpark\b"
    r"|\btheat(re|er)\b|\bgallery\b|\bschool\b",
    re.I,
)

TRIVIA_RE = re.compile(r"\btrivia\b", re.I)
# Must capture the whole number: r"\$\s*\d" matched only the first digit, so
# "$20.00" parsed as 2.0 and failed the >= 5 gate in _has_solid_price.
PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
RSL_RE = re.compile(r"\brsl\b", re.I)


def _price_amount(text):
    """Numeric dollar amount in text, or None."""
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _has_price(event):
    ps = event.get("price_sort")
    if isinstance(ps, (int, float)) and ps is not None and ps > 0:
        return True
    return _price_amount(event.get("price_text")) is not None


def _has_solid_price(event):
    """Priced enough to look commercial (pub trivia tickets etc).

    Gold-coin donations and $1-2 charges stay community.
    """
    ps = event.get("price_sort")
    if isinstance(ps, (int, float)) and ps is not None and ps >= 5:
        return True
    m = PRICE_RE.search(event.get("price_text") or "")
    if m:
        try:
            return float(m.group(1)) >= 5
        except ValueError:
            return False
    return False


def _venue_hit(location, address):
    blob = f"{location or ''} {address or ''}"
    # Avoid "public/publishing/published" false positives for \bpub\b
    blob_wo_public = re.sub(r"publi[cs]\w*|publish\w*", " ", blob, flags=re.I)
    # Unambiguous commercial words win outright, so a venue named
    # "Parkview Tavern" is not rescued by the allowlist's \bpark\b.
    for rx in HARD_VENUE_RE:
        if rx.search(blob_wo_public if rx.pattern == r"\bpub\b" else blob):
            return True
    # Ambiguous words may be exempted by a clearly community venue string.
    if not ALLOWLIST_RE.search(blob) or RSL_RE.search(blob):
        for rx in SOFT_VENUE_RE:
            if rx.search(blob):
                return True
        return False
    # No \bpub\b guard here: the HARD loop above already returned True on
    # blob_wo_public for that pattern, so reaching this point proves the scrub
    # left no bare "pub". The guard could never change the result.
    for rx in SOFT_VENUE_RE:
        if rx.search(blob):
            return True
    return False


def is_commercial(event):
    """Return (flag, reason). flag is True when a pub/meal-deal promo is
    detected; reason is a short machine-readable tag explaining the match.
    """
    name = event.get("name") or ""
    desc = event.get("description") or ""
    loc = event.get("location") or ""
    addr = event.get("address") or ""
    text = f"{name}\n{desc}"
    venue_blob = f"{loc} {addr}"

    for rx in STRONG_RE:
        if rx.search(text) or rx.search(venue_blob):
            return True, f"strong:{rx.pattern}"

    venue = _venue_hit(loc, addr)
    priced = _has_price(event)
    has_trivia = bool(TRIVIA_RE.search(text))

    # User rule: trivia is commercial only when priced,
    # or when hosted at a commercial venue (pub trivia).
    if has_trivia and (venue or _has_solid_price(event)):
        # `venue` short-circuits, so naming pricing in the same reason string
        # would claim a check that never ran.
        return True, "trivia+venue" if venue else "trivia+priced"

    # Weak venue terms need corroboration: priced meal or RSL+meal context.
    if venue:
        meal_ctx = re.search(
            r"lunch|dinner|meal|bistro|trivia|raffle|happy|special|\$",
            text, re.I,
        )
        if priced or meal_ctx:
            return True, "venue+meal-context"

    # RSL in text + meal/priced context (venue may be blank, e.g. bayside_live)
    if RSL_RE.search(text) and (priced or re.search(
            r"meal|bistro|dinner|lunch|trivia|raffle|happy", text, re.I)):
        return True, "rsl+meal-context"

    return False, ""


if __name__ == "__main__":
    # (event, expected flag). Reasons are the machine-readable tags above;
    # asserting the flag alone would let a classifier start returning a
    # different reason for the same verdict and go unnoticed. These are the
    # documented invariants from the module docstring, plus the false
    # positives that the word-bounded allowlist was written to prevent.
    community = {"location": "Beaumaris Library, 96 Reserve Road, Beaumaris VIC 3193",
                 "address": "96 Reserve Road, Beaumaris"}
    tests = [
        # Strong food/deal keywords count outright, with no corroboration.
        ({"name": "Parmas for Seniors", "description": "Free Parma lunch."},
         True, "strong:"),
        ({"name": "Steak Night", "price_text": "$25"}, True, "strong:"),
        ({"name": "Happy Hour", "description": "Half price drinks."},
         True, "strong:"),
        ({"name": "Members Draw", "description": "Monthly draw."},
         True, "strong:"),

        # A hard venue word is a precondition, not a verdict: the event still
        # needs a price or a meal context. Parkview Tavern is detected as a
        # denylisted venue (it is not rescued by the allowlist), and trivia at
        # it is commercial on the venue alone.
        ({"name": "Trivia Night", "description": "Every Tuesday.",
          "location": "Parkview Tavern"}, True, "trivia+venue"),
        ({"name": "Social Night", "description": "Come along.",
          "location": "Parkview Tavern"}, False, ""),
        ({"name": "Quiz", "description": "Prizes to be won.",
          "location": "Parkview Tavern", "price_text": "$10"},
         True, "venue+meal-context"),

        # A soft venue word IS rescued by a community venue string, until
        # something corroborates it.
        ({"name": "Social Night", "description": "Bring a friend.",
          "location": "Chelsea Community Centre (Sports Bar)"}, False, ""),
        ({"name": "Social Night", "description": "Bring a friend, $20 entry.",
          "location": "Chelsea Community Centre (Sports Bar)",
          "price_text": "$20"}, True, "venue+meal-context"),

        # "pub" is hard, but the public/publishing scrub keeps prose safe.
        ({"name": "Pubs with a Past", "description": "A heritage talk.",
          "location": "Mordialloc Library"}, False, ""),
        ({"name": "Public Library Talk", "description": "Published history.",
          "location": "Chelsea Library"}, False, ""),

        # Trivia: commercial only when priced or at a denylisted venue.
        ({"name": "Trivia Night", "description": "Every Tuesday.",
          "location": "Mentone Community Centre"}, False, ""),
        ({"name": "Trivia Night", "description": "Every Tuesday. $5 entry.",
          "price_text": "$5"}, True, "trivia+priced"),
        ({"name": "Trivia Night", "description": "Every Tuesday. $3 entry.",
          "price_text": "$3"}, False, ""),  # too cheap to look commercial

        # Weak venue words need corroboration; gold-coin stays community.
        ({"name": "Innkeeper Talk", "location": "The Green Inn"}, False, ""),
        ({"name": "Innkeeper Talk", "location": "The Green Inn",
          "description": "Includes a roast dinner.", "price_text": "$30"},
         True, "venue+meal-context"),
        ({"name": "Gold Coin Donation", "description": "A gold coin donation.",
          "location": "Chelsea Community Centre"}, False, ""),

        # RSL needs a meal/priced context, and beats the allowlist.
        ({"name": "RSL Bingo", "description": "Eyes down please.",
          "location": "Chelsea RSL"}, False, ""),
        ({"name": "RSL Lunch", "description": "Members meal deal.",
          "location": "Chelsea RSL"}, True, "rsl+meal-context"),

        # Community venues stay community.
        ({"name": "Mahjong", "description": "Learn the game.",
          **community}, False, ""),
        ({"name": "Gentle Exercise", "description": "Tai chi for all.",
          "location": "Rowville Community Centre"}, False, ""),
    ]

    failures = []
    for i, (event, expected, reason_prefix) in enumerate(tests, 1):
        flag, reason = is_commercial(event)
        if flag != expected:
            failures.append(
                f"case {i} {event['name']!r}: expected flag={expected}, "
                f"got flag={flag} (reason={reason!r})")
        elif reason_prefix and not reason.startswith(reason_prefix):
            failures.append(
                f"case {i} {event['name']!r}: expected reason prefix "
                f"{reason_prefix!r}, got {reason!r}")

    if failures:
        print(f"commercial: {len(failures)}/{len(tests)} cases FAILED")
        for f in failures:
            print(f"  FAIL: {f}")
        raise SystemExit(1)
    print(f"commercial: {len(tests)} cases passed")
