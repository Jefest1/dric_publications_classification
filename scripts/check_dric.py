from __future__ import annotations

import sys
import argparse
import asyncio
import hashlib
import logging
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

# Ensure project root is on sys.path so 'settings' can be imported regardless of cwd
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
from firecrawl import FirecrawlApp
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage
from playwright.async_api import async_playwright, Browser

from settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


# External API clients initialised once
firecrawl_client = FirecrawlApp(api_key=settings.FIRECRAWL_API_KEY)

# Caches
_ARTICLE_CACHE: dict[str, str] = {}
_DRIC_CACHE: dict[str, str] = {}

# Simple per-process rate-limit tracking to avoid hammering Firecrawl / Groq.
_LAST_FIRECRAWL_CALL: float | None = None
_FIRECRAWL_MIN_INTERVAL = 2  
_LAST_GROQ_CALL: float | None = None
_GROQ_MIN_INTERVAL = 1 

llm = ChatGroq(model="meta-llama/llama-4-scout-17b-16e-instruct", temperature=0, api_key=settings.GROQ_API_KEY)


PROMPT_TEMPLATE = (
    "Answer YES or NO only. Does the following text acknowledge funding or support from "
    "the Directorate of Research Innovation and Consultancy (DRIC) of the University of Cape Coast"
    "Look for phrases like 'Directorate of Research Innovation and Consultancy', or 'DRIC'"
    "\n\n{text}"
)


async def _extract_article_url(ctx, scholar_url: str) -> str | None:
    """Resolve a *scholar_url* to a crawl-ready full-text link.

    We now only look for the primary publisher link shown by Google Scholar:
        • CSS selector `#gsc_oci_title > a`

    If the selector is missing or the href still points to Scholar, we return ``None``.
    """

    page = None
    try:
        # Open a fresh page for this navigation
        page = await ctx.new_page()
        await page.goto(scholar_url, timeout=30_000, wait_until="domcontentloaded")

        link = None
        try:
            link = await page.locator("#gsc_oci_title a").first.get_attribute("href", timeout=5000)
        except Exception:
            pass

        if link:
            if link.startswith("http") and "scholar.google" not in urlparse(link).netloc:
                return link  # primary success

            logging.info(
                "Title hyperlink unresolved or loops back to Scholar — url=%s href=%s",
                scholar_url,
                link,
            )
        else:
            logging.info("No hyperlink found in title for %s", scholar_url)

        # ---------------- Fallback: citation table (row 9) -----------------
        try:
            table_link = await (
                page.locator('#gsc_oci_table > div:nth-child(9) .gsc_oci_value a')
                .first
                .get_attribute("href", timeout=3000)
            )
        except Exception:
            table_link = None

        if not table_link:
            logging.info("Fallback table-link not found for %s", scholar_url)
            return None

        logging.info("Fallback table-link found → %s", table_link)

        if "ir.ucc.edu.gh/xmlui/handle" not in table_link:
            # normal external link
            if "scholar.google" not in urlparse(table_link).netloc:
                return table_link
            logging.info("Table-link loops back to Scholar, skipping")
            return None

        # ---------------- Dive into UCC repository ------------------------
        try:
            logging.info("UCC repository detected, navigating…")
            await page.goto(table_link, timeout=30_000, wait_until="domcontentloaded")

            repo_file = await (
                page.locator("#aspect_artifactbrowser_ItemViewer_div_item-view .file-list .file-link a")
                .first
                .get_attribute("href", timeout=5000)
            )
            if repo_file:
                if repo_file.startswith("/"):
                    repo_file = urljoin(table_link, repo_file)
                logging.info("Resolved UCC repository file link → %s", repo_file)
                return repo_file
            logging.warning("No file link found inside UCC repository page %s", table_link)
        except Exception as ub_exc:
            logging.warning("Error processing UCC repository fallback (%s): %s", table_link, ub_exc)

        return None

    except Exception as exc:
        logging.error(f"Playwright nav error ({scholar_url}): {exc}")
        return None

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


def _crawl_article(url: str, max_retries: int = 3) -> str:
    """Fetch main content from *url* using Firecrawl with basic 429 retry logic."""

    if url in _ARTICLE_CACHE:
        return _ARTICLE_CACHE[url]

    global _LAST_FIRECRAWL_CALL

    # Build Firecrawl scrape options once
    scrape_opts: dict = {
        "formats": ["markdown"],
        "timeout": 45_000,  # a bit lower
        "blockAds": False,
        "proxy": "stealth",
    }
    if not url.lower().endswith(".pdf"):
        scrape_opts["waitFor"] = 500  # shorter JS wait

    delay_base = _FIRECRAWL_MIN_INTERVAL

    for attempt in range(1, max_retries + 1):
        if _LAST_FIRECRAWL_CALL is not None:
            elapsed = time.monotonic() - _LAST_FIRECRAWL_CALL
            if elapsed < _FIRECRAWL_MIN_INTERVAL:
                time.sleep(_FIRECRAWL_MIN_INTERVAL - elapsed)

        try:
            logging.debug("Firecrawl attempt %d for %s", attempt, url)
            result = firecrawl_client.scrape_url(url=url, **scrape_opts)

            # Firecrawl may return a dict (recommended) or raw string depending on version
            if isinstance(result, dict):
                text = result.get("markdown") or result.get("text") or ""
            else:
                text = str(result)

            text = text or ""

            _ARTICLE_CACHE[url] = text
            logging.info("Firecrawl ok (%d chars) → %s", len(text), url)
            return text

        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "rate limit" in msg:
                backoff = delay_base * attempt  # 2,4,6s
                logging.warning(f"Firecrawl rate-limit hit. Retry {attempt}/{max_retries} in {backoff}s…")
                time.sleep(backoff)
                continue

            logging.error(f"Firecrawl error ({url}): {exc}")
            break  # non-rate-limit error – don't retry further

    _ARTICLE_CACHE[url] = ""
    return ""


def _ask_dric(text: str, max_retries: int = 4) -> str:
    """Classify text via Groq with retry on 429."""

    if not text:
        return "NO"

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest in _DRIC_CACHE:
        return _DRIC_CACHE[digest]

    global _LAST_GROQ_CALL

    for attempt in range(1, max_retries + 1):
        # Respect minimum interval between calls
        if _LAST_GROQ_CALL is not None:
            elapsed = time.monotonic() - _LAST_GROQ_CALL
            if elapsed < _GROQ_MIN_INTERVAL:
                time.sleep(_GROQ_MIN_INTERVAL - elapsed)

        try:
            logging.debug("Groq attempt %d (chars=%d)", attempt, len(text))
            prompt = PROMPT_TEMPLATE.format(text=text)
            _LAST_GROQ_CALL = time.monotonic()
            resp_msg = llm.invoke([HumanMessage(content=prompt)])
            answer_raw = (resp_msg.content or "").strip().upper()
            answer = "YES" if answer_raw.startswith("YES") else "NO"
            logging.info("Groq classified: %s", answer)
            _DRIC_CACHE[digest] = answer
            return answer

        except Exception as exc:
            msg = str(exc).lower()
            if "rate limit" in msg or "429" in msg:
                backoff = 1.5 * attempt  # shorter
                logging.warning(f"Groq rate-limit hit. Retry {attempt}/{max_retries} in {backoff}s…")
                time.sleep(backoff)
                continue

            logging.error(f"Groq (LangChain) error: {exc}")
            break

    _DRIC_CACHE[digest] = "NO"
    return "NO"



async def _process_row(ctx, row: pd.Series) -> dict:
    scholar_link: str = str(row["scholar_link"])

    logging.info(f"Visiting Google Scholar page → {scholar_link}")

    article_url = await _extract_article_url(ctx, scholar_link)
    if not article_url:
        logging.warning(f"Could not resolve article link for {row['title'][:60]}...")
        return {
            "authors": row["authors"],
            "title": row["title"],
            "year": row["year"],
            "scholar_link": scholar_link,
            "dric": "NF",
        }

    logging.info(f"Fetching article content → {article_url}")

    # Firecrawl and Groq are blocking – run in a thread so we don't block the
    # event loop. Handle failures gracefully.
    article_text: str = await asyncio.to_thread(_crawl_article, article_url)

    if article_text:
        logging.info("Fetched article text (%d chars, %d words)", len(article_text), len(article_text.split()))
        dric_ans = await asyncio.to_thread(_ask_dric, article_text)
    else:
        logging.warning(f"Empty article text for {article_url}")
        dric_ans = "NF"

    logging.info(f"LLM response for '{row['title'][:60]}...' → {dric_ans}")

    return {
        "authors": row["authors"],
        "title": row["title"],
        "year": row["year"],
        "scholar_link": scholar_link,
        "dric": dric_ans,
    }


async def process_period_async(period: str):
    logging.info(f"Processing period {period}")
    raw_file = Path("Data") / period / "raw_publications.csv"
    if not raw_file.exists():
        raise FileNotFoundError(raw_file)

    out_dir = Path("Data") / period / "preprocessed_files"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"rsg_{period}_preprocessed.csv"

    df_in = pd.read_csv(raw_file)
    logging.info(f"Loaded {len(df_in)} publication records")

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True, args=["--disable-gl-drawing-for-tests", "--no-sandbox"])

        # Enable JavaScript for more reliable navigation; disable images/css for speed.
        ctx = await browser.new_context(
            java_script_enabled=True,
            viewport={"width": 1280, "height": 800},
        )

        results: list[dict] = []
        for _, row in df_in.iterrows():
            res = await _process_row(ctx, row)
            results.append(res)

        await ctx.close()
        await browser.close()

    df_out = pd.DataFrame(results)
    if "scholar_link" in df_out.columns:
        df_out = df_out.drop(columns=["scholar_link"])
    df_out.to_csv(out_file, index=False)
    logging.info(f"Saved results -> {out_file}")



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True, help="Academic year string e.g. 2016-2017 or 2020")
    args = parser.parse_args()

    asyncio.run(process_period_async(args.period)) 