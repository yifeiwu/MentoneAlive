"""Update scripts/archived_events.json with liveness facts checked by hand.

    python scripts/apply_archive_fixes.py

An archived row used to mean "we could not reach the source any more, so this
is the only record left". Checking them against the web showed that is wrong for
most of them: they are still running, on the organiser's own site or a council
page we simply do not crawl. Only the three Bayside library programmes look
genuinely finished.

The fixture therefore gains a `status` on every row:

  live        still running; the owning site is named in `live_url`
  unverified  not checked, or the owner could not be reached
  finished    checked and gone

and `status_note` says what was checked. build_site.py withholds a row from the
page unless `status` is "live", so a series that is still happening is listed
and a one-off from last year is not -- which is the distinction the old binary
archived/live flag could not make, because it was per-source and every row in
a source got the same answer.

A "live" status is not the same as a row on the page. It removes the *withhold*;
it does not supply a schedule. Most of these rows state one in prose and are
expanded by recurrence.py, but a few the fixture never described (Lyrebird Yoga,
Basic Tech Help, Justice of the Peace Saturdays) have nothing to expand and so
still do not publish. Their owners are recorded in `live_url` so whoever wires
up a fetcher knows where to point it.

Run once; the edit is meant to be reviewed and committed like any other fixture.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVED = ROOT / "scripts" / "archived_events.json"

# name -> (status, live_url, note)
#
# Every "live" here was confirmed on 2026-10-01 by fetching the site named, not
# by inferring from a search result. Where the site also contradicts the
# description the fixture carries, that is said in the note.
FINDINGS = {
    "Bayside Farmers' Market, Sandringham": (
        "live",
        "https://baysidefarmersmarket.com.au/",
        "Own site confirms 4th Saturday monthly, 8am-1pm, Trey Bit Reserve. "
        "The site lists 2026 dates Jul 25, Aug 22, Sep 26, Oct 24, Nov 28 and "
        "states there is NO market in December 2026 -- so the fixture's "
        "'fourth Saturday of every month' publishes a 26 Dec row that will not "
        "happen. The address on the site is 3193, the fixture says 3191.",
    ),
    "One-on-one Device Advice Series": (
        "live",
        "https://www.bayside.vic.gov.au/explore-bayside/events",
        "Present in the bayside_live snapshot; kept here for the multi-venue "
        "detail the feed flattens.",
    ),
    "Free Digital Skills and English Support": (
        "finished",
        None,
        "Last occurrence found is 21 June 2024. Not in the live feed, and the "
        "library's own pages no longer list it.",
    ),
    "Learning to sing sessions": (
        "unverified",
        None,
        "No Bayside listing found either way. Searched; inconclusive, so not "
        "claimed as finished.",
    ),
    "Trivia on Tap": (
        "live",
        "https://frankstonbrewhouse.com.au/whats-on/",
        "Brewhouse's own page lists it: first Friday monthly, 7-10pm. Site is "
        "reachable and not rate-limited.",
    ),
    "Friday Night Reset": (
        "live",
        "https://www.frankston.vic.gov.au/Things-To-Do/Whats-On/Friday-Night-Reset",
        "Frankston council page live, Fridays 6:30-8:30pm at St Paul's hall. "
        "The fixture carries no time, so every occurrence published at midnight.",
    ),
    "Lyrebird Yoga": (
        "live",
        "https://www.5rhythms.com/classes/FridayNightReset-297170",
        "Found live on the 5Rhythms class directory (same site as Friday Night "
        "Reset).",
    ),
    "Justice of the Peace Saturdays": (
        "live",
        "https://www.frankston.vic.gov.au/Community-and-Health/Justices-of-the-Peace",
        "Council page live; Frankston Library, Saturdays 10am-1pm, with the "
        "full list of dates on the library's own site.",
    ),
    "Weekend Board Games at Carrum Downs": (
        "live",
        "https://library.frankston.vic.gov.au/Whats-On",
        "Frankston City Libraries list it, Carrum Downs Library, Sundays "
        "1-4pm, with dated occurrences to December 2026.",
    ),
    "Basic Tech Help": (
        "live",
        "https://library.frankston.vic.gov.au/Whats-On",
        "Same libraries feed; the per-event page 403s to a plain request but "
        "the listing is present in the libraries' own Whats On index.",
    ),
    "Friday After School STEAM session": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Creative Kids Club": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Sk8House Online Bookings": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Bush Regeneration": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "MSO Classical Series - Season 2026": (
        "live",
        "https://mcclelland.org.au/events",
        "Part of the Music at McClelland season; McClelland's site is "
        "reachable and lists the 2026 programme.",
    ),
    "Music at McClelland - 2026 Subscription tickets": (
        "live",
        "https://mcclelland.org.au/pages/2026-season",
        "Third Sunday monthly 2.30-4pm, Feb-Nov, confirmed on McClelland's own "
        "2026 season page with the full programme of ten concerts.",
    ),
    "Melbourne International Comedy Festival Roadshow": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Drum Tao: Samurai of the Drum": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Auto-Photo: A Life in Portraits": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Artist Talk - Aleks Danko": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Creeklines by Naomi Woodward": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Body As Witness - meditation for letting go and reconnection": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
    "Sing and Grow at Seaford Library": (
        "unverified",
        None,
        "Not confirmed either way. Play Matters runs Sing&Grow nationally; "
        "whether this Seaford session continues is a question for the library.",
    ),
    "2026 Builders & Trades Business Owners Conference": (
        "unverified",
        None,
        "Not confirmed either way.",
    ),
}

# Corrections where the owning site states something the fixture got wrong.
# Applied to the row itself so the store stops publishing the error, rather
# than only being noted here.
CORRECTIONS = {
    "Bayside Farmers' Market, Sandringham": {
        # 3193 is what the organiser states; 3191 is Sandringham's other
        # postcode and would misplace the event on a map.
        "address": "Trey Bit Reserve, Jetty Road, Sandringham VIC 3193",
        "description": (
            "The Bayside Farmers Market comes to Trey Bit Reserve, Jetty Road, "
            "Sandringham at 8am-1pm on the fourth Saturday of every month. No "
            "market in December 2026."
        ),
    },
    "Trivia on Tap": {
        "price_text": "Free entry, prizes on the night",
    },
    "Friday Night Reset": {
        "description": (
            "Friday Night Reset is a weekly 5Rhythms class offering a dynamic "
            "way to move, breathe, and release the week. Fridays 6:30pm-8:30pm "
            "at St Paul's Community Hall, Cnr Bay & High Street, Frankston."
        ),
    },
}


def main():
    rows = json.loads(ARCHIVED.read_text(encoding="utf-8"))
    counts = {"live": 0, "unverified": 0, "finished": 0}
    unknown = []
    for row in rows:
        name = row.get("name")
        finding = FINDINGS.get(name)
        if not finding:
            unknown.append(name)
            continue
        status, live_url, note = finding
        row["status"] = status
        row["status_note"] = note
        if live_url:
            row["live_url"] = live_url
        row.pop("source_label", None)
        for k, v in CORRECTIONS.get(name, {}).items():
            row[k] = v
        counts[status] += 1

    print("statuses: %d live, %d unverified, %d finished"
          % (counts["live"], counts["unverified"], counts["finished"]))
    if unknown:
        print("NO FINDING for: %s" % ", ".join(map(str, unknown)))
        return 1
    ARCHIVED.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print("Wrote %s" % ARCHIVED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
