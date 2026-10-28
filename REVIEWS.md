# Review exporter (CLI)

`reviews-main.py` collects **all** reviews of an F95zone thread — not just the first page the
desktop app shows — together with all metadata the forum exposes:

| Field | Example |
|---|---|
| review id | `370384` (permanent, from `/thread-reviews/370384/`) |
| author + member id | `ortomer` / `2927338` |
| score | `5.0` |
| text | full review body (line breaks preserved, zero-width junk stripped) |
| helpful votes | `👍 1 / 👎 0` (yes **and** no, the app only keeps yes) |
| reviewer record | `14 reviews · gives 3.4★ on average` |
| date | epoch timestamp + the forum's local ISO date |
| permalink | `https://f95zone.to/thread-reviews/370384/` |

Results are written to **Markdown** (one file per thread) and optionally **JSONL**. No database is used.

## 1. Requirements

* Python 3.11 or newer (same as the app)
* A few libraries — the scraper reuses the indexer's HTTP stack:

```bash
python -m pip install -r requirements-indexer.txt
```

`requirements-indexer.txt` also pulls FastAPI/Redis/uvicorn which the CLI does not need, if you
prefer a minimal install:

```bash
python -m pip install aiohttp aiolimiter beautifulsoup4 lxml
```

You do **not** need the GUI requirements, and you do not need to run the indexer or Redis.

## 2. F95zone cookie (required for threads with more than 20 reviews)

F95zone shows **only the first page of reviews (20) to guests**; pagination links are hidden and
`/reviews/page-2` silently redirects back to page 1. A logged in session is required for the rest.

Get the `xf_user` cookie from your browser:

1. Log into <https://f95zone.to> in a browser
2. Open devtools → Application/Storage → Cookies → `https://f95zone.to`
3. Copy the **value** of `xf_user`

Then either export it:

```bash
export COOKIE_XF_USER="<the xf_user value>"
# or a whole cookie header, which also works:
export COOKIE_XF_USER="xf_user=...; xf_tfa_trust=..."
```

or pass it per run with `--cookie`.

The cookie is only ever sent to `f95zone.to`, never to any third party, and it is not written to
disk by this tool (only your shell/env file holds it). Treat it like a password: it *is* your
session.

## 3. Usage

```bash
# by thread id
python reviews-main.py --id 44173

# by thread URL
python reviews-main.py --url https://f95zone.to/threads/f95checker-willyjl.44173/

# search by title (only threads tracked by F95zone Latest Updates)
python reviews-main.py --title "Being a DIK" --list
python reviews-main.py --title "Being a DIK" --yes

# into a notes vault, with a JSONL copy
python reviews-main.py --id 44173 -o ~/notes/f95-reviews --jsonl

# re-runs
python reviews-main.py --id 44173 --incremental     # only new/changed reviews
python reviews-main.py --id 44173 --resume          # continue an interrupted run
```

| Flag | Description |
|---|---|
| `--id`, `--url`, `--title` | where to scrape (exactly one) |
| `--list` | with `--title`: only print search hits, don't scrape |
| `-o`, `--out DIR` | output directory (default `reviews`) |
| `--sort` | `oldest` (default), `newest`, `latest`, `highest`, `lowest`, `helpful` |
| `--delay SECONDS` | pause between pages, default `1.0` (+ jitter). Don't go below ~0.5 |
| `--max-pages N` | stop after N pages |
| `--incremental` | scrape `sort=newest`, stop at the first page with nothing new |
| `--resume` | start again at the last page that was fully fetched |
| `--jsonl` | also write `<thread>.jsonl` (one JSON object per review) |
| `--cookie` | cookie header or bare `xf_user` value (default `$COOKIE_XF_USER`) |
| `--user-agent` | override the browser User-Agent |
| `-y`, `--yes` | accept the top search hit instead of failing on ambiguity |
| `-q`, `--quiet` | only warnings and errors |

Exit codes: `0` success · `1` scrape/parse error · `2` bad arguments or ambiguous title ·
`3` finished but reviews are missing (almost always a missing/expired cookie).

## 4. Output

```
reviews/
├── 44173-f95checker-willyjl.md          # the export
├── 44173-f95checker-willyjl.jsonl       # only with --jsonl
└── .reviews-state/
    └── 44173.json                       # what was already fetched (incremental/resume)
```

The state directory is what makes `--incremental`, `--resume` and edit detection work — keep it
next to the Markdown files. Deleting it only means the next run refetches everything.

Markdown format:

```markdown
---
thread_id: 44173
title: "F95Checker [WillyJL]"
url: https://f95zone.to/threads/44173/
rating: 5.00/5
reviews_total: 40
reviews_exported: 40
scraped: 2026-10-28T16:41:35Z
source: f95zone.to
---

# F95Checker [WillyJL] — reviews

- thread: https://f95zone.to/threads/44173/
- rating: 5.0/5 · 40 reviews
- scraped: 2026-10-28 16:41 UTC · 40/40 reviews · 2 pages
- order: chronological

## ★★★★★ 5.0/5 — ortomer — 2026-09-15

> 1 review · gives 5.0★ on average · 👍 1 / 👎 0 · [permalink](https://f95zone.to/thread-reviews/370384/)

Would recommend.

Makes life easier, when its otherwise very hard to follow on threads if a new version is out or not.
```

* reviews are always written **chronologically** (oldest first) so re-exports produce clean diffs,
  regardless of the `--sort` used to fetch them
* review text is escaped where it would otherwise break the document (`#`, `>`)
* if the export is incomplete, the header gains a `⚠️ Only X of Y reviews were collected` note
* line breaks inside a review become paragraphs; the text itself is unchanged
* the JSONL records contain the same fields plus a `hash` of the text, useful to spot edited reviews


## 5. Etiquette, limits and how it relates to the app

* The scraper talks to `f95zone.to` directly (reusing the indexer's session and rate limiter), it
  does **not** use `api.f95checker.dev` and does not affect the shared cache.
* 20 reviews per page: 40 reviews ≈ 2 pages ≈ 3 s, 500 reviews ≈ 25 pages ≈ 30 s,
  5000 reviews ≈ 250 pages ≈ 5 min at the default delay.
* `--delay` defaults to 1 s with jitter. F95zone rate limits (HTTP 429) are retried with backoff;
  captchas abort with a clear message instead of hammering the site.
* This is for personal archiving/analysis. The review text belongs to its authors — don't
  republish scraped corpora.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `warning: only 20 of N reviews were collected` + exit 3 | no/expired cookie, see §2 |
| `Session logged out` | cookie expired, grab a fresh `xf_user` |
| `Cloudflare captcha` | open the reviews page in a browser, solve it, copy the new `xf_user` |
| `You are being ratelimited` | wait a few minutes, increase `--delay` |
| `Thread missing` | wrong id, or the thread was deleted/moved to a private forum |
| `Failed to parse reviews page` / `PARSING FAILED` | F95zone changed the reviews markup; the parser in `common/parser.py` needs an update |
| `No F95zone threads found for '<title>'` | title search only covers Latest Updates threads, use `--id`/`--url` |
| `N threads match '<title>'` + exit 2 | pick one with `--id`, or re-run with `--yes` |

## 7. Tests

The parser and Markdown writer have offline tests with a synthetic fixture:

```bash
python -m unittest discover -s tests -t . -v
```

