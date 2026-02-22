"""NetFile portal RadTreeView scraper — elections + candidates via Playwright.

The NetFile portal at https://public.netfile.com/pub2/?AID=CSHA uses a
Telerik RadTreeView with server-side expand (expandMode:2). Tree structure:

  Election (e.g. "11/08/2022 General Election")  [depth 1]
    ├─ Candidates                                  [depth 2]
    │   ├─ Office Name                             [depth 3]
    │   │   ├─ Candidate Name (with link)          [depth 4]
    │   │   └─ ...
    │   └─ ...
    ├─ Measures                                    [depth 2]
    └─ Independent Expenditures / FPPC 497         [depth 2]

Uses Playwright (headless Chromium) since the Telerik AJAX postback format
is not feasible to replicate with plain httpx.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

PORTAL_URL = "https://public.netfile.com/pub2/?AID=CSHA"
EXPAND_WAIT_MS = 2500  # wait after each expand click


@dataclass
class ScrapedCandidate:
    name: str
    office: str
    portal_filer_id: str | None = None  # from ?id=XXX in link
    link_url: str | None = None


@dataclass
class ScrapedMeasure:
    name: str
    letter: str | None = None


@dataclass
class ScrapedElection:
    name: str           # e.g. "11/08/2022 General Election"
    node_value: str     # e.g. "200780681"
    date: date | None = None
    election_type: str | None = None  # primary, general, special, udel
    year: int | None = None
    candidates: list[ScrapedCandidate] = field(default_factory=list)
    measures: list[ScrapedMeasure] = field(default_factory=list)


def _parse_election_text(text: str) -> tuple[date | None, str | None, int | None]:
    """Parse '11/08/2022 General Election' -> (date, type, year)."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(.*?)\s*Election", text)
    if not m:
        return None, None, None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    etype = m.group(4).strip().lower()  # primary, general, special, udel
    try:
        d = date(year, month, day)
    except ValueError:
        d = None
    return d, etype, year


def _extract_portal_filer_id(url: str | None) -> str | None:
    """Extract ?id=XXX from AllFilingsByCandidate.aspx?id=XXX&..."""
    if not url:
        return None
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else None


def _get_node_info(li_element):
    """Get text, depth, has_plus, link for a tree LI element."""
    text_el = li_element.query_selector(".rtIn")
    if not text_el:
        return None

    text = text_el.text_content().strip()
    has_plus = li_element.query_selector(".rtPlus") is not None
    depth = li_element.evaluate("""el => {
        let d = 0, p = el.parentElement;
        while (p) {
            if (p.classList && p.classList.contains('rtUL')) d++;
            p = p.parentElement;
        }
        return d;
    }""")
    link_el = li_element.query_selector("a")
    href = link_el.get_attribute("href") if link_el else None

    return {"text": text, "depth": depth, "has_plus": has_plus, "href": href, "li": li_element}


def _js_click_plus(li_element):
    """Click the expand toggle via JS (bypasses visibility check)."""
    plus = li_element.query_selector(".rtPlus")
    if plus:
        plus.evaluate("el => el.click()")
        return True
    return False


def _js_click_minus(li_element):
    """Click the collapse toggle via JS."""
    minus = li_element.query_selector(".rtMinus")
    if minus:
        minus.evaluate("el => el.click()")
        return True
    return False


def scrape_elections_sync(
    min_year: int = 2016,
    max_year: int = 2026,
    expand_candidates: bool = True,
) -> list[ScrapedElection]:
    """Scrape election tree from NetFile portal (synchronous, uses Playwright).

    Args:
        min_year: Only include elections from this year onward.
        max_year: Only include elections up to this year.
        expand_candidates: If True, expand each election + office to get candidates.

    Returns:
        List of ScrapedElection with candidates populated.
    """
    from playwright.sync_api import sync_playwright

    elections: list[ScrapedElection] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        logger.info("Loading portal page: %s", PORTAL_URL)
        page.goto(PORTAL_URL, timeout=30000)
        page.wait_for_selector(".rtIn", timeout=10000)

        # Parse election node names and their JS nodeData values
        node_data_raw = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const m = s.textContent.match(/"nodeData":\\[(.+?)\\]/);
                if (m) return JSON.parse('[' + m[1] + ']');
            }
            return [];
        }""")

        # Build election list from top-level tree nodes
        all_lis = page.query_selector_all(".rtLI")
        election_lis = []

        for i, li in enumerate(all_lis):
            info = _get_node_info(li)
            if not info or info["depth"] != 1:
                continue
            d, etype, year = _parse_election_text(info["text"])
            if year is None or year < min_year or year > max_year:
                continue

            node_val = node_data_raw[i]["value"] if i < len(node_data_raw) else str(i)
            se = ScrapedElection(
                name=info["text"],
                node_value=node_val,
                date=d,
                election_type=etype,
                year=year,
            )
            elections.append(se)
            election_lis.append(li)
            logger.info("  Found election: %s (node=%s)", info["text"], node_val)

        if not expand_candidates:
            browser.close()
            return elections

        # Expand each election to get candidates
        for se, eli in zip(elections, election_lis):
            logger.info("Expanding: %s", se.name)
            try:
                _expand_election(page, se, eli)
            except Exception as e:
                logger.error("Failed to expand %s: %s", se.name, e)

        browser.close()

    logger.info("Scraped %d elections with %d total candidates",
                len(elections),
                sum(len(e.candidates) for e in elections))
    return elections


def _expand_election(page, se: ScrapedElection, election_li):
    """Expand a single election node and populate its candidates/measures."""
    if not _js_click_plus(election_li):
        logger.warning("No expand toggle for: %s", se.name)
        return

    page.wait_for_timeout(EXPAND_WAIT_MS)

    # After expansion, find child LI elements that belong to this election.
    # Children are inside a nested UL within the election LI.
    child_ul = election_li.query_selector("ul.rtUL")
    if not child_ul:
        logger.warning("No child UL after expanding: %s", se.name)
        return

    child_lis = child_ul.query_selector_all(":scope > li.rtLI")

    # Children at depth 2 are sections: "Candidates", "Measures", etc.
    # Under "Candidates" section, depth-3 nodes are offices
    for section_li in child_lis:
        info = _get_node_info(section_li)
        if not info:
            continue

        if info["text"] == "Candidates":
            # Office nodes are inside this section's child UL
            candidates_ul = section_li.query_selector("ul.rtUL")
            if not candidates_ul:
                continue
            office_lis = candidates_ul.query_selector_all(":scope > li.rtLI")
            for office_li in office_lis:
                oinfo = _get_node_info(office_li)
                if not oinfo:
                    continue
                _expand_office(page, se, office_li, oinfo["text"])

        elif info["text"] == "Measures":
            measures_ul = section_li.query_selector("ul.rtUL")
            if not measures_ul:
                continue
            measure_lis = measures_ul.query_selector_all(":scope > li.rtLI")
            for mli in measure_lis:
                minfo = _get_node_info(mli)
                if minfo and minfo["text"] != "No Measures to View":
                    se.measures.append(ScrapedMeasure(name=minfo["text"]))

    # Collapse to keep DOM manageable
    _js_click_minus(election_li)
    page.wait_for_timeout(500)


def _expand_office(page, se: ScrapedElection, office_li, office_name: str):
    """Expand an office node to get candidate names + links."""
    if not _js_click_plus(office_li):
        # Leaf node — no candidates under this office
        return

    page.wait_for_timeout(EXPAND_WAIT_MS)

    # Get candidate child LIs
    cand_ul = office_li.query_selector("ul.rtUL")
    if not cand_ul:
        return

    cand_lis = cand_ul.query_selector_all(":scope > li.rtLI")
    for cli in cand_lis:
        cinfo = _get_node_info(cli)
        if not cinfo:
            continue

        portal_id = _extract_portal_filer_id(cinfo["href"])
        se.candidates.append(ScrapedCandidate(
            name=cinfo["text"],
            office=office_name,
            portal_filer_id=portal_id,
            link_url=cinfo["href"],
        ))

    # Collapse
    _js_click_minus(office_li)
    page.wait_for_timeout(300)


async def scrape_elections(
    min_year: int = 2016,
    max_year: int = 2026,
    expand_candidates: bool = True,
) -> list[ScrapedElection]:
    """Async wrapper — runs sync Playwright in a thread."""
    return await asyncio.to_thread(
        scrape_elections_sync,
        min_year=min_year,
        max_year=max_year,
        expand_candidates=expand_candidates,
    )
