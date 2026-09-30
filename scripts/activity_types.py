"""Rule-based activity-type classifier shared by all event sources.

classify_types(name, description, category="") -> list of TYPES.

Multi-tag: children/family is orthogonal to market/musical, so an event
collects every matching rule across both title and description (union).
Results are ordered by TYPES for stable display. ["Other"] iff nothing
matches, never alongside real tags.
"""
import re

TYPES = [
    "Art & Craft",
    "Books & Reading",
    "Children & Families",
    "Dance",
    "Exercise & Fitness",
    "Food & Drink",
    "Games & Cards",
    "Health & Wellbeing",
    "Info Session",
    "Market & Exhibition",
    "Movies & Cinema",
    "Music & Performance",
    "Nature & Environment",
    "Seniors Festival",
    "Social & Community",
    "Sport & Outdoors",
    "Technology",
    "Other",
]

# (type, [regexes]) - all matched case-insensitively. Patterns are anchored
# with \b wherever an unanchored form would also match inside an unrelated
# word (e.g. r"eat " matched "great", r"organ\b" matched "Morgan").
# Rule order is no longer load-bearing for correctness (union collects all
# matches); TYPES order determines display order.
RULES = [
    ("Seniors Festival", [r"seniors? festival", r"senior'?s? festival", r"kingston_seniors", r"bayside_seniors"]),
    ("Movies & Cinema", [r"movie", r"film", r"cinema", r"screening", r"pinocchio"]),
    ("Dance", [r"ballroom danc", r"belly danc", r"line danc", r"\bdanc(e|ing|ers)\b",
               r"ballet", r"jazz class", r"jazz routine", r"broadway jazz",
               r"fiesta latina", r"dancing gear"]),
    ("Music & Performance", [r"concert", r"choir", r"karaoke", r"\bsing\b", r"singing",
                              r"singers", r"melod", r"\bband\b", r"orchestra", r"\bgig\b",
                              r"comedy", r"comedian", r"theatre", r"theater",
                              r"cabaret", r"circus", r"magic show", r"illusionist",
                              r"opera", r"jazz (trio|band|quartet|night|jam|live|"
                              r"session|festival)", r"tenor", r"elvis", r"orbison",
                              r"carol", r"sonantinas", r"mosaic", r"jazzeoke",
                              r"piano", r"\borgan\b", r"battle of the bands",
                              r"wizard of oz", r"musical", r"emma memma",
                              r"drum theatre", r"\bdrum"]),
    ("Exercise & Fitness", [r"\byoga\b", r"pilates", r"zumba", r"\bgym\b", r"gymnastics",
                            r"tai chi", r"qigong", r"\bswim\b", r"swimming\b",
                            r"strength", r"\bstrong", r"\bbalance\b",
                            r"gentle exercis",
                            r"exercis", r"fitness class",
                            r"active adult", r"newcombe", r"movement is medicine",
                            r"move & groove", r"\bmovement\b", r"aerobic",
                            r"body and balance", r"love to live",
                            r"meditation", r"soundbath", r"sound bath",
                            r"mums and bubs", r"posture fit", r"power hour",
                            r"move and connect", r"morning yoga", r"hatha",
                            r"mcclelland.*yoga", r"silver salties yoga",
                            r"self defence", r"self defense",
                            r"casual swim", r"water safety",
                            r"calisthenics", r"holiday fun",
                            # Bare "chair " caught "chair lift" and "folding
                            # chair"; only the fitness senses belong here.
                            r"chair (yoga|based|exercise|movement)"]),
    ("Sport & Outdoors", [r"soccer", r"football", r"netball", r"tennis", r"\bbowls\b",
                          r"lawn bowls", r"indoor bowls", r"croquet", r"\bgolf\b",
                          r"cricket", r"basketball", r"badminton", r"\bwalk\b", r"walking",
                          r"trail", r"hike", r"hiking", r"\bride\b", r"cycling",
                          r"\bcycle\b", r"nature walk", r"beach walk", r"bushwalk",
                          r"\bshed\b",
                          r"table tennis", r"darts", r"sailing", r"kayak",
                          r"fishing", r"petanque", r"afl\b", r"martial art",
                          r"aikido", r"karate", r"judo", r"taekwondo"]),
    ("Info Session", [r"information session", r"info session", r"pension",
                      r"superannuation", r"retirement", r"downsizing",
                      r"accommodation options", r"transport", r"home energy",
                      r"energy upgrades", r"financial", r"budgeting",
                      r"will(s| preparation)", r"estate planning", r"seminar",
                      r"trybooking", r"eventbrite", r"histor", r"heritage",
                      r"\btraining (session|workshop|course|program)\b",
                      r"neighbourhood watch", r"citizenship",
                      r"english language", r"forum"]),
    ("Games & Cards", [r"\bcards?\b", r"mahjong", r"mah jong", r"bridge\b",
                       r"chess", r"scrabble", r"rummikub", r"\b500\b", r"canasta",
                       r"board ?game", r"bingo", r"dungeons", r"dragons",
                       r"role-?play", r"tabletop",
                       r"jigsaw", r"puzzle", r"crafty challenge"]),
    ("Art & Craft", [r"\bpaint", r"(?<!martial )\barts?\b", r"\bcraft", r"\bsew", r"crochet",
                     r"\bknit", r"\bweav", r"\bdraw", r"pottery", r"ceramics",
                     r"colouring", r"coloring", r"diamond art", r"bedazzle",
                     r"mandala", r"printmaking", r"print making", r"sculpture",
                     r"photography", r"choir.*craft", r"\bbasket", r"artvo",
                     r"makerspace", r"artist",
                     r"pom pom", r"seed ball", r"bee.buddies", r"collage",
                     r"paper craft", r"story.*craft", r"spring stories",
                     r"coffee club", r"manga"]),
    # A rule's position here is no longer a correctness lever: the classifier
    # unions every match (see _all_matches), so a term's *wording* decides
    # what it catches, not what precedes it. The entries below are grouped by
    # subject for readability. Where a rule is deliberately narrow (see
    # "Environment", "Nature & Environment"), the comment says what the
    # width buys -- e.g. bare "garden" matched venue names like
    # "Aspendale Gardens", so only the activity sense is listed.
    ("Nature & Environment", [r"\bbirds?\b", r"birdlife", r"shorebird",
                              r"wildfowl", r"wildlife", r"biodivers",
                              r"\bmicrobats?\b", r"pollinat", r"\bflora\b",
                              r"\bfauna\b", r"wetland", r"bushland",
                              r"\bkoalas?\b", r"kangaroo", r"butterfl",
                              r"compost", r"worm farm", r"\bharvest\b",
                              r"meadow", r"\bnature\b", r"\breserve\b",
                              # "environment" alone is ordinary English ("a
                              # relaxed supportive environment"), and "nursery"
                              # is often a venue ("Bay Road Nursery Cafe"), so
                              # both need a qualified form.
                              r"environmental", r"environment (health|science|"
                              r"project|grant|week|day|forum|group)",
                              r"community nursery",
                              # Bare "garden" also matched venue names
                              # ("Aspendale Gardens"), so name the activity.
                              r"community garden", r"garden group",
                              r"gardening", r"garden (tour|visit|work|project)"]),
    # Narrow by wording, not by position: every Chatty Cafe session describes
    # coffee, tea and cake, so the Food & Drink vocabulary must not be a
    # catch-all for the program.
    ("Social & Community", [r"chatty", r"social group",
                            r"cuppa", r"catch ?up",
                            r"newcomers", r"new to the (area|suburb|city)",
                            r"friendship",
                            r"men'?s shed", r"u3a",
                            r"probis", r"rotary", r"lions", r"\brsl\b",
                            r"get together", r"loneliness", r"letterbox", r"\bagm\b", r"ceremony",
                            r"celebration", r"\bparty\b", r"gathering",
                            r"playgroup", r"playspace", r"\bfamil(y|ies)\b",
                            r"justice of the peace", r"bus trip", r"day trip",
                            r"\bouting\b", r"excursion", r"death caf",
                            r"grief", r"bereave", r"friendly fellas",
                            r"fellas", r"men'?s group", r"seniors club"]),
    # Clinical nutrition vocabulary. "Eat Well"/"Dietitian" are the titles a
    # health talk actually carries, and the Food & Drink vocabulary below
    # would otherwise claim them on "eating"/"diet" alone.
    ("Health & Wellbeing", [r"\bdiet(itian)?\b", r"\bnutrition(ist|al)?\b",
                            r"eat well", r"age well", r"eat healthy",
                            r"\bhealthy eating\b", r"wellbeing"]),
    # \beat\b rather than r"eat ": the unanchored form also matched "great",
    # "meat" and "beat", so "a great opportunity" filed an event as food.
    ("Food & Drink", [r"\bcook", r"baking", r"recipe", r"dumpling", r"\bpasta\b", r"bbq",
                      r"barbecue", r"lunch", r"dinner", r"breakfast", r"brunch",
                      r"high tea", r"afternoon tea", r"morning tea",
                      r"picnic", r"\bwines?\b", r"\bbeers?\b", r"baklava", r"\bfoods?\b",
                      r"\bmeals?\b", r"\beat\b", r"dining", r"restaurant", r"cafe\b",
                      r"coffee", r"chocolate"]),
    ("Technology", [r"\btech\b", r"computer", r"cyber", r"digital", r"\bai\b",
                    r"chatgpt", r"artificial intelligence", r"smartphone",
                    r"\btablets?\b", r"ipad", r"ebook", r"e-book", r"internet",
                    r"online safety", r"scam", r"readytechgo", r"stem\b",
                    r"augmented reality", r"merge cube", r"virtual reality",
                    r"\brobot"]),
    # Nutrition lives in the pre-rule above Food & Drink; the rest is here.
    ("Health & Wellbeing", [r"immunis", r"vaccin", r"hearing", r"eye health",
                            r"heart health", r"dementia",
                            r"mental health",
                            r"calm and confident", r"confiden", r"resilien",
                            r"mindful", r"first aid", r"cpr",
                            r"defibrillator", r"carer", r"aged care",
                            r"my aged care", r"health check", r"blood pressure",
                             r"diabetes", r"arthritis", r"falls prevention",
                             r"fall prevention program"]),
    # Narrow by wording, not by position: a child-specific program with a real
    # subject of its own still collects that subject's tag, so "Calm and
    # Confident Kids" is Health & Wellbeing as well as this. Deliberately no
    # \bfamil(y|ies)\b: that is Social & Community's, and "family friendly" is
    # pricing boilerplate, not a children's event.
    ("Children & Families", [r"playwork", r"messy play", r"muddy play",
                             r"\bchild(ren)?\b", r"\bkids?\b",
                             r"school holiday", r"holiday (program|activit|scheme)",
                             r"\bparenting\b", r"neurodivers",
                             r"\btots?\b", r"little ones", r"\bjuniors?\b",
                             r"early years", r"kindergarten"]),
    ("Books & Reading", [r"book club", r"author", r"storytime", r"\breading\b",
                         r"borrowbox", r"podcast", r"storie?s\b", r"writing",
                         r"writers?", r"poetry", r"book week", r"book launch",
                         r"\blibrary\b"]),
    # "gallery" needs an event sense: in a title it is usually the venue
    # ("Sunday Jazz at the Gallery"), which is not a market.
    ("Market & Exhibition", [r"\bmarket", r"exhibition", r"\bfete\b",
                             r"\bfair\b", r"\bstalls?\b",
                             r"biennale", r"gallery (opening|exhibition|night|"
                             r"event|launch|sale)",
                             r"museum", r"art show", r"quilt show", r"fashion"]),
]

_COMPILED = [(t, [re.compile(p, re.I) for p in pats]) for t, pats in RULES]

# Every rule must name a real type, or the UI renders a filter checkbox for a
# category that can never be selected.
_BAD_TYPES = sorted({t for t, _ in RULES} - set(TYPES))
assert not _BAD_TYPES, f"rules reference undeclared types: {_BAD_TYPES}"


def _all_matches(text):
    """Return the set of types matching text (one entry per rule hit)."""
    found = set()
    for etype, patterns in _COMPILED:
        for pat in patterns:
            if pat.search(text):
                found.add(etype)
                break
    return found


def classify_types(name, description="", category=""):
    """Return every applicable type for one event, in TYPES order.

    Union of title (+ category) and description matches, so orthogonal
    facets compose: a kids market is both Children & Families and
    Market & Exhibition; a musical for families is both Music and Children.
    ["Other"] iff nothing matches.
    """
    title = (name or "") + "\n" + (category or "")
    matched = _all_matches(title) | _all_matches(description or "")
    if not matched:
        return ["Other"]
    order = {t: i for i, t in enumerate(TYPES)}
    return sorted(matched, key=lambda t: order.get(t, len(order)))


if __name__ == "__main__":
    # (name, description, category, expected tags in TYPES order). Multi-tag:
    # orthogonal facets compose rather than collapsing to one winner, so
    # expectations list every applicable tag. These assert rather than print.
    tests = [
        ("Mahjong", "Come along to learn the fun game of Mahjong.", "",
         ["Games & Cards"]),
        ("Sunday Jazz at the Gallery", "Live jazz trio.", "",
         ["Music & Performance"]),
        ("Jazz Class (Beginners)", "Learn a beginners Broadway jazz routine.", "",
         ["Dance"]),
        ("AI in Everyday Life Presented by ReadyTechGo", "ChatGPT basics.", "",
         ["Technology"]),
        ("Sing-a-long", "Community singing of 60s pop.", "",
         ["Music & Performance"]),
        ("Basic Tech Help", "One-on-one help at Frankston Library.", "",
         ["Books & Reading", "Technology"]),
        ("Dungeons and Dragons", "Tabletop role playing game.", "",
         ["Games & Cards"]),
        ("Immunisation Session 2026", "Children vaccination.", "",
         ["Children & Families", "Health & Wellbeing"]),
        ("Table Tennis and Darts", "Table tennis or darts.", "",
         ["Sport & Outdoors"]),
        ("Chatty Cafe Frankston", "Casual conversation over coffee.", "",
         ["Food & Drink", "Social & Community"]),
        ("Biketober - Ride, Rate, Win!", "Ride anywhere, rate routes.", "",
         ["Sport & Outdoors"]),
        ("Transport Information Session", "Taxi program, community bus.", "",
         ["Info Session"]),
        # Orthogonal facets compose: food + market + music + social.
        ("Sakura in Hampton", "High tea ceremony, drumming, stalls.", "",
         ["Food & Drink", "Market & Exhibition", "Music & Performance",
          "Social & Community"]),
        ("Artists in Residence Exhibition", "Showcases artworks created in workshops.",
         "", ["Art & Craft", "Market & Exhibition"]),
        ("Mahjong Open Day", "Learn mahjong; beginners welcome.", "",
         ["Games & Cards"]),
        ("Recording Life Stories", "Podcast workshop on capturing life stories.",
         "", ["Books & Reading"]),
        ("The Thin Blue Line - Police at the Brighton Cemetery",
         "Stories of police buried here.", "", ["Books & Reading"]),
        ("Explore the Solar System with Merge Cube", "STEM and augmented reality.",
         "", ["Technology"]),
        ("Justice of the Peace Mondays", "Free document witnessing service.", "",
         ["Social & Community"]),
        ("Seniors Aikido Demonstration", "Gentle Japanese martial art.", "",
         ["Sport & Outdoors"]),
        ("Bus Trip", "Day trip to RAAF Museum with lunch.", "",
         ["Food & Drink", "Market & Exhibition", "Social & Community"]),
        ("Death Cafe", "Talk openly about death and dying.", "",
         ["Food & Drink", "Social & Community"]),
        ("Fashion Parade", "Latest fashion by Postie.", "", ["Market & Exhibition"]),
        ("Lyrebird Playgroup", "Parents and children play and connect.", "",
         ["Children & Families", "Social & Community"]),
        ("Calm and Confident Kids", "Confidence and self-regulation for children.",
         "", ["Children & Families", "Health & Wellbeing"]),
        ("Stronger Me", "Stay active, feel stronger.", "", ["Exercise & Fitness"]),
        ("PlaySpace", "Children and parents enjoy time together.", "",
         ["Children & Families", "Social & Community"]),
        ("Move and Connect", "Low impact exercise class set to music.", "",
         ["Exercise & Fitness"]),
        ("Chatty Cafe Frankston", "Casual conversation over coffee.", "",
         ["Food & Drink", "Social & Community"]),
        ("Chatty Cafe - Game On!", "Free morning tea with trivia and games.", "",
         ["Food & Drink", "Social & Community"]),
        ("Making Healthy Dumplings Masterclass",
         "Make dumplings; community group.", "", ["Food & Drink"]),
        ("Centenarians Celebration", "Special luncheon honouring centenarians.",
         "", ["Food & Drink", "Social & Community"]),
        # Chatty Cafe is both social and food (coffee/tea in description).
        ("Chatty Cafe Frankston", "Casual conversation over coffee.", "",
         ["Food & Drink", "Social & Community"]),
        # "training" in the blurb does not add an Info tag to a craft title.
        ("Watercolour Painting Workshop", "Includes training provided.", "",
         ["Art & Craft"]),
        ("Gallery Opening Night", "Come along to the exhibition launch.", "",
         ["Market & Exhibition"]),
        # A bare "support skills" course is not an information session
        ("Everyday Support Skills", "Practical skills for daily life.", "",
         ["Other"]),
        ("", "", "", ["Other"]),

        # --- regressions: substring patterns that matched inside other words ---
        # r"eat " matched "great": a French conversation night is not a meal.
        ("French Lounge", "Francais? No pressure, just a great opportunity to "
         "listen and meet others.", "", ["Other"]),
        # r"organ\b" matched "Janis Morgan" and filed an art workshop as music.
        ("Portrait Painting with Janis Morgan",
         "Term 4 bookings with an experienced artist.", "",
         ["Art & Craft"]),
        # Cooking class plus "whole family" boilerplate (Social via famil).
        ("Pasta Masterclass", "Learn fresh pasta. Great for the whole family.",
         "", ["Food & Drink", "Social & Community"]),
        # "all welcome" is boilerplate, not a social program.
        ("Tech Help Desk", "Drop in for one-on-one help. All welcome.", "",
         ["Technology"]),
        # Netball (Sport) plus "balance" drill (Exercise) both apply.
        ("FunNet for 7-9 year olds - Beginner Netball Skills",
         "Practice your netball skills and balance in a circle.", "",
         ["Exercise & Fitness", "Sport & Outdoors"]),
        # Nutrition talk is both food and health.
        ("Eat Well, Age Well with Joel Feren",
         "Practising Dietitian Joel Feren on eating well as you age.", "",
         ["Food & Drink", "Health & Wellbeing"]),
        # Zumba is exercise set to dance/music wording.
        ("Zumba", "A fun dance class set to music.", "",
         ["Dance", "Exercise & Fitness"]),
        # Title-free row still uses the description (art exhibition).
        ("'Refugia' by Kerri Wilson McConchie",
         "A multi-disciplinary exhibition of photographs and drawings.", "",
         ["Art & Craft", "Market & Exhibition"]),

        # --- Nature & Environment ---
        ("Wild In Bayside - Shorebirds and Migration",
         "An expert guide to the birds of the bay.", "",
         ["Nature & Environment"]),
        ("Black gold! A composting and worm farming presentation",
         "How to keep a worm farm happy.", "", ["Nature & Environment"]),
        ("Community Garden Group", "Dig, plant and share the harvest.", "",
         ["Nature & Environment"]),
        # Art depicting flora/wildlife is both art and nature-themed.
        ("'Refugia' by Kerri Wilson McConchie",
         "Collage of indigenous flora projected onto a wildlife corridor.",
         "", ["Art & Craft", "Nature & Environment"]),
        # "nursery" and "environment" are venue names / ordinary English.
        ("Chatty Cafe - Bay Road Nursery Cafe",
         "Chatty Cafe at Bay Road Nursery Cafe.",
         "", ["Food & Drink", "Social & Community"]),
        ("Everyday Conversation - Beginner, Intermediate, and Advanced classes",
         "Speaking practice in a relaxed supportive environment.", "", ["Other"]),
        # "Gardens" in a venue name is not a gardening event.
        ("Cranbourne Gardens in late Spring", "Open day at the gardens.", "",
         ["Other"]),

        # --- Children & Families ---
        ("Palm Plaza Playwork Free Activities", "For young children.", "",
         ["Children & Families"]),
        ("RSPCA Dog Safety Workshop - School Holiday Activities",
         "Keeping kids and dogs safe.", "", ["Children & Families"]),
        # A child-specific health program is both.
        ("Calm and Confident Kids",
         "Confidence and self-regulation for children.", "",
         ["Children & Families", "Health & Wellbeing"]),
        # Dance + food (baklava/afternoon tea) + family audience (Social).
        ("Belly Dance and Baklava Afternoon tea", "Bring the whole family.", "",
         ["Dance", "Food & Drink", "Social & Community"]),
    ]
    failures = []
    for n, d, c, expected in tests:
        got = classify_types(n, d, c)
        flag = "ok " if got == expected else "BAD"
        if got != expected:
            failures.append((n, expected, got))
        print("%s %-45s -> %s" % (flag, n, got))
    if failures:
        print("\n%d classification failure(s):" % len(failures))
        for n, expected, got in failures:
            print(f"  {n!r}: expected {expected!r}, got {got!r}")
        raise SystemExit(1)
    print(f"\nall {len(tests)} classifications as expected")
