#!/usr/bin/env python3
"""
Literature Tracker — Digest Generator
Reads the SQLite database and produces a Markdown digest of
new papers since the last run (or since a given date).
"""

import sqlite3
import textwrap
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml

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
# Source grouping for readable output
# ---------------------------------------------------------------------------

SOURCE_ORDER = [
    ("Top 5 Journals", [
        "American Economic Review",
        "Econometrica",
        "Journal of Political Economy",
        "Quarterly Journal of Economics",
        "Review of Economic Studies",
    ]),
    ("Top General & Applied Journals", [
        "Review of Economics and Statistics",
        "Journal of the European Economic Association",
        "AEJ: Applied Economics",
        "AEJ: Economic Policy",
    ]),
    ("Field Journals", [
        "Journal of Labor Economics",
        "Journal of Public Economics",
        "Journal of Health Economics",
        "Journal of Human Resources",
        "RAND Journal of Economics",
        "Journal of Urban Economics",
        "Journal of Development Economics",
        "Journal of Econometrics",
    ]),
    ("NBER Working Papers", [
        "NBER Working Paper",
    ]),
    ("Broad Discovery", None),  # catch-all for openalex_discovery
]


def get_papers_since(conn: sqlite3.Connection, since: str) -> list[dict]:
    cursor = conn.execute(
        """SELECT title, authors, abstract, journal, source, url, doi,
                  pub_date, relevant
           FROM papers
           WHERE fetched_at >= ?
           ORDER BY relevant DESC, journal, pub_date DESC""",
        (since,),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rsplit(" ", 1)[0] + "..."


def format_paper(paper: dict, include_abstract: bool, max_abstract: int) -> str:
    star = " **[RELEVANT]**" if paper["relevant"] else ""
    title = paper["title"]
    authors = paper["authors"] or "Unknown"
    url = paper["url"]

    lines = []
    if url:
        lines.append(f"- **[{title}]({url})**{star}")
    else:
        lines.append(f"- **{title}**{star}")
    lines.append(f"  *{authors}*")

    if paper["pub_date"]:
        lines.append(f"  Published: {paper['pub_date']}")

    if include_abstract and paper["abstract"]:
        short = truncate(paper["abstract"], max_abstract)
        wrapped = textwrap.fill(short, width=90, initial_indent="  > ", subsequent_indent="  > ")
        lines.append(wrapped)

    lines.append("")
    return "\n".join(lines)


def generate_digest(since_date: str = None, lookback_days: int = 7) -> str:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    output_cfg = cfg.get("output", {})
    include_abstract = output_cfg.get("include_abstracts", True)
    max_abstract = output_cfg.get("max_abstract_length", 500)

    conn = sqlite3.connect(str(DB_PATH))

    if since_date is None:
        since_date = (datetime.now() - timedelta(days=lookback_days)).isoformat()

    papers = get_papers_since(conn, since_date)
    conn.close()

    if not papers:
        return "# Literature Digest\n\nNo new papers found since last check.\n"

    today = datetime.now().strftime("%Y-%m-%d")
    relevant_count = sum(1 for p in papers if p["relevant"])

    lines = [
        f"# Literature Digest — {today}",
        "",
        f"**{len(papers)} new papers** found ({relevant_count} flagged as relevant to your keywords).",
        f"Covering: {since_date[:10]} to {today}.",
        "",
        "---",
        "",
    ]

    # Organize papers by journal into groups
    journal_to_papers: dict[str, list[dict]] = {}
    for p in papers:
        j = p["journal"]
        journal_to_papers.setdefault(j, []).append(p)

    placed_journals = set()

    for group_name, journal_list in SOURCE_ORDER:
        if journal_list is None:
            group_papers = [
                p for p in papers if p["source"] == "openalex_discovery"
                and p["journal"] not in placed_journals
            ]
        else:
            group_papers = []
            for j in journal_list:
                if j in journal_to_papers:
                    group_papers.extend(journal_to_papers[j])
                    placed_journals.add(j)

        if not group_papers:
            continue

        lines.append(f"## {group_name}")
        lines.append("")

        if journal_list is None:
            for p in group_papers:
                lines.append(format_paper(p, include_abstract, max_abstract))
        else:
            current_journal = None
            for p in group_papers:
                if p["journal"] != current_journal:
                    current_journal = p["journal"]
                    lines.append(f"### {current_journal}")
                    lines.append("")
                lines.append(format_paper(p, include_abstract, max_abstract))

    # Any journals not in our predefined groups
    remaining = {j: ps for j, ps in journal_to_papers.items() if j not in placed_journals}
    if remaining:
        lines.append("## Other Sources")
        lines.append("")
        for j, ps in remaining.items():
            lines.append(f"### {j}")
            lines.append("")
            for p in ps:
                lines.append(format_paper(p, include_abstract, max_abstract))

    return "\n".join(lines)


def run(lookback_days: int = 7):
    log.info("Generating digest (last %d days)...", lookback_days)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    digest_dir = ROOT / cfg.get("output", {}).get("digest_dir", "output/digests")
    digest_dir.mkdir(parents=True, exist_ok=True)

    md = generate_digest(lookback_days=lookback_days)

    today = datetime.now().strftime("%Y-%m-%d")
    out_path = digest_dir / f"digest_{today}.md"
    out_path.write_text(md, encoding="utf-8")

    log.info("Digest written to %s", out_path)
    return str(out_path)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    args = parser.parse_args()
    run(lookback_days=args.days)
