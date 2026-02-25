#!/usr/bin/env python3
"""
Literature Tracker — Fetch Module
Pulls new papers from RSS feeds, NBER, and OpenAlex API.
Stores results in a local SQLite database with deduplication.
"""

import sqlite3
import hashlib
import re
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "data" / "papers.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            paper_id   TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            authors    TEXT,
            abstract   TEXT,
            journal    TEXT,
            source     TEXT,       -- 'rss', 'openalex', 'nber'
            url        TEXT,
            doi        TEXT,
            oa_url     TEXT DEFAULT '',
            pub_date   TEXT,
            fetched_at TEXT NOT NULL,
            relevant   INTEGER DEFAULT 0
        )
    """)
    # Migrations for existing databases
    for col, typedef in [
        ("oa_url", "TEXT DEFAULT ''"),
        ("picked", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetched ON papers(fetched_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_journal ON papers(journal)
    """)
    conn.commit()


def paper_exists(conn: sqlite3.Connection, paper_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    return row is not None


def insert_paper(conn: sqlite3.Connection, paper: dict):
    conn.execute(
        """INSERT OR IGNORE INTO papers
           (paper_id, title, authors, abstract, journal, source, url, doi, oa_url, pub_date, fetched_at, relevant)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            paper["paper_id"],
            paper["title"],
            paper.get("authors", ""),
            paper.get("abstract", ""),
            paper.get("journal", ""),
            paper.get("source", ""),
            paper.get("url", ""),
            paper.get("doi", ""),
            paper.get("oa_url", ""),
            paper.get("pub_date", ""),
            datetime.now().isoformat(),
            paper.get("relevant", 0),
        ),
    )


def update_oa_url(conn: sqlite3.Connection, paper_id: str, oa_url: str):
    """Backfill OA URL for papers that gained one since initial fetch."""
    if oa_url:
        conn.execute(
            "UPDATE papers SET oa_url = ? WHERE paper_id = ? AND (oa_url IS NULL OR oa_url = '')",
            (oa_url, paper_id),
        )


def make_id(title: str, authors: str = "") -> str:
    """Deterministic ID from normalized title + first author."""
    raw = re.sub(r"\s+", " ", (title + authors).lower().strip())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def check_relevance(title: str, abstract: str, keywords: list[str]) -> bool:
    text = (title + " " + abstract).lower()
    return any(kw.lower() in text for kw in keywords)


# ---------------------------------------------------------------------------
# RSS feed fetcher
# ---------------------------------------------------------------------------

def fetch_rss(feed_url: str, journal_name: str, keywords: list[str]) -> list[dict]:
    papers = []
    log.info("  RSS: %s", journal_name)
    try:
        feed = feedparser.parse(feed_url)
        if feed.bozo and not feed.entries:
            log.warning("  Feed error for %s: %s", journal_name, feed.bozo_exception)
            return papers
    except Exception as e:
        log.error("  Failed to parse %s: %s", journal_name, e)
        return papers

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        authors = ", ".join(
            a.get("name", "") for a in entry.get("authors", entry.get("author_detail", []))
            if isinstance(a, dict)
        ) or entry.get("author", "")

        abstract = entry.get("summary", "") or entry.get("description", "")
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        link = entry.get("link", "")
        doi = entry.get("prism_doi", "") or entry.get("dc_identifier", "")

        pub_date = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_date = time.strftime("%Y-%m-%d", entry.published_parsed)
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            pub_date = time.strftime("%Y-%m-%d", entry.updated_parsed)

        paper_id = make_id(title, authors)
        relevant = check_relevance(title, abstract, keywords)

        papers.append({
            "paper_id": paper_id,
            "title": title,
            "authors": authors,
            "abstract": abstract[:2000],
            "journal": journal_name,
            "source": "rss",
            "url": link,
            "doi": doi,
            "pub_date": pub_date,
            "relevant": int(relevant),
        })

    log.info("    Found %d entries", len(papers))
    return papers


# ---------------------------------------------------------------------------
# OpenAlex fetcher (for journals without RSS)
# ---------------------------------------------------------------------------

def fetch_openalex_journal(
    openalex_id: str,
    journal_name: str,
    email: str,
    keywords: list[str],
    lookback_days: int = 60,
) -> list[dict]:
    papers = []
    log.info("  OpenAlex: %s", journal_name)
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    source_short = openalex_id.replace("https://openalex.org/", "")

    url = "https://api.openalex.org/works"
    params = {
        "filter": f"primary_location.source.id:{source_short},from_publication_date:{since}",
        "sort": "publication_date:desc",
        "per_page": 50,
        "mailto": email,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("  OpenAlex error for %s: %s", journal_name, e)
        return papers

    for work in data.get("results", []):
        title = work.get("title", "").strip()
        if not title:
            continue

        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in work.get("authorships", [])[:10]
        )

        abstract = ""
        inv_index = work.get("abstract_inverted_index")
        if inv_index:
            word_positions = []
            for word, positions in inv_index.items():
                for pos in positions:
                    word_positions.append((pos, word))
            word_positions.sort()
            abstract = " ".join(w for _, w in word_positions)

        doi = work.get("doi", "") or ""
        paper_url = work.get("primary_location", {}).get("landing_page_url", "") or doi
        pub_date = work.get("publication_date", "")

        oa_info = work.get("open_access") or {}
        oa_url = oa_info.get("oa_url", "") or ""

        paper_id = make_id(title, authors)
        relevant = check_relevance(title, abstract, keywords)

        papers.append({
            "paper_id": paper_id,
            "title": title,
            "authors": authors,
            "abstract": abstract[:2000],
            "journal": journal_name,
            "source": "openalex",
            "url": paper_url,
            "doi": doi,
            "oa_url": oa_url,
            "pub_date": pub_date,
            "relevant": int(relevant),
        })

    log.info("    Found %d entries", len(papers))
    return papers


# ---------------------------------------------------------------------------
# OpenAlex broad keyword discovery
# ---------------------------------------------------------------------------

def fetch_openalex_discovery(
    email: str,
    keywords: list[str],
    lookback_days: int = 14,
    max_results: int = 50,
) -> list[dict]:
    papers = []
    log.info("  OpenAlex discovery search")
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    batched_keywords = [keywords[i : i + 3] for i in range(0, len(keywords), 3)]

    for batch in batched_keywords:
        query = " OR ".join(batch)
        url = "https://api.openalex.org/works"
        params = {
            "search": query,
            "filter": f"from_publication_date:{since},type:article",
            "sort": "publication_date:desc",
            "per_page": min(max_results, 50),
            "mailto": email,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("  Discovery search error: %s", e)
            continue

        for work in data.get("results", []):
            title = work.get("title", "").strip()
            if not title:
                continue

            source_info = (work.get("primary_location") or {}).get("source") or {}
            journal_name = source_info.get("display_name", "Unknown")

            authors = ", ".join(
                a.get("author", {}).get("display_name", "")
                for a in work.get("authorships", [])[:10]
            )

            abstract = ""
            inv_index = work.get("abstract_inverted_index")
            if inv_index:
                word_positions = []
                for word, positions in inv_index.items():
                    for pos in positions:
                        word_positions.append((pos, word))
                word_positions.sort()
                abstract = " ".join(w for _, w in word_positions)

            doi = work.get("doi", "") or ""
            paper_url = (work.get("primary_location") or {}).get("landing_page_url", "") or doi
            pub_date = work.get("publication_date", "")

            oa_info = work.get("open_access") or {}
            oa_url = oa_info.get("oa_url", "") or ""

            paper_id = make_id(title, authors)

            papers.append({
                "paper_id": paper_id,
                "title": title,
                "authors": authors,
                "abstract": abstract[:2000],
                "journal": journal_name,
                "source": "openalex_discovery",
                "url": paper_url,
                "doi": doi,
                "oa_url": oa_url,
                "pub_date": pub_date,
                "relevant": 1,
            })

        time.sleep(0.2)

    log.info("    Found %d discovery results", len(papers))
    return papers


# ---------------------------------------------------------------------------
# NBER working papers via RSS
# ---------------------------------------------------------------------------

def fetch_nber(feeds: list[dict], keywords: list[str]) -> list[dict]:
    papers = []
    for feed_info in feeds:
        name = feed_info["name"]
        url = feed_info["url"]
        log.info("  NBER: %s", name)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.error("  NBER feed error: %s", e)
            continue

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            authors = entry.get("author", "")
            abstract = entry.get("summary", "") or entry.get("description", "")
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()
            link = entry.get("link", "")

            pub_date = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_date = time.strftime("%Y-%m-%d", entry.published_parsed)

            paper_id = make_id(title, authors)
            relevant = check_relevance(title, abstract, keywords)

            papers.append({
                "paper_id": paper_id,
                "title": title,
                "authors": authors,
                "abstract": abstract[:2000],
                "journal": "NBER Working Paper",
                "source": "nber",
                "url": link,
                "doi": "",
                "pub_date": pub_date,
                "relevant": int(relevant),
            })

        log.info("    Found %d entries", len(feed.entries))
    return papers


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    log.info("=" * 60)
    log.info("Literature Tracker — Fetch Run")
    log.info("=" * 60)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    email = cfg.get("email", "")
    keywords = cfg.get("keywords", [])
    journals = cfg.get("journals", [])
    nber_cfg = cfg.get("nber", {})
    discovery_cfg = cfg.get("openalex_discovery", {})

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    new_count = 0
    total_fetched = 0

    # --- Journal feeds ---
    log.info("Fetching journal publications...")
    for j in journals:
        if j["type"] == "rss":
            papers = fetch_rss(j["url"], j["name"], keywords)
        elif j["type"] == "openalex":
            papers = fetch_openalex_journal(
                j["openalex_id"], j["name"], email, keywords
            )
        else:
            log.warning("  Unknown type for %s: %s", j["name"], j["type"])
            continue

        for p in papers:
            total_fetched += 1
            if not paper_exists(conn, p["paper_id"]):
                insert_paper(conn, p)
                new_count += 1
            update_oa_url(conn, p["paper_id"], p.get("oa_url", ""))
        conn.commit()
        time.sleep(0.3)

    # --- NBER working papers ---
    if nber_cfg.get("enabled"):
        log.info("Fetching NBER working papers...")
        nber_papers = fetch_nber(nber_cfg.get("feeds", []), keywords)
        for p in nber_papers:
            total_fetched += 1
            if not paper_exists(conn, p["paper_id"]):
                insert_paper(conn, p)
                new_count += 1
            update_oa_url(conn, p["paper_id"], p.get("oa_url", ""))
        conn.commit()

    # --- OpenAlex discovery ---
    if discovery_cfg.get("enabled"):
        log.info("Running OpenAlex broad discovery...")
        disc_papers = fetch_openalex_discovery(
            email,
            keywords,
            lookback_days=discovery_cfg.get("lookback_days", 14),
            max_results=discovery_cfg.get("max_results_per_query", 50),
        )
        for p in disc_papers:
            total_fetched += 1
            if not paper_exists(conn, p["paper_id"]):
                insert_paper(conn, p)
                new_count += 1
            update_oa_url(conn, p["paper_id"], p.get("oa_url", ""))
        conn.commit()

    conn.close()

    log.info("-" * 60)
    log.info("Done. Fetched %d items, %d new papers stored.", total_fetched, new_count)
    log.info("Database: %s", DB_PATH)
    return new_count


if __name__ == "__main__":
    run()
