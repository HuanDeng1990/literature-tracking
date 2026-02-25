#!/usr/bin/env python3
"""
Literature Tracker — Job Market Paper Fetcher
Run once in December to load JMP candidates into the database.

Reads candidates from data/jmp_candidates.yaml, fetches paper metadata
via Semantic Scholar / OpenAlex, and stores them as source='jmp'.

Usage:
    python3 code/05_fetch_jmp.py              # fetch all candidates
    python3 code/05_fetch_jmp.py --dry-run    # preview without writing to DB
"""

import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "data" / "papers.db"
JMP_PATH = ROOT / "data" / "jmp_candidates.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _make_id(title: str, authors: str = "") -> str:
    raw = re.sub(r"\s+", " ", (title + authors).lower().strip())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _search_semantic_scholar(title: str) -> Optional[dict]:
    """Search Semantic Scholar by title and return paper metadata."""
    try:
        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": title[:200],
                "limit": 3,
                "fields": "title,authors,abstract,url,openAccessPdf,externalIds,year",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        if not results:
            return None

        # Pick the best match by title similarity
        title_lower = title.lower().strip()
        for r in results:
            r_title = (r.get("title") or "").lower().strip()
            if _title_similarity(title_lower, r_title) > 0.6:
                return r
        return results[0] if results else None
    except Exception as e:
        log.debug("Semantic Scholar search failed: %s", e)
        return None


def _search_openalex(title: str, email: str) -> Optional[dict]:
    """Search OpenAlex by title and return paper metadata."""
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "search": title[:200],
                "per_page": 3,
                "mailto": email,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None

        title_lower = title.lower().strip()
        for r in results:
            r_title = (r.get("title") or "").lower().strip()
            if _title_similarity(title_lower, r_title) > 0.6:
                return r
        return None
    except Exception as e:
        log.debug("OpenAlex search failed: %s", e)
        return None


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two titles."""
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(len(words_a), len(words_b))


def _extract_abstract_from_openalex(work: dict) -> str:
    inv_index = work.get("abstract_inverted_index")
    if not inv_index:
        return ""
    word_positions = []
    for word, positions in inv_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def fetch_candidate_metadata(candidate: dict, email: str) -> Optional[dict]:
    """
    Given a candidate dict from the YAML, try to resolve paper metadata.
    Returns a paper dict ready for DB insertion, or None on failure.
    """
    name = candidate.get("name", "Unknown")
    school = candidate.get("school", "Unknown")
    title = candidate.get("paper_title", "")
    url = candidate.get("paper_url", "")
    fields = candidate.get("fields", [])

    if not title and not url:
        log.warning("  Skipping %s (%s): no title or URL provided.", name, school)
        return None

    abstract = ""
    doi = ""
    oa_url = url if url.endswith(".pdf") else ""
    resolved_title = title

    # Try Semantic Scholar first (better for working papers)
    if title:
        log.info("  Searching Semantic Scholar for: %s", title[:60])
        ss_result = _search_semantic_scholar(title)
        if ss_result:
            resolved_title = ss_result.get("title") or title
            abstract = ss_result.get("abstract") or ""
            oa_pdf = (ss_result.get("openAccessPdf") or {}).get("url", "")
            if oa_pdf:
                oa_url = oa_pdf
            ext_ids = ss_result.get("externalIds") or {}
            doi = ext_ids.get("DOI", "")
            if not url:
                url = ss_result.get("url", "")
            log.info("    Found on Semantic Scholar")
        time.sleep(0.3)

    # Try OpenAlex as fallback
    if not abstract and title:
        log.info("  Searching OpenAlex for: %s", title[:60])
        oa_result = _search_openalex(title, email)
        if oa_result:
            resolved_title = oa_result.get("title") or title
            abstract = _extract_abstract_from_openalex(oa_result)
            doi = oa_result.get("doi", "") or ""
            oa_info = oa_result.get("open_access") or {}
            if oa_info.get("oa_url") and not oa_url:
                oa_url = oa_info["oa_url"]
            if not url:
                loc = oa_result.get("primary_location") or {}
                url = loc.get("landing_page_url", "") or doi
            log.info("    Found on OpenAlex")
        time.sleep(0.3)

    paper_id = _make_id(resolved_title, name)
    fields_str = ", ".join(fields) if fields else ""
    authors = f"{name} ({school})"

    return {
        "paper_id": paper_id,
        "title": resolved_title,
        "authors": authors,
        "abstract": abstract[:2000],
        "journal": "Job Market Paper",
        "source": "jmp",
        "url": url,
        "doi": doi,
        "oa_url": oa_url,
        "pub_date": "",
        "relevant": 1,
    }


def run(dry_run: bool = False) -> int:
    log.info("=" * 60)
    log.info("Literature Tracker — JMP Fetch")
    log.info("=" * 60)

    if not JMP_PATH.exists():
        log.warning("No JMP candidates file at %s", JMP_PATH)
        return 0

    with open(JMP_PATH) as f:
        jmp_data = yaml.safe_load(f) or {}

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    email = cfg.get("email", "")
    candidates = jmp_data.get("candidates") or []
    season = jmp_data.get("season", "unknown")

    if not candidates:
        log.info("No candidates listed in %s. Add entries and re-run.", JMP_PATH)
        return 0

    log.info("Season: %s — %d candidates", season, len(candidates))

    if not dry_run:
        # Import init_db from fetch module
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from importlib import import_module
        fetch_mod = import_module("01_fetch")

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        fetch_mod.init_db(conn)

    new_count = 0
    for candidate in candidates:
        name = candidate.get("name", "Unknown")
        school = candidate.get("school", "Unknown")
        log.info("Processing: %s (%s)", name, school)

        paper = fetch_candidate_metadata(candidate, email)
        if not paper:
            continue

        if dry_run:
            log.info("  [DRY RUN] Would add: %s", paper["title"][:70])
            new_count += 1
            continue

        existing = conn.execute(
            "SELECT 1 FROM papers WHERE paper_id = ?", (paper["paper_id"],)
        ).fetchone()
        if not existing:
            fetch_mod.insert_paper(conn, paper)
            new_count += 1
            log.info("  Added: %s", paper["title"][:70])
        else:
            log.info("  Already in DB: %s", paper["title"][:70])

    if not dry_run:
        conn.commit()
        conn.close()

    log.info("-" * 60)
    log.info("Done. %d JMPs added to database.", new_count)
    return new_count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch job market papers")
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes")
    args = parser.parse_args()
    count = run(dry_run=args.dry_run)
    print(f"→ {count} JMPs {'would be ' if args.dry_run else ''}added.")
