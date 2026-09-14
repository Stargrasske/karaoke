"""Artist genre classifier with broad genre taxonomy and multi-source consensus.

Solves the genre ambiguity problem by:
1. Recognizing that genre classification is inherently multi-label and subjective:
   artists naturally span multiple styles (e.g. Eric Clapton is both blues and rock,
   Miles Davis spans cool jazz, modal jazz, and jazz fusion).
2. Decoupling track titles from genre classification:
   Django Reinhardt's "Blues en mineur" is Gypsy Jazz / Swing, NOT Delta Blues,
   even though "Blues" appears in the song title.
3. Grouping 500+ granular subgenres into 16 canonical broad genres for smart search:
   blues, jazz, rock, metal, punk, folk, country, electronic, hip hop, soul,
   reggae, classical, pop, ambient, latin, world.
4. Combining an instant offline curated seed database for top artists with
   polite online crawling (MusicBrainz API and Wikidata API) and local SQLite caching.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from .logger import log

# 16 Canonical Broad Genres for high-level search and filtering
BROAD_GENRES = (
    "blues",
    "jazz",
    "rock",
    "metal",
    "punk",
    "folk",
    "country",
    "electronic",
    "hip hop",
    "soul",
    "reggae",
    "classical",
    "pop",
    "ambient",
    "latin",
    "world",
)

# Granular subgenres explicitly mapped to one or more canonical broad genres
SUBGENRE_TO_BROAD: dict[str, list[str]] = {
    # Blues & hybrids
    "blues": ["blues"],
    "electric blues": ["blues"],
    "chicago blues": ["blues"],
    "delta blues": ["blues"],
    "texas blues": ["blues"],
    "country blues": ["blues", "country"],
    "blues rock": ["blues", "rock"],
    "blues-rock": ["blues", "rock"],
    "soul blues": ["blues", "soul"],
    "rhythm and blues": ["blues", "soul"],
    "rhythm & blues": ["blues", "soul"],
    "r&b": ["soul", "blues"],
    "boogie woogie": ["blues", "jazz"],
    "acoustic blues": ["blues", "folk"],
    "jump blues": ["blues", "jazz"],
    "british blues": ["blues", "rock"],
    "contemporary blues": ["blues"],
    "harmonica blues": ["blues"],
    "piano blues": ["blues"],
    "louisiana blues": ["blues"],
    "memphis blues": ["blues"],
    "swamp blues": ["blues"],
    "piedmont blues": ["blues", "folk"],

    # Jazz & hybrids
    "jazz": ["jazz"],
    "gypsy jazz": ["jazz"],
    "gypsy swing": ["jazz"],
    "jazz manouche": ["jazz"],
    "manouche": ["jazz"],
    "swing": ["jazz"],
    "swing music": ["jazz"],
    "bebop": ["jazz"],
    "hard bop": ["jazz"],
    "cool jazz": ["jazz"],
    "modal jazz": ["jazz"],
    "free jazz": ["jazz"],
    "post-bop": ["jazz"],
    "jazz fusion": ["jazz", "rock"],
    "fusion": ["jazz", "rock"],
    "jazz rock": ["jazz", "rock"],
    "smooth jazz": ["jazz", "pop"],
    "vocal jazz": ["jazz", "pop"],
    "big band": ["jazz"],
    "dixieland": ["jazz"],
    "contemporary jazz": ["jazz"],
    "latin jazz": ["latin", "jazz"],
    "acid jazz": ["jazz", "electronic", "soul"],
    "nu jazz": ["jazz", "electronic"],
    "jazz-funk": ["jazz", "soul"],
    "jazz funk": ["jazz", "soul"],
    "bossa nova": ["latin", "jazz"],

    # Rock & subgenres
    "rock": ["rock"],
    "classic rock": ["rock"],
    "hard rock": ["rock"],
    "psychedelic rock": ["rock"],
    "psychedelic": ["rock"],
    "psych rock": ["rock"],
    "progressive rock": ["rock"],
    "prog rock": ["rock"],
    "art rock": ["rock"],
    "space rock": ["rock", "electronic"],
    "garage rock": ["rock"],
    "alternative rock": ["rock"],
    "alt-rock": ["rock"],
    "indie rock": ["rock"],
    "indie": ["rock", "pop"],
    "post-rock": ["rock", "ambient"],
    "math rock": ["rock"],
    "noise rock": ["rock", "punk"],
    "grunge": ["rock", "punk"],
    "stoner rock": ["rock", "metal"],
    "desert rock": ["rock"],
    "shoegaze": ["rock"],
    "dream pop": ["rock", "pop"],
    "glam rock": ["rock"],
    "rock and roll": ["rock"],
    "rock & roll": ["rock"],
    "rock 'n' roll": ["rock"],
    "rockabilly": ["rock", "country"],
    "southern rock": ["rock", "country"],
    "roots rock": ["rock", "folk"],
    "heartland rock": ["rock"],
    "krautrock": ["rock", "electronic"],
    "industrial rock": ["rock", "electronic"],
    "gothic rock": ["rock", "punk"],
    "goth rock": ["rock", "punk"],
    "goth": ["rock", "punk"],
    "new wave": ["rock", "pop"],
    "dark wave": ["rock", "electronic"],
    "jangle pop": ["rock", "pop"],
    "power pop": ["rock", "pop"],
    "pub rock": ["rock"],
    "instrumental rock": ["rock"],
    "experimental rock": ["rock"],
    "post-punk": ["punk", "rock"],
    "post-punk revival": ["punk", "rock"],
    "proto-punk": ["punk", "rock"],
    "britpop": ["rock", "pop"],

    # Metal
    "metal": ["metal"],
    "heavy metal": ["metal", "rock"],
    "thrash metal": ["metal"],
    "death metal": ["metal"],
    "black metal": ["metal"],
    "doom metal": ["metal"],
    "power metal": ["metal"],
    "speed metal": ["metal"],
    "groove metal": ["metal"],
    "sludge metal": ["metal", "punk"],
    "sludge": ["metal", "punk"],
    "stoner metal": ["metal", "rock"],
    "progressive metal": ["metal", "rock"],
    "prog metal": ["metal", "rock"],
    "symphonic metal": ["metal", "classical"],
    "folk metal": ["metal", "folk"],
    "nu metal": ["metal", "rock", "hip hop"],
    "industrial metal": ["metal", "electronic"],
    "glam metal": ["metal", "rock"],
    "hair metal": ["metal", "rock"],
    "nwobhm": ["metal", "rock"],
    "metalcore": ["metal", "punk"],
    "deathcore": ["metal"],
    "grindcore": ["metal", "punk"],

    # Punk
    "punk": ["punk"],
    "punk rock": ["punk", "rock"],
    "hardcore punk": ["punk"],
    "hardcore": ["punk"],
    "pop punk": ["punk", "pop"],
    "skate punk": ["punk"],
    "crust punk": ["punk"],
    "anarcho-punk": ["punk"],
    "ska punk": ["punk", "reggae"],
    "emo": ["punk", "rock"],
    "screamo": ["punk"],
    "riot grrrl": ["punk"],
    "garage punk": ["punk", "rock"],
    "post-hardcore": ["punk", "rock"],

    # Folk & Singer-Songwriter
    "folk": ["folk"],
    "contemporary folk": ["folk"],
    "traditional folk": ["folk"],
    "folk rock": ["folk", "rock"],
    "indie folk": ["folk", "rock"],
    "anti-folk": ["folk", "punk"],
    "freak folk": ["folk", "rock"],
    "singer-songwriter": ["folk", "pop"],
    "celtic": ["folk", "world"],
    "celtic folk": ["folk", "world"],
    "irish folk": ["folk", "world"],
    "sea shanty": ["folk"],
    "acoustic": ["folk"],

    # Country & Americana
    "country": ["country"],
    "country rock": ["country", "rock"],
    "outlaw country": ["country"],
    "alt-country": ["country", "rock"],
    "alternative country": ["country", "rock"],
    "americana": ["country", "folk"],
    "bluegrass": ["country", "folk"],
    "progressive bluegrass": ["country", "folk"],
    "honky tonk": ["country"],
    "western swing": ["country", "jazz"],
    "nashville sound": ["country", "pop"],
    "bakersfield sound": ["country"],
    "contemporary country": ["country", "pop"],

    # Electronic / Dance
    "electronic": ["electronic"],
    "electronica": ["electronic"],
    "techno": ["electronic"],
    "house": ["electronic"],
    "deep house": ["electronic"],
    "tech house": ["electronic"],
    "acid house": ["electronic"],
    "french house": ["electronic"],
    "electro": ["electronic"],
    "electro house": ["electronic"],
    "idm": ["electronic"],
    "intelligent dance music": ["electronic"],
    "breakbeat": ["electronic"],
    "breakcore": ["electronic"],
    "drum and bass": ["electronic"],
    "drum & bass": ["electronic"],
    "dnb": ["electronic"],
    "jungle": ["electronic"],
    "dubstep": ["electronic"],
    "trance": ["electronic"],
    "psytrance": ["electronic"],
    "goa trance": ["electronic"],
    "downtempo": ["electronic", "ambient"],
    "trip hop": ["electronic", "hip hop"],
    "trip-hop": ["electronic", "hip hop"],
    "glitch": ["electronic"],
    "chiptune": ["electronic"],
    "synthwave": ["electronic", "pop"],
    "retrowave": ["electronic"],
    "industrial": ["electronic", "rock"],
    "ebm": ["electronic"],
    "future bass": ["electronic"],
    "uk garage": ["electronic"],
    "eurodance": ["electronic", "pop"],

    # Hip Hop / Rap
    "hip hop": ["hip hop"],
    "hip-hop": ["hip hop"],
    "rap": ["hip hop"],
    "trap": ["hip hop"],
    "gangsta rap": ["hip hop"],
    "east coast hip hop": ["hip hop"],
    "west coast hip hop": ["hip hop"],
    "southern hip hop": ["hip hop"],
    "conscious hip hop": ["hip hop"],
    "underground hip hop": ["hip hop"],
    "alternative hip hop": ["hip hop"],
    "boom bap": ["hip hop"],
    "hardcore hip hop": ["hip hop"],
    "cloud rap": ["hip hop"],
    "drill": ["hip hop"],
    "grime": ["hip hop", "electronic"],
    "crunk": ["hip hop"],
    "turntablism": ["hip hop"],

    # Soul / Funk / R&B
    "soul": ["soul"],
    "contemporary r&b": ["soul", "pop"],
    "neo-soul": ["soul"],
    "neo soul": ["soul"],
    "funk": ["soul"],
    "motown": ["soul"],
    "northern soul": ["soul"],
    "southern soul": ["soul"],
    "philly soul": ["soul"],
    "psychedelic soul": ["soul", "rock"],
    "gospel": ["soul", "folk"],
    "doo-wop": ["soul", "pop"],
    "disco": ["soul", "pop", "electronic"],
    "p-funk": ["soul"],
    "funk rock": ["soul", "rock"],

    # Reggae & Ska
    "reggae": ["reggae"],
    "roots reggae": ["reggae"],
    "dub": ["reggae"],
    "dancehall": ["reggae"],
    "ska": ["reggae"],
    "rocksteady": ["reggae"],
    "ragga": ["reggae"],
    "lovers rock": ["reggae"],
    "2 tone": ["reggae", "punk"],

    # Classical
    "classical": ["classical"],
    "baroque": ["classical"],
    "romantic": ["classical"],
    "renaissance": ["classical"],
    "medieval": ["classical"],
    "opera": ["classical"],
    "choral": ["classical"],
    "symphony": ["classical"],
    "chamber music": ["classical"],
    "contemporary classical": ["classical"],
    "modern classical": ["classical"],
    "minimalism": ["classical", "ambient"],
    "neoclassical": ["classical"],

    # Pop
    "pop": ["pop"],
    "dance-pop": ["pop", "electronic"],
    "synthpop": ["pop", "electronic"],
    "synth-pop": ["pop", "electronic"],
    "electropop": ["pop", "electronic"],
    "indie pop": ["pop", "rock"],
    "art pop": ["pop", "rock"],
    "baroque pop": ["pop", "rock"],
    "chamber pop": ["pop", "rock"],
    "traditional pop": ["pop", "jazz"],
    "brill building": ["pop"],
    "teen pop": ["pop"],
    "europop": ["pop"],
    "k-pop": ["pop"],
    "j-pop": ["pop"],
    "chanson": ["pop", "folk"],
    "easy listening": ["pop"],
    "lounge": ["pop", "jazz"],

    # Ambient
    "ambient": ["ambient"],
    "dark ambient": ["ambient"],
    "drone": ["ambient"],
    "ambient techno": ["ambient", "electronic"],
    "space music": ["ambient"],
    "new age": ["ambient"],
    "chillout": ["ambient", "electronic"],
    "soundscape": ["ambient"],

    # Latin
    "latin": ["latin"],
    "samba": ["latin"],
    "salsa": ["latin"],
    "tango": ["latin"],
    "flamenco": ["latin", "world"],
    "cumbia": ["latin"],
    "bachata": ["latin"],
    "merengue": ["latin"],
    "latin rock": ["latin", "rock"],
    "latin pop": ["latin", "pop"],
    "tropicalia": ["latin", "rock", "pop"],
    "reggaeton": ["latin", "hip hop"],
    "mambo": ["latin", "jazz"],

    # World
    "world": ["world"],
    "world music": ["world"],
    "afrobeat": ["world", "soul"],
    "highlife": ["world"],
    "rumba": ["world", "latin"],
    "fado": ["world", "folk"],
    "qawwali": ["world"],
    "klezmer": ["world", "folk"],
    "carnatic": ["world", "classical"],
    "hindustani": ["world", "classical"],
    "bhangra": ["world", "pop"],
}

# Curated offline seed dictionary for immediate, zero-latency resolution
# Format: normalized_artist -> (specific_genres, broad_genres)
CURATED_SEED_ARTISTS: dict[str, tuple[list[str], list[str]]] = {
    "django reinhardt": (["gypsy jazz", "jazz", "swing"], ["jazz"]),
    "b.b. king": (["blues", "electric blues", "chicago blues"], ["blues"]),
    "bb king": (["blues", "electric blues", "chicago blues"], ["blues"]),
    "eric clapton": (["blues rock", "blues", "classic rock"], ["blues", "rock"]),
    "frank sinatra": (["vocal jazz", "traditional pop", "swing"], ["jazz", "pop"]),
    "frank zappa": (["experimental rock", "progressive rock", "jazz fusion"], ["rock", "jazz"]),
    "miles davis": (["modal jazz", "cool jazz", "bebop", "jazz fusion"], ["jazz", "rock"]),
    "johnny cash": (["country", "outlaw country", "rockabilly", "folk"], ["country", "folk", "rock"]),
    "jimi hendrix": (["blues rock", "psychedelic rock", "hard rock"], ["rock", "blues"]),
    "the jimi hendrix experience": (["blues rock", "psychedelic rock", "hard rock"], ["rock", "blues"]),
    "muddy waters": (["chicago blues", "delta blues", "blues"], ["blues"]),
    "howlin' wolf": (["chicago blues", "electric blues", "blues"], ["blues"]),
    "howlin wolf": (["chicago blues", "electric blues", "blues"], ["blues"]),
    "stevie ray vaughan": (["blues rock", "texas blues", "electric blues"], ["blues", "rock"]),
    "stevie ray vaughan & double trouble": (["blues rock", "texas blues", "electric blues"], ["blues", "rock"]),
    "buddy guy": (["chicago blues", "electric blues", "blues"], ["blues"]),
    "robert johnson": (["delta blues", "country blues", "blues"], ["blues"]),
    "albert king": (["electric blues", "soul blues", "blues"], ["blues", "soul"]),
    "john lee hooker": (["boogie blues", "delta blues", "electric blues"], ["blues"]),
    "rory gallagher": (["blues rock", "blues", "hard rock"], ["blues", "rock"]),
    "led zeppelin": (["hard rock", "blues rock", "heavy metal"], ["rock", "blues", "metal"]),
    "the rolling stones": (["rock", "blues rock", "roots rock"], ["rock", "blues"]),
    "rolling stones": (["rock", "blues rock", "roots rock"], ["rock", "blues"]),
    "pink floyd": (["progressive rock", "psychedelic rock", "art rock"], ["rock"]),
    "iron maiden": (["heavy metal", "nwobhm"], ["metal", "rock"]),
    "nine inch nails": (["industrial rock", "alternative rock", "industrial"], ["rock", "electronic"]),
    "aphex twin": (["idm", "ambient techno", "drill 'n' bass", "electronic"], ["electronic", "ambient"]),
    "aphex twin (aka afx)": (["idm", "acid techno", "electronic"], ["electronic"]),
    "aphex twin (aka universal indicator)": (["acid techno", "electronic"], ["electronic"]),
    "tom waits": (["experimental rock", "blues", "jazz", "cabaret"], ["rock", "blues", "jazz"]),
    "ella fitzgerald": (["vocal jazz", "swing", "bebop"], ["jazz", "pop"]),
    "nina simone": (["vocal jazz", "soul", "blues", "jazz"], ["jazz", "soul", "blues"]),
    "ray charles": (["r&b", "soul", "blues", "jazz"], ["soul", "blues", "jazz"]),
    "neil young": (["folk rock", "classic rock", "country rock"], ["rock", "folk", "country"]),
    "neil young & crazy horse": (["folk rock", "hard rock", "grunge"], ["rock", "folk"]),
    "neil young & the shocking pinks": (["rockabilly", "rock and roll"], ["rock", "country"]),
    "sonic youth": (["noise rock", "alternative rock", "post-punk"], ["rock", "punk"]),
    "the cure": (["post-punk", "gothic rock", "new wave", "alternative rock"], ["rock", "punk", "pop"]),
    "nirvana": (["grunge", "alternative rock", "punk rock"], ["rock", "punk"]),
    "bob marley": (["roots reggae", "reggae", "ska"], ["reggae"]),
    "bob marley & the wailers": (["roots reggae", "reggae", "ska"], ["reggae"]),
    "black sabbath": (["heavy metal", "doom metal", "hard rock"], ["metal", "rock"]),
    "metallica": (["thrash metal", "heavy metal"], ["metal", "rock"]),
    "slayer": (["thrash metal", "speed metal"], ["metal"]),
    "daft punk": (["french house", "electronic", "disco", "synthpop"], ["electronic", "pop"]),
    "kraftwerk": (["krautrock", "electronic", "synthpop", "techno"], ["electronic", "rock", "pop"]),
    "radiohead": (["art rock", "alternative rock", "electronic"], ["rock", "electronic"]),
    "david bowie": (["glam rock", "art rock", "pop rock", "new wave"], ["rock", "pop"]),
    "queen": (["glam rock", "hard rock", "progressive rock", "pop rock"], ["rock", "pop"]),
    "the beatles": (["pop rock", "psychedelic rock", "rock"], ["rock", "pop"]),
    "the doors": (["psychedelic rock", "blues rock", "acid rock"], ["rock", "blues"]),
    "santana": (["latin rock", "blues rock", "jazz fusion"], ["latin", "rock", "blues", "jazz"]),
    "lou reed": (["glam rock", "art rock", "proto-punk"], ["rock"]),
    "the velvet underground": (["art rock", "proto-punk", "experimental rock"], ["rock", "punk"]),
    "joy division": (["post-punk", "gothic rock"], ["punk", "rock"]),
    "new order": (["synthpop", "post-punk", "dance-rock"], ["electronic", "pop", "rock"]),
    "depeche mode": (["synthpop", "new wave", "dark wave"], ["electronic", "pop", "rock"]),
    "the smiths": (["indie pop", "jangle pop", "post-punk"], ["rock", "pop", "punk"]),
    "pixies": (["alternative rock", "indie rock", "noise rock"], ["rock"]),
    "dinosaur jr.": (["alternative rock", "noise rock", "indie rock"], ["rock"]),
    "dinosaur jr": (["alternative rock", "noise rock", "indie rock"], ["rock"]),
    "mogwai": (["post-rock", "instrumental rock", "ambient"], ["rock", "ambient"]),
    "65daysofstatic": (["post-rock", "math rock", "electronic"], ["rock", "electronic"]),
    "fugazi": (["post-hardcore", "punk rock", "indie rock"], ["punk", "rock"]),
    "motorpsycho": (["progressive rock", "psychedelic rock", "stoner rock"], ["rock"]),
    "ozric tentacles": (["space rock", "psychedelic rock", "ambient"], ["rock", "electronic", "ambient"]),
    "venetian snares": (["breakcore", "idm", "glitch"], ["electronic"]),
    "squarepusher": (["drum and bass", "drill 'n' bass", "idm", "jazz fusion"], ["electronic", "jazz"]),
    "john frusciante": (["alternative rock", "experimental rock", "acoustic"], ["rock", "folk"]),
    "red hot chili peppers": (["funk rock", "alternative rock", "funk metal"], ["rock", "soul"]),
    "primus": (["funk metal", "alternative metal", "experimental rock"], ["metal", "rock", "soul"]),
    "amy winehouse": (["soul", "r&b", "neo-soul", "jazz"], ["soul", "jazz"]),
    "the white stripes": (["garage rock", "blues rock", "punk blues"], ["rock", "blues", "punk"]),
    "white stripes": (["garage rock", "blues rock", "punk blues"], ["rock", "blues", "punk"]),
    "arctic monkeys": (["indie rock", "garage rock", "post-punk revival"], ["rock"]),
    "foo fighters": (["alternative rock", "post-grunge", "hard rock"], ["rock"]),
    "deep purple": (["hard rock", "heavy metal", "blues rock"], ["rock", "metal", "blues"]),
    "van halen": (["hard rock", "heavy metal", "glam metal"], ["rock", "metal"]),
    "king crimson": (["progressive rock", "art rock", "jazz fusion"], ["rock", "jazz"]),
    "al di meola": (["jazz fusion", "latin jazz", "flamenco"], ["jazz", "latin", "world"]),
    "the upsetters": (["dub", "roots reggae", "reggae"], ["reggae"]),
    "lee scratch perry": (["dub", "roots reggae", "reggae"], ["reggae"]),
    "lee 'scratch' perry": (["dub", "roots reggae", "reggae"], ["reggae"]),
    "blondie": (["new wave", "punk rock", "disco", "pop rock"], ["rock", "pop", "punk"]),
    "bonnie \"prince\" billy": (["indie folk", "lo-fi", "americana", "alt-country"], ["folk", "country", "rock"]),
    "bonnie 'prince' billy": (["indie folk", "lo-fi", "americana", "alt-country"], ["folk", "country", "rock"]),
    "bonnie prince billy": (["indie folk", "lo-fi", "americana", "alt-country"], ["folk", "country", "rock"]),
    "jethro tull": (["progressive rock", "folk rock", "hard rock"], ["rock", "folk"]),
    "coldplay": (["pop rock", "post-britpop", "alternative rock"], ["pop", "rock"]),
    "madness": (["ska", "2 tone", "pop rock"], ["reggae", "pop", "rock"]),
    "the smashing pumpkins": (["alternative rock", "grunge", "shoegaze"], ["rock"]),
    "smashing pumpkins": (["alternative rock", "grunge", "shoegaze"], ["rock"]),
    "queens of the stone age": (["stoner rock", "desert rock", "hard rock", "alternative rock"], ["rock"]),
    "kyuss": (["stoner rock", "desert rock", "heavy metal"], ["rock", "metal"]),
    "janis joplin": (["blues rock", "psychedelic rock", "soul"], ["blues", "rock", "soul"]),
    "janis joplin (with the kozmic blues band)": (["blues rock", "soul blues", "psychedelic rock"], ["blues", "soul", "rock"]),
    "j.j. cale": (["blues rock", "roots rock", "americana"], ["blues", "rock", "country"]),
    "j.j. cale & eric clapton": (["blues rock", "blues", "americana"], ["blues", "rock"]),
    "b.b. king & eric clapton": (["blues", "electric blues", "blues rock"], ["blues", "rock"]),
    "albert collins": (["texas blues", "electric blues", "blues"], ["blues"]),
    "otis rush": (["chicago blues", "electric blues", "blues"], ["blues"]),
    "t-bone walker": (["electric blues", "jump blues", "blues"], ["blues", "jazz"]),
    "elmore james": (["chicago blues", "slide guitar blues", "blues"], ["blues"]),
    "koko taylor": (["chicago blues", "electric blues", "blues"], ["blues"]),
    "taj mahal": (["country blues", "acoustic blues", "blues"], ["blues", "folk"]),
    "gary moore": (["blues rock", "hard rock", "heavy metal"], ["blues", "rock", "metal"]),
    "joe bonamassa": (["blues rock", "electric blues", "hard rock"], ["blues", "rock"]),
    "the black keys": (["garage rock", "blues rock", "indie rock"], ["rock", "blues"]),
    "black keys": (["garage rock", "blues rock", "indie rock"], ["rock", "blues"]),
    # Gypsy Jazz / Jazz Manouche
    "bireli lagrene": (["gypsy jazz", "jazz manouche", "jazz fusion"], ["jazz"]),
    "biréli lagrène": (["gypsy jazz", "jazz manouche", "jazz fusion"], ["jazz"]),
    "angelo debarre": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "jimmy rosenberg": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "romane": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "rocky gresset": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "samson schmitt": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "noe reinhardt": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "noé reinhardt": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "yorgui loeffler": (["gypsy jazz", "jazz manouche"], ["jazz"]),
    "florin niculescu": (["gypsy jazz", "jazz violin"], ["jazz"]),
    "oscar aleman": (["swing", "jazz", "choro"], ["jazz", "latin"]),
    "oscar alemán": (["swing", "jazz", "choro"], ["jazz", "latin"]),
    "paul desmond": (["cool jazz", "jazz"], ["jazz"]),
    "caravan palace": (["electro swing", "electronic"], ["electronic", "jazz"]),
    # Rock / Alt / Indie
    "the killers": (["indie rock", "alternative rock", "post-punk revival"], ["rock", "pop"]),
    "twenty one pilots": (["alternative rock", "indie pop"], ["rock", "pop"]),
    "alt-j": (["indie rock", "art rock", "indie pop"], ["rock", "pop"]),
    "alt j": (["indie rock", "art rock", "indie pop"], ["rock", "pop"]),
    "black midi": (["experimental rock", "post-punk", "math rock"], ["rock", "punk"]),
    "zita swoon": (["indie rock", "folk rock"], ["rock", "folk"]),
}


def normalize_artist(artist: str) -> str:
    """Normalize artist string for robust cross-table and cache matching.

    Lowercases, strips extraneous punctuation and extra whitespace.
    """
    if not artist:
        return ""
    # Strip leading/trailing whitespace and quotes
    s = artist.strip().casefold()
    # Normalize unicode quotation marks/apostrophes
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    # Collapse multiple whitespaces
    s = re.sub(r"\s+", " ", s)
    return s


def map_to_broad_genres(genre: str) -> list[str]:
    """Map a granular genre or tag to one or more canonical broad genres.

    Uses dictionary lookup first, then resilient keyword/stem fallback.
    """
    clean = normalize_artist(genre)
    if not clean:
        return []

    # Direct dictionary lookup
    if clean in SUBGENRE_TO_BROAD:
        return list(SUBGENRE_TO_BROAD[clean])

    # Direct match if it's already one of the 16 canonical broad genres
    if clean in BROAD_GENRES:
        return [clean]

    # Stem / keyword fallback heuristics
    broads: set[str] = set()
    if "blues" in clean:
        broads.add("blues")
    if "jazz" in clean or "swing" in clean or "bop" in clean:
        broads.add("jazz")
    if "metal" in clean or "nwobhm" in clean:
        broads.add("metal")
    if "punk" in clean or "hardcore" in clean or "emo" in clean:
        broads.add("punk")
    if "rock" in clean or "grunge" in clean or "shoegaze" in clean:
        broads.add("rock")
    if "country" in clean or "bluegrass" in clean or "americana" in clean or "honky" in clean:
        broads.add("country")
    if "folk" in clean or "acoustic" in clean:
        broads.add("folk")
    if "reggae" in clean or "dub" in clean or "ska" in clean or "dancehall" in clean:
        broads.add("reggae")
    if "soul" in clean or "funk" in clean or "r&b" in clean or "motown" in clean:
        broads.add("soul")
    if "hip hop" in clean or "hip-hop" in clean or "rap" in clean or "trap" in clean or "boom bap" in clean:
        broads.add("hip hop")
    if any(k in clean for k in ("techno", "house", "electro", "trance", "synth", "breakbeat", "idm", "dnb")):
        broads.add("electronic")
    if "ambient" in clean or "drone" in clean or "chillout" in clean or "soundscape" in clean:
        broads.add("ambient")
    if any(k in clean for k in ("classical", "baroque", "symphon", "opera", "choral", "orchestr")):
        broads.add("classical")
    if any(k in clean for k in ("latin", "salsa", "samba", "tango", "bossa", "cumbia", "flamenco")):
        broads.add("latin")
    if any(k in clean for k in ("afrobeat", "celtic", "world", "klezmer", "fado")):
        broads.add("world")
    if "pop" in clean or "chanson" in clean:
        broads.add("pop")

    return sorted(broads)


classify_subgenre_to_broad = map_to_broad_genres


# Last request timestamp to enforce polite rate-limiting for MusicBrainz (max 1 req/sec)
_LAST_MUSICBRAINZ_REQ: float = 0.0


def fetch_musicbrainz_genres(artist: str, timeout: float = 4.0) -> list[str]:
    """Fetch community consensus genre tags for an artist from MusicBrainz.

    Complies with MusicBrainz API policies (User-Agent header and rate limiting).
    """
    global _LAST_MUSICBRAINZ_REQ
    now = time.time()
    elapsed = now - _LAST_MUSICBRAINZ_REQ
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _LAST_MUSICBRAINZ_REQ = time.time()

    query = urllib.parse.quote(f'artist:"{artist}"')
    url = f"https://musicbrainz.org/ws/2/artist/?query={query}&fmt=json&limit=3"
    headers = {
        "User-Agent": "KaraokeBroadGenreClassifier/1.0 (https://github.com/tina/karaoke)",
        "Accept": "application/json",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"MusicBrainz fetch failed for '{artist}': {exc}")
        return []

    artists = data.get("artists") or []
    if not artists:
        return []

    # Pick the top matching artist
    candidate = artists[0]
    tags = candidate.get("tags") or []
    # Sort tags by positive community vote count
    tags.sort(key=lambda t: t.get("count", 0), reverse=True)

    genres: list[str] = []
    for t in tags:
        tag_name = str(t.get("name") or "").strip().lower()
        if not tag_name or t.get("count", 0) <= 0:
            continue
        # Filter out non-genre metadata tags like 'british', 'female vocalist', etc.
        if map_to_broad_genres(tag_name):
            genres.append(tag_name)

    return genres[:6]


def fetch_wikidata_genres(artist: str, timeout: float = 4.0) -> list[str]:
    """Fallback fetch from Wikipedia/Wikidata for musical genre statements."""
    query = urllib.parse.quote(artist)
    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&utf8=&format=json&srlimit=1"
    headers = {
        "User-Agent": "KaraokeBroadGenreClassifier/1.0 (https://github.com/tina/karaoke)",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"Wikipedia search failed for '{artist}': {exc}")
        return []

    search_hits = data.get("query", {}).get("search", [])
    if not search_hits:
        return []

    snippet = search_hits[0].get("snippet", "").lower()
    genres: list[str] = []
    for g in BROAD_GENRES:
        if g in snippet:
            genres.append(g)

    return genres


def classify_artist(
    artist: str,
    conn: Optional[sqlite3.Connection] = None,
    *,
    online: bool = True,
) -> tuple[list[str], list[str]]:
    """Resolve an artist's consensus specific genres and canonical broad genres.

    Resolution pipeline:
    1. Check SQLite `artist_genres` table.
    2. Check curated offline seed list.
    3. If online=True, fetch from MusicBrainz API (fallback to Wikipedia).
    4. Save results to SQLite cache.

    Returns:
        (specific_genres, broad_genres)
    """
    norm = normalize_artist(artist)
    if not norm:
        return ([], [])

    # 1. Check SQLite cache if connection provided
    if conn is not None:
        try:
            cur = conn.execute(
                """
                SELECT genre, broad_genre FROM artist_genres
                WHERE artist_normalized = ?
                ORDER BY weight DESC
                """,
                (norm,),
            )
            rows = cur.fetchall()
            if rows:
                specific: list[str] = []
                broad: set[str] = set()
                for r in rows:
                    g = str(r[0]).strip()
                    bg = str(r[1]).strip()
                    if g and g not in specific:
                        specific.append(g)
                    if bg:
                        broad.add(bg)
                return (specific, sorted(broad))
        except Exception:
            pass

    # 2. Check offline curated seed dictionary
    if norm in CURATED_SEED_ARTISTS:
        seed_spec, seed_broad = CURATED_SEED_ARTISTS[norm]
        if conn is not None:
            _save_to_db(norm, seed_spec, seed_broad, source="seed", conn=conn)
        return (list(seed_spec), list(seed_broad))

    # Also check if artist is in seed without "the " prefix
    if norm.startswith("the ") and norm[4:] in CURATED_SEED_ARTISTS:
        seed_spec, seed_broad = CURATED_SEED_ARTISTS[norm[4:]]
        if conn is not None:
            _save_to_db(norm, seed_spec, seed_broad, source="seed", conn=conn)
        return (list(seed_spec), list(seed_broad))

    # Also check if collaborative/ensemble prefix is in seed
    # (e.g. "Django Reinhardt & le quintette du Hot club de France" -> "Django Reinhardt")
    prefix = re.split(r"(?:,\s*|/\s*|\s+(?:&|and|with|feat\.?|ft\.?|et ses|et son|and his|& his|and her|& her|presents?)\s+)", norm)[0].strip()
    if prefix != norm and prefix in CURATED_SEED_ARTISTS:
        seed_spec, seed_broad = CURATED_SEED_ARTISTS[prefix]
        if conn is not None:
            _save_to_db(norm, seed_spec, seed_broad, source="seed", conn=conn)
        return (list(seed_spec), list(seed_broad))

    # Also check if any known seed artist name is a leading prefix before punctuation
    for seed_name, (seed_spec, seed_broad) in CURATED_SEED_ARTISTS.items():
        if len(seed_name) >= 5 and (norm.startswith(seed_name + " ") or norm.startswith(seed_name + ",") or norm.startswith(seed_name + " -")):
            if conn is not None:
                _save_to_db(norm, seed_spec, seed_broad, source="seed", conn=conn)
            return (list(seed_spec), list(seed_broad))

    # 3. Online fetch if permitted
    specific_genres: list[str] = []
    if online:
        mb_tags = fetch_musicbrainz_genres(artist)
        if mb_tags:
            specific_genres = mb_tags
        else:
            wiki_tags = fetch_wikidata_genres(artist)
            if wiki_tags:
                specific_genres = wiki_tags

    # Expand specific genres to broad genres
    broad_set: set[str] = set()
    for g in specific_genres:
        for bg in map_to_broad_genres(g):
            broad_set.add(bg)

    broad_list = sorted(broad_set)

    # 4. Cache in SQLite
    if conn is not None and specific_genres:
        _save_to_db(norm, specific_genres, broad_list, source="online", conn=conn)

    return (specific_genres, broad_list)


def _save_to_db(
    artist_normalized: str,
    specific_genres: list[str],
    broad_genres: list[str],
    source: str,
    conn: sqlite3.Connection,
) -> None:
    """Save resolved artist genres into SQLite table."""
    now = time.time()
    for spec in specific_genres:
        bg_list = map_to_broad_genres(spec) or broad_genres or ["world"]
        for bg in bg_list:
            try:
                conn.execute(
                    """
                    INSERT INTO artist_genres (
                        artist_normalized, genre, broad_genre, weight, source, fetched_at
                    ) VALUES (?, ?, ?, 1.0, ?, ?)
                    ON CONFLICT(artist_normalized, genre, broad_genre) DO UPDATE SET
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    (artist_normalized, spec, bg, source, now),
                )
            except Exception as e:
                log(f"Error saving artist_genres for '{artist_normalized}': {e}")
    try:
        conn.commit()
    except Exception:
        pass


def seed_database(conn: sqlite3.Connection) -> int:
    """Pre-populate the SQLite artist_genres table with all curated seed artists.

    Returns the number of artist entries populated.
    """
    count = 0
    now = time.time()
    for norm_artist, (spec_genres, broad_genres) in CURATED_SEED_ARTISTS.items():
        for spec in spec_genres:
            bg_list = map_to_broad_genres(spec) or broad_genres
            for bg in bg_list:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artist_genres (
                        artist_normalized, genre, broad_genre, weight, source, fetched_at
                    ) VALUES (?, ?, ?, 1.0, 'seed', ?)
                    """,
                    (norm_artist, spec, bg, now),
                )
        count += 1
    conn.commit()
    return count
