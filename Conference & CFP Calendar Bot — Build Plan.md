# Conference & CFP Calendar Bot — Build Plan

Oct 21, 2026 · @Seth Keeton

## Overview

Build a bot that checks 12 voice, music and laryngology conference sites every week. It publishes one subscribable calendar feed (.ics) that holds each conference's dates and its call-for-presentations (CFP) deadline. It runs entirely on free services: GitHub Actions for the schedule, GitHub Pages for the feed, and the Gemini API free tier for reading the pages.

**How to use this plan:** start Claude Code in an empty folder and say: "Read PLAN.md and build this project. Ask me before anything that costs money or needs my accounts." Paste this doc (exported as Markdown) into PLAN.md first.

**Hard requirements**

- $0 to run. No paid tiers, servers or domains.
- Runs unattended once a week, with no computer of Seth's switched on.
- Output is a calendar feed Seth subscribes to once in Google Calendar, Apple Calendar or Outlook.
- The feed shows both conference dates and CFP deadlines, with the location, the submission link and a change note in each event.
- When it is unsure, or a site fails, it says so in the feed instead of failing silently.

## Architecture

&#91;embedded content: weekly pipeline · 7 steps, 1 decision\]

Each Monday the workflow fetches every site and sends only changed pages to Gemini. Code checks each date against its source sentence, updates the stored records, and republishes the feed; Seth's calendar picks it up on its own.

## Sources

The sites live in `sources.yaml`, so Seth can add or remove one without touching code. Each entry has an `id`, a name, one or more start URLs, and optional hints.

| id | Conference | Start URL | Notes for the build |
| --- | --- | --- | --- |
| nats-national | NATS National Conference | [nats.org/national\_conference.html](https://www.nats.org/national_conference.html) | Biennial; CFP is often on a linked page |
| nats-calwest | NATS Cal-Western Region Conference | [calwestnats.org/reg-conference](https://www.calwestnats.org/reg-conference) | Regional; small site |
| icvt | International Congress of Voice Teachers | [nats.org/international-congress-of-voice-teachers.html](https://www.nats.org/international-congress-of-voice-teachers.html) | Every 4 years; may show nothing upcoming for long stretches |
| pava | PAVA Symposium | [pavavocology.org](https://pavavocology.org) | Start at home page; follow "Symposium" links |
| tvf | Voice Foundation Annual Symposium | [voicefoundation.org](https://www.voicefoundation.org/) | Use https; symposium and abstract pages are linked from home |
| fall-voice | Fall Voice Conference | [fallvoice.org](https://www.fallvoice.org/) | Likely JavaScript-rendered; needs the browser fallback |
| sin-city | Sin City Laryngology | [sincitylaryngology.com](https://sincitylaryngology.com/) | Medical meeting; may have abstracts rather than presentations |
| nw-voice | Northwest Voice | [northwestvoice.org](https://www.northwestvoice.org/) | Small site |
| cms-national | CMS National Conference | [music.org/events/national-conferences.html](https://www.music.org/events/national-conferences.html) | Lists several future years; capture each |
| cms-regional | CMS chapter (regional) conferences | [music.org/events/chapter-conferences.html](https://www.music.org/events/chapter-conferences.html) | One page lists many regions; one event per region |
| cms-international | CMS International Conference | Find from music.org events menu | URL not given; Claude Code should locate it and confirm with Seth |
| mla | Music Library Association Annual Meeting | [musiclibraryassoc.org/page/Meetings](https://www.musiclibraryassoc.org/page/Meetings) | Membership-platform site; may block plain requests |

**Open questions for Seth**

- CMS chapters: track every region, or only some (for example Rocky Mountain and Pacific Southwest)? Default: all, with a `regions:` filter in the config.
- NATS regions: Cal-Western only, or also Intermountain? Default: Cal-Western only, as listed.

## Data model and extraction

Gemini turns each page's text into strict JSON; code then validates it, so the model never writes the calendar directly.

**Model.** Use a Gemini Flash or Flash-Lite model that Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing) marks free of charge. Keep the model name in config (`GEMINI_MODEL`), because Google changes the free lineup often. Pro models are no longer free. About 12 to 30 calls a week is far below any free-tier daily limit.

**Privacy note.** On the free tier Google may use inputs to improve its models. That is fine here: the inputs are public web pages.

**Record per conference** (`data/state.json`, keyed by `source_id` + `edition`):

| Field | Type | Example |
| --- | --- | --- |
| source\_id | string | `tvf` |
| edition | string | `2027` or `2027-rocky-mountain` |
| name | string | Annual Symposium (illustrative) |
| start\_date, end\_date | ISO date or null | `2027-05-26` |
| location | string or null | Philadelphia, PA |
| format | in-person, virtual, hybrid, or null | in-person |
| cfp\_deadline | ISO date or null | `2027-01-15` |
| cfp\_deadline\_time | string or null | 11:59 PM ET |
| cfp\_url, info\_url | URL or null | page links |
| evidence | short quote per date | the sentence the date came from |
| confidence | high, medium, low | high |
| first\_seen, last\_changed | ISO date | set by code, not by the model |

**Prompt rules** (put in `prompts/extract.txt`):

1. Return JSON matching the schema, using Gemini's structured-output mode (`response_mime_type: application/json` with a schema).
2. Report only dates the page states. Never guess a year or infer a deadline.
3. For every date, include the short sentence it came from as evidence.
4. List every upcoming edition the page mentions. Ignore past ones.
5. Separate the presentation, poster, paper and abstract deadlines if the page gives several. Prefer the main presentation or abstract deadline.
6. Note an "extended" deadline as the new deadline, with the old one in a note.

**Validation in code** (reject or downgrade to low confidence when it fails):

- Each evidence quote must appear in the page text.
- `end_date` is on or after `start_date`, and both are within the next 4 years.
- The CFP deadline falls before the conference start.
- A date that differs from last week's stored value is a change, not a new event.

## Calendar feed

The bot writes one file, `docs/conferences.ics`, served by GitHub Pages. Seth subscribes to its URL once; the calendar app re-fetches it on its own, usually within a day. Build it with the Python `icalendar` library.

| Event | When | Title pattern | Notes |
| --- | --- | --- | --- |
| Conference | All-day, start to end date | `NATS National Conference — Location` | DTEND is the day after the last day, per the iCalendar spec |
| CFP deadline | All-day, on the deadline | `CFP due: NATS National Conference` | Time and time zone go in the description |
| Heads-up | 30 and 7 days before each CFP deadline | `CFP in 30 days: NATS National` | Lead times set in config (`reminder_days: [30, 7]`) |
| Site check | The run date, only when a site fails 3 weeks running | `Bot couldn't read: Fall Voice` | Makes failures visible in the calendar itself |

**Why separate heads-up events:** Google Calendar ignores the built-in alarms (VALARM) in subscribed feeds; Apple Calendar honors them. Real events work everywhere. Include VALARMs too, for apps that use them.

**Every event description includes:** location and format, CFP link, conference info link, the evidence sentence, confidence, last-checked date, and a change note when anything moved ("Changed Oct 21: deadline moved from Jan 15 to Feb 1").

**Rules**

- Each event has a stable UID (`<source_id>-<edition>-<type>@cfp-bot`), so a changed date updates the event instead of duplicating it. Bump SEQUENCE and LAST-MODIFIED on every change.
- Low-confidence events get `(unconfirmed)` at the start of the title.
- Events stay in the feed for 12 months after they end, then drop out.
- Also publish `docs/index.html`: a plain status page with every source, its last successful read, and the subscribe link.

## Repo, workflow and secrets

Use one **public** GitHub repo, so GitHub Pages and Actions are both free. It holds no private data; the only secret is the Gemini key, which lives in Actions secrets, not in the code.

```
cfp-bot/
  PLAN.md                  this plan
  sources.yaml             the sites (Sources section)
  prompts/extract.txt      the extraction prompt
  cfpbot/
    __main__.py            runs the whole pipeline
    fetch.py               plain fetch, browser fallback, link-following
    extract.py             Gemini call with JSON schema
    validate.py            the checks in Data model
    merge.py               compare with state, record changes
    ics.py                 build conferences.ics
    status.py              build index.html
  data/state.json          current records + change history
  data/pages/<id>.txt      last page text per source, for change detection
  docs/conferences.ics     the feed (served by Pages)
  docs/index.html          status page
  tests/fixtures/          saved copies of each site for offline tests
  requirements.txt         requests, beautifulsoup4, playwright, google-genai, icalendar, pyyaml
  .github/workflows/weekly.yml
```

**Save API calls:** if a page's text matches last week's, skip Gemini for that source and keep the stored record.

**Workflow** (`.github/workflows/weekly.yml`):

```yaml
name: weekly-check
on:
  schedule:
    - cron: "17 13 * * 1"   # Mondays 13:17 UTC (about 7 AM in Utah)
  workflow_dispatch: {}      # "Run workflow" button for manual runs
permissions:
  contents: write
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: pip install -r requirements.txt
      - run: python -m playwright install --with-deps chromium
      - run: python -m cfpbot
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}
      - name: Commit results
        run: |
          git config user.name "cfp-bot"
          git config user.email "cfp-bot@users.noreply.github.com"
          git add data docs
          git commit -m "Weekly check $(date -u +%F)" || echo "No changes"
          git push
```

Claude Code should update the action versions to the current ones when it builds this. The schedule avoids the top of the hour, when GitHub's scheduler is busiest and runs are often delayed.

## Reliability

The bot must keep producing a correct feed when one site breaks, and must say so when it can't.

**Fetching**

- Try a plain HTTP request first (browser-like user agent, 30-second timeout).
- Fall back to headless Chromium (Playwright) when the text is short or holds no date-like strings. This covers JavaScript-built sites.
- From each start page, follow up to 5 same-site links whose text or URL contains words like call, proposal, abstract, submission, present, symposium or conference.
- Wait 2 seconds between requests and respect robots.txt.
- If a site blocks GitHub's servers outright, allow a `manual:` block in `sources.yaml` where Seth can type the dates himself; the feed marks those events as manual.

**Gemini**

- Retry on HTTP 429 (rate limit) with exponential backoff, at most 3 tries, and pause about 5 seconds between calls.
- If Gemini is down, keep last week's records and note "not re-checked this week" in the descriptions.

**Failures**

- One source failing never stops the run; it is logged and shown on the status page.
- After 3 failed weeks in a row, the feed gets a "Bot couldn't read" event (Calendar feed section).
- GitHub emails the repo owner when a whole run fails; leave that notification on.

**Keeping the schedule alive.** GitHub turns off scheduled workflows in public repos after 60 days with no repository activity. The status page records the run date, so every run makes a commit. If GitHub ever turns it off anyway, it is re-enabled with one click on the Actions tab.

**Testing**

- Save one copy of each site in `tests/fixtures/` and test extraction offline against them.
- Add `python -m cfpbot --dry-run`, which prints what it found without writing any files.
- Add `--only <id>` to test one source at a time.

## Setup and acceptance

Seth does five one-time steps; Claude Code does the rest and should walk him through each one.

**Seth's steps**

1. Have a free GitHub account, and install the GitHub CLI (`gh`) so Claude Code can create the repo.
2. Get a free Gemini API key at [Google AI Studio](https://aistudio.google.com/). Don't turn on billing; that keeps it on the free tier.
3. In the repo's settings, add the key as an Actions secret named `GEMINI_API_KEY`, and add an Actions variable `GEMINI_MODEL` with the current free Flash model name.
4. Turn on GitHub Pages, serving the `docs` folder of the main branch.
5. Subscribe to `https://<github-username>.github.io/cfp-bot/conferences.ics` in Google Calendar (add a calendar from a URL) or Apple Calendar (new calendar subscription).

**Build order for Claude Code**

1. Fetching for all 12 sources, with fixtures saved.
2. Extraction and validation, tested offline on the fixtures.
3. State, change detection and the .ics feed.
4. Status page, workflow, then one manual run on GitHub.

**Done when**

- [ ] A manual run finishes in under 20 minutes with no paid service involved.
- [ ] Every source shows on the status page as read, or with a clear failure reason.
- [ ] The feed opens in Google Calendar and shows conference spans and CFP deadlines for every source that has them.
- [ ] Editing a date in a fixture and re-running updates the existing event (no duplicate) and adds a change note.
- [ ] Every date in the feed has an evidence sentence that appears on the source page.
- [ ] A second run with no site changes makes no Gemini calls.
- [ ] Seth can add a site by editing `sources.yaml` alone.
