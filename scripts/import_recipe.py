#!/usr/bin/env python3
"""Import a recipe from a web page into recipes/<slug>.json.

Usage:
    python scripts/import_recipe.py <url> [--tags "side dish, holiday"] [--notes "..."]
    python scripts/import_recipe.py --html saved-page.html --url <url>
    python scripts/import_recipe.py --rebuild-index

Only the schema.org Recipe JSON-LD embedded in the page is used, so ads,
life stories, and comment sections never make it into the saved recipe.
Standard library only; no pip install needed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES_DIR = ROOT / "recipes"
IMAGES_DIR = ROOT / "images"
INDEX_PATH = RECIPES_DIR / "index.json"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")

SECTION_PREFIX = "## "


class ImportError_(Exception):
    pass


# --------------------------------------------------------------------------- fetching


def _decode_body(raw: bytes, encoding: str | None, charset: str | None) -> str:
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode(charset or "utf-8", errors="replace")


def fetch_http(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return _decode_body(body, resp.headers.get("Content-Encoding"), resp.headers.get_content_charset())


def find_chrome() -> str | None:
    env = os.environ.get("CHROME_BIN")
    if env and shutil.which(env):
        return shutil.which(env)
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def fetch_chrome(url: str, timeout: int = 75) -> str:
    """Load the page in headless Chrome and return the rendered DOM.

    Some recipe sites (Allrecipes and other People Inc. brands) reject non-browser
    TLS fingerprints regardless of User-Agent. Chrome prints the DOM once the page
    loads but often never exits because of ad scripts, so stdout is streamed and
    Chrome is killed as soon as the closing </html> arrives.
    """
    chrome = find_chrome()
    if not chrome:
        raise ImportError_("headless Chrome not found (set CHROME_BIN)")
    profile = tempfile.mkdtemp(prefix="recipe-chrome-")
    cmd = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--blink-settings=imagesEnabled=false",
        f"--user-data-dir={profile}",
        f"--user-agent={BROWSER_UA}",
        "--virtual-time-budget=5000",
        "--dump-dom",
        url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks: list[bytes] = []
    done = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        tail = b""
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            tail = (tail + chunk)[-64:]
            if b"</html>" in tail.lower():
                break
        done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    done.wait(timeout)
    proc.kill()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
    shutil.rmtree(profile, ignore_errors=True)
    page = b"".join(chunks).decode("utf-8", errors="replace")
    if not page.strip():
        raise ImportError_("headless Chrome returned an empty page")
    return page


def fetch_page(url: str) -> str:
    """Plain HTTP first, then headless Chrome if blocked or no recipe data found."""
    reason = ""
    try:
        page = fetch_http(url)
        if find_recipe_nodes(page):
            return page
        reason = "no Recipe JSON-LD in plain HTTP response"
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = f"network error: {exc}"
    print(f"  plain fetch failed ({reason}); retrying with headless Chrome", file=sys.stderr)
    return fetch_chrome(url)


# --------------------------------------------------------------------------- JSON-LD extraction


class _LdJsonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._buf: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            kind = (dict(attrs).get("type") or "").strip().lower()
            if kind == "application/ld+json":
                self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._buf is not None:
            self.blocks.append("".join(self._buf))
            self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return [str(x).split("/")[-1].lower() for x in t]


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def find_recipe_nodes(page: str) -> list[dict]:
    parser = _LdJsonParser()
    parser.feed(page)
    recipes = []
    for block in parser.blocks:
        text = block.strip().removeprefix("<!--").removesuffix("-->").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", text))
            except json.JSONDecodeError:
                continue
        for node in _walk(data):
            if "recipe" in _types(node):
                recipes.append(node)
    return recipes


# --------------------------------------------------------------------------- normalization

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(v) for v in value)
    if isinstance(value, dict):
        value = value.get("text") or value.get("name") or ""
    text = html.unescape(html.unescape(str(value)))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = text.replace("\u00a0", " ")
    return _WS_RE.sub(" ", text).strip()


_FRACTIONS = {
    0.125: "⅛", 0.25: "¼", 0.333: "⅓", 0.375: "⅜", 0.5: "½",
    0.625: "⅝", 0.667: "⅔", 0.75: "¾", 0.875: "⅞",
}


def _pretty_number(match: re.Match) -> str:
    raw = match.group(0)
    try:
        value = float(raw)
    except ValueError:
        return raw
    whole = int(value)
    frac = round(value - whole, 3)
    if frac == 0:
        return str(whole)
    for target, glyph in _FRACTIONS.items():
        if abs(frac - target) < 0.01:
            return f"{whole} {glyph}" if whole else glyph
    return raw


_SLASH_FRACTIONS = {
    (1, 8): "⅛", (1, 4): "¼", (1, 3): "⅓", (3, 8): "⅜", (1, 2): "½",
    (5, 8): "⅝", (2, 3): "⅔", (3, 4): "¾", (7, 8): "⅞",
}


def _slash_fraction(match: re.Match) -> str:
    glyph = _SLASH_FRACTIONS.get((int(match.group(2)), int(match.group(3))))
    if not glyph:
        return match.group(0)
    return f"{match.group(1)} {glyph}" if match.group(1) else glyph


def tidy_ingredient(text: str) -> str:
    """Write quantities the same way regardless of source: "0.5 teaspoon" and
    "1/2 teaspoon" both become "½ teaspoon", and "7 -8 slices" becomes "7-8 slices"."""
    text = clean_text(text)
    text = re.sub(r"(?<![\d.])\d*\.\d+(?![\d.])", _pretty_number, text)
    text = re.sub(r"(?<![\d/])(?:(\d+)\s+)?(\d)/(\d)(?![\d/])", _slash_fraction, text)
    return re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", text)


_DURATION_RE = re.compile(
    r"^P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<mo>\d+(?:\.\d+)?)M)?(?:(?P<w>\d+(?:\.\d+)?)W)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$",
    re.I,
)


def parse_duration(value) -> int | None:
    """ISO 8601 duration (PT1H30M, P0DT0H45M, PT65M) -> whole minutes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    text = str(value).strip()
    m = _DURATION_RE.match(text)
    if not m or not any(m.groupdict().values()):
        if text.isdigit():
            return int(text) or None
        return None
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    minutes = (g["y"] * 525600 + g["mo"] * 43800 + g["w"] * 10080 + g["d"] * 1440
               + g["h"] * 60 + g["m"] + g["s"] / 60)
    return round(minutes) or None


def pick_image_url(value) -> str | None:
    """Image may be a string, ImageObject, or list of either; prefer the widest."""
    candidates: list[tuple[int, str]] = []

    def add(item):
        if isinstance(item, str) and item.strip():
            candidates.append((0, item.strip()))
        elif isinstance(item, dict):
            url = item.get("url") or item.get("contentUrl") or item.get("@id")
            if isinstance(url, list):
                for u in url:
                    add(u)
                return
            if isinstance(url, str) and url.strip():
                try:
                    width = int(float(item.get("width") or 0))
                except (TypeError, ValueError):
                    width = 0
                candidates.append((width, url.strip()))
        elif isinstance(item, list):
            for sub in item:
                add(sub)

    add(value)
    candidates = [c for c in candidates if c[1].startswith(("http://", "https://", "//"))]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c[0]) if any(w for w, _ in candidates) else candidates[0]
    url = best[1]
    return "https:" + url if url.startswith("//") else url


def parse_instructions(value) -> list[str]:
    """Flatten HowToStep / HowToSection / plain strings into a list of steps.

    Section headings are kept as entries prefixed with "## ".
    """
    steps: list[str] = []

    def add(item):
        if item is None:
            return
        if isinstance(item, str):
            text = item
            if "<li" in text.lower():
                for li in re.findall(r"<li[^>]*>(.*?)</li>", text, flags=re.S | re.I):
                    add(li)
                return
            parts = [p for p in re.split(r"\n\s*\n|\r\n\r\n|\n", html.unescape(text)) if clean_text(p)]
            for part in parts if len(parts) > 1 else [text]:
                cleaned = re.sub(r"^\s*(?:step\s*)?\d+[.)]\s+", "", clean_text(part), flags=re.I)
                if cleaned:
                    steps.append(cleaned)
            return
        if isinstance(item, list):
            for sub in item:
                add(sub)
            return
        if isinstance(item, dict):
            types = _types(item)
            if "howtosection" in types:
                name = clean_text(item.get("name"))
                if name:
                    steps.append(SECTION_PREFIX + name)
                add(item.get("itemListElement") or item.get("steps"))
                return
            if "itemlist" in types:
                add(item.get("itemListElement"))
                return
            text = clean_text(item.get("text") or item.get("description") or item.get("name"))
            if text:
                steps.append(text)
            if item.get("itemListElement") and not item.get("text"):
                add(item.get("itemListElement"))

    add(value)
    deduped = []
    for s in steps:
        if not deduped or deduped[-1] != s:
            deduped.append(s)
    return deduped


def parse_yield(value) -> str:
    values = value if isinstance(value, list) else [value]
    texts = [clean_text(v) for v in values if clean_text(v)]
    if not texts:
        return ""
    best = max(texts, key=lambda t: (not t.isdigit(), len(t)))
    best = re.sub(r"\bserving\(s\)", "servings", best, flags=re.I)
    if re.fullmatch(r"\d+(?:\s*-\s*\d+)?", best):
        return f"{best} servings"
    return best


def _names(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name")
        if isinstance(item, str):
            out.extend(p for p in (clean_text(x) for x in item.split(",")) if p)
    return out


def normalize_tags(values) -> list[str]:
    tags: list[str] = []
    for v in values:
        t = clean_text(v).lower().strip(" #")
        if t and len(t) <= 30 and t not in tags:
            tags.append(t)
    return tags


def split_tags(text: str | None) -> list[str]:
    if not text:
        return []
    return normalize_tags(re.split(r"[,;\n]", text))


def source_name_for(node: dict, url: str) -> str:
    publisher = node.get("publisher")
    if isinstance(publisher, list):
        publisher = publisher[0] if publisher else None
    if isinstance(publisher, dict) and clean_text(publisher.get("name")):
        return clean_text(publisher.get("name"))
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    return host


def slugify(text: str) -> str:
    text = html.unescape(text).lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80].strip("-") or "recipe"


def normalize(node: dict, url: str) -> dict:
    title = clean_text(node.get("name") or node.get("headline"))
    if not title:
        raise ImportError_("recipe has no name")
    raw_ingredients = node.get("recipeIngredient") or node.get("ingredients") or []
    if isinstance(raw_ingredients, str):
        raw_ingredients = raw_ingredients.split("\n")
    ingredients = [tidy_ingredient(i) for i in raw_ingredients if clean_text(i)]
    steps = parse_instructions(node.get("recipeInstructions"))
    if not ingredients and not steps:
        raise ImportError_("recipe has no ingredients or steps")

    prep = parse_duration(node.get("prepTime"))
    cook = parse_duration(node.get("cookTime"))
    total = parse_duration(node.get("totalTime"))
    if total is None and (prep or cook):
        total = (prep or 0) + (cook or 0)

    authors = _names(node.get("author"))
    tags = normalize_tags(_names(node.get("recipeCategory")) + _names(node.get("recipeCuisine")))

    return {
        "title": title,
        "source_url": url,
        "source_name": source_name_for(node, url),
        "author": ", ".join(a.title() if a.isupper() else a for a in authors),
        "image": None,
        "image_source_url": pick_image_url(node.get("image")),
        "description": clean_text(node.get("description")),
        "prep_minutes": prep,
        "cook_minutes": cook,
        "total_minutes": total,
        "servings": parse_yield(node.get("recipeYield")),
        "ingredients": ingredients,
        "steps": steps,
        "tags": tags,
        "notes": "",
    }


# --------------------------------------------------------------------------- persistence


def load_all() -> list[dict]:
    recipes = []
    for path in sorted(RECIPES_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            recipes.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            print(f"  warning: could not parse {path}", file=sys.stderr)
    return recipes


def find_existing(source_url: str | None = None, title: str | None = None) -> dict | None:
    def key(u: str) -> str:
        p = urllib.parse.urlparse(u.strip())
        return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()

    for r in load_all():
        if source_url and r.get("source_url") and key(r["source_url"]) == key(source_url):
            return r
        if title and not source_url and not r.get("source_url") and r.get("title", "").lower() == title.lower():
            return r
    return None


def unique_slug(base: str) -> str:
    slug, n = base, 2
    while (RECIPES_DIR / f"{slug}.json").exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def download_image(url: str, slug: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={**BROWSER_HEADERS, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                                                   "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = (resp.headers.get_content_type() or "").lower()
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  warning: image download failed ({exc})", file=sys.stderr)
        return None
    ext = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/avif": ".avif", "image/gif": ".gif"}.get(ctype)
    if ext is None:
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            print(f"  warning: image has unexpected type {ctype!r}; skipping", file=sys.stderr)
            return None
    IMAGES_DIR.mkdir(exist_ok=True)
    for old in IMAGES_DIR.glob(f"{slug}.*"):
        old.unlink()
    path = IMAGES_DIR / f"{slug}{ext}"
    path.write_bytes(data)
    return f"images/{path.name}"


FIELD_ORDER = ["slug", "title", "source_url", "source_name", "author", "image", "image_source_url", "description",
               "prep_minutes", "cook_minutes", "total_minutes", "servings", "ingredients", "steps", "tags", "notes",
               "added"]


def save_recipe(recipe: dict) -> Path:
    RECIPES_DIR.mkdir(exist_ok=True)
    ordered = {k: recipe.get(k) for k in FIELD_ORDER}
    for k, v in recipe.items():
        ordered.setdefault(k, v)
    path = RECIPES_DIR / f"{recipe['slug']}.json"
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
    return path


def build_index() -> Path:
    entries = []
    for r in load_all():
        entries.append({
            "slug": r["slug"],
            "title": r.get("title", ""),
            "image": r.get("image"),
            "description": r.get("description", ""),
            "total_minutes": r.get("total_minutes"),
            "servings": r.get("servings", ""),
            "tags": r.get("tags", []),
            "source_name": r.get("source_name", ""),
            "added": r.get("added", ""),
            "search": " ".join([r.get("title", ""), " ".join(r.get("tags", [])),
                                " ".join(i for i in r.get("ingredients", []) if not i.startswith(SECTION_PREFIX))]).lower(),
        })
    entries.sort(key=lambda e: (e.get("added") or "", e["title"].lower()), reverse=True)
    INDEX_PATH.write_text(json.dumps({"count": len(entries), "recipes": entries}, indent=2, ensure_ascii=False) + "\n")
    return INDEX_PATH


def today() -> str:
    return dt.date.today().isoformat()


def import_url(url: str, extra_tags: list[str] | None = None, notes: str = "", force: bool = False,
               page: str | None = None) -> dict:
    url = url.strip()
    existing = find_existing(source_url=url)
    if existing and not force:
        print(f"  already have {url} as {existing['slug']}; skipping (use --force to re-import)")
        return existing

    if page is None:
        print(f"Fetching {url}")
        page = fetch_page(url)
    nodes = find_recipe_nodes(page)
    if not nodes:
        raise ImportError_("no schema.org Recipe found on the page")
    node = max(nodes, key=lambda n: len(n.get("recipeIngredient") or []) + len(str(n.get("recipeInstructions") or "")))
    recipe = normalize(node, url)

    recipe["slug"] = existing["slug"] if existing else unique_slug(slugify(recipe["title"]))
    recipe["added"] = existing.get("added") if existing else today()
    recipe["tags"] = normalize_tags(recipe["tags"] + (extra_tags or []) + ((existing or {}).get("tags") or []))
    recipe["notes"] = notes or (existing or {}).get("notes", "")
    if recipe["image_source_url"]:
        recipe["image"] = download_image(recipe["image_source_url"], recipe["slug"])

    path = save_recipe(recipe)
    build_index()
    print(f"  saved {path.relative_to(ROOT)} ({len(recipe['ingredients'])} ingredients, {len(recipe['steps'])} steps)")
    return recipe


def split_lines(text: str | None) -> list[str]:
    if not text:
        return []
    lines = []
    for line in re.split(r"\r?\n", text):
        line = line.strip()
        line = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
        if line:
            lines.append(line)
    return lines


def import_manual(title: str, ingredients: str, steps: str, notes: str = "", tags: str = "", source_url: str = "",
                  description: str = "", servings: str = "", prep: str = "", cook: str = "", total: str = "") -> dict:
    """Create a recipe from typed-in text (one ingredient / step per line)."""
    title = clean_text(title)
    if not title:
        raise ImportError_("manual recipe needs a title")
    existing = find_existing(source_url=source_url or None, title=title)
    if existing:
        print(f"  already have {title!r} as {existing['slug']}; skipping")
        return existing

    def minutes(v: str) -> int | None:
        v = (v or "").strip()
        if not v:
            return None
        if v.upper().startswith("P"):
            return parse_duration(v)
        h = re.search(r"(\d+(?:\.\d+)?)\s*h", v, re.I)
        m = re.search(r"(\d+)\s*m", v, re.I)
        if h or m:
            return round(float(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
        return int(float(v)) if re.fullmatch(r"\d+(?:\.\d+)?", v) else None

    prep_m, cook_m, total_m = minutes(prep), minutes(cook), minutes(total)
    if total_m is None and (prep_m or cook_m):
        total_m = (prep_m or 0) + (cook_m or 0)
    recipe = {
        "slug": unique_slug(slugify(title)),
        "title": title,
        "source_url": source_url or None,
        "source_name": urllib.parse.urlparse(source_url).netloc.removeprefix("www.") if source_url else "Family recipe",
        "author": "",
        "image": None,
        "image_source_url": None,
        "description": clean_text(description),
        "prep_minutes": prep_m,
        "cook_minutes": cook_m,
        "total_minutes": total_m,
        "servings": parse_yield(servings) if servings else "",
        "ingredients": [tidy_ingredient(i) if not i.startswith("#") else SECTION_PREFIX + i.lstrip("# ")
                        for i in split_lines(ingredients)],
        "steps": [clean_text(s) if not s.startswith("#") else SECTION_PREFIX + s.lstrip("# ") for s in split_lines(steps)],
        "tags": split_tags(tags),
        "notes": (notes or "").strip(),
        "added": today(),
    }
    path = save_recipe(recipe)
    build_index()
    print(f"  saved {path.relative_to(ROOT)} (manual)")
    return recipe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="recipe page URL")
    ap.add_argument("--tags", default="", help="comma-separated tags to add")
    ap.add_argument("--notes", default="", help="family notes to store with the recipe")
    ap.add_argument("--force", action="store_true", help="re-import even if this URL already exists")
    ap.add_argument("--html", help="parse a saved HTML file instead of fetching (use with --url)")
    ap.add_argument("--rebuild-index", action="store_true", help="only regenerate recipes/index.json")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    if args.rebuild_index:
        print(f"wrote {build_index().relative_to(ROOT)}")
        return 0
    if not args.url:
        ap.error("a recipe URL is required")
    page = Path(args.html).read_text(errors="replace") if args.html else None
    try:
        import_url(args.url, split_tags(args.tags), args.notes, args.force, page=page)
    except (ImportError_, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
