from __future__ import annotations

import argparse
import asyncio
import pandas as pd
import re
from pathlib import Path
from typing import List, Tuple
import logging

from playwright.async_api import async_playwright, Page
from fuzzywuzzy import fuzz

UCC_SCHOLAR_URL = "https://scholar.ucc.edu.gh/publications"

# CSS selectors – verified in manual devtools session
SEARCH_INPUT = "#searchInput"
TABLE_ROW = "datatable-row-wrapper"
# Column order in the UI: 1) #, 2) TITLE, 3) AUTHORS, 4) CITED BY, 5) YEAR
TITLE_ANCHOR = "datatable-body-cell:nth-child(2) a"
AUTHORS_CELL = "datatable-body-cell:nth-child(3)"
# The year appears in the 5th visible column in the current UI
YEAR_CELL = "datatable-body-cell:nth-child(5)"
# Header cell selector to sort by year
YEAR_HEADER = "datatable-header-cell:nth-child(5)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def scrape_for_name(
    page: Page,
    query: str,
    year_min: int,
    year_max: int,
    per_name_limit: int | None = None,
) -> List[Tuple[str, str, str, str]]:
    """Return list of (authors, title, year, link) rows for **query**.

    Strategy:
        1. Type query into global search box.
        2. Click the YEAR header twice → descending sort (newest first).
        3. Scroll the virtual scroller while the current row's year ≥ year_min.
        4. Stop when:   • no new rows render   OR   • pub_year < year_min   OR   • optional per_name_limit reached.
    """

    await page.fill(SEARCH_INPUT, "")  # clear previous search
    await page.fill(SEARCH_INPUT, query)
    await page.keyboard.press("Enter")

    # Wait until the datatable is refreshed (rows appear or confirm empty) –
    # max 1.5 s fallback instead of hard-sleeping every time.
    try:
        await page.wait_for_selector(TABLE_ROW, timeout=1500)
    except Exception:
        # No rows – early return
        return []

    # Ensure we are sorted descending by year (two clicks toggles asc→desc)
    try:
        await page.click(YEAR_HEADER)
        await page.click(YEAR_HEADER)
        # small debounce so DOM settles
        await page.wait_for_timeout(250)
    except Exception:
        # If header not clickable, continue without explicit sort
        pass

    results: List[Tuple[str, str, str, str]] = []

    # scroller that controls virtual scrolling inside the data-table
    scroller = page.locator("datatable-scroller")

    last_count = -1
    while True:
        rows = page.locator(TABLE_ROW)
        count = await rows.count()

        # extract any newly rendered rows
        for i in range(last_count + 1, min(count, per_name_limit if per_name_limit is not None else float("inf"))):
            row = rows.nth(i)
            authors = (await row.locator(AUTHORS_CELL).inner_text()).strip()
            title_el = row.locator(TITLE_ANCHOR)
            title = (await title_el.inner_text()).strip()
            link = await title_el.get_attribute("href") or ""
            year_text = (await row.locator(YEAR_CELL).inner_text()).strip()

            try:
                pub_year = int(re.search(r"(\d{4})", year_text).group(1))
            except Exception:
                pub_year = None

            # Break if we've scrolled past the target period (since we sorted desc)
            if pub_year is not None and pub_year < year_min:
                return results

            results.append((authors, title, year_text, link))

        if count == last_count or (
            per_name_limit is not None and len(results) >= per_name_limit
        ):
            break  # no new rows rendered or hit limit

        last_count = count

        # Scroll further down inside the table to trigger more rows
        try:
            await scroller.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
        except Exception:
            break  # scroller not found or cannot scroll further
        await page.wait_for_timeout(250)

    if per_name_limit is not None:
        return results[:per_name_limit]
    return results


def clean_match(candidate: str, target: str) -> bool:
    return fuzz.partial_ratio(candidate.lower(), target.lower()) >= 90


def build_queries(full_name: str) -> List[str]:
    """Return list of search strings to cover common author name variants.

    Variants generated:
        1. full name (unchanged)
        2. last name only
        3. first name only
        4. first initial + last name ("G Adjei")
        5. each middle name (if any) in full
        6. each middle initial + last name ("K Adjei")
    """
    parts = [p.strip() for p in full_name.split() if p.strip()]
    if not parts:
        return [full_name]

    first = parts[0]
    last = parts[-1]
    middles = parts[1:-1]

    variants = {
        full_name.strip(),
        f"{first[0]} {last}",  # F Anyan
    }

    for m in middles:
        variants.add(f"{m[0]} {last}")  # K Anyan

    # concatenated initials (no space) + last name -> "FK Anyan"
    initials_concat = first[0] + "".join(m[0] for m in middles)
    if initials_concat:
        variants.add(f"{initials_concat} {last}")

    # first name + space + middle+last initials concatenated -> "Festus KA"
    if middles:
        mid_last_concat = "".join(m[0] for m in middles) + last[0]
        variants.add(f"{first} {mid_last_concat}")

    # return list preserving original preference order
    preferred_order = [
        full_name.strip(),
        f"{first[0]} {last}",
    ]
    preferred_order += [f"{m[0]} {last}" for m in middles]
    if initials_concat:
        preferred_order.append(f"{initials_concat} {last}")
    if middles:
        preferred_order.append(f"{first} {mid_last_concat}")

    ordered: List[str] = []
    for v in preferred_order:
        if v and v not in ordered:
            ordered.append(v)
    for v in variants:
        if v not in ordered:
            ordered.append(v)
    return ordered


async def fetch_period(period: str, per_name_limit: int = 10):
    csv_awardee = Path("Data/awardees_by_period") / f"awardees_{period}.csv"
    if not csv_awardee.exists():
        raise FileNotFoundError(csv_awardee)

    # Load awardee names with pandas for robustness to varying column order/names
    awardee_df = pd.read_csv(csv_awardee)
    if "awardee" in awardee_df.columns:
        awardees = awardee_df["awardee"].dropna().tolist()
    else:
        # Fallback: assume the second column contains the names
        awardees = awardee_df.iloc[:, 1].dropna().tolist()

    logging.info(f"Loaded {len(awardees)} awardees for period {period}")
    # Prepare output path
    out_dir = Path("Data") / period
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "raw_publications.csv"

    collected: List[Tuple[str, str, int, str]] = []  # (authors, title, year, link)

    # Determine numeric year bounds from provided period string
    years_in_period = [int(y) for y in re.findall(r"\d{4}", period)]
    if not years_in_period:
        raise ValueError(f"Cannot determine year(s) from period string '{period}'. Provide as 'YYYY' or 'YYYY-YYYY'.")
    if len(years_in_period) == 1:
        year_min = year_max = years_in_period[0]
    else:
        year_min, year_max = min(years_in_period), max(years_in_period)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(UCC_SCHOLAR_URL, timeout=60000)
        # TODO: apply year filter via UI when figured out – placeholder only.
        await page.wait_for_selector(SEARCH_INPUT)

        logging.info(f"Starting fetch for period {period}")
        period_label = f"{year_min}" if year_min == year_max else f"{year_min}-{year_max}"

        for awardee in awardees:
            queries = build_queries(awardee)
            for q in queries:
                logging.info(f"Search '{q}' for {awardee} within {period_label}")
                rows = await scrape_for_name(page, q, year_min, year_max, per_name_limit if per_name_limit > 0 else None)
                logging.info(f"  Found {len(rows)} rows for query '{q}'")

                for authors, title, year_text, link in rows:
                    # parse year
                    try:
                        pub_year = int(re.search(r"(\d{4})", year_text).group(1))
                    except Exception:
                        continue

                    if year_min <= pub_year <= year_max:
                        collected.append((authors, title, pub_year, link))

        await browser.close()

    df_out = pd.DataFrame(collected, columns=["authors", "title", "year", "scholar_link"])

    # Remove rows that are fully identical across all columns
    before_dedup = len(df_out)
    df_out.drop_duplicates(inplace=True)
    after_dedup = len(df_out)
    if after_dedup < before_dedup:
        logging.info(f"Dropped {before_dedup - after_dedup} exact duplicate rows")

    df_out.to_csv(out_file, index=False)
    logging.info(f"Saved {len(collected)} records -> {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True, help="Academic year string, e.g. 2016-2017 or 2015/2016 or 2020")
    parser.add_argument("--limit", type=int, default=1000, help="Max rows per awardee to capture; 0 = unlimited (default 1000)")
    args = parser.parse_args()

    asyncio.run(fetch_period(args.period, args.limit)) 