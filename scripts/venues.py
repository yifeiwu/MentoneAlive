"""Reading a place out of an event row: online, and which suburb.

Two questions about "where", both asked by more than one module and owned by
neither. `is_online()` decides whether a row needs a street address at all --
`webfetch_granicus.py` drops a listing the source gives no venue for, and
`health_check.py` fails the build on a published row that has none, so if the
two disagreed the fetcher would keep publishing a row the check then rejects.
`extract_suburb()` reads the gazetted locality out of an address string, for
the suburb filter and the page's filter checkboxes.

Both live here rather than in either caller because both are needed by the fetch
layer and the render layer, and putting them in either one makes the other
import across that boundary. That was a live inversion: `extract_suburb` lived
in build_site.py (rendering) and `fetch_urllib_sources.py` imported it to run
a catchment filter, which is the same layering reversal D2a exists to prevent.
"""
import re

from vic_suburbs import canonical_suburb

# Venue strings that mean "not in a room". Matched case-insensitively against the
# whole location, so "Online via Zoom" is exempt too, not just "online".
ONLINE_VENUE_WORDS = ("online", "zoom", "webinar", "virtual", "livestream",
                      "livestreamed", "remote", "teams")

# The state token that follows the suburb in an Australian address. The old
# postcode fallback matched the last word before the postcode, which for
# "..., Beaumaris, Victoria 3193" is the *state* -- so 131 rows got a suburb
# of "Victoria". Suburbs that are also state names are not a thing in VIC, so
# treating these as non-suburbs is safe.
_STATE_TOKENS = {"victoria", "vic", "vics", "australia", "nsw", "new south wales",
                 "queensland", "qld", "sa", "south australia", "tas", "tasmania",
                 "nt", "wa", "western australia", "act"}

# The road-type words an Australian address line can end with.
#
# Shared with `webfetch_granicus.py`, which rejects a listing's "address" that is
# really an event title by requiring a street word. The two lists had drifted,
# so a road type could be a street to the fetcher and a suburb to the renderer
# for the same address.
#
# It deliberately does NOT live in webfetch_http.py with the other shared
# helpers. That module is the fetch layer: a browser-impersonating session, a
# retry policy, the detail crawl and the progress reporter. The render layer
# should not have to import any of that to read a suburb out of an address.
#
# A `|`-joined alternation rather than a list, because both callers splice it
# straight into a pattern.
STREET_SUFFIX_WORDS = (
    "Road|Rd|Street|St|Avenue|Ave|Highway|Hwy|Parade|Pde|Drive|Dr|Lane|"
    "Ln|Place|Pl|Square|Sq|Terrace|Court|Ct|Boulevard|Blvd|Walk|"
    "Crescent|Cres|Close|Way|Trail|Parkway|Circuit|Cct|Promenade|Prom|"
    "Esplanade"
)

_STREET_TAIL = re.compile(
    r"\b(" + STREET_SUFFIX_WORDS + r")b\.?$".replace("b", r"\b"), re.I)


def _is_street(seg):
    """True when a comma-segment is a street/road name, not a suburb."""
    return bool(_STREET_TAIL.search((seg or "").strip()))


def is_online(location):
    """True when the location says the event is not held in a physical room.

    Empty is deliberately *not* online. A blank location is a missing venue, and
    a missing venue is the thing this module exists to catch, so treating it as
    an exemption would hide the exact defect the check is for.

    A placeholder venue ("TBC", "To be confirmed") is likewise *not* online.
    It is also not a licence to publish: it falls through to `needs_address()`
    returning True, so the row is held to the same address rule as any other.
    That is the behaviour a separate UNKNOWN_VENUE_WORDS tuple used to be
    asked for; the behaviour was already right by this route rather than by the
    constant, so the constant was deleted rather than wired up.
    """
    loc = (location or "").strip().lower()
    if not loc:
        return False
    return any(re.search(rf"\b{re.escape(word)}\b", loc)
               for word in ONLINE_VENUE_WORDS)


def needs_address(row):
    """True when the row must carry a street address to be publishable.

    `row` is an event dict, so a source can call this on its own output before
    the row is stored, not just the verifier on a published row.
    """
    return not is_online(row.get("location"))


def extract_suburb(address):
    """Extract Australian suburb from an address string.

    Anchored on the postcode, which is the reliable token, and reads the
    token(s) immediately before it. The previous version matched the last
    word before the postcode, so a well-formed address such as
    "Beaumaris Library, 96 Reserve Road, Beaumaris, Victoria 3193" yielded
    "Victoria" -- the state -- as the suburb. Reading the whole segment
    before the state (and refusing state names outright) fixes that.

    The bare-postcode fallback is anchored to the end of the string and only
    accepts a Victorian postcode (3xxx). Unanchored, any four digits --
    a street number ("1218 Nepean Highway"), a year ("workshops during
    2026") or a phone fragment ("Community Connections 1300") -- read as
    a postcode and the comma-segment before it published as a suburb.

    Every candidate is validated against vic_suburbs.KNOWN_SUBURBS (gazetted
    Victorian localities for the catchment). An unknown candidate returns ""
    rather than publishing a venue name or parsing artefact as a suburb; the
    health check then names the newcomer so a real locality can be added.
    """
    a = (address or "").strip()
    if not a:
        return ""

    def placeable(seg):
        return (seg and seg.lower() not in _STATE_TOKENS
                and not _is_street(seg) and not re.search(r"\d", seg))

    # "... VIC 3192" / "... Victoria 3193" -> capture everything before the
    # state token. Non-greedy avoids a trailing comma in the capture, and
    # since the match must end at the state+postcode there is only one place
    # it can anchor, so first vs last is not the issue.
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\.?\s+(3\d{3})\b", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        return canonical_suburb(seg) if placeable(seg) else ""

    # "..., Frankston, VIC" -- state present, no postcode. Same rule.
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\s*$", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        return canonical_suburb(seg) if placeable(seg) else ""

    # No state token at all. Fall back to a bare postcode, but only when it
    # ends the address and looks like a Victorian postcode -- "Patterson Lakes
    # Community Centre, 2-30 Thompson Rd, Patterson Lakes 3198" ends in the
    # postcode and its last token is a street, so guard against that. Without
    # the end anchor and the 3xxx requirement, a house number ("Cheltenham
    # Hall, 1218 Nepean Highway, Cheltenham"), a year ("..., workshops
    # during 2026") or a phone fragment ("..., Community Connections 1300")
    # all read as postcodes.
    m = re.search(r"^(.+?)[,\s]+(3\d{3})\s*[.,]?\s*(?:,\s*Australia\s*)?$", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        return canonical_suburb(seg) if placeable(seg) else ""

    # No postcode or state at all, but a street plus a trailing suburb:
    # "Cheltenham Hall, 1218 Nepean Highway, Cheltenham" states the suburb
    # plainly. Accept the last comma segment when an earlier segment looks
    # like a street and the last looks like a suburb (no digits, not a
    # state, not a street). Without the street requirement, a bare venue
    # name ("Kingston Arts Centre") would publish as its own suburb; without
    # the no-digits requirement, a title plus a year ("..., workshops
    # during 2026") would do the same.
    segs = [s.strip().strip(".") for s in re.split(r",", a) if s.strip()]
    if len(segs) >= 2:
        last = segs[-1]
        if (2 <= len(last) <= 40 and last.lower() not in _STATE_TOKENS
                and not _is_street(last) and not re.search(r"\d", last)
                and re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", last)
                and any(re.match(r"^\s*\d", s) or _is_street(s)
                        for s in segs[:-1])):
            return canonical_suburb(last)
    return ""
