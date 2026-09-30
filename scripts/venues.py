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
    That was the intent of a separate UNKNOWN_VENUE_WORDS tuple that nothing
    ever read -- the behaviour was already right, by this route rather than by
    the constant, so the constant has been deleted rather than wired up.
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
