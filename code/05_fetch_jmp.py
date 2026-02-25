#!/usr/bin/env python3
"""
Literature Tracker — Automated Job Market Paper Fetcher
Scrapes top econ PhD program placement pages, extracts candidate info,
resolves paper metadata, and stores JMPs in the database.

Runs once in December automatically (via launchd) or manually:
    python3 code/05_fetch_jmp.py              # full scrape
    python3 code/05_fetch_jmp.py --dry-run    # preview without writing to DB
"""

import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "data" / "papers.db"
JMP_MANUAL_PATH = ROOT / "data" / "jmp_candidates.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
})

# ================================================================
# Department scraper definitions
# Each returns a list of candidate dicts with keys:
#   name, school, fields (list), paper_title, paper_url, website
# ================================================================

DEPARTMENTS = [
    {
        "name": "MIT",
        "school": "MIT",
        "url": "https://economics.mit.edu/academic-programs/phd-program/job-market",
        "parser": "mit",
    },
    {
        "name": "Harvard",
        "school": "Harvard",
        "url": "https://www.economics.harvard.edu/job-market-candidates",
        "parser": "harvard",
    },
    {
        "name": "Stanford",
        "school": "Stanford",
        "url": "https://economics.stanford.edu/graduate/job-market-candidates",
        "parser": "stanford",
    },
    {
        "name": "Princeton",
        "school": "Princeton",
        "url": "https://economics.princeton.edu/graduate-program/job-market-and-placements/2025-job-market-candidates/",
        "parser": "generic",
    },
    {
        "name": "Chicago",
        "school": "Chicago",
        "url": "https://economics.uchicago.edu/people/2025-26-job-market-candidates",
        "parser": "chicago",
    },
    {
        "name": "Yale",
        "school": "Yale",
        "url": "https://economics.yale.edu/phd-program/placement",
        "parser": "generic",
    },
    {
        "name": "Columbia",
        "school": "Columbia",
        "url": "https://econ.columbia.edu/phd/job-market-candidates/",
        "parser": "columbia",
    },
    {
        "name": "Northwestern",
        "school": "Northwestern",
        "url": "https://economics.northwestern.edu/people/phd-job-market-candidates/index.html",
        "parser": "generic",
    },
    {
        "name": "Berkeley",
        "school": "UC Berkeley",
        "url": "https://econ.berkeley.edu/graduate/placement/job-market-candidates-phd",
        "parser": "berkeley",
    },
    {
        "name": "Penn",
        "school": "Penn",
        "url": "https://economics.sas.upenn.edu/graduate/job-market-candidates",
        "parser": "generic",
    },
    {
        "name": "Duke",
        "school": "Duke",
        "url": "https://econ.duke.edu/news/phd-job-market-candidates-0",
        "parser": "generic",
    },
    {
        "name": "NYU",
        "school": "NYU",
        "url": "https://as.nyu.edu/departments/econ/job-market.html",
        "parser": "generic",
    },
    {
        "name": "LSE",
        "school": "LSE",
        "url": "https://www.lse.ac.uk/economics/job-market/job-market-candidates",
        "parser": "generic",
    },
    {
        "name": "Michigan",
        "school": "Michigan",
        "url": "https://lsa.umich.edu/econ/phd-program/job-market-candidates.html",
        "parser": "generic",
    },
    {
        "name": "Wisconsin",
        "school": "Wisconsin",
        "url": "https://econ.wisc.edu/doctoral/job-market/",
        "parser": "generic",
    },
]


def _fetch_page(url: str, timeout: int = 20) -> Optional[BeautifulSoup]:
    try:
        resp = SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("  HTTP %d for %s", resp.status_code, url)
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning("  Failed to fetch %s: %s", url, e)
        return None


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _looks_like_pdf(url: str) -> bool:
    return url.lower().endswith(".pdf") or "pdf" in url.lower()


# ----------------------------------------------------------------
# MIT: listing page has candidate names + profile links + fields.
# Individual profile pages have full JMP info.
# ----------------------------------------------------------------

def _parse_mit(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []
    # MIT lists candidates as linked names followed by field text
    # Find all links to individual profile pages
    profile_links = soup.find_all("a", href=re.compile(r"/people/phd-students/"))
    seen_names = set()

    for link in profile_links:
        name = _clean_text(link.get_text())
        href = link.get("href", "")
        if not name or name in seen_names:
            continue
        if not _is_plausible_name(name):
            continue
        seen_names.add(name)
        profile_url = urljoin(base_url, href)

        # Try to extract fields from surrounding text
        parent = link.find_parent()
        fields_text = ""
        if parent:
            raw = parent.get_text(separator="|")
            parts = [p.strip() for p in raw.split("|") if p.strip() and p.strip() != name]
            fields_text = ", ".join(parts)

        # Fetch individual profile page for JMP details
        paper_title, paper_url, abstract = _scrape_mit_profile(profile_url)

        candidates.append({
            "name": name,
            "school": "MIT",
            "fields": [f.strip() for f in fields_text.split(",") if f.strip()] if fields_text else [],
            "paper_title": paper_title,
            "paper_url": paper_url,
            "abstract": abstract,
            "website": profile_url,
        })
        time.sleep(0.3)

    return candidates


def _scrape_mit_profile(url: str) -> Tuple[str, str, str]:
    """Fetch an MIT candidate profile page and extract JMP title, URL, abstract."""
    soup = _fetch_page(url)
    if not soup:
        return "", "", ""

    # Look for "Job Market Paper" heading
    jmp_heading = soup.find(string=re.compile(r"Job\s+Market\s+Paper", re.I))
    if not jmp_heading:
        return "", "", ""

    # The JMP title is usually in a link after the heading
    heading_el = jmp_heading.find_parent()
    if not heading_el:
        return "", "", ""

    # Walk forward to find the link
    paper_title = ""
    paper_url = ""
    abstract = ""

    for sibling in heading_el.find_next_siblings():
        link = sibling.find("a") if sibling.name != "a" else sibling
        if link and link.get("href"):
            paper_title = _clean_text(link.get_text())
            paper_url = link["href"]
            break

    # Look for abstract
    abstract_el = soup.find(string=re.compile(r"Abstract", re.I))
    if abstract_el:
        parent = abstract_el.find_parent()
        if parent:
            next_p = parent.find_next_sibling()
            if next_p:
                abstract = _clean_text(next_p.get_text())[:2000]

    return paper_title, paper_url, abstract


# ----------------------------------------------------------------
# Harvard: listing page has candidate names + fields.
# Individual pages may have JMP info.
# ----------------------------------------------------------------

def _parse_harvard(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []
    # Harvard lists candidates as h3 headings with fields below
    headings = soup.find_all(["h3", "h4"])

    for h in headings:
        name = _clean_text(h.get_text())
        if not _is_plausible_name(name):
            continue

        # Extract fields from following text
        fields = []
        next_el = h.find_next_sibling()
        if next_el:
            fields_text = _clean_text(next_el.get_text())
            # Fields are typically separated by newlines in the source
            fields = [f.strip() for f in re.split(r"[,\n]", fields_text) if f.strip() and len(f.strip()) > 3]

        # Try to find a link to their personal page
        link = h.find("a")
        website = link["href"] if link and link.get("href") else ""
        if website and not website.startswith("http"):
            website = urljoin(base_url, website)

        candidates.append({
            "name": name,
            "school": "Harvard",
            "fields": fields[:5],
            "paper_title": "",
            "paper_url": "",
            "abstract": "",
            "website": website,
        })

    return candidates


# ----------------------------------------------------------------
# Stanford: well-structured with name, paper title, fields, advisors
# ----------------------------------------------------------------

def _parse_stanford(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []
    # Stanford uses h2 headings for candidate names
    headings = soup.find_all("h2")

    for h in headings:
        name = _clean_text(h.get_text())
        if not _is_plausible_name(name):
            continue

        paper_title = ""
        paper_url = ""
        fields = []
        website = ""

        # Walk through siblings/following elements to find metadata
        container = h.find_parent()
        if not container:
            continue

        text_block = container.get_text(separator="\n")

        # Extract paper title
        jmp_match = re.search(r"Job Market Paper:\s*\n\s*(.+?)(?:\n|$)", text_block)
        if jmp_match:
            paper_title = jmp_match.group(1).strip()

        # Extract fields
        fields_match = re.search(r"Fields of Study:\s*\n\s*(.+?)(?:\n|$)", text_block)
        if fields_match:
            fields = [f.strip() for f in fields_match.group(1).split(",") if f.strip()]

        # Look for email link or personal website link
        email_link = container.find("a", href=re.compile(r"mailto:"))
        if email_link:
            email = email_link["href"].replace("mailto:", "")

        # Find paper link if available
        for link in container.find_all("a"):
            href = link.get("href", "")
            if _looks_like_pdf(href):
                paper_url = href
                break

        candidates.append({
            "name": name,
            "school": "Stanford",
            "fields": fields,
            "paper_title": paper_title,
            "paper_url": paper_url,
            "abstract": "",
            "website": website,
        })

    return candidates


# ----------------------------------------------------------------
# Chicago: paper titles + PDF links are directly on the page.
# Need to pair them with candidate names.
# ----------------------------------------------------------------

def _parse_chicago(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []
    # Chicago: plain-text structured blocks:
    #   Candidate Name
    #   Research Focuses: ...
    #   Job Market Paper: "Title"
    #   References: ...
    main = soup.find("main") or soup.find("div", role="main") or soup
    text = main.get_text(separator="\n")

    # Split into candidate blocks by "Job Market Paper"
    blocks = re.split(r"(?=Job Market Paper)", text)

    for block in blocks:
        if "Job Market Paper" not in block:
            continue
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        # Extract paper title (text in quotes or after "Job Market Paper")
        title = ""
        for line in lines:
            if "Job Market Paper" in line:
                continue
            # Look for quoted title or a line that's a reasonable title
            match = re.search(r'["\u201c](.+?)["\u201d]', line)
            if match:
                title = match.group(1).strip()
                break

        # If no quoted title, find the link text
        if not title:
            for link in soup.find_all("a", href=True):
                href = link["href"]
                link_text = _clean_text(link.get_text())
                if (link_text and len(link_text) > 15 and
                    any(ext in href.lower() for ext in [".pdf", "drive.google", "dropbox"])):
                    if link_text in block:
                        title = link_text
                        break

        if not title:
            continue

        # Find PDF URL for this title
        paper_url = ""
        for link in soup.find_all("a", href=True):
            if _clean_text(link.get_text()).strip('""\u201c\u201d ') == title.strip('""\u201c\u201d '):
                paper_url = link["href"]
                break

        # Look backwards in the original text for the candidate name
        # The name appears a few lines before "Job Market Paper"
        jmp_pos = text.find("Job Market Paper")
        if jmp_pos == -1:
            jmp_pos = text.find(title[:30])
        # Find the block before this occurrence
        pre_text = text[:text.find(block[:50]) + 1] if block[:50] in text else ""
        pre_lines = [l.strip() for l in pre_text.split("\n") if l.strip()]

        name = ""
        fields = []
        # Walk backward to find name and fields
        for i in range(len(pre_lines) - 1, -1, -1):
            line = pre_lines[i]
            if "Research Focuses" in line or "Research Focus" in line:
                fields_text = re.sub(r"Research\s+Focus(es)?:?\s*", "", line)
                fields = [f.strip() for f in fields_text.split(",") if f.strip() and len(f.strip()) > 3]
                # Name is typically the line before "Research Focuses"
                if i > 0:
                    candidate_name = pre_lines[i - 1].strip()
                    if _is_plausible_name(candidate_name):
                        name = candidate_name
                break

        if not name:
            name = _name_from_url(paper_url) or "Unknown"

        candidates.append({
            "name": name,
            "school": "Chicago",
            "fields": fields,
            "paper_title": title,
            "paper_url": paper_url,
            "abstract": "",
            "website": "",
        })

        # Remove this block from text to avoid re-matching
        text = text.replace(block[:50], "", 1)

    return candidates


def _name_from_url(url: str) -> str:
    """Try to extract a person's name from a JMP URL."""
    # Common patterns: .../lastName_JMP.pdf, .../FirstName_LastName_JMP.pdf
    # github.io URLs often have the username
    parts = url.lower().split("/")
    for part in parts:
        if "github.io" in part:
            username = part.split(".")[0]
            return username.replace("-", " ").title()
    # Try filename
    filename = parts[-1] if parts else ""
    filename = re.sub(r"\.(pdf|html?)$", "", filename, flags=re.I)
    filename = re.sub(r"[_-]?(jmp|job.?market|paper|draft|latest|v\d+|compressed).*$", "", filename, flags=re.I)
    filename = filename.replace("_", " ").replace("-", " ").strip()
    words = filename.split()
    if 2 <= len(words) <= 4 and all(w.isalpha() for w in words):
        return filename.title()
    return ""


# ----------------------------------------------------------------
# Columbia: very well structured — name, fields, paper title + PDF
# ----------------------------------------------------------------

def _parse_columbia(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []

    # Columbia has each candidate in a section with links
    # Pattern: [Name](website), Fields, [Paper Title](pdf_url), Advisors
    # We look for links that go to JMP PDFs
    all_sections = soup.find_all(["tr", "div", "section"])

    # Alternative: find pairs of (candidate name link, paper PDF link)
    candidate_links = []
    paper_links = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = _clean_text(link.get_text())
        if not text or len(text) < 3:
            continue
        if "wp-content" in href and ".pdf" in href:
            paper_links.append((text, href, link))
        elif ("github.io" in href or "google.com/view" in href or
              "sites.google" in href) and len(text.split()) <= 5:
            candidate_links.append((text, href, link))

    # Also extract from the "Candidate Name:" pattern in text
    text_content = soup.get_text()
    name_pattern = re.compile(
        r"Candidate Name:\s*\[?([^\]\n]+)\]?"
        r".*?Field\(s\):\s*([^\n]+)"
        r".*?Paper Title:\s*\[?([^\]\n]+)\]?",
        re.DOTALL
    )

    for match in name_pattern.finditer(text_content):
        name = _clean_text(match.group(1))
        fields_str = _clean_text(match.group(2))
        paper_title = _clean_text(match.group(3))

        # Find the PDF URL for this paper
        paper_url = ""
        for pt, pu, _ in paper_links:
            if _title_similarity(paper_title.lower(), pt.lower()) > 0.5:
                paper_url = pu
                break

        fields = [f.strip() for f in fields_str.split(",") if f.strip()]

        if name and paper_title:
            candidates.append({
                "name": name,
                "school": "Columbia",
                "fields": fields,
                "paper_title": paper_title,
                "paper_url": paper_url,
                "abstract": "",
                "website": "",
            })

    # Deduplicate by name
    seen = set()
    unique = []
    for c in candidates:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)

    return unique


# ----------------------------------------------------------------
# Berkeley: structured listing with h3 names, fields, website links
# ----------------------------------------------------------------

def _parse_berkeley(soup: BeautifulSoup, base_url: str) -> List[dict]:
    candidates = []
    # Berkeley format: blocks separated by "Program Entry YYYY"
    # Each block: "LastName, FirstName" / Fields / Website / Email
    main = soup.find("main") or soup
    text = main.get_text(separator="\n")

    blocks = re.split(r"Program Entry(?:\s+\d{4})?", text)

    for block in blocks[1:]:
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue

        # First line is "LastName, FirstName" format
        raw_name = lines[0].strip()
        # Convert "LastName, FirstName" to "FirstName LastName"
        if "," in raw_name:
            parts = [p.strip() for p in raw_name.split(",", 1)]
            if len(parts) == 2:
                name = f"{parts[1]} {parts[0]}"
            else:
                name = raw_name
        else:
            name = raw_name

        if not _is_plausible_name(name):
            continue

        # Fields line
        fields = []
        if len(lines) > 1 and lines[1] not in ("Website", "Email"):
            fields = [f.strip() for f in lines[1].split(",") if f.strip() and len(f.strip()) > 3]

        # Find website URL from the HTML
        website = ""
        name_el = soup.find(string=re.compile(re.escape(raw_name[:15])))
        if name_el:
            container = name_el.find_parent("div") or name_el.find_parent()
            if container:
                for a in container.find_all("a", href=True):
                    href = a["href"]
                    if a.get_text().strip() == "Website" and href.startswith("http"):
                        website = href
                        break

        candidates.append({
            "name": name,
            "school": "UC Berkeley",
            "fields": fields[:5],
            "paper_title": "",
            "paper_url": "",
            "abstract": "",
            "website": website,
        })

    return candidates


# ----------------------------------------------------------------
# Name validation helpers
# ----------------------------------------------------------------

STOP_WORDS = {
    "undergraduate students", "graduate students", "after stanford",
    "contact us", "main menu", "social menu", "footer menu",
    "current students", "program rules", "frequently used forms",
    "financial support opportunities", "peer advising",
    "campus advising resources", "academic guide", "alumni notes",
    "speakers series", "student organizations", "affiliated faculty",
    "in memoriam", "phd students", "independent study",
    "pursuing academic positions", "pursuing non-academic positions",
    "affiliated candidates pursuing academic positions",
    "placement history", "job market", "job market candidates",
    "placement information", "prospective students",
    "view past placement", "candidates", "fields",
    # Navigation items that pass name checks
    "site links", "join our team", "privacy policy", "our programs",
    "annual magazine", "all people", "student directory",
    "related course credit", "senior essay", "double majors",
    "frequently asked questions", "get advice", "course offerings",
    "major requirements", "course selection", "common questions",
    "prospective majors", "graduate student directory",
}


def _is_plausible_name(text: str) -> bool:
    """Check if a string looks like a person name (not navigation/heading garbage)."""
    if not text:
        return False
    text_lower = text.lower().strip()
    if text_lower in STOP_WORDS:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 5:
        return False
    if not all(w[0].isupper() for w in words if len(w) > 1):
        return False
    # Reject if any word is a common non-name word
    bad_words = {
        "university", "economics", "department", "professor", "faculty",
        "program", "students", "candidates", "menu", "search", "contact",
        "about", "resources", "news", "events", "seminars", "research",
        "teaching", "academic", "positions", "affiliated", "pursuing",
        "placement", "information", "admissions", "financial", "support",
        "independent", "study", "committee", "director", "history",
        "advisor", "advisors", "non-academic", "view", "download",
        "policy", "requirements", "offerings", "directory", "majors",
        "essay", "credit", "questions", "advice", "magazine", "team",
        "privacy", "links", "site", "people", "all", "our", "join",
        "annual", "senior", "double", "common", "related", "course",
        "selection", "get", "prospective", "graduate",
    }
    if any(w.lower() in bad_words for w in words):
        return False
    # All words should be reasonable name words (letters, hyphens, apostrophes)
    if not all(re.match(r"^[A-Za-z\u00C0-\u024F\'\-\.]+$", w) for w in words):
        return False
    return True


# ----------------------------------------------------------------
# Generic parser: tries common patterns across different dept pages
# ----------------------------------------------------------------

def _parse_generic(soup: BeautifulSoup, base_url: str, school: str) -> List[dict]:
    candidates = []
    seen_names = set()

    # Strategy 1: find headings that look like person names
    for heading in soup.find_all(["h2", "h3", "h4"]):
        name = _clean_text(heading.get_text())
        if not name or name in seen_names:
            continue
        if not _is_plausible_name(name):
            continue

        seen_names.add(name)

        # Look for paper title and URL nearby
        paper_title = ""
        paper_url = ""
        fields = []
        website = ""

        # Check for link in the heading itself
        link = heading.find("a")
        if link and link.get("href"):
            website = urljoin(base_url, link["href"])

        # Look in following siblings for paper info and fields
        container = heading.find_parent()
        if container:
            for a in container.find_all("a", href=True):
                href = a["href"]
                text = _clean_text(a.get_text())
                if _looks_like_pdf(href) and text and len(text) > 10:
                    paper_title = text
                    paper_url = href if href.startswith("http") else urljoin(base_url, href)
                    break

            # Try to extract fields from text
            block_text = container.get_text(separator="\n")
            for line in block_text.split("\n"):
                line = line.strip()
                if re.search(r"(economics|theory|finance|econometrics|trade|development|labor|public|health|industrial|political|behavioral|macro|micro)", line, re.I):
                    if len(line) < 200 and name not in line:
                        fields = [f.strip() for f in re.split(r"[,\n;]", line) if f.strip() and len(f.strip()) > 3]
                        break

        candidates.append({
            "name": name,
            "school": school,
            "fields": fields[:6],
            "paper_title": paper_title,
            "paper_url": paper_url,
            "abstract": "",
            "website": website,
        })

    # Strategy 2: if no headings found, look for linked names + PDF pairs
    if not candidates:
        pdf_links = []
        name_links = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = _clean_text(link.get_text())
            if not text:
                continue
            if _looks_like_pdf(href) and len(text) > 10:
                pdf_links.append((text, href, link))
            elif len(text.split()) in (2, 3, 4):
                words = text.split()
                if all(w[0].isupper() and w.isalpha() for w in words):
                    name_links.append((text, href, link))

        # Try to pair names with papers
        for name, href, link in name_links:
            if name in seen_names:
                continue
            seen_names.add(name)
            website = urljoin(base_url, href)

            # Find the nearest PDF link after this name link
            paper_title = ""
            paper_url = ""
            for pt, pu, pl in pdf_links:
                # Check if this PDF is "close" in DOM position
                if link.sourceline and pl.sourceline:
                    if 0 < pl.sourceline - link.sourceline < 30:
                        paper_title = pt
                        paper_url = pu
                        break

            candidates.append({
                "name": name,
                "school": school,
                "fields": [],
                "paper_title": paper_title,
                "paper_url": paper_url,
                "abstract": "",
                "website": website,
            })

    return candidates


# ================================================================
# Metadata resolution: fill in paper titles via Semantic Scholar
# for candidates where scraping didn't find the JMP title.
# ================================================================

def _title_similarity(a: str, b: str) -> float:
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(len(words_a), len(words_b))


def _search_semantic_scholar_by_author(name: str) -> Optional[dict]:
    """Search Semantic Scholar for recent papers by an author."""
    try:
        resp = SESSION.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": name,
                "limit": 5,
                "fields": "title,authors,abstract,url,openAccessPdf,externalIds,year",
                "year": f"{datetime.now().year - 1}-{datetime.now().year}",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        if not results:
            return None

        name_parts = set(name.lower().split())
        for r in results:
            for author in r.get("authors", []):
                author_parts = set(author.get("name", "").lower().split())
                if len(name_parts & author_parts) >= 2:
                    return r
        return None
    except Exception as e:
        log.debug("Semantic Scholar search failed for %s: %s", name, e)
        return None


def _search_openalex_by_author(name: str, email: str) -> Optional[dict]:
    """Search OpenAlex for recent works by author name."""
    try:
        resp = SESSION.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"raw_author_name.search:{name},from_publication_date:{datetime.now().year - 1}-01-01",
                "sort": "publication_date:desc",
                "per_page": 5,
                "mailto": email,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None

        name_parts = set(name.lower().split())
        for r in results:
            for auth in r.get("authorships", []):
                author_parts = set(auth.get("author", {}).get("display_name", "").lower().split())
                if len(name_parts & author_parts) >= 2:
                    return r
        return None
    except Exception as e:
        log.debug("OpenAlex search failed for %s: %s", name, e)
        return None


def _is_economics_paper(title: str, venue: str = "") -> bool:
    """Heuristic check that a paper is likely economics, not biology/physics/etc."""
    text = (title + " " + venue).lower()
    # Reject if clearly from another field
    non_econ = [
        "cell", "protein", "gene", "genome", "molecular", "clinical",
        "patient", "diagnosis", "therapy", "surgical", "cancer", "tumor",
        "neuron", "cortex", "patholog", "symptom", "virus", "bacteria",
        "phylogen", "species", "ecosystem", "lattice", "quantum",
        "photon", "magnetic", "spectroscop", "chemical", "polymer",
        "alloy", "crystal", "nanoparticle", "enzyme", "amino acid",
        "morpholog", "treadmill", "phage", "immortality",
        "derivatization", "lc-ms", "icp-ms", "spc versus", "hypert",
        "drone", "cybersec", "murine", "transcriptom", "decalcified",
        "innovator", "llm", "deep learning", "neural network",
        "scaling law", "pre-training", "trigger",
    ]
    if any(w in text for w in non_econ):
        return False
    # Also reject if title is too short/generic (likely wrong match)
    if len(title) < 15:
        return False
    return True


def _resolve_missing_metadata(candidate: dict, email: str) -> dict:
    """For candidates missing paper_title, try Semantic Scholar / OpenAlex."""
    if candidate.get("paper_title"):
        return candidate

    name = candidate["name"]
    log.info("    Resolving paper for %s via API search...", name)

    # Try Semantic Scholar
    ss = _search_semantic_scholar_by_author(name)
    if ss:
        title = ss.get("title", "")
        venue = ss.get("venue", "")
        if title and _is_economics_paper(title, venue):
            candidate["paper_title"] = title
            candidate["abstract"] = ss.get("abstract", "") or ""
            oa = (ss.get("openAccessPdf") or {}).get("url", "")
            if oa:
                candidate["paper_url"] = oa
            ext = ss.get("externalIds") or {}
            candidate["doi"] = ext.get("DOI", "")
            log.info("      Found via Semantic Scholar: %s", title[:60])
            return candidate
        elif title:
            log.debug("      Rejected non-econ paper: %s", title[:60])
    time.sleep(0.3)

    # Try OpenAlex
    oa_result = _search_openalex_by_author(name, email)
    if oa_result:
        title = oa_result.get("title", "")
        source = ((oa_result.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        if title and _is_economics_paper(title, source):
            candidate["paper_title"] = title
            inv = oa_result.get("abstract_inverted_index")
            if inv:
                word_pos = []
                for word, positions in inv.items():
                    for pos in positions:
                        word_pos.append((pos, word))
                word_pos.sort()
                candidate["abstract"] = " ".join(w for _, w in word_pos)[:2000]
            oa_info = oa_result.get("open_access") or {}
            if oa_info.get("oa_url"):
                candidate["paper_url"] = oa_info["oa_url"]
            candidate["doi"] = oa_result.get("doi", "") or ""
            log.info("      Found via OpenAlex: %s", title[:60])
            return candidate
        elif title:
            log.debug("      Rejected non-econ paper: %s", title[:60])

    log.info("      No paper found for %s", name)
    return candidate


# ================================================================
# Database storage
# ================================================================

def _make_id(title: str, authors: str = "") -> str:
    raw = re.sub(r"\s+", " ", (title + authors).lower().strip())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _store_candidate(conn: sqlite3.Connection, candidate: dict, insert_fn) -> bool:
    """Convert a candidate dict to a paper record and store it. Returns True if new."""
    title = candidate.get("paper_title", "")
    if not title:
        return False

    name = candidate["name"]
    school = candidate["school"]
    paper_id = _make_id(title, name)

    existing = conn.execute(
        "SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    if existing:
        return False

    paper = {
        "paper_id": paper_id,
        "title": title,
        "authors": f"{name} ({school})",
        "abstract": candidate.get("abstract", "")[:2000],
        "journal": "Job Market Paper",
        "source": "jmp",
        "url": candidate.get("paper_url") or candidate.get("website", ""),
        "doi": candidate.get("doi", ""),
        "oa_url": candidate.get("paper_url", "") if _looks_like_pdf(candidate.get("paper_url", "")) else "",
        "pub_date": "",
        "relevant": 1,
    }
    insert_fn(conn, paper)
    return True


# ================================================================
# Main entry point
# ================================================================

def run(dry_run: bool = False) -> int:
    log.info("=" * 60)
    log.info("Literature Tracker — Automated JMP Scraper")
    log.info("=" * 60)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    email = cfg.get("email", "")

    # Allow overriding department URLs from config
    config_departments = {}
    jm_cfg = cfg.get("job_market", {})
    for src in jm_cfg.get("sources", []):
        config_departments[src["name"]] = src["url"]

    if not dry_run:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from importlib import import_module
        fetch_mod = import_module("01_fetch")
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        fetch_mod.init_db(conn)

    all_candidates = []

    # Phase 1: Scrape all department pages
    for dept in DEPARTMENTS:
        name = dept["name"]
        url = config_departments.get(name + " Economics", dept["url"])
        parser = dept["parser"]
        school = dept["school"]

        log.info("Scraping %s (%s)...", name, url)
        soup = _fetch_page(url)
        if not soup:
            log.warning("  Could not fetch %s — skipping", name)
            continue

        if parser == "mit":
            candidates = _parse_mit(soup, url)
        elif parser == "harvard":
            candidates = _parse_harvard(soup, url)
        elif parser == "stanford":
            candidates = _parse_stanford(soup, url)
        elif parser == "chicago":
            candidates = _parse_chicago(soup, url)
        elif parser == "columbia":
            candidates = _parse_columbia(soup, url)
        elif parser == "berkeley":
            candidates = _parse_berkeley(soup, url)
        else:
            candidates = _parse_generic(soup, url, school)

        # Sanity check: a single department shouldn't have >30 candidates.
        # If it does, the parser likely grabbed navigation/faculty garbage.
        MAX_PER_DEPT = 30
        if len(candidates) > MAX_PER_DEPT:
            log.warning("  %s returned %d candidates (likely parser noise) — truncating to %d",
                        name, len(candidates), MAX_PER_DEPT)
            candidates = candidates[:MAX_PER_DEPT]

        log.info("  Found %d candidates from %s", len(candidates), name)
        all_candidates.extend(candidates)
        time.sleep(0.5)

    # Phase 1b: Also load any manual additions from YAML
    if JMP_MANUAL_PATH.exists():
        with open(JMP_MANUAL_PATH) as f:
            manual_data = yaml.safe_load(f) or {}
        manual_candidates = manual_data.get("candidates") or []
        for mc in manual_candidates:
            all_candidates.append({
                "name": mc.get("name", "Unknown"),
                "school": mc.get("school", "Unknown"),
                "fields": mc.get("fields", []),
                "paper_title": mc.get("paper_title", ""),
                "paper_url": mc.get("paper_url", ""),
                "abstract": "",
                "website": "",
            })
        if manual_candidates:
            log.info("Loaded %d manual candidates from YAML", len(manual_candidates))

    log.info("-" * 60)
    log.info("Total candidates scraped: %d", len(all_candidates))

    # Phase 2: Resolve missing paper metadata
    candidates_with_title = sum(1 for c in all_candidates if c.get("paper_title"))
    candidates_without = [c for c in all_candidates if not c.get("paper_title")]
    log.info("  %d have paper titles, %d need API resolution",
             candidates_with_title, len(candidates_without))

    for c in candidates_without:
        if not _is_plausible_name(c["name"]):
            log.debug("  Skipping non-name: %s", c["name"])
            continue
        _resolve_missing_metadata(c, email)
        time.sleep(0.3)

    # Final count
    final_with_title = sum(1 for c in all_candidates if c.get("paper_title"))
    log.info("After resolution: %d candidates with paper titles", final_with_title)

    # Phase 3: Store in database
    new_count = 0
    for c in all_candidates:
        if not c.get("paper_title"):
            continue

        if dry_run:
            log.info("  [DRY] %s (%s): %s", c["name"], c["school"], c["paper_title"][:60])
            new_count += 1
            continue

        if _store_candidate(conn, c, fetch_mod.insert_paper):
            new_count += 1
            log.info("  Added: %s (%s) — %s", c["name"], c["school"], c["paper_title"][:50])

    if not dry_run:
        conn.commit()
        conn.close()

    log.info("=" * 60)
    log.info("Done. %d JMPs %sadded to database.", new_count, "would be " if dry_run else "")
    return new_count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape JMP candidates from top econ programs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes")
    args = parser.parse_args()
    count = run(dry_run=args.dry_run)
    print(f"\n→ {count} JMPs {'would be ' if args.dry_run else ''}added.")
