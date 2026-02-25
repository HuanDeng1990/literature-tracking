#!/usr/bin/env python3
"""
Literature Tracker — Weekly Reading List
Scores all papers from the past week across multiple dimensions
(journal tier, field relevance, structural content, novel data/measurement)
and selects the top N for a curated weekly reading list.
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
# Journal tier lookup
# ---------------------------------------------------------------------------

TOP5 = {
    "American Economic Review",
    "Econometrica",
    "Journal of Political Economy",
    "Quarterly Journal of Economics",
    "Review of Economic Studies",
}

TOP_FIELD = {
    "Review of Economics and Statistics",
    "Journal of the European Economic Association",
    "AEJ: Applied Economics",
    "AEJ: Economic Policy",
    "AEJ: Microeconomics",
    "JPE Microeconomics",
}

FIELD_JOURNALS = {
    "Journal of Labor Economics",
    "Journal of Public Economics",
    "Journal of Health Economics",
    "Journal of Human Resources",
    "RAND Journal of Economics",
    "Journal of Urban Economics",
    "Journal of Development Economics",
    "Journal of Econometrics",
    "Journal of Political Economy Microeconomics",
}


def _keyword_hits(text: str, keywords: list[str]) -> int:
    text_lower = text.lower()
    return sum(1 for kw in keywords if kw.lower() in text_lower)


def score_paper(paper: dict, cfg_picks: dict, weights: dict) -> float:
    """
    Compute a composite score for a paper.
    Higher = more likely to be selected for the weekly reading list.
    """
    score = 0.0
    journal = paper["journal"]
    text = (paper["title"] + " " + paper["abstract"]).lower()

    # --- Journal tier ---
    if journal in TOP5:
        score += weights.get("journal_top5", 30)
    elif journal in TOP_FIELD:
        score += weights.get("journal_top_field", 20)
    elif journal in FIELD_JOURNALS:
        score += weights.get("journal_field", 15)
    elif "NBER" in journal:
        score += weights.get("nber", 18)

    # --- Field match (labor, political economy, applied micro) ---
    field_kws = cfg_picks.get("field_keywords", {})
    field_hits = 0
    for _field_name, kw_list in field_kws.items():
        field_hits += _keyword_hits(text, kw_list)
    if field_hits > 0:
        score += weights.get("field_match", 25) * min(field_hits, 5) / 3.0

    # --- Structural paper bonus ---
    struct_hits = _keyword_hits(text, cfg_picks.get("structural_keywords", []))
    if struct_hits > 0:
        score += weights.get("structural", 20) * min(struct_hits, 4) / 2.0

    # --- Novel data bonus ---
    data_hits = _keyword_hits(text, cfg_picks.get("novel_data_keywords", []))
    if data_hits > 0:
        score += weights.get("novel_data", 15) * min(data_hits, 4) / 2.0

    # --- Novel measurement / conceptualization bonus ---
    meas_hits = _keyword_hits(text, cfg_picks.get("novel_measurement_keywords", []))
    if meas_hits > 0:
        score += weights.get("novel_measurement", 15) * min(meas_hits, 3) / 2.0

    # --- General keyword relevance ---
    if paper.get("relevant"):
        score += weights.get("keyword_relevant", 10)

    return round(score, 2)


def get_recent_papers(conn: sqlite3.Connection, days: int) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        """SELECT paper_id, title, authors, abstract, journal, source,
                  url, doi, oa_url, pub_date, relevant
           FROM papers
           WHERE fetched_at >= ?""",
        (since,),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def pick_weekly_reading(lookback_days: int = 7) -> tuple[str, str, list[dict]]:
    """
    Score, rank, and format the top N papers.
    Returns (markdown_string, output_file_path, selected_paper_dicts).
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    picks_cfg = cfg.get("weekly_picks", {})
    weights = picks_cfg.get("weights", {})
    num_papers = picks_cfg.get("num_papers", 7)
    output_cfg = cfg.get("output", {})
    max_abstract = output_cfg.get("max_abstract_length", 500)

    conn = sqlite3.connect(str(DB_PATH))
    papers = get_recent_papers(conn, lookback_days)
    conn.close()

    if not papers:
        log.warning("No papers found in the last %d days.", lookback_days)
        return "# Weekly Reading List\n\nNo new papers this week.\n", "", []

    for p in papers:
        p["_score"] = score_paper(p, picks_cfg, weights)

    papers.sort(key=lambda p: p["_score"], reverse=True)

    # Deduplicate: keep only the highest-scoring entry per unique title
    seen_titles = set()
    unique = []
    for p in papers:
        title_norm = p["title"].strip().lower()
        if title_norm not in seen_titles:
            seen_titles.add(title_norm)
            unique.append(p)

    selected = unique[:num_papers]

    # --- Format the reading list ---
    today = datetime.now()
    week_start = today + timedelta(days=1)
    week_end = week_start + timedelta(days=6)
    week_label = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"

    lines = [
        f"# Weekly Reading List",
        f"## {week_label}",
        "",
        f"*Curated on {today.strftime('%A, %B %d, %Y')} — 7 papers selected from "
        f"{len(papers)} new items this week.*",
        "",
        "Selection criteria: labor economics, political economy, applied micro, "
        "structural models, novel data, novel measurement/conceptualization.",
        "",
        "---",
        "",
    ]

    for i, p in enumerate(selected, 1):
        title = p["title"]
        authors = p["authors"] or "Unknown"
        journal = p["journal"]
        url = p["url"]
        abstract = p["abstract"] or ""
        score = p["_score"]
        pub_date = p["pub_date"]

        tags = _make_tags(p, picks_cfg)
        tag_str = "  " + " ".join(f"`{t}`" for t in tags) if tags else ""

        lines.append(f"### {i}. {title}")
        lines.append("")

        if url:
            lines.append(f"**[Open paper]({url})**")
        lines.append(f"*{authors}*")
        lines.append(f"*{journal}*" + (f" — {pub_date}" if pub_date else ""))
        if tag_str:
            lines.append(tag_str)
        lines.append("")

        if abstract:
            short = abstract[:max_abstract]
            if len(abstract) > max_abstract:
                short = short.rsplit(" ", 1)[0] + "..."
            wrapped = textwrap.fill(
                short, width=90, initial_indent="> ", subsequent_indent="> "
            )
            lines.append(wrapped)
            lines.append("")

        lines.append("---")
        lines.append("")

    # Runner-up list (next 7)
    runners = unique[num_papers : num_papers + 7]
    if runners:
        lines.append("## Also worth a look")
        lines.append("")
        for p in runners:
            url_bit = f" — [link]({p['url']})" if p["url"] else ""
            lines.append(f"- **{p['title']}** (*{p['journal']}*){url_bit}")
        lines.append("")

    md = "\n".join(lines)

    # Write to file
    picks_dir = ROOT / output_cfg.get("weekly_picks_dir", "output/weekly_reading")
    picks_dir.mkdir(parents=True, exist_ok=True)
    fname = f"reading_{today.strftime('%Y-%m-%d')}.md"
    out_path = picks_dir / fname
    out_path.write_text(md, encoding="utf-8")

    log.info("Weekly reading list: %s", out_path)
    return md, str(out_path), selected


def _make_tags(paper: dict, picks_cfg: dict) -> list[str]:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    tags = []

    field_kws = picks_cfg.get("field_keywords", {})
    for field_name, kw_list in field_kws.items():
        if _keyword_hits(text, kw_list) > 0:
            pretty = field_name.replace("_", " ").title()
            tags.append(pretty)

    if _keyword_hits(text, picks_cfg.get("structural_keywords", [])) > 0:
        tags.append("Structural")
    if _keyword_hits(text, picks_cfg.get("novel_data_keywords", [])) > 0:
        tags.append("Novel Data")
    if _keyword_hits(text, picks_cfg.get("novel_measurement_keywords", [])) > 0:
        tags.append("Novel Measurement")

    journal = paper["journal"]
    if journal in TOP5:
        tags.append("Top 5")
    elif "NBER" in journal:
        tags.append("NBER")

    return tags


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    args = parser.parse_args()
    md, path, selected = pick_weekly_reading(lookback_days=args.days)
    if path:
        print(f"→ Weekly reading list: {path}")
        print(f"→ {len(selected)} papers selected")
