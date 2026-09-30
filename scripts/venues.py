"""What counts as an event held online.

An event needs somewhere to go, so every published row carries a street
address. The one exemption is an event held online: it has no venue and no
suburb to be in or out of a catchment. That exemption is the only way a row is
allowed to publish without an address.

It lives here rather than in one of the two callers because both need it and
neither should own it: `webfetch_granicus.py` drops a listing the source gives
no venue for, and `health_check.py` fails the build on a published row that has
neither. If the two disagreed, the fetcher would keep publishing a row the check
then rejects.
"""

# Venue strings that mean "not in a room". Matched case-insensitively against the
# whole location, so "Online via Zoom" is exempt too, not just "online".
ONLINE_VENUE_WORDS = ("online", "zoom", "webinar", "virtual", "livestream",
                      "livestreamed", "remote", "teams")

def is_online(location):
    """True when the location says the event is not held in a physical room.

    Empty is deliberately *not* online. A blank location is a missing venue, and
    a missing venue is the thing this module exists to catch, so treating it as
    an exemption would hide the exact defect the check is for.

    A placeholder venue ("TBC", "To be confirmed") is likewise *not* online.
    It is also not a licence to publish: it falls through to `needs_address()`
    returning True, so the row is held to the same address rule as any other.
    That is the behaviour a separate UNKNOWN_VENUE_WORDS tuple used to be
    asked for; the behaviour was already right by this route rather than by
    the constant, so the constant was deleted rather than wired up.
    """
    loc = (location or "").strip().lower()
    if not loc:
        return False
    return any(word in loc for word in ONLINE_VENUE_WORDS)


def needs_address(row):
    """True when the row must carry a street address to be publishable.

    `row` is an event dict, so a source can call this on its own output before
    the row is stored, not just the verifier on a published row.
    """
    return not is_online(row.get("location"))


# The road-type words an Australian address line can end with.
#
# Like the rule above, it lives here because two modules need it and neither
# should own it: `webfetch_granicus.py` rejects a listing's "address" that is
# really an event title by requiring a street word, and `build_site.
# extract_suburb()` refuses to read one of these as a suburb. The two lists had
# drifted, so a road type could be a street to the fetcher and a suburb to the
# renderer for the same address.
#
# It deliberately does NOT live in webfetch_http.py with the other shared
# helpers. That module is the fetch layer: a browser-impersonating session, a
# retry policy, the detail crawl and the progress reporter. build_site.py is
# the render layer and should not have to import any of that to read a suburb
# out of an address, and venues.py is already the dependency-free module for
# shared place-string decisions.
#
# A `|`-joined alternation rather than a list, because both callers splice it
# straight into a pattern.
STREET_SUFFIX_WORDS = (
    "Road|Rd|Street|St|Avenue|Ave|Highway|Hwy|Parade|Pde|Drive|Dr|Lane|"
    "Ln|Place|Pl|Square|Sq|Terrace|Court|Ct|Boulevard|Blvd|Walk|"
    "Crescent|Cres|Close|Way|Trail|Parkway|Circuit|Cct|Promenade|Prom|"
    "Esplanade"
)
