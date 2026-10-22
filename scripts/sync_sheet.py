#!/usr/bin/env python3
"""Import new rows from a published Google Sheet (CSV) into recipes/.

The sheet URL comes from the RECIPE_SHEET_CSV environment variable (in Actions it
is the repository variable of the same name). If it's unset, exit quietly.

Recognized columns (header names are case-insensitive; extra columns such as a
Google Form "Timestamp" are ignored):
    url, title, ingredients, steps, notes, tags,
    description, servings, prep, cook, total, image

Rows with a url are imported from the page. Rows without a url need a title plus
ingredients and steps (one per line inside the cell) and are saved as-is. If a
url import fails but the row also has ingredients and steps, those are used.

data/sheet-state.json remembers which rows were already handled, so deleting a
recipe file from the repo won't make it come back on the next run.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_recipe as ir  # noqa: E402

STATE_PATH = ir.ROOT / "data" / "sheet-state.json"
MAX_ATTEMPTS = 3

ALIASES = {
    "url": ("url", "link", "recipe url", "recipe link", "source", "source url"),
    "title": ("title", "name", "recipe", "recipe name"),
    "ingredients": ("ingredients", "ingredient list"),
    "steps": ("steps", "instructions", "directions", "method"),
    "notes": ("notes", "note", "family notes", "comments"),
    "tags": ("tags", "tag", "categories", "category"),
    "description": ("description", "summary"),
    "servings": ("servings", "serves", "yield"),
    "prep": ("prep", "prep time"),
    "cook": ("cook", "cook time"),
    "total": ("total", "total time", "time"),
    "image": ("image", "photo", "image url", "photo url"),
}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"imported": [], "failed": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    state["imported"] = sorted(set(state["imported"]))
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": ir.BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig", errors="replace")
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise SystemExit("RECIPE_SHEET_CSV returned HTML, not CSV. Use File > Share > Publish to web > CSV.")
    reader = csv.DictReader(io.StringIO(text))
    lookup = {}
    for header in reader.fieldnames or []:
        h = (header or "").strip().lower()
        for field, names in ALIASES.items():
            if h in names and field not in lookup.values():
                lookup[header] = field
                break
    rows = []
    for raw in reader:
        row = {field: (raw.get(header) or "").strip() for header, field in lookup.items()}
        if any(row.values()):
            rows.append(row)
    return rows


def row_key(row: dict) -> str | None:
    if row.get("url"):
        p = urllib.parse.urlparse(row["url"])
        return "url:" + (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()
    if row.get("title"):
        return "title:" + row["title"].strip().lower()
    return None


def handle_row(row: dict) -> str:
    url = row.get("url", "")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    has_manual = bool(row.get("title") and row.get("ingredients") and row.get("steps"))
    tags = ir.split_tags(row.get("tags"))

    if url:
        try:
            recipe = ir.import_url(url, tags, row.get("notes", ""))
            return recipe["slug"]
        except (ir.ImportError_, urllib.error.URLError, OSError) as exc:
            if not has_manual:
                raise
            print(f"  url import failed ({exc}); using the typed-in ingredients and steps")

    if not has_manual:
        raise ir.ImportError_("row needs a url, or a title with ingredients and steps")
    recipe = ir.import_manual(
        title=row["title"], ingredients=row["ingredients"], steps=row["steps"], notes=row.get("notes", ""),
        tags=row.get("tags", ""), source_url=url, description=row.get("description", ""),
        servings=row.get("servings", ""), prep=row.get("prep", ""), cook=row.get("cook", ""),
        total=row.get("total", ""),
    )
    image_url = row.get("image", "")
    if image_url.startswith(("http://", "https://")) and not recipe.get("image"):
        recipe["image_source_url"] = image_url
        recipe["image"] = ir.download_image(image_url, recipe["slug"])
        ir.save_recipe(recipe)
        ir.build_index()
    return recipe["slug"]


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    sheet_url = os.environ.get("RECIPE_SHEET_CSV", "").strip()
    if not sheet_url:
        print("RECIPE_SHEET_CSV is not set; nothing to sync.")
        return 0

    state = load_state()
    rows = fetch_csv(sheet_url)
    print(f"Sheet has {len(rows)} row(s)")
    added, failed = [], []
    for row in rows:
        key = row_key(row)
        if not key or key in state["imported"]:
            continue
        attempts = state["failed"].get(key, {}).get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            continue
        label = row.get("url") or row.get("title")
        if not row.get("url") and not (row.get("ingredients") and row.get("steps")):
            print(f"- {label}: waiting for ingredients and steps")
            continue
        print(f"- {label}")
        existed = bool(row.get("url") and ir.find_existing(source_url=row["url"]))
        try:
            slug = handle_row(row)
        except Exception as exc:  # one bad row shouldn't stop the rest
            attempts += 1
            state["failed"][key] = {"attempts": attempts, "error": str(exc)[:300]}
            failed.append((label, str(exc), attempts))
            print(f"  failed (attempt {attempts}/{MAX_ATTEMPTS}): {exc}")
            continue
        state["imported"].append(key)
        state["failed"].pop(key, None)
        if not existed:
            added.append((label, slug))

    save_state(state)
    ir.build_index()

    summary = [f"Imported {len(added)} recipe(s), {len(failed)} failure(s)."]
    summary += [f"- added `{slug}` from {label}" for label, slug in added]
    summary += [f"- FAILED {label}: {err} (attempt {n}/{MAX_ATTEMPTS})" for label, err, n in failed]
    print("\n".join(summary))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write("\n".join(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
