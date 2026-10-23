# CMT Conference Agent

Collects conference links from [Microsoft CMT](https://cmt3.research.microsoft.com) by signing in with **your own CMT account** in a real browser (Playwright) and reading the **Conference List** page the same way you would: open the *All Conferences* tab, optionally type in the filter box, press *Show More* / *Show All*, and read the table.

For every conference you get its **CMT link**, its **own website (External URL)**, short name, start/end dates and location, saved as Word, CSV and JSON.

**Full illustrated guide** (architecture, step-by-step run flow, tools, setup, every option, troubleshooting): [docs/CMT_Conference_Agent_Guide.pdf](docs/CMT_Conference_Agent_Guide.pdf)

## How it stays legitimate

| Safeguard | What it means |
|---|---|
| Your own login | Signs in with your CMT account (typed by you in the browser, or from your local `.env`). It only reads pages that account can already see. |
| No bypassing | CAPTCHAs, email verification and other checks are never automated; the agent waits for you to complete them in the browser window. |
| Uses the site's own UI | It clicks the same tabs and buttons a person would. It never calls CMT's API directly; it only reads the data the page itself loaded. |
| Polite pacing | A random 2–5 s pause before every action that makes CMT do work (configurable). One browser tab, no parallel requests. |
| robots.txt | Checked before the first page is opened; a disallow rule stops the run. (CMT currently doesn't publish one.) |
| Audit trail | Every page opened and button pressed is logged to `logs/audit-YYYYMMDD.jsonl`. Passwords and cookies are never logged. |
| Data minimisation | Only conference-level metadata is collected (names, dates, places, links), no personal data. |

You remain responsible for following the [Microsoft Terms of Use](https://www.microsoft.com/en-us/legal/terms-of-use) and for how you use the data.

## Setup (Windows, macOS or Linux)

Requires Python 3.11+.

```powershell
cd cmt-conference-agent
pip install -r requirements.txt
python -m playwright install chromium
```

Optional: to let the agent sign in for you, copy `.env.example` to `.env` and fill in `CMT_EMAIL` and `CMT_PASSWORD`. Without them, a browser window opens and you sign in yourself.

## Usage

**1. Run everything with one command** (from the project folder):

```powershell
python main.py
```

It starts the browser (the window can take 10–20 s to appear), logs in with your `.env` details or the saved session, loads every conference in *All Conferences*, saves the files and leaves the browser open. `python main.py` is short for `python main.py extract`; put options straight after it:

```powershell
python main.py --upcoming               # only conferences ending today or later (fast: stops paging early)
python main.py --search "vision"        # same as typing in CMT's "type to filter..." box
python main.py --max-pages 5            # first page + 5 x "Show More" (newest end dates first)
python main.py --tab my                 # conferences you are involved in ("My Conferences")
python main.py --tab both --headless    # both tabs, no browser window
```

**Pick a timeline** with `--from` and `--to`. They select by each conference's **start date**, and the results come out soonest first:

```powershell
python main.py --from 2027-01 --to 2027-03      # starting January-March 2027
python main.py --from today --to 2026-12-31     # starting between today and 31 December 2026
python main.py --from 2027                      # starting in 2027 or later
python main.py --to 2025                        # started in 2025 or earlier
```

Dates can be `YYYY`, `YYYY-MM`, `YYYY-MM-DD` or `today`; a year or month in `--to` counts up to its last day. Future timelines load only as many pages as needed; past ones load the full list once (about 2 minutes) and filter it. The file names include the dates, e.g. `cmt_conferences_all_2027-01-01_to_2027-03-31_<time>.docx`.

**2. Sign in only** (optional; step 1 logs in by itself). Saves the session to `.auth/cmt_state.json` for later runs:

```powershell
python main.py login
```

When a run finishes, the CSV, JSON and Word files are already saved and the browser **stays open** on CMT so you can look around. Close the window (or press Ctrl+C in the terminal) to end the run, or add `--close` to have it close by itself.

| Option | Meaning |
|---|---|
| `--tab all\|my\|both` | Which CMT tab to read (default `all`). |
| `-s, --search TEXT` | Filter text; CMT matches it against name, city, country and short name. |
| `--upcoming` | Keep conferences that end today or later, soonest first. CMT lists newest end dates first, so paging stops once it is past today. |
| `--from DATE`, `--to DATE` | Keep conferences starting in this window, soonest first. `DATE` is `YYYY`, `YYYY-MM`, `YYYY-MM-DD` or `today`. |
| `--max-pages N` | Press *Show More* at most N times instead of *Show All*. |
| `--headless` | No browser window. Needs a saved session or `.env` credentials. |
| `--close` | Close the browser when done instead of leaving it open (also for `login`). |
| `--format csv json docx` | Which files to write; any of the three (default: all three). E.g. `--format docx` for only the Word file. |
| `--out DIR` | Output folder (default `output/`). |
| `--min-delay` / `--max-delay` | Seconds between actions (default 2–5). |
| `-v` | Debug logging. |

**3. Sign out locally** (deletes the saved session file):

```powershell
python main.py logout
```

## Output

Every run automatically writes three files to `output/`, named `cmt_conferences_<tab>_<date-time>`:

| File | Best for |
|---|---|
| `.docx` | Reading and sharing: a Word document (A4 landscape) with a title, a summary line and one table: Name, Start, End, Location, CMT link, Website. Links are clickable; the header repeats on every page. |
| `.csv` | Excel and filtering: every field below, one row per conference. |
| `.json` | Programs: the same fields plus a `meta` block (source, time, options, robots.txt status). |

The full list (~10,000 conferences) gives a Word document of several hundred pages; writing it takes a few seconds.

Fields in the CSV and JSON:

| Column | Example (illustrative) |
|---|---|
| `name` | International Conference on Example Systems |
| `short_name` | `ICES2027` |
| `cmt_url` | `https://cmt3.research.microsoft.com/ICES2027` |
| `external_url` | the conference's own website, as entered by its chairs (a missing `http://` is added) |
| `start_date`, `end_date` | `2027-05-10` |
| `location`, `city`, `country` | `Lisbon, Portugal` |
| `contact_url` | CMT's "Email Chairs" form (My Conferences only) |
| `lists` | `all`, `my` or `my+all` |

The CSV is UTF-8 with BOM so Excel opens it correctly. Cells that start with `=`, `+`, `-` or `@` are prefixed with `'` so a malicious conference name can't run as a spreadsheet formula.

## How it works

1. **Robots check.** Reads `robots.txt` with the browser's own session before opening any page.
2. **Session.** Opens `/Conference/Recent`. If CMT redirects to `/User/Login`, it signs in with `.env` credentials or waits for you to sign in, then saves the session.
3. **Conference List.** Clicks *All Conferences* (and/or stays on *My Conferences*), types the search text slowly into *type to filter...* (as CMT's help recommends), then presses *Show All* or *Show More*.
4. **Two views of the same data.** It reads the rendered table (`Name | Start Date | Location | External URL`) and also the JSON the page downloads from `/api/odata/Conferences` while it clicks. The table is what you see; the JSON adds end dates, city/country and the exact short name. If either source is missing, the other one still produces results.
5. **Export.** Writes the CSV, JSON and Word files, prints a short summary, then leaves the browser open until you close it (unless `--close` or `--headless`).

## Troubleshooting

| Problem | What to do |
|---|---|
| Nothing seems to happen for a while | Starting Playwright and Chromium can take 10–20 s before the window appears. |
| The terminal waits after "Done." | It's keeping the browser open for you. Close the window or press Ctrl+C; use `--close` to skip this. |
| "Login needed but the browser is headless" | Run `python main.py login` without `--headless` once, or check the `.env` credentials. |
| Login fails with your credentials | CMT may want email verification or a password reset. Run `python main.py login` and finish it in the window. |
| "The CMT conference list did not appear" | CMT's page may have changed. A screenshot and the HTML are saved in `logs/debug/` to help adjust the selectors in `cmt_agent/extractor.py`. |
| "CMT reports N conferences but only M rows loaded" | Same as above: the *Show More / Show All* controls likely changed. See `logs/debug/`. |
| "Executable doesn't exist" | Run `python -m playwright install chromium`. |
| "No module named 'playwright'" (or `docx`) | That Python doesn't have the packages. Run `pip install -r requirements.txt` with it, or in VS Code pick the interpreter that has them (Ctrl+Shift+P → *Python: Select Interpreter*). |
| "Word file skipped" | Run `pip install python-docx`; the CSV and JSON were still written. |
| Session expired | Run `python main.py login` again. |

Keep `.env`, `.auth/` and `logs/debug/` private: they contain your credentials, an active session, or pages from your account. All three are git-ignored.

## Project layout

```
main.py                  entry point (python main.py --help)
cmt_agent/
  cli.py                 command line
  agent.py               login / extract workflows
  auth.py                sign-in, session save/reuse
  extractor.py           Conference List page: tabs, filter, paging, table + JSON capture
  models.py              Conference record and normalisation
  compliance.py          rate limiter, robots.txt, audit log
  exporters.py           CSV / JSON / Word output
  word_export.py         the Word (.docx) document
  config.py              settings from .env / environment
docs/                    CMT_Conference_Agent_Guide.pdf (the full guide)
```
