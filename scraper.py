"""Polite scraper for elicitatii.anaf.ro – category "bunuri imobile".

Strategy (kept deliberately light on the remote site):
  * the full listing is fetched with limit=100  -> ~2 requests per refresh
  * the county of each auction is learned once and cached:
      - first run: one listing request per county (filtre={"judet": id})
      - later runs: one detail-page request only for auctions not seen before
  * every request to ANAF is spaced by a random 3-6 s pause, uses a normal
    browser User-Agent, and stops on HTTP 429/403 instead of retrying hard.
  * geocoding (OpenStreetMap Nominatim) is cached forever and limited to
    1 request / 1.5 s as their usage policy requires. It resolves the județ
    first and only accepts places inside it.
"""
import json
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://elicitatii.anaf.ro"
CATEGORY = "/licitatii/categorii/bunuri-imobile"
CDN = BASE + "/cdn/public/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
DATA = Path(__file__).parent / "data"

# county id (ANAF filter value) -> (name, lat, lon of county centroid)
COUNTIES = {
    "1": ("Alba", 46.15, 23.58), "2": ("Arad", 46.29, 21.70), "3": ("Argeș", 45.03, 24.88),
    "4": ("Bacău", 46.48, 26.83), "5": ("Bihor", 47.03, 22.08), "6": ("Bistrița-Năsăud", 47.25, 24.55),
    "7": ("Botoșani", 47.83, 26.75), "8": ("Brașov", 45.80, 25.35), "9": ("Brăila", 45.08, 27.60),
    "10": ("Buzău", 45.30, 26.70), "11": ("Caraș-Severin", 45.15, 22.00), "12": ("Cluj", 46.78, 23.50),
    "13": ("Constanța", 44.20, 28.40), "14": ("Covasna", 45.90, 26.00), "51": ("Călărași", 44.30, 27.00),
    "61": ("DGAMC", 44.43, 26.09), "16": ("Dolj", 44.15, 23.70), "15": ("Dâmbovița", 44.85, 25.45),
    "17": ("Galați", 45.75, 27.80), "52": ("Giurgiu", 44.05, 25.85), "18": ("Gorj", 45.00, 23.25),
    "19": ("Harghita", 46.50, 25.55), "20": ("Hunedoara", 45.80, 22.85), "21": ("Ialomița", 44.55, 27.45),
    "22": ("Iași", 47.20, 27.30), "23": ("Ilfov", 44.55, 26.15), "24": ("Maramureș", 47.65, 24.00),
    "25": ("Mehedinți", 44.55, 22.85), "40": ("București", 44.43, 26.10), "26": ("Mureș", 46.60, 24.65),
    "27": ("Neamț", 46.95, 26.25), "28": ("Olt", 44.30, 24.40), "29": ("Prahova", 45.10, 25.95),
    "30": ("Satu Mare", 47.75, 22.90), "32": ("Sibiu", 45.85, 24.30), "33": ("Suceava", 47.55, 25.70),
    "31": ("Sălaj", 47.15, 23.15), "34": ("Teleorman", 44.00, 25.25), "35": ("Timiș", 45.75, 21.25),
    "36": ("Tulcea", 45.05, 28.95), "37": ("Vaslui", 46.60, 27.70), "39": ("Vrancea", 45.80, 27.00),
    "38": ("Vâlcea", 45.20, 24.10),
}


def norm(s):
    return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower().strip()


COUNTY_BY_NAME = {norm(v[0]): k for k, v in COUNTIES.items()}
COUNTY_BY_NAME.update({"municipiul bucuresti": "40", "bucuresti": "40"})

# county id -> ISO 3166-2 code, as Nominatim reports it in address["ISO3166-2-lvl4"] (DGAMC is not a county)
COUNTY_ISO = {
    "1": "RO-AB", "2": "RO-AR", "3": "RO-AG", "4": "RO-BC", "5": "RO-BH", "6": "RO-BN", "7": "RO-BT", "8": "RO-BV",
    "9": "RO-BR", "10": "RO-BZ", "11": "RO-CS", "12": "RO-CJ", "13": "RO-CT", "14": "RO-CV", "51": "RO-CL",
    "16": "RO-DJ", "15": "RO-DB", "17": "RO-GL", "52": "RO-GR", "18": "RO-GJ", "19": "RO-HR", "20": "RO-HD",
    "21": "RO-IL", "22": "RO-IS", "23": "RO-IF", "24": "RO-MM", "25": "RO-MH", "40": "RO-B", "26": "RO-MS",
    "27": "RO-NT", "28": "RO-OT", "29": "RO-PH", "30": "RO-SM", "32": "RO-SB", "33": "RO-SV", "31": "RO-SJ",
    "34": "RO-TR", "35": "RO-TM", "36": "RO-TL", "37": "RO-VS", "39": "RO-VN", "38": "RO-VL",
}


def county_id(name):
    """'PRAHOVA', 'Județul Prahova', 'Municipiul București' -> county id, or None."""
    return COUNTY_BY_NAME.get(re.sub(r"^jud(?:etul|et|\.)?\s*", "", norm(name)).strip(" .,"))


def address_county(address):
    """The județ an address names itself ('…, jud. Arad', '…, Galati', 'Bucuresti, Sector 1'), or None.

    The auction page's "Județ" is the tax office's county, which is not always where the property is."""
    text = norm(address)
    if not text:
        return None
    m = re.search(r"\bjud(?:etul|et|\.)?\s*\.?\s*([a-z][a-z \-]+?)\s*(?:[,.;]|$)", text)
    if m and county_id(m.group(1)):
        return county_id(m.group(1))
    parts = [p.strip(" .") for p in text.split(",") if p.strip(" .")]
    if parts and county_id(parts[-1]):
        return county_id(parts[-1])
    if re.search(r"\bsector(?:ul)?\s*\d", text) or (parts and county_id(parts[0]) == "40"):
        return "40"
    return None


class Blocked(Exception):
    pass


class Fetcher:
    """Single-session fetcher with randomised pauses between ANAF requests."""

    def __init__(self, log=print, min_delay=3.0, max_delay=6.0):
        self.log, self.min_delay, self.max_delay = log, min_delay, max_delay
        self.last = 0.0
        self.count = 0

    def get(self, path, params=None):
        wait = self.last + random.uniform(self.min_delay, self.max_delay) - time.time()
        if wait > 0:
            time.sleep(wait)
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                raise Blocked(f"ANAF answered {e.code} – stopping so we don't keep pushing")
            raise
        finally:
            self.last = time.time()
            self.count += 1
        return body


# ---------------------------------------------------------------- RSC parsing
def rsc_rows(html):
    """Return {row_id: value} from the Next.js flight payload embedded in the page."""
    payload = ""
    for chunk in re.findall(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            a = json.loads(chunk)
        except ValueError:
            continue
        if len(a) > 1 and isinstance(a[1], str):
            payload += a[1]
    # Flight format: "<hexid>:<row>\n", except text rows "<hexid>:T<hexlen>,<bytes>"
    # whose content may contain raw newlines, so parse sequentially on bytes.
    buf = payload.encode("utf-8")
    rows, pos, n = {}, 0, len(buf)
    while pos < n:
        colon = buf.find(b":", pos)
        if colon < 0:
            break
        rid = buf[pos:colon].decode("ascii", "replace").strip()
        pos = colon + 1
        if buf[pos:pos + 1] == b"T":
            comma = buf.find(b",", pos)
            length = int(buf[pos + 1:comma], 16)
            rows[rid] = ("text", buf[comma + 1:comma + 1 + length].decode("utf-8", "replace"))
            pos = comma + 1 + length
            continue
        end = buf.find(b"\n", pos)
        end = n if end < 0 else end
        raw = buf[pos:end].decode("utf-8", "replace")
        pos = end + 1
        try:
            if raw.startswith("I["):
                rows[rid] = ("import", json.loads(raw[1:])[-1])
            elif raw[:1] in "[{\"":
                rows[rid] = ("json", json.loads(raw))
        except ValueError:
            pass
    return rows


def resolve(node, rows, depth=0):
    """Inline "$Lxx" references and replace component refs with their names."""
    if depth > 60:
        return node
    if isinstance(node, str) and re.fullmatch(r"\$L?[0-9a-f]+", node):
        row = rows.get(node.lstrip("$L"))
        if row is None:
            return node
        if row[0] == "import":
            return "<" + row[1] + ">"
        if row[0] == "text":
            return row[1]
        return resolve(row[1], rows, depth + 1)
    if isinstance(node, list):
        return [resolve(x, rows, depth + 1) for x in node]
    if isinstance(node, dict):
        return {k: resolve(v, rows, depth + 1) for k, v in node.items()}
    return node


def walk(node):
    yield node
    if isinstance(node, list):
        for x in node:
            yield from walk(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk(v)


def text_of(node):
    if isinstance(node, (str, int, float)) and not (isinstance(node, str) and node.startswith("$")):
        return str(node)
    if isinstance(node, list):
        if len(node) == 4 and node[0] == "$":
            return text_of(node[3].get("children") if isinstance(node[3], dict) else None)
        return "".join(text_of(x) for x in node)
    return ""


def parse_listing(html):
    rows = rsc_rows(html)
    total, last_page = None, 1
    cards = []
    for rid, (kind, val) in rows.items():
        if kind != "json":
            continue
        for n in walk(val):
            if isinstance(n, list) and len(n) == 4 and n[0] == "$" and isinstance(n[3], dict):
                props = n[3]
                if props.get("data-slot") == "card" and isinstance(n[2], str) and n[2].isdigit():
                    cards.append((n[2], n))
                if "lastPage" in props:
                    last_page = props["lastPage"]
                ch = props.get("children")
                if isinstance(ch, list) and len(ch) == 2 and ch[1] == " licitații":
                    total = ch[0]
    items = {}
    for lid, card in cards:
        if lid in items:
            continue
        tree = resolve(card, rows)
        it = {"id": int(lid)}
        for n in walk(tree):
            if not (isinstance(n, list) and len(n) == 4 and n[0] == "$" and isinstance(n[3], dict)):
                continue
            t, p = n[1], n[3]
            href = p.get("href")
            if isinstance(href, str):
                if href.startswith("/licitatii/produs/"):
                    it["url"] = BASE + href
                elif href.startswith(CATEGORY + "/"):
                    it["subcategorie"] = text_of(p.get("children")).strip()
                    it["subcategorieSlug"] = href.rsplit("/", 1)[-1]
            if p.get("data-slot") == "card-title":
                it["titlu"] = text_of(p.get("children")).strip()
            if t == "<LicitatieCardContent>":
                for k in ("stare", "dataOraIntrarePublicitate", "dataOraStart", "dataOraFinal",
                          "descriere", "valutaPret", "unitateMasura", "cantitate"):
                    it[k] = p.get(k)
                try:
                    it["pret"] = float(p.get("pretPornireLicitatie"))
                except (TypeError, ValueError):
                    it["pret"] = None
            if "media" in p and isinstance(p["media"], list) and p["media"] and "imagine" not in it:
                m0 = p["media"][0]
                path = m0.get("thumbnailPath") or m0.get("path")
                if path:
                    it["imagine"] = CDN + path
            cls = p.get("className") or ""
            if "-rotate-45" in cls and "bg-red" in cls:
                txt = text_of(p.get("children"))
                mm = re.search(r"(\d+)", txt)
                if mm:
                    it["reducere"] = int(mm.group(1))
        items[lid] = it
    return list(items.values()), total, last_page


def parse_detail_county(html):
    m = re.search(r'"judet":"([^"]+)"', html.replace('\\"', '"'))
    if not m:
        rows = rsc_rows(html)
        for _, (kind, val) in rows.items():
            if kind == "json":
                for n in walk(val):
                    if isinstance(n, dict) and isinstance(n.get("judet"), str):
                        return COUNTY_BY_NAME.get(norm(n["judet"]))
        return None
    return COUNTY_BY_NAME.get(norm(m.group(1)))


# ---------------------------------------------------------------- locality
_NAME = r"([A-ZĂÂÎȘŞȚŢ][\w\-ăâîșşțţ]+(?:[ \-][A-ZĂÂÎȘŞȚŢ][\w\-ăâîșşțţ]+)?)"
LOC_PATTERNS = [
    r"(?i:\bsat(?:ul)?\.?)\s*" + _NAME,
    r"(?i:\bloc\.|\blocalitate(?:a)?|\blocalit[aă][tț]ii)\s*" + _NAME,
    r"(?i:\bU\.?A\.?T\.?)\s+" + _NAME,
    r"(?i:\bcom\.|\bcomuna|\bcomunei|\bor[aă][sș](?:ul)?|\bors\.|\bmun\.|\bmunicipiul(?:ui)?)\s*" + _NAME,
    r"(?i:\bcart(?:ea|e)\s+funciar[aăe]|\bC\.?F\.?)\s*(?i:nr\.?|num[aă]rul)?\s*\d+(?:[/\-]\w+)?\s*(?i:a\s+)?"
    r"(?i:UAT\s+)?" + _NAME,
    r"(?i:\bsituat[aă]?\s+[iî]n)\s+" + _NAME,
]
STOP = {"nr", "cad", "cf", "teren", "constructie", "constructii", "si", "in", "cu", "situat", "situata",
        "extravilan", "intravilan", "categorie", "inscris", "inscrisa", "inregistrat", "inregistrata",
        "top", "lot", "parcela", "str", "strada", "bl", "ap", "judet", "judetul", "jud", "casa", "vila",
        "apartament", "vie", "tarla", "tarlaua", "punct", "sporadic", "vechi", "cota", "numar", "cadastral"}


def guess_locality(text):
    for pat in LOC_PATTERNS:
        for m in re.finditer(pat, text or ""):
            words = []
            for w in re.split(r"[ ]+", m.group(1).strip(" ,.-")):
                if norm(w) in STOP:
                    break
                if not re.search(r"\d", w):
                    words.append(w)
            if words and len(words[0]) > 2:
                return " ".join(words)
    return None


class Geocoder:
    """Nominatim lookups, always in this order: the județ first, then the place inside it.

    Every result must carry the județ's ISO code (address["ISO3166-2-lvl4"]), so a pin can
    never land in another county – e.g. "str. Venus, Ploiești" can't resolve to Strada Venus in București.
    """
    V = "v2|"  # cache key prefix; bump to re-geocode everything once

    def __init__(self, log=print):
        self.log = log
        self.path = DATA / "geocache.json"
        cache = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.cache = {k: v for k, v in cache.items() if k.startswith(self.V)}
        self.last = 0.0

    def save(self):
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1))

    STREET_TYPES = {"road", "house", "building", "residential", "street", "house_number", "highway",
                    "industrial", "commercial", "retail", "construction", "farmyard", "primary", "secondary",
                    "tertiary", "unclassified", "living_street", "pedestrian", "service", "trunk", "footway", "place"}
    PLACE_KEYS = ("city", "town", "village", "municipality", "suburb", "city_district", "hamlet", "quarter",
                  "neighbourhood", "residential", "locality", "isolated_dwelling")

    def _query(self, params):
        """One rate-limited Nominatim call. Returns the result list, or None on a network error."""
        wait = self.last + 1.5 - time.time()
        if wait > 0:
            time.sleep(wait)
        q = urllib.parse.urlencode({**params, "format": "jsonv2", "limit": 5, "addressdetails": 1,
                                    "countrycodes": "ro"})
        req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + q,
                                     headers={"User-Agent": "elicitatii-local-viewer/1.0 (personal use)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # network issue: don't cache, try next time
            self.log(f"geocoding failed for {params}: {e}")
            return None
        finally:
            self.last = time.time()

    def area(self, county_id):
        """Step 1 – the județ itself: {"bbox": [south, north, west, east], "center": [lat, lon]} or None."""
        iso = COUNTY_ISO.get(county_id)
        if not iso:
            return None
        key = f"{self.V}area:{county_id}"
        if key not in self.cache:
            name = COUNTIES[county_id][0]
            arr = self._query({"city": name, "country": "Romania"} if iso == "RO-B"
                              else {"county": name, "country": "Romania"})
            if arr is None:
                return None
            hit = next((x for x in arr if x.get("address", {}).get("ISO3166-2-lvl4") == iso), None)
            self.cache[key] = hit and {"bbox": [float(v) for v in hit["boundingbox"]],
                                       "center": [float(hit["lat"]), float(hit["lon"])]}
        return self.cache[key]

    def center(self, county_id):
        a = self.area(county_id)
        return a["center"] if a else list(COUNTIES[county_id][1:3])

    def _in_place(self, hit, places):
        names = [norm(hit.get("name"))] + [norm(hit["address"].get(k)) for k in self.PLACE_KEYS]
        want = [norm(p) for p in places]
        return any(n and w and (n == w or n.startswith(w + " ") or w.startswith(n + " ") or n.endswith(" " + w))
                   for n in names for w in want)

    def search(self, county_id, params, places=()):
        """Step 2 – a place inside the județ. Returns [lat, lon, addresstype] or None (cached)."""
        key = self.V + county_id + "|" + norm(json.dumps(params, sort_keys=True, ensure_ascii=False))
        if key in self.cache:
            return self.cache[key]
        iso, area = COUNTY_ISO.get(county_id), self.area(county_id)
        if area:
            s, n, w, e = area["bbox"]
            params = {**params, "viewbox": f"{w},{n},{e},{s}", "bounded": 1}
        arr = self._query(params)
        if arr is None:
            return None
        res = None
        for x in arr:
            # Step 3 – verify: inside the județ, and in the locality that was asked for
            if iso and x.get("address", {}).get("ISO3166-2-lvl4") != iso:
                continue
            if places and not self._in_place(x, places):
                continue
            res = [float(x["lat"]), float(x["lon"]), x.get("addresstype") or x.get("type") or ""]
            break
        self.cache[key] = res
        return res

    def locate(self, locality, county_id):
        r = self.search(county_id, {"q": f"{locality}, {COUNTIES[county_id][0]}"}, [locality])
        return r[:2] if r else None

    def locate_address(self, address, county_id):
        """Geocode an ANAF address. Returns ([lat, lon], precision, label) or (None, None, None)."""
        county = COUNTIES[county_id][0]
        street, places = parse_address(address, county)
        if street and places:
            where = {"city": places[0], "country": "Romania"}
            if county_id != "40":
                where["county"] = county
            for s in street_variants(street):
                r = self.search(county_id, {"street": s, **where}, places)
                if r and r[2] in self.STREET_TYPES:
                    return r[:2], "adresa", f"{street}, {places[0]}"
        for p in places:
            r = self.search(county_id, {"q": f"{p}, {county}"}, [p])
            if r:
                return r[:2], "localitate", p
        return None, None, None


def street_variants(street):
    """'Strada T. Vladimirescu 24/78' -> ['24/78 Strada T. Vladimirescu', '24 Strada …', 'Strada …', 'Strada Vladimirescu']"""
    name, _, num = street.rpartition(" ")
    if not (name and re.match(r"^\d", num)):
        name, num = street, ""
    out = []
    if num:
        out.append(f"{num} {name}")
        first = re.match(r"\d+", num).group()
        if first != num:
            out.append(f"{first} {name}")
    out.append(name)
    bare = re.sub(r"\b[A-Za-zĂÂÎȘŞȚŢăâîșşțţ]{1,2}\.\s*", "", name).strip()  # drop initials: "T. Vladimirescu"
    if bare != name and len(bare.split()) > 1:
        out.append(bare)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- addresses
_STREET = re.compile(r"^(?:str|strada|bd|bdul|b-dul|bulevardul|calea|sos|soseaua|șos|aleea|al|piata|"
                     r"intrarea|drumul|splaiul|fundatura|ulita)\b\.?\s*", re.I)
_VILLAGE = re.compile(r"^(?:sat(?:ul)?|loc(?:alitatea)?|localit[aă][tț]ii)\b\.?\s*", re.I)
_ADMIN = re.compile(r"^(?:com(?:una)?|or(?:a[sș]|[sș])(?:ul)?|mun(?:icipiul)?|sector(?:ul)?|cartier(?:ul)?)\b\.?\s*",
                    re.I)
_SKIP = re.compile(r"^(?:jud(?:etul|\.)?|judet|romania|cod\s*postal|cp|bl|bloc|sc|scara|et|etaj|ap|apartament|"
                   r"tarla|t\d|parcela|p\d|cf|nr\.?\s*cad|cad|top|lot|km|zona|punct(?:ul)?)\b", re.I)


_STREET_NAME = {"str": "Strada", "strada": "Strada", "bd": "Bulevardul", "bdul": "Bulevardul", "b-dul": "Bulevardul",
                "bulevardul": "Bulevardul", "calea": "Calea", "sos": "Șoseaua", "soseaua": "Șoseaua", "aleea": "Aleea",
                "al": "Aleea", "piata": "Piața", "intrarea": "Intrarea", "drumul": "Drumul", "splaiul": "Splaiul",
                "fundatura": "Fundătura", "ulita": "Ulița"}


def _title(s):
    return " ".join(w.capitalize() if w.isupper() else w for w in s.split())


def parse_address(address, county=""):
    """Split 'STR. X NR. 5, SAT Y, COM. Z, JUD. W' into ('Strada X 5', ['Y', 'Z'])."""
    if not address:
        return None, []
    text = re.sub(r"\s+", " ", address).strip()
    # some addresses have no commas: break before known keywords
    text = re.sub(r"\s(?=(?:SAT|COM\.?|COMUNA|ORAS|ORAȘ|MUN\.?|MUNICIPIUL|SECTOR(?:UL)?|JUD\.?|JUDET(?:UL)?|LOC\.?|STR\.?|STRADA|NR\.?|BLOC|BL\.?)\s)",
                  ", ", text, flags=re.I)
    street, villages, admins = None, [], []
    for raw in [p.strip(" .;") for p in text.split(",")]:
        if not raw or norm(raw) in (norm(county), "romania"):
            continue
        if _STREET.match(raw):
            kind = _STREET_NAME.get(norm(_STREET.match(raw).group(0)).strip(" ."), "Strada")
            name = _STREET.sub("", raw)
            if _STREET.match(name):  # "str. Aleea Nuci" -> "Aleea Nuci"
                kind = _STREET_NAME.get(norm(_STREET.match(name).group(0)).strip(" ."), kind)
                name = _STREET.sub("", name)
            m = re.search(r"\bnr\.?\s*([\w/-]+)", name, re.I)
            name = re.sub(r"\bnr\.?.*$", "", name, flags=re.I).strip(" ,.")
            if name and not name.isdigit():
                street = f"{kind} {_title(name)}" + (f" {m.group(1)}" if m else "")
        elif re.match(r"^nr\.?\s*", raw, re.I):
            if street and not re.search(r"\d", street.split()[-1]):
                street += " " + re.sub(r"^nr\.?\s*", "", raw, flags=re.I).split()[0]
        elif _VILLAGE.match(raw):
            villages.append(_title(_VILLAGE.sub("", raw)))
        elif _ADMIN.match(raw):
            nm = _ADMIN.sub("", raw)
            if re.match(r"^\d+$", nm):  # "SECTOR 3"
                admins.append("Sector " + nm)
            else:
                admins.append(_title(nm))
        elif _SKIP.match(raw) or re.search(r"\d", raw):
            continue
        elif len(raw) > 2:
            villages.append(_title(raw))  # bare name, usually the village/locality
    places = [_ABBR.sub(lambda m: _ABBR_FULL[norm(m.group(1))], p) for p in villages + admins if p]
    return street, list(dict.fromkeys(places))


_ABBR = re.compile(r"^(Tg|Rm|Sf|Cp)\b\.?\s*", re.I)
_ABBR_FULL = {"tg": "Târgu ", "rm": "Râmnicu ", "sf": "Sfântu ", "cp": "Câmpulung "}


# ---------------------------------------------------------------- detail page
DETAIL_VERSION = 2  # bump when parse_detail learns a new field worth re-reading


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_detail(html):
    """Extract the auction record (addresses, valuation, guarantee…) from a /licitatii/produs/ page."""
    rows = rsc_rows(html)
    rec, notes = None, {}
    for _, (kind, val) in rows.items():
        if kind != "json":
            continue
        tree = resolve(val, rows)
        for n in walk(tree):
            if rec is None and isinstance(n, dict) and "locDetinereAdresa" in n:
                rec = n
            elif isinstance(n, str) and ": " in n and len(n) < 600:
                k, _, v = n.partition(": ")
                if k in ("Adresa identificare bun", "Sarcini", "Uzura", "Semne particulare", "Constatari"):
                    notes.setdefault(k, v.strip())
    if not rec:
        return None
    # "La această licitație puteți plasa ofertă sub prețul de pornire … dar nu mai mică de <b>13.475 lei</b>"
    m = re.search(r'nu mai mic[ăa] de.{0,120}?"children":\["([\d.,]+)"', html.replace('\\"', '"'), re.S)
    oferta_minima = None
    if m:
        v = m.group(1)
        oferta_minima = _num(v.replace(".", "").replace(",", ".") if re.search(r"\.\d{3}(?!\d)", v) or "," in v else v)
    clean = lambda v: None if v in (None, "", "$undefined") else v
    struct = rec.get("detaliiStructura") if isinstance(rec.get("detaliiStructura"), dict) else {}
    return {
        "adresa": clean(rec.get("locDetinereAdresa")),
        "telefon": clean(rec.get("locDetinereTelefon")),
        "adresaPredare": clean(rec.get("locPredareAdresa")),
        "adresaIdentificare": clean(notes.get("Adresa identificare bun")),
        "pretEvaluare": _num(rec.get("pretEvaluare")),
        "garantie": _num(rec.get("garantieParticipare")),
        "pasMinim": _num(rec.get("praguriOfertareMinim")),
        "cotaTva": _num(rec.get("cotaTva")),
        "nrLicitatie": rec.get("nrLicitatie"),
        "sechestrat": bool(rec.get("isSechestrat")),
        "permiteVizionare": bool(rec.get("permiteVizionare")),
        "nrOferte": rec.get("oferteTotalItems") or 0,
        "sarcini": clean(notes.get("Sarcini")),
        "unitateFiscala": clean(struct.get("denumire")),
        "judetNume": clean(rec.get("judet")),
        "ofertaMinima": oferta_minima,  # set only when the page allows bids below the starting price
        "v": DETAIL_VERSION,
    }


# ---------------------------------------------------------------- refresh
def load_json(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save_json(name, obj):
    DATA.mkdir(exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(DATA / name)


TRACKED = ["titlu", "pret", "stare", "dataOraStart", "dataOraFinal", "reducere", "descriere", "subcategorie", "nrOferte"]
SNAP = ["imagine", "pret", "subcategorie", "judet", "localitate", "reducere", "nrOferte", "dataOraFinal"]  # kept with each change so removed auctions still show details
RECHECK_PER_RUN = 20      # detail pages re-read per refresh to keep bid counts current
RECHECK_AFTER_H = 12      # …only when the last read is older than this


def diff(old, new):
    old_by, new_by = {a["id"]: a for a in old}, {a["id"]: a for a in new}
    changes = []
    for i, a in new_by.items():
        if i not in old_by:
            changes.append({"tip": "adaugat", "id": i, "titlu": a.get("titlu"), "url": a.get("url")})
        else:
            fields = [{"camp": f, "vechi": old_by[i].get(f), "nou": a.get(f)}
                      for f in TRACKED if old_by[i].get(f) != a.get(f)]
            if fields:
                changes.append({"tip": "modificat", "id": i, "titlu": a.get("titlu"),
                                "url": a.get("url"), "campuri": fields})
    for i, a in old_by.items():
        if i not in new_by:
            changes.append({"tip": "eliminat", "id": i, "titlu": a.get("titlu"), "url": a.get("url"),
                            "snap": {k: a.get(k) for k in SNAP}})
    return changes


def refresh(log=print, progress=lambda **k: None):
    DATA.mkdir(exist_ok=True)
    f = Fetcher(log=log)
    started = time.time()

    # 1) full listing, 100 per page
    items, page, last_page, total = [], 1, 1, None
    while page <= last_page:
        progress(etapa="listare", pagina=page)
        log(f"Downloading list – page {page}")
        html = f.get(CATEGORY, {"limit": 100, "pagina": page} if page > 1 else {"limit": 100})
        got, t, lp = parse_listing(html)
        total = total or t
        last_page = lp or 1
        items += got
        page += 1
        if page > 20:
            break
    seen = {}
    for it in items:
        seen.setdefault(it["id"], it)
    items = list(seen.values())
    log(f"{len(items)} auctions found (site says {total})")
    if not items:
        raise RuntimeError("Could not extract any auctions – has the page structure changed?")

    # 2) counties
    county_cache = {int(k): v for k, v in load_json("counties.json", {}).items()}
    missing = [it for it in items if it["id"] not in county_cache]
    if len(missing) > 40:
        log(f"{len(missing)} auctions without a county – checking the county filter ({len(COUNTIES)} requests)")
        for n, cid in enumerate(COUNTIES, 1):
            progress(etapa="judete", curent=n, total=len(COUNTIES))
            pg, lp = 1, 1
            while pg <= lp:
                params = {"filtre": json.dumps({"judet": cid}), "limit": 100}
                if pg > 1:
                    params["pagina"] = pg
                got, _, lp = parse_listing(f.get(CATEGORY, params))
                for it in got:
                    county_cache[it["id"]] = cid
                pg += 1
        save_json("counties.json", county_cache)
        missing = [it for it in items if it["id"] not in county_cache]
    # 3) detail pages (Loc deținere address, valuation, guarantee…), fetched once per auction
    details = {int(k): v for k, v in load_json("details.json", {}).items()}
    # re-read pages parsed by an older parser when they may carry the "bid below starting price" message
    stale = lambda it: (details[it["id"]] or {}).get("v", 1) < DETAIL_VERSION and it.get("reducere") == 50
    todo = [it for it in items if it.get("url") and (it["id"] not in details or stale(it))]
    # bid counts change: re-read a few of the oldest reads, auctions ending soonest first
    def age_h(it):
        t = (details.get(it["id"]) or {}).get("citit")
        return (time.time() - time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600 if t else 1e9
    queued = {it["id"] for it in todo}
    recheck = sorted((it for it in items if it.get("url") and it["id"] not in queued and age_h(it) > RECHECK_AFTER_H),
                     key=lambda it: it.get("dataOraFinal") or "")[:RECHECK_PER_RUN]
    if recheck:
        log(f"Re-checking bids on {len(recheck)} auctions")
    todo += recheck
    if todo:
        log(f"Fetching {len(todo)} detail pages (~{max(1, round(len(todo) * 5.5 / 60))} min)")
        f.min_delay, f.max_delay = 4.0, 7.0
        try:
            for n, it in enumerate(todo, 1):
                progress(etapa="detalii", curent=n, total=len(todo))
                d = parse_detail(f.get(urllib.parse.urlparse(it["url"]).path))
                details[it["id"]] = d or {}
                details[it["id"]]["citit"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                if n % 10 == 0:
                    save_json("details.json", details)
        except Blocked as e:
            log(f"{e}. Keeping the {len(details)} detail pages fetched so far; the rest will be fetched next time.")
        save_json("details.json", details)
        save_json("counties.json", county_cache)

    # county = the județ named in the property address, else the page's "Județ", else the listing filter's
    for it in items:
        d = details.get(it["id"]) or {}
        cid = address_county(d.get("adresa")) or address_county(d.get("adresaIdentificare")) or county_id(d.get("judetNume"))
        if cid and county_cache.get(it["id"]) != cid:
            if it["id"] in county_cache:
                log(f"#{it['id']}: county {COUNTIES[county_cache[it['id']]][0]} -> {COUNTIES[cid][0]}")
            county_cache[it["id"]] = cid
    save_json("counties.json", county_cache)

    # 4) geolocation, always inside the județ: Loc deținere address first, then the locality in the
    #    description, then the județ centre. data/pins.json ({id: [lat, lon]}) overrides by hand.
    geo = Geocoder(log=log)
    pins = load_json("pins.json", {})
    for n, it in enumerate(items, 1):
        d = details.get(it["id"]) or {}
        it.update({k: v for k, v in d.items() if k != "judetNume"})
        cid = county_cache.get(it["id"])
        it["judetId"] = cid
        it["judet"] = COUNTIES[cid][0] if cid else None
        progress(etapa="geocodare", curent=n, total=len(items))
        pos, precizie, label = None, None, None
        if str(it["id"]) in pins:
            pos, precizie = pins[str(it["id"])][:2], "adresa"
        elif cid:
            for addr in (d.get("adresa"), d.get("adresaIdentificare")):
                if addr:
                    pos, precizie, label = geo.locate_address(addr, cid)
                    if pos:
                        break
        loc = label or guess_locality((it.get("descriere") or "") + " " + (it.get("titlu") or ""))
        it["localitate"] = (parse_address(d.get("adresa"), it["judet"] or "")[1] or [loc])[0] if d.get("adresa") else loc
        if not pos and cid and loc:
            pos = geo.locate(loc, cid)
            precizie = "localitate" if pos else None
        if not pos and cid:
            lat, lon = geo.center(cid)
            rnd = random.Random(it["id"])  # stable jitter so markers don't stack
            pos = [lat + rnd.uniform(-0.12, 0.12), lon + rnd.uniform(-0.18, 0.18)]
            precizie = "judet"
        it["lat"], it["lon"] = (pos or [None, None])
        it["precizie"] = precizie
    geo.save()

    # 5) diff + persist
    old = load_json("auctions.json", {}).get("items", [])
    changes = diff(old, items) if old else []
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # carry history across refreshes: when we first saw it, and its last different price
    old_by = {a["id"]: a for a in old}
    for it in items:
        prev = old_by.get(it["id"])
        it["vazutPrima"] = (prev or {}).get("vazutPrima") or now
        it["nou"] = bool(old) and prev is None
        if prev and prev.get("pret") is not None and prev.get("pret") != it.get("pret"):
            it["pretAnterior"] = prev["pret"]
        elif prev and prev.get("pretAnterior") is not None:
            it["pretAnterior"] = prev["pretAnterior"]
    save_json("auctions.json", {"actualizat": now, "totalSite": total, "items": items})
    logbook = load_json("changelog.json", [])
    logbook.insert(0, {
        "data": now,
        "durata": round(time.time() - started),
        "cereriAnaf": f.count,
        "total": len(items),
        "primaRulare": not old,
        "adaugate": sum(c["tip"] == "adaugat" for c in changes),
        "modificate": sum(c["tip"] == "modificat" for c in changes),
        "eliminate": sum(c["tip"] == "eliminat" for c in changes),
        "schimbari": changes,
    })
    save_json("changelog.json", logbook[:500])
    log(f"Done: {len(items)} auctions, {len(changes)} changes, {f.count} ANAF requests")
    return len(changes)


if __name__ == "__main__":
    refresh()
