#!/usr/bin/env python3
"""
Literature Tracker — Paper Downloader
Downloads PDFs for the weekly reading list using a multi-source
fallback chain:
  1. OpenAlex OA URL (already in DB)
  2. Unpaywall API (free, best legal OA coverage)
  3. Semantic Scholar API (supplementary OA source)
  4. NBER direct PDF link (for NBER working papers)

Papers without any discoverable OA version are logged for manual download.
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Mimic a real browser to avoid 403s from publisher CDNs
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
}


def _sanitize_filename(title: str, max_len: int = 80) -> str:
    """Turn a paper title into a safe, readable filename."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:max_len]


def _is_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def _clean_doi(doi: str) -> str:
    """Extract bare DOI from a full URL or bare string."""
    if not doi:
        return ""
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/"):]
    return doi


# ---------------------------------------------------------------------------
# Source 1: OpenAlex OA URL (already in DB)
# ---------------------------------------------------------------------------

def _try_openalex_oa(oa_url: str, timeout: int) -> Optional[bytes]:
    if not oa_url:
        return None
    log.info("    [OpenAlex OA] Trying %s", oa_url[:80])
    try:
        resp = requests.get(oa_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and _is_pdf(resp.content):
            return resp.content
        # Some OA URLs point to HTML landing pages; try appending .pdf or
        # following the "pdf" link heuristic
        if resp.status_code == 200 and b"<html" in resp.content[:500].lower():
            # Try common PDF redirect patterns
            for suffix in [".pdf", "/pdf", "/export/pdf"]:
                pdf_url = oa_url.rstrip("/") + suffix
                resp2 = requests.get(pdf_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
                if resp2.status_code == 200 and _is_pdf(resp2.content):
                    return resp2.content
    except Exception as e:
        log.debug("    [OpenAlex OA] Failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Source 2: Unpaywall API
# ---------------------------------------------------------------------------

def _try_unpaywall(doi: str, email: str, timeout: int) -> Optional[bytes]:
    bare_doi = _clean_doi(doi)
    if not bare_doi:
        return None
    api_url = f"https://api.unpaywall.org/v2/{bare_doi}"
    log.info("    [Unpaywall] Looking up DOI %s", bare_doi)
    try:
        resp = requests.get(api_url, params={"email": email}, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()

        # Try best OA location first, then all locations
        candidates = []
        best = data.get("best_oa_location") or {}
        if best.get("url_for_pdf"):
            candidates.append(best["url_for_pdf"])
        if best.get("url"):
            candidates.append(best["url"])
        for loc in data.get("oa_locations", []):
            if loc.get("url_for_pdf"):
                candidates.append(loc["url_for_pdf"])
            if loc.get("url"):
                candidates.append(loc["url"])

        for pdf_url in dict.fromkeys(candidates):  # dedupe preserving order
            log.info("    [Unpaywall] Trying %s", pdf_url[:80])
            try:
                resp2 = requests.get(pdf_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
                if resp2.status_code == 200 and _is_pdf(resp2.content):
                    return resp2.content
            except Exception:
                continue
    except Exception as e:
        log.debug("    [Unpaywall] Failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar API
# ---------------------------------------------------------------------------

def _try_semantic_scholar(doi: str, title: str, timeout: int) -> Optional[bytes]:
    # Try by DOI first, then by title search
    bare_doi = _clean_doi(doi)
    pdf_url = None

    if bare_doi:
        log.info("    [SemanticScholar] Looking up DOI %s", bare_doi)
        api_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{bare_doi}"
        try:
            resp = requests.get(
                api_url, params={"fields": "openAccessPdf"}, timeout=timeout
            )
            if resp.status_code == 200:
                data = resp.json()
                oa = data.get("openAccessPdf") or {}
                pdf_url = oa.get("url")
        except Exception as e:
            log.debug("    [SemanticScholar] DOI lookup failed: %s", e)

    if not pdf_url and title:
        log.info("    [SemanticScholar] Searching by title")
        try:
            resp = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title[:200], "limit": 1, "fields": "openAccessPdf"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                results = resp.json().get("data", [])
                if results:
                    oa = results[0].get("openAccessPdf") or {}
                    pdf_url = oa.get("url")
        except Exception as e:
            log.debug("    [SemanticScholar] Title search failed: %s", e)

    if pdf_url:
        log.info("    [SemanticScholar] Trying %s", pdf_url[:80])
        try:
            resp = requests.get(pdf_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and _is_pdf(resp.content):
                return resp.content
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Source 4: NBER direct PDF
# ---------------------------------------------------------------------------

def _try_nber_direct(url: str, timeout: int) -> Optional[bytes]:
    if not url or "nber.org" not in url:
        return None
    # Extract paper number from URL like https://www.nber.org/papers/w34862
    match = re.search(r"/papers/(w\d+)", url)
    if not match:
        return None
    paper_num = match.group(1)
    pdf_url = f"https://www.nber.org/system/files/working_papers/{paper_num}/{paper_num}.pdf"
    log.info("    [NBER Direct] Trying %s", pdf_url)
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and _is_pdf(resp.content):
            return resp.content
    except Exception as e:
        log.debug("    [NBER Direct] Failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Main download orchestrator
# ---------------------------------------------------------------------------

def download_papers(papers: list[dict], week_label: str) -> dict:
    """
    Attempt to download PDFs for a list of papers.

    Args:
        papers: list of dicts with keys: title, authors, doi, url, oa_url, journal
        week_label: string like "2026-02-25" for the subfolder name

    Returns:
        dict with keys: downloaded (list of paths), manual (list of paper dicts)
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    dl_cfg = cfg.get("download", {})
    if not dl_cfg.get("enabled", True):
        log.info("Downloads disabled in config.")
        return {"downloaded": [], "manual": []}

    email = cfg.get("email", "user@example.com")
    timeout = dl_cfg.get("timeout", 60)
    base_dir = ROOT / dl_cfg.get("papers_dir", "output/weekly_reading/papers")
    week_dir = base_dir / week_label
    week_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    manual = []

    for i, paper in enumerate(papers, 1):
        title = paper.get("title", "untitled")
        doi = paper.get("doi", "")
        url = paper.get("url", "")
        oa_url = paper.get("oa_url", "")
        journal = paper.get("journal", "")

        safe_name = f"{i:02d}_{_sanitize_filename(title)}.pdf"
        dest = week_dir / safe_name

        if dest.exists():
            log.info("  [%d/%d] Already downloaded: %s", i, len(papers), safe_name)
            downloaded.append(str(dest))
            continue

        log.info("  [%d/%d] %s", i, len(papers), title[:70])

        pdf_bytes = None

        # Fallback chain
        pdf_bytes = _try_openalex_oa(oa_url, timeout)

        if not pdf_bytes:
            pdf_bytes = _try_unpaywall(doi, email, timeout)

        if not pdf_bytes:
            pdf_bytes = _try_semantic_scholar(doi, title, timeout)

        if not pdf_bytes:
            pdf_bytes = _try_nber_direct(url, timeout)

        if pdf_bytes:
            dest.write_bytes(pdf_bytes)
            size_mb = len(pdf_bytes) / (1024 * 1024)
            log.info("    ✓ Saved (%.1f MB): %s", size_mb, safe_name)
            downloaded.append(str(dest))
        else:
            log.warning("    ✗ No OA PDF found — manual download needed")
            manual.append(paper)

        time.sleep(0.5)

    # Write a summary of what needs manual download
    if manual:
        manual_path = week_dir / "manual_downloads.md"
        lines = [
            "# Papers Requiring Manual Download",
            "",
            "These papers had no freely available PDF. Use your society "
            "credentials (AEA, Econometric Society, SOLE) to download them.",
            "",
        ]
        for p in manual:
            url_link = p.get("url") or p.get("doi") or ""
            lines.append(f"- **{p['title']}** (*{p.get('journal', '')}*)")
            if url_link:
                lines.append(f"  [Download link]({url_link})")
            lines.append("")
        manual_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("  Manual download list: %s", manual_path)

    log.info(
        "Download summary: %d/%d downloaded, %d need manual download",
        len(downloaded), len(papers), len(manual),
    )
    return {"downloaded": downloaded, "manual": manual}


if __name__ == "__main__":
    import argparse
    import sqlite3

    parser = argparse.ArgumentParser(description="Download PDFs for weekly picks")
    parser.add_argument("--week", type=str, help="Week label (YYYY-MM-DD)")
    args = parser.parse_args()

    from datetime import datetime, timedelta
    week_label = args.week or datetime.now().strftime("%Y-%m-%d")

    DB_PATH = ROOT / "data" / "papers.db"
    conn = sqlite3.connect(str(DB_PATH))
    since = (datetime.now() - timedelta(days=7)).isoformat()
    cursor = conn.execute(
        """SELECT title, authors, doi, url, oa_url, journal
           FROM papers WHERE fetched_at >= ?
           ORDER BY relevant DESC LIMIT 7""",
        (since,),
    )
    cols = [d[0] for d in cursor.description]
    papers = [dict(zip(cols, row)) for row in cursor.fetchall()]
    conn.close()

    if papers:
        result = download_papers(papers, week_label)
        print(f"→ Downloaded {len(result['downloaded'])} papers")
        if result["manual"]:
            print(f"→ {len(result['manual'])} papers need manual download")
    else:
        print("No papers to download.")
