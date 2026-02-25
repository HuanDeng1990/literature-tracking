# Literature Tracking

Automated tracker for economics publications, NBER working papers, and job market papers. Designed for applied microeconomists who want a weekly digest, curated reading list, and auto-downloaded PDFs.

## What it does

1. **Fetches** new papers from three sources:
   - **Journal RSS feeds** — top-5 (AER, Econometrica, JPE, QJE, REStud), top field journals (JLE, JPubE, JHE, RAND, etc.)
   - **NBER working papers** — via their official RSS feed
   - **OpenAlex API** — for journals without RSS (AEA journals) and broad keyword-based discovery across all indexed sources
2. **Deduplicates** against a local SQLite database so you only see new papers.
3. **Flags relevance** based on your configured keywords (structural estimation, causal inference, etc.).
4. **Generates a Markdown digest** organized by journal tier, with abstracts and links.
5. **Picks 7 best papers** for a weekly reading list, scored on:
   - Journal tier (top-5 weighted highest)
   - Field match (labor economics, political economy, applied micro)
   - Structural model content
   - Novel data usage
   - Novel measurement / conceptualization
6. **Downloads PDFs** automatically using a multi-source fallback chain (see below).
7. **Sends email + macOS notification** when your reading list is ready.
8. **Runs automatically every Sunday at 9 AM** via macOS `launchd`.

## Quick start

```bash
# Install dependencies
pip3 install -r requirements.txt

# Edit config.yaml — set your email and customize keywords/journals
# Then run:
python3 code/00_master.py

# Options:
python3 code/00_master.py --days 14        # 14-day lookback
python3 code/00_master.py --fetch-only     # just fetch, no digest or picks
python3 code/00_master.py --digest-only    # regenerate digest from existing DB
python3 code/00_master.py --picks-only     # regenerate weekly reading list + downloads
python3 code/00_master.py --no-download    # skip PDF downloads
python3 code/00_master.py --no-notify      # skip notifications
```

Outputs:
- Full digest → `output/digests/digest_YYYY-MM-DD.md`
- Weekly 7-paper reading list → `output/weekly_reading/reading_YYYY-MM-DD.md`
- Downloaded PDFs → `output/weekly_reading/papers/YYYY-MM-DD/`
- Manual download list → `output/weekly_reading/papers/YYYY-MM-DD/manual_downloads.md`

## Automatic PDF downloads

For each of the 7 weekly picks, the downloader tries a **multi-source fallback chain** to find a legal open-access PDF:

| Priority | Source | Coverage |
|---|---|---|
| 1 | **OpenAlex OA URL** | Indexed OA versions (preprints, repositories) |
| 2 | **Unpaywall API** | Best legal OA coverage — green OA, author manuscripts, NBER drafts |
| 3 | **Semantic Scholar** | Supplementary OA source, good for recent preprints |
| 4 | **NBER direct link** | Direct PDF for NBER working papers |

**What gets downloaded automatically:**
- Papers with NBER working paper versions (very common for top-5 authors)
- Gold/green open access papers
- Author-posted preprints (via institutional repositories, SSRN)
- Older papers that have been made freely available

**What needs manual download:**
- Very recent papers from subscription journals (QJE, REStat, etc.) where no preprint exists yet
- A `manual_downloads.md` file is generated with direct DOI links for these papers
- Use your AEA, Econometric Society, or SOLE credentials to download them

As papers age, their OA availability improves. Re-running the tracker later may find PDFs that weren't available initially.

## Email setup (Gmail)

Email reminders are configured to send to `denghuannsd@gmail.com`. To activate:

1. **Enable 2-Step Verification** on your Google account at https://myaccount.google.com/security

2. **Create an App Password**:
   - Go to https://myaccount.google.com/apppasswords
   - Select "Mail" and "Mac" (or "Other")
   - Copy the 16-character password Google generates

3. **Set the environment variable** (add to your `~/.zshrc`):
   ```bash
   export LIT_TRACKER_EMAIL_PWD="xxxx xxxx xxxx xxxx"
   ```
   Then `source ~/.zshrc`.

4. **For the launchd job** to see the variable, also add it to the plist or create `~/.launchd.conf`, or add to the plist `EnvironmentVariables`:
   ```bash
   # Quick approach: edit the plist to include:
   #   <key>EnvironmentVariables</key>
   #   <dict>
   #       <key>LIT_TRACKER_EMAIL_PWD</key>
   #       <string>xxxx xxxx xxxx xxxx</string>
   #   </dict>
   ```

Each Sunday you'll receive an email with the full reading list (titles, authors, abstracts, links) and a list of any papers needing manual download.

## Automatic weekly schedule

A `launchd` job is installed at `~/Library/LaunchAgents/com.littracker.weekly.plist` that runs every **Sunday at 9:00 AM**. It will:

1. Fetch all new papers from the past week
2. Generate the full digest
3. Pick the top 7 papers for your reading list
4. Download available PDFs
5. Send email + macOS notification
6. Auto-open the reading list

**Manage the schedule:**

```bash
# Check status
launchctl list | grep littracker

# Disable
launchctl unload ~/Library/LaunchAgents/com.littracker.weekly.plist

# Re-enable
launchctl load ~/Library/LaunchAgents/com.littracker.weekly.plist

# After editing the plist:
launchctl unload ~/Library/LaunchAgents/com.littracker.weekly.plist
cp com.littracker.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.littracker.weekly.plist
```

Logs: `data/temp/launchd_stdout.log` and `data/temp/launchd_stderr.log`.

## Weekly reading list scoring

The picker scores every paper fetched in the past week and selects the top 7. Scoring dimensions (configurable weights in `config.yaml` under `weekly_picks.weights`):

| Dimension | What it rewards | Default weight |
|---|---|---|
| `journal_top5` | Published in AER, Econometrica, JPE, QJE, REStud | 30 |
| `journal_top_field` | Published in REStat, JEEA, AEJ journals | 20 |
| `nber` | NBER working paper | 18 |
| `jmp` | Job market paper from top program | 17 |
| `field_match` | Matches labor, political economy, or applied micro keywords | 25 |
| `structural` | Structural model, counterfactual, dynamic programming, BLP, etc. | 20 |
| `novel_data` | Administrative data, linked data, satellite, text-as-data, etc. | 15 |
| `novel_measurement` | New measure, index, decomposition, conceptualization, etc. | 15 |
| `keyword_relevant` | Matches general keywords from the keywords list | 10 |

Papers can accumulate points from multiple dimensions. The reading list also shows a "Also worth a look" runner-up list of 7 more papers.

## Configuration

All settings live in `config.yaml`:

| Section | What to customize |
|---|---|
| `email` | Your email — gives you faster OpenAlex API access |
| `keywords` | Terms checked against title + abstract for relevance flagging |
| `weekly_picks` | Field keywords, structural/data/measurement keywords, scoring weights |
| `journals` | Add/remove journals; each needs `type: rss` (with URL) or `type: openalex` (with source ID) |
| `nber.feeds` | Uncomment program-specific feeds (labor, public, IO, etc.) for targeted tracking |
| `openalex_discovery` | Toggle broad keyword search; adjust lookback window |
| `download` | Toggle auto-download, set timeout, change papers directory |
| `notification` | macOS banner, auto-open, email settings |
| `output` | Toggle abstracts, set max length, change output directories |

### Adding a new journal

**If it has an RSS feed:**
```yaml
- name: "Journal of Finance"
  type: rss
  url: "https://onlinelibrary.wiley.com/feed/15406261/most-recent"
```

**If no RSS, use OpenAlex** (find the source ID at [openalex.org/sources](https://openalex.org/sources)):
```yaml
- name: "Some Journal"
  type: openalex
  openalex_id: "S12345678"
```

## Folder structure

```
├── config.yaml                    # All settings
├── com.littracker.weekly.plist    # macOS launchd schedule (source copy)
├── com.littracker.jmp.plist       # December-only JMP fetch schedule
├── code/
│   ├── 00_master.py               # Orchestrates fetch → digest → picks → download → notify
│   ├── 01_fetch.py                # Pulls papers from RSS / OpenAlex / NBER
│   ├── 02_digest.py               # Generates full Markdown digest
│   ├── 03_weekly_picks.py         # Scores and selects top 7 papers
│   ├── 04_download.py             # Multi-source PDF downloader
│   ├── 05_fetch_jmp.py            # Job market paper fetcher (December)
│   └── notify.py                  # macOS notifications + email
├── data/
│   ├── papers.db                  # SQLite database (auto-created)
│   ├── jmp_candidates.yaml        # Curated JMP list (update each December)
│   └── temp/                      # Logs
├── output/
│   ├── digests/                   # Full weekly/daily digests
│   └── weekly_reading/
│       ├── reading_YYYY-MM-DD.md  # Curated 7-paper reading lists
│       └── papers/                # Downloaded PDFs by week
│           └── YYYY-MM-DD/
│               ├── 01_paper_title.pdf
│               ├── ...
│               └── manual_downloads.md
├── requirements.txt
└── README.md
```

## Job market papers

JMPs are seasonal (October–May), so the system handles them differently from regular journal tracking:

**How it works:**

1. **In December**, populate `data/jmp_candidates.yaml` with candidates whose research interests you — visit the department placement pages listed in the file, browse [EconNow](https://econ.now/candidates), or ask colleagues.
2. **Run the JMP fetch** (or let the launchd job handle it on Dec 10):
   ```bash
   python3 code/00_master.py --jmp              # fetch & add to DB
   python3 code/05_fetch_jmp.py --dry-run       # preview without writing
   ```
3. The script resolves paper metadata via Semantic Scholar and OpenAlex, finds OA PDFs, and stores them as `source=jmp` in the database.
4. JMPs then compete with regular papers in the weekly reading scorer — they appear alongside top-5 and NBER papers in your weekly picks.

**YAML format** (`data/jmp_candidates.yaml`):
```yaml
candidates:
  - name: "Jane Doe"
    school: "MIT"
    fields: ["labor", "public"]
    paper_title: "The Effect of X on Y: Evidence from Z"
    paper_url: "https://janedoe.com/jmp.pdf"    # optional, helps PDF download
```

**Scheduling:** A separate launchd job (`com.littracker.jmp.plist`) runs December 10 to auto-fetch. You can also run `--jmp` manually anytime.

```bash
# Manage JMP schedule
launchctl list | grep littracker.jmp
launchctl unload ~/Library/LaunchAgents/com.littracker.jmp.plist
launchctl load ~/Library/LaunchAgents/com.littracker.jmp.plist
```

## Data sources & limits

- **RSS feeds**: Free, no auth. Some journals may change URLs over time — update `config.yaml` if a feed stops working.
- **OpenAlex API**: Free, no key needed. 100k requests/day limit. Adding your email in config gets you into the "polite pool" with better rate limits.
- **Unpaywall API**: Free, just needs an email. Best coverage of legal OA locations.
- **Semantic Scholar API**: Free, no key needed. Rate-limited to ~100 requests/5 minutes.
- **NBER RSS**: Free, reliable, updated weekly on Mondays.
