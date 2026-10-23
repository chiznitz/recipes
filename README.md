# Heather's Recipe Box

Family recipes in one clean, consistent format, no matter where they came from.

**Live site:** https://chiznitz.github.io/recipes/

Plain static site served by GitHub Pages from the root of `main`. There's no build
step: `index.html` reads the JSON files in `recipes/`.

## Adding a recipe

### From a link (works from the GitHub phone app)

1. Open the repo in the GitHub app (or github.com) and go to **Actions**, then **Add recipes**.
2. Tap **Run workflow**.
3. Paste the recipe page link into **Recipe URL**. Tags and family notes are optional.
4. Tap **Run workflow**. It takes about a minute, and the site updates a minute or two after that.

Shortcut: https://github.com/chiznitz/recipes/actions/workflows/add-recipes.yml

The importer reads only the structured recipe data that recipe sites publish for
Google (schema.org `Recipe`), so ads, pop-ups, and the life story above the recipe are
never saved. If a site doesn't publish that data, the run fails with a red X and
nothing is changed. Add that recipe through the sheet instead (below).

### From a Google Sheet (or a Google Form that feeds one)

The same workflow checks a Google Sheet every hour and imports any rows it hasn't seen yet.

The site's **Add a recipe** link (in the footer of every page) opens the family Google Form,
which feeds this sheet:
https://docs.google.com/forms/d/e/1FAIpQLSeDqux4RSuLJjeHEYwAQh0f1tM6EgGlyRR-nRMEVmthjfPkNQ/viewform

1. Make a sheet with a header row. Use any of these columns, in any order (extra
   columns, like a Form's `Timestamp`, are ignored):

   | column | what goes in it |
   | --- | --- |
   | `url` | link to a recipe page (optional) |
   | `title` | recipe name (required when there's no url) |
   | `ingredients` | one ingredient per line inside the cell |
   | `steps` | one step per line inside the cell |
   | `notes` | family notes ("Grandma doubles the butter") |
   | `tags` | comma-separated, e.g. `thanksgiving, side dish` |
   | `description`, `servings`, `prep`, `cook`, `total`, `image` | optional extras for typed-in recipes; times like `45`, `1h 15m`, or `PT45M` |

   * A row with a `url` is imported from that page. Any `tags` and `notes` in the row are added to it.
   * A row without a `url` is a typed-in family recipe and needs `title`, `ingredients`, and `steps`.
     Until all three are filled in, the row is left alone, so a half-typed recipe won't be saved.
   * Inside `ingredients` or `steps`, a line starting with `## ` becomes a section heading,
     e.g. `## For the topping`.
   * If a link fails but the row also has typed-in ingredients and steps, those are used.

2. In Google Sheets: **File → Share → Publish to web**. Pick the sheet tab, choose
   **Comma-separated values (.csv)**, click **Publish**, and copy the link.
3. In this repo: **Settings → Secrets and variables → Actions → Variables → New repository variable**.
   Name it `RECIPE_SHEET_CSV` and paste the link as the value.

That's it. Until the variable exists, the hourly job is skipped. To import right away
instead of waiting for the hour, run **Add recipes** with the URL box left blank.

`data/sheet-state.json` remembers which rows were already handled, so deleting a
recipe from the repo doesn't bring it back on the next sync. A link that fails 3 times
is skipped after that. Remove its entry under `failed` in that file to retry it.

> GitHub pauses scheduled workflows in repos with no commits for 60 days. If the
> hourly sync stops, open **Actions → Add recipes** and click **Enable workflow**.

### From a computer

```sh
python3 scripts/import_recipe.py https://www.allrecipes.com/recipe/24746/baked-cream-corn/ --tags "thanksgiving"
python3 -m http.server   # preview at http://localhost:8000
```

Python 3.10+ with no extra packages. Some sites (Allrecipes and other People Inc.
brands) block scripted downloads. When that happens the importer opens the page in
headless Chrome instead, so have Chrome or Chromium installed (GitHub's runners already do).
You can also save the page from your browser and run
`python3 scripts/import_recipe.py --html saved.html --url <url>`.

## Editing or removing a recipe

Every recipe is a plain JSON file in `recipes/`. Edit it on github.com (pencil icon)
to fix a typo, add notes, or add tags. To remove one, delete `recipes/<slug>.json`
and its photo in `images/`. Either way `recipes/index.json` is rebuilt automatically.
Locally, run `python3 scripts/import_recipe.py --rebuild-index`.

## Recipe format

`recipes/<slug>.json`:

```json
{
  "slug": "baked-cream-corn",
  "title": "Baked Cream Corn",
  "source_url": "https://www.allrecipes.com/recipe/24746/baked-cream-corn/",
  "source_name": "Allrecipes",
  "author": "Tamara Hersom",
  "image": "images/baked-cream-corn.jpg",
  "image_source_url": "https://…",
  "description": "…",
  "prep_minutes": 10,
  "cook_minutes": 45,
  "total_minutes": 65,
  "servings": "8 servings",
  "ingredients": ["2 eggs", "…"],
  "steps": ["Preheat the oven to 350 degrees F (175 degrees C).", "…"],
  "tags": ["dinner", "side dish", "southern"],
  "notes": "",
  "added": "2026-10-22"
}
```

* Times are whole minutes (`null` if unknown).
* An entry in `ingredients` or `steps` that starts with `## ` is a section heading.
* `recipes/index.json` is generated, so don't edit it by hand.

## Files

| path | what it is |
| --- | --- |
| `index.html` | the whole site: card grid, search, tag filters, recipe view, print styles |
| `recipes/` | one JSON file per recipe, plus the generated `index.json` |
| `images/` | recipe photos downloaded by the importer |
| `scripts/import_recipe.py` | fetch a page, extract and normalize the recipe, save the JSON and photo |
| `scripts/sync_sheet.py` | import new rows from the published Google Sheet |
| `.github/workflows/add-recipes.yml` | hourly sheet sync, add-by-URL button, and index rebuilds |
