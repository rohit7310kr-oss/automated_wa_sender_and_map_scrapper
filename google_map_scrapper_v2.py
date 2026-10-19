# QUERIES = [
#     "grocery shops",
#     "kirana stores",
#     "general stores",
#     "supermarkets",
#     "medical stores",
#     "shoe stores",
#     "clothing stores",
#     "mobile shops",
#     "electronics shops",
#     "hardware stores",
#     "stationery shops",
#     "sweet shops",
#     "bakeries",
#     "restaurants",
#     "salons",
# ]

# REGIONS = [
#     "risali",
#     "sector 6",
#     "civic center",
#     "nehru nagar",
#     "avanti bai chowk",
#     "near me",          # special case: becomes "<query> near me" (uses browser/IP location)
#     "supela",
#     "ghadi chowk",
#     "bhilai steel plant",
#     "bhilai power house",
#     "malvi nagar",
#     "junwani",
#     "kohka",
#     "borsi",
#     "chandkhuri",
# ]

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from playwright.async_api import async_playwright

# =====================================================================
# CONFIG: edit these, then just run the file. No prompts.
# =====================================================================
QUERIES = [
   "CA"
]

# One entry per city. Each city has its own state and its own list of areas.
# "regions": []  -> searches the whole city once (no area split).
LOCATIONS = [
    {
        "city": "Bhilai",
        "state": "Chhattisgarh",
        "regions": [
            "risali", "sector 6", "civic center", "nehru nagar",
            "avanti bai chowk", "supela", "ghadi chowk", "bhilai steel plant",
            "bhilai power house", "malvi nagar", "junwani", "kohka",
            "borsi", "chandkhuri",
        ],
    },
      {
        "city": "Durg",
        "state": "Chhattisgarh",
        "regions": [
            "padmanabhpur",
            "indira market",
            "civil lines",
            "kolihapuri",
            "mohan nagar",
            "gayatri nagar",
            "shivaji nagar",
            "maruti nagar",
            "nagpura",
            "pulgaon",
            "anjora",
            "kachandur",
            "station road",
            "gandhi chowk",
            "jamul",
        ],
    },
]

# False: one CSV per query, all cities together   -> collected-data/grocery_shops.csv
# True : one CSV per query per city               -> collected-data/bhilai/grocery_shops.csv
SPLIT_BY_CITY = False

RESULTS_PER_SEARCH = 120   # Google Maps gives ~120 max per search
OUTPUT_DIR = Path(__file__).parent / "collected-data"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"   # remembers finished searches
RESUME = True              # re-running skips searches already finished

WORKERS = 4                # parallel browser windows
HEADLESS = False

# =====================================================================
# SELECTORS
# =====================================================================
FEED = 'div[role="feed"]'
LINKS = f'{FEED} a.hfpxzc'
LEGACY = f'{FEED} > div > div[jsaction]'

COLUMNS = [
    "Business Name", "Business Type", "Phone Number", "Phone Digits", "Address", "Website",
    "Category", "City", "State", "Region", "Search Query",
]


# =====================================================================
# SHARED STATE
# =====================================================================
class Category:
    """All rows for one output file (one query, optionally one city)."""

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.rows = []
        self.seen = set()
        self.lock = asyncio.Lock()
        self._load_existing()

    def _load_existing(self):
        if not self.path.exists():
            return
        df = pd.read_csv(self.path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        for row in df.to_dict("records"):
            row = {c: row.get(c, "N/A") for c in COLUMNS}
            self.rows.append(row)
            self.seen.add(make_key(row["Business Name"], row["Phone Digits"], row["Address"]))

    async def save(self):
        async with self.lock:
            snapshot = list(self.rows)
            await asyncio.to_thread(self._write_atomic, snapshot)

    def _write_atomic(self, snapshot):
        # write to a temp file first, then swap it in: a crash mid-write
        # can never leave a half-written CSV behind
        tmp = self.path.with_suffix(".csv.tmp")
        pd.DataFrame(snapshot, columns=COLUMNS).to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, self.path)


class Progress:
    def __init__(self):
        self.done = set()
        self.lock = asyncio.Lock()
        if PROGRESS_FILE.exists():
            try:
                self.done = set(json.loads(PROGRESS_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                print("progress.json unreadable, starting progress tracking fresh "
                      "(your CSV data is untouched, duplicates are filtered)")

    async def mark_done(self, search):
        async with self.lock:
            self.done.add(search)
            data = json.dumps(sorted(self.done), indent=2)
            await asyncio.to_thread(self._write_atomic, data)

    def _write_atomic(self, data):
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, PROGRESS_FILE)


# =====================================================================
# HELPERS
# =====================================================================
def log(wid, msg):
    print(f"[W{wid}] {msg}", flush=True)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def clean(text):
    if not text:
        return "N/A"
    text = re.sub(r"[\ue000-\uf8ff]", "", text)
    return text.strip() or "N/A"


def digits_only(phone):
    return re.sub(r"\D", "", phone) if phone != "N/A" else "N/A"


def make_key(name, phone_digits, address):
    """Unique-contact key: phone number if present, else name + address."""
    if phone_digits and phone_digits != "N/A":
        return ("phone", phone_digits)
    return ("shop", name, address)


def build_jobs():
    """One job per (query, city, region)."""
    jobs = []
    for q in QUERIES:
        for loc in LOCATIONS:
            city, state = loc["city"], loc["state"]
            regions = loc["regions"] or [None]      # None = whole city
            for r in regions:
                area = f"{r} " if r else ""
                search = f"{q} in {area}{city} {state}".lower()

                if SPLIT_BY_CITY:
                    file_key = (q, city)
                    path = OUTPUT_DIR / slug(city) / f"{slug(q)}.csv"
                else:
                    file_key = (q,)
                    path = OUTPUT_DIR / f"{slug(q)}.csv"

                jobs.append({
                    "query": q, "city": city, "state": state,
                    "region": r or "All",
                    "search": search, "file_key": file_key, "path": path,
                })
    return jobs


async def cards(page):
    links = page.locator(LINKS)
    return links if await links.count() > 0 else page.locator(LEGACY)


async def get_attr_text(page, selector, strip_prefix):
    elem = page.locator(selector).first
    if await elem.count() == 0:
        return "N/A"
    raw = await elem.get_attribute("aria-label") or await elem.inner_text()
    return clean(raw.replace(strip_prefix, ""))


async def scroll_feed(page, wid, target_count):
    last_count, stale_rounds = 0, 0
    while True:
        await page.locator(FEED).evaluate("node => node.scrollBy(0, 10000)")
        await asyncio.sleep(2.0)

        current = await (await cards(page)).count()
        log(wid, f"loaded {current} listings in DOM")

        if current >= target_count:
            break
        if await page.get_by_text("You've reached the end of the list").count() > 0:
            log(wid, "reached end of results")
            break

        if current == last_count:
            stale_rounds += 1
            if stale_rounds >= 5:
                log(wid, "no new results loading, stopping scroll")
                break
        else:
            stale_rounds, last_count = 0, current


async def wait_for_new_name(page, previous_name, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        elem = page.locator("h1.DUwDvf").first
        if await elem.count() > 0:
            name = clean(await elem.inner_text())
            if name != "N/A" and name != previous_name:
                return name
        await asyncio.sleep(0.25)
    return None


# =====================================================================
# SCRAPE ONE SEARCH -> returns number of NEW unique contacts, or None on failure
# =====================================================================
async def scrape_job(page, wid, job, category):
    search = job["search"]
    log(wid, f"=== '{search}' ===")
    await page.goto(f"https://www.google.com/maps/search/{quote_plus(search)}", timeout=60000)

    try:
        await page.wait_for_selector(FEED, timeout=15000)
    except Exception:
        log(wid, "results sidebar not found (no results, single result, or captcha)")
        return None

    await scroll_feed(page, wid, RESULTS_PER_SEARCH)

    total_cards = await (await cards(page)).count()
    log(wid, f"processing {total_cards} cards")

    added = 0
    previous_name = None

    for i in range(min(total_cards, RESULTS_PER_SEARCH)):
        try:
            card = (await cards(page)).nth(i)
            await card.scroll_into_view_if_needed()
            await card.click()

            name = await wait_for_new_name(page, previous_name)
            if name is None:
                await card.click(force=True)
                name = await wait_for_new_name(page, previous_name)
            if name is None:
                log(wid, f"card {i + 1}/{total_cards}: panel did not load, skipped")
                continue
            previous_name = name

            address = await get_attr_text(page, 'button[data-item-id="address"]', "Address: ")
            phone = await get_attr_text(page, 'button[data-item-id^="phone:tel:"]', "Phone: ")
            phone_digits = digits_only(phone)

            # Google's own label, e.g. "Certified public accountant".
            # The class name (DkEaL) and the jsaction id change, so match on "category".
            type_elem = page.locator('button[jsaction*="category"]').first
            business_type = clean(await type_elem.inner_text()) if await type_elem.count() > 0 else "N/A"

            website = "N/A"
            web_elem = page.locator('a[data-item-id="authority"]').first
            if await web_elem.count() > 0:
                website = await web_elem.get_attribute("href") or "N/A"

            # unique check + add with no await in between -> safe across workers
            key = make_key(name, phone_digits, address)
            if key in category.seen:
                continue
            category.seen.add(key)
            category.rows.append({
                "Business Name": name,
                "Business Type": business_type,
                "Phone Number": phone,
                "Phone Digits": phone_digits,
                "Address": address,
                "Website": website,
                "Category": job["query"],
                "City": job["city"],
                "State": job["state"],
                "Region": job["region"],
                "Search Query": search,
            })
            added += 1
            log(wid, f"[{category.name}: {len(category.rows)}] {name} | {phone}")

        except Exception as e:
            log(wid, f"skipped card {i + 1}: {type(e).__name__}")
            continue

    return added


# =====================================================================
# WORKER
# =====================================================================
async def worker(wid, browser, queue, categories, progress, counter):
    await asyncio.sleep((wid - 1) * 2)   # stagger starts

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()

    try:
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            counter["n"] += 1
            tag = f"({counter['n']}/{counter['total']})"
            category = categories[job["file_key"]]

            try:
                if RESUME and job["search"] in progress.done:
                    log(wid, f"{tag} already done, skipping: {job['search']}")
                    continue

                added = await scrape_job(page, wid, job, category)

                if added:
                    await category.save()
                if added is None:
                    log(wid, f"{tag} FAILED, will retry on next run")
                else:
                    await progress.mark_done(job["search"])
                    log(wid, f"{tag} +{added} new unique | {category.path.name} now has {len(category.rows)}")

            except Exception as e:
                log(wid, f"{tag} job crashed ({type(e).__name__}): {job['search']}")
            finally:
                queue.task_done()

            await asyncio.sleep(2)

        log(wid, "no more jobs, closing")
    finally:
        await context.close()


# =====================================================================
# MAIN
# =====================================================================
async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    jobs = build_jobs()

    # one Category (= one CSV) per file_key; existing files are loaded to resume
    categories = {}
    for job in jobs:
        if job["file_key"] not in categories:
            job["path"].parent.mkdir(parents=True, exist_ok=True)
            label = " / ".join(job["file_key"])
            categories[job["file_key"]] = Category(label, job["path"])

    progress = Progress()

    queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    counter = {"n": 0, "total": len(jobs)}
    n_workers = min(WORKERS, len(jobs))

    print(f"{len(QUERIES)} queries x {len(LOCATIONS)} cities -> {len(jobs)} searches")
    print(f"{len(categories)} output file(s) in: {OUTPUT_DIR}")
    print(f"Starting {n_workers} worker(s)\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await asyncio.gather(
            *(worker(i + 1, browser, queue, categories, progress, counter)
              for i in range(n_workers))
        )
        await browser.close()

    print("\nDone. Unique contacts per file:")
    for cat in categories.values():
        print(f"  {cat.path.relative_to(OUTPUT_DIR)}: {len(cat.rows)}")


if __name__ == "__main__":
    asyncio.run(main())