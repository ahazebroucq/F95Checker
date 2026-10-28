#!/usr/bin/env python3
"""Export every review of an F95zone thread to Markdown (and optionally JSONL).

Setup, configuration and troubleshooting are documented in REVIEWS.md.
"""
import argparse
import asyncio
import logging
import os
import sys
import time

from common import meta
from indexer import f95zone, reviews
from indexer.reviewstore import ReviewStore

DEFAULT_OUT_DIR = "reviews"

EPILOG = """\
examples:
  reviews-main.py --id 44173
  reviews-main.py --url https://f95zone.to/threads/f95checker-willyjl.44173/
  reviews-main.py --title "Being a DIK" --list
  reviews-main.py --title "Being a DIK" --yes -o ~/notes/reviews
  reviews-main.py --id 44173 --incremental
  reviews-main.py --id 44173 --jsonl

notes:
  F95zone only shows the first page of reviews to guests, so a login cookie is required
  for threads with more than 20 reviews. Either pass --cookie or set COOKIE_XF_USER.

  Output is <out>/<thread-id>-<slug>.md plus <out>/.reviews-state/<thread-id>.json, which
  remembers what was already fetched (used by --incremental and --resume).

exit codes:
  0  success
  1  scrape/parse error
  2  bad arguments or ambiguous title
  3  export finished but reviews are missing (usually a missing/expired cookie)
"""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reviews-main.py",
        description="Export all F95zone reviews of a thread to Markdown (and optionally JSONL).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", metavar="THREAD_ID", help="F95zone thread ID (e.g. 44173)")
    target.add_argument("--url", metavar="URL", help="F95zone thread URL")
    target.add_argument("--title", metavar="TITLE", help='Game title to search for (e.g. "Being a DIK")')
    parser.add_argument("--list", action="store_true", help="only list search results for --title, don't scrape")
    parser.add_argument("-o", "--out", metavar="DIR", default=DEFAULT_OUT_DIR,
                        help=f"output directory (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--sort", choices=reviews.REVIEWS_SORTS, default=None,
                        help="order to scrape the reviews in (default: oldest, newest with --incremental)")
    parser.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                        help="pause between pages (default: 1.0)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages")
    parser.add_argument("--incremental", action="store_true",
                        help="only fetch reviews newer than the ones already saved")
    parser.add_argument("--resume", action="store_true",
                        help="continue from the last fully fetched page of a previous run")
    parser.add_argument("--jsonl", action="store_true",
                        help="also write a .jsonl file with one JSON record per review")
    parser.add_argument("--cookie", default=None, metavar="COOKIE",
                        help="F95zone cookie header or just the xf_user value (default: $COOKIE_XF_USER)")
    parser.add_argument("--user-agent", default=reviews.BROWSER_USER_AGENT, metavar="UA",
                        help="HTTP User-Agent to use")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="accept the best search result instead of failing on multiple matches")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    parser.add_argument("--version", action="version", version=f"F95Checker {meta.version_name}")
    return parser


def print_hits(hits: list[reviews.SearchHit]) -> None:
    print(f"{'id':>7}  {'rating':>6}  {'likes':>6}  title", file=sys.stdout)
    for hit in hits:
        version = f"  ({hit.version})" if hit.version else ""
        print(
            f"{hit.thread_id:>7}  {hit.rating:>6.2f}  {hit.likes:>6}  {hit.title}{version}",
            file=sys.stdout,
        )
    print(f"\n{len(hits)} result(s)", file=sys.stdout)
    sys.stdout.flush()


def pick_hit(hits: list[reviews.SearchHit], title: str, assume_yes: bool) -> int:
    exact = [hit for hit in hits if hit.title.strip().lower() == title.strip().lower()]
    if len(exact) == 1:
        return exact[0].thread_id
    if len(hits) == 1:
        return hits[0].thread_id
    if assume_yes:
        return hits[0].thread_id
    print_hits(hits)
    raise reviews.ReviewsError(
        f"{len(hits)} threads match {title!r}, pick one with --id",
        hint="Re-run with --id <thread id>, or with --yes to accept the first (most liked) match",
        code=2,
    )


async def run(args: argparse.Namespace) -> int:
    thread_id = reviews.parse_thread_id(args.id or args.url) if (args.id or args.url) else None
    if args.list and thread_id is not None:
        raise reviews.ReviewsError("--list only works together with --title")

    async with f95zone.lifespan():
        cookies = reviews.parse_cookie(args.cookie or os.environ.get("COOKIE_XF_USER"))
        f95zone.cookies = cookies
        if not cookies and not args.list:
            log("note: no F95zone cookie given, F95zone only shows the first page of reviews to guests")

        if thread_id is None:
            log(f"searching F95zone for {args.title!r}...")
            hits = await reviews.search_threads(args.title)
            if not hits:
                raise reviews.ReviewsError(
                    f"No F95zone threads found for {args.title!r}",
                    hint="Only threads tracked by F95zone Latest Updates are searchable, "
                         "use --id or --url for anything else",
                )
            if args.list:
                print_hits(hits)
                return 0
            thread_id = pick_hit(hits, args.title, args.yes)

        store = ReviewStore.load(args.out, thread_id)
        sort = args.sort or ("newest" if args.incremental else "oldest")
        start_page = 1
        if args.resume and store.data["last_page"] and not args.incremental:
            start_page = int(store.data["last_page"]) + 1
        stop_at_ids = store.known_ids if args.incremental else None
        known_ids = store.known_ids

        previous = {
            key: tuple(review.get(field) for field in ("hash", "score", "helpful_yes", "helpful_no"))
            for key, review in store.reviews.items()
        }

        def on_page(page: int, new_items: list, collected: int, total: int) -> None:
            # Save after every page so --resume works even if a long scrape gets interrupted
            new, updated, _ = store.merge_items(new_items)
            store.data["last_page"] = max(int(store.data["last_page"]), page)
            store.data["last_scraped"] = int(time.time())
            store.save()
            log(f"  page {page}: {new} new, {updated} updated ({collected}/{total or '?'} reviews)")

        try:
            result = await reviews.scrape(
                thread_id,
                sort=sort,
                delay=args.delay,
                start_page=start_page,
                max_pages=args.max_pages,
                stop_at_ids=stop_at_ids,
                known_ids=known_ids,
                user_agent=args.user_agent or None,
                on_page=on_page,
            )
        except reviews.ReviewsError as exc:
            # Keep whatever was merged so far, a later --resume/--incremental can pick up from here
            store.save()
            log(f"error: {exc.message}")
            if exc.hint:
                log(f"hint: {exc.hint}")
            if exc.details:
                log(f"details: {exc.details}")
            if store.collected:
                log(f"kept {store.collected} review(s), resume with --resume")
            return 1

        if result.disabled:
            log(f"thread {thread_id} has reviews disabled")
            return 0

        store.merge_result(result)
        store.save()

        new = sum(1 for key in store.reviews if key not in previous)
        updated = sum(
            1
            for key, review in store.reviews.items()
            if key in previous and tuple(
                review.get(field) for field in ("hash", "score", "helpful_yes", "helpful_no")
            ) != previous[key]
        )

        markdown_path = store.write_markdown()
        if args.jsonl:
            log(f"jsonl: {store.write_jsonl()}")

        total = int(store.data["total"] or store.collected)
        log(f"{store.data['title'] or f'thread {thread_id}'}")
        log(f"exported {store.collected}/{total} reviews ({new} new, {updated} updated)")
        log(f"markdown: {markdown_path}")

        if store.collected < total:
            if result.guest_limited or reviews.guest_mode():
                reason = "F95zone only serves the first page of reviews to guests"
            else:
                reason = "the scrape stopped early (--max-pages?)"
            log(f"warning: only {store.collected} of {total} reviews were collected, {reason}")
            log("         see REVIEWS.md for how to provide a login cookie")
            return 3
        return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be 1 or higher")
    if args.delay < 0:
        parser.error("--delay can't be negative")
    try:
        return asyncio.run(run(args))
    except reviews.ReviewsError as exc:
        log(f"error: {exc.message}")
        if exc.hint:
            log(f"hint: {exc.hint}")
        if exc.details:
            log(f"details: {exc.details}")
        return exc.code
    except KeyboardInterrupt:
        log("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())

