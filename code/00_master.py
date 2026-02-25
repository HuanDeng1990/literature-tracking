#!/usr/bin/env python3
"""
Literature Tracker — Master Script
Runs the full pipeline: fetch → digest → weekly picks → download → notify.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

fetch_mod = import_module("01_fetch")
digest_mod = import_module("02_digest")
picks_mod = import_module("03_weekly_picks")
download_mod = import_module("04_download")
jmp_mod = import_module("05_fetch_jmp")
notify_mod = import_module("notify")


def main():
    parser = argparse.ArgumentParser(
        description="Run the literature tracker: fetch + digest + picks + download."
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Lookback window for the digest (default: 7 days)",
    )
    parser.add_argument(
        "--fetch-only", action="store_true",
        help="Only fetch, skip digest, picks, and downloads",
    )
    parser.add_argument(
        "--digest-only", action="store_true",
        help="Only generate digest from existing DB",
    )
    parser.add_argument(
        "--picks-only", action="store_true",
        help="Only generate weekly reading list from existing DB",
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="Skip PDF downloads",
    )
    parser.add_argument(
        "--no-notify", action="store_true",
        help="Skip notifications",
    )
    parser.add_argument(
        "--jmp", action="store_true",
        help="Fetch job market papers from data/jmp_candidates.yaml (run in December)",
    )
    args = parser.parse_args()

    new_count = 0
    digest_path = ""
    picks_path = ""
    picks_md = ""
    selected_papers = []
    dl_result = {"downloaded": [], "manual": []}

    # Step 0: JMP fetch (December only, triggered manually)
    if args.jmp:
        jmp_count = jmp_mod.run()
        print(f"→ {jmp_count} job market papers added to database.")
        if not args.picks_only:
            return

    # Step 1: Fetch
    if not args.digest_only and not args.picks_only:
        new_count = fetch_mod.run()
        print(f"\n→ {new_count} new papers added to database.")

    # Step 2: Full digest
    if not args.fetch_only and not args.picks_only:
        digest_path = digest_mod.run(lookback_days=args.days)
        print(f"→ Digest written to: {digest_path}")

    # Step 3: Weekly reading picks
    if not args.fetch_only and not args.digest_only:
        picks_md, picks_path, selected_papers = picks_mod.pick_weekly_reading(
            lookback_days=args.days
        )
        if picks_path:
            print(f"→ Weekly reading list: {picks_path}")

    # Step 4: Download PDFs for selected papers
    if selected_papers and not args.no_download and not args.fetch_only:
        week_label = datetime.now().strftime("%Y-%m-%d")
        dl_result = download_mod.download_papers(selected_papers, week_label)
        n_dl = len(dl_result["downloaded"])
        n_man = len(dl_result["manual"])
        print(f"→ Downloaded {n_dl}/{len(selected_papers)} papers" +
              (f", {n_man} need manual download" if n_man else ""))

    # Step 5: Notify
    if not args.no_notify and picks_path:
        n_dl = len(dl_result["downloaded"])
        n_man = len(dl_result["manual"])
        dl_note = f" {n_dl} PDFs downloaded." if n_dl else ""
        man_note = f" {n_man} need manual download." if n_man else ""
        summary = (
            f"Your 7 papers for the week are ready."
            f"{dl_note}{man_note}"
        )

        email_body = picks_md
        if dl_result["manual"]:
            email_body += "\n\n---\n\n## Papers needing manual download\n\n"
            for p in dl_result["manual"]:
                url = p.get("url") or p.get("doi") or ""
                email_body += f"- **{p['title']}** (*{p.get('journal', '')}*)\n"
                if url:
                    email_body += f"  {url}\n"

        notify_mod.notify(
            reading_list_path=picks_path,
            summary=summary,
            body_md=email_body,
        )


if __name__ == "__main__":
    main()
