"""Gazetted Victorian localities for the catchment, as a validation list.

Why a curated list and not the full Vicmap Admin download:

- The authoritative source (Vicmap Admin Locality Polygon, via Data.Vic)
  ships as GIS polygons (SHP/GDB/DWG) with ~3000 bounded localities. It is
  the right answer for a geocoder, but vendoring it here would add a large
  binary artefact plus an update cadence this repo cannot honour.
- This pipeline only publishes south-east Melbourne (Kingston, Bayside,
  Glen Eira / Port Phillip fringe, Greater Dandenong Springvale/Keysborough,
  Frankston archivals). A ~90-name curated set covers every suburb published
  today plus its immediate neighbours, stays reviewable in a diff, and works
  offline in checks.py / GHA with no new dependency.

When to extend: health_check.py fails the build on a non-empty suburb that
is not in this set, naming the newcomer. If it is a real gazetted locality
(check VICNAMES / maps.land.vic.gov.au), add it here in alphabetical order.
If it is a venue name or parsing artefact, fix the extractor instead.

Names are canonical Title Case as gazetted (e.g. "Brighton East", not
"Brighton east"). Lookup is case-insensitive via canonical_suburb().
"""

# Canonical gazetted names. Grouped by LGA-ish area for curation, sorted
# alphabetically within each group.
KNOWN_SUBURBS = frozenset({
    # City of Kingston (core catchment)
    "Aspendale",
    "Aspendale Gardens",
    "Bonbeach",
    "Braeside",
    "Carrum",
    "Chelsea",
    "Chelsea Heights",
    "Cheltenham",
    "Clarinda",
    "Clayton South",
    "Dingley Village",
    "Edithvale",
    "Heatherton",
    "Highett",
    "Mentone",
    "Moorabbin",
    "Mordialloc",
    "Oakleigh South",
    "Parkdale",
    "Patterson Lakes",
    "Waterways",
    # City of Bayside
    "Beaumaris",
    "Black Rock",
    "Brighton",
    "Brighton East",
    "Hampton",
    "Hampton East",
    "Sandringham",
    # Glen Eira / Port Phillip / Stonnington fringe
    "Bentleigh",
    "Bentleigh East",
    "Carnegie",
    "Caulfield",
    "Caulfield East",
    "Caulfield North",
    "Caulfield South",
    "Elsternwick",
    "Elwood",
    "Gardenvale",
    "Glen Huntly",
    "McKinnon",
    "Murrumbeena",
    "Ormond",
    "St Kilda",
    "St Kilda East",
    # City of Greater Dandenong (catchment is Springvale/Keysborough, but
    # neighbours appear in addresses and must validate rather than vanish.
    # Cleveland, Notting Hill and Rowville are inside the Greater Dandenong
    # `suburb_filter`, so a listing there must resolve to a suburb rather than
    # an empty string.)
    "Bangholme",
    "Cleveland",
    "Dandenong",
    "Dandenong North",
    "Dandenong South",
    "Doveton",
    "Keysborough",
    "Noble Park",
    "Noble Park North",
    "Notting Hill",
    "Rowville",
    "Springvale",
    "Springvale South",
    # Frankston / Casey / Mornington fringe (archivals + neighbours)
    "Carrum Downs",
    "Cranbourne",
    "Frankston",
    "Frankston North",
    "Frankston South",
    "Langwarrin",
    "Seaford",
    "Skye",
    # City of Monash fringe
    "Clayton",
    "Huntingdale",
    "Oakleigh",
    "Oakleigh East",
    # City of Melbourne edge (trail walks etc. state these; validated so
    # they publish rather than empty out)
    "Melbourne",
    "Southbank",
})

# Lower-folded index for case-insensitive lookup that also returns the
# canonical spelling, so "cheltenham" publishes as "Cheltenham".
_CANONICAL = {s.lower(): s for s in KNOWN_SUBURBS}


def canonical_suburb(candidate):
    """Return the gazetted spelling of `candidate`, or "" when unknown.

    Empty/blank in -> "". Leading/trailing space ignored. Comparison is
    case-insensitive; the returned value is always the canonical Title Case
    from KNOWN_SUBURBS, so the events.json suburb column stays stable
    regardless of how a source capitalised the address.
    """
    key = (candidate or "").strip().lower()
    if not key:
        return ""
    return _CANONICAL.get(key, "")


def is_known_suburb(candidate):
    """True when `candidate` is a gazetted locality in the catchment set."""
    return bool(canonical_suburb(candidate))
