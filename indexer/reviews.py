"""Bulk F95zone review scraper, used by reviews-main.py (documented in REVIEWS.md).

This is intentionally separate from indexer/scraper.py: the cache indexer only keeps the
first page of reviews because that is all the desktop app displays, while this module
walks every reviews page of a thread and keeps all metadata (review id, member id,
permalink, helpful votes, reviewer record, ...).

It reuses the indexer's aiohttp session, cookie and error handling, so it only works
inside `async with f95zone.lifespan()`.
"""
import asyncio
import dataclasses
import functools
import json
import logging
import random
import re
import time
import urllib.parse

from common import parser
from indexer import f95zone

logger = logging.getLogger(__name__)

REVIEWS_URL = f95zone.HOST + "/threads/{thread}/reviews/"
REVIEWS_PAGE_URL = f95zone.HOST + "/threads/{thread}/reviews/page-{page}"
REVIEWS_SORTS = ("oldest", "newest", "latest", "highest", "lowest", "helpful")
REVIEWS_PER_PAGE = parser.reviews_per_page
MAX_RATELIMIT_RETRIES = 6
THREAD_URL_REGEX = re.compile(r"threads/(?:[^./]+\.)?(\d+)")
THREAD_SLUG_REGEX = re.compile(r"/threads/([^/]+?)\.(\d+)/")

# F95zone serves fewer captchas to browser-like clients, same as the app does (modules/api.py)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LOGIN_HINT = (
    "F95zone only shows the first page of reviews to guests. Pass a logged in cookie with "
    "--cookie or set the COOKIE_XF_USER environment variable (see REVIEWS.md)."
)
ERROR_HINTS = {
    "SESSION_LOGGED_OUT": "Your F95zone session is no longer valid, refresh the xf_user cookie. " + LOGIN_HINT,
    "CLOUDFLARE_CAPTCHA": "F95zone served a captcha. Open the reviews page in a browser, solve it, then copy the new xf_user cookie.",
    "F95ZONE_RATELIMIT": "You are being ratelimited. Wait a few minutes and/or increase --delay.",
    "F95ZONE_UNAVAILABLE": "F95zone is unreachable right now, try again in a few minutes.",
    "THREAD_MISSING": "That thread does not exist (or was deleted / moved to a private forum).",
    "PARSING_FAILED": "The reviews page structure changed, the parser needs an update.",
    "UNKNOWN_RESPONSE": "F95zone returned an unexpected response.",
    "INTERNAL_ERROR": "Internal error while scraping.",
}


class ReviewsError(Exception):
    def __init__(self, message: str, hint: str = "", details: str = "", code: int = 1):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details
        self.code = code


@dataclasses.dataclass(slots=True)
class ScrapeResult:
    thread_id: int
    url: str
    title: str
    slug: str
    sort: str
    total: int
    average: float
    pages: int
    last_page: int
    items: list[parser.ParsedReviewItem]
    disabled: bool = False
    guest_limited: bool = False


@dataclasses.dataclass(slots=True)
class SearchHit:
    thread_id: int
    title: str
    version: str
    creator: str
    rating: float
    likes: int
    category: str
    url: str


def parse_thread_id(value: str) -> int:
    value = str(value).strip()
    if value.isdigit():
        thread_id = int(value)
    elif match := THREAD_URL_REGEX.search(value):
        thread_id = int(match.group(1))
    else:
        raise ReviewsError(
            f"Could not find a thread ID in {value!r}",
            hint="Pass --id 12345 or a full https://f95zone.to/threads/<name>.<id>/ URL",
            code=2,
        )
    if not 1 <= thread_id < 1_000_000:
        raise ReviewsError(f"Invalid thread ID {thread_id}", code=2)
    return thread_id


def parse_cookie(value: str | None) -> dict[str, str]:
    """Accepts either a raw xf_user value or a whole cookie header."""
    if not value:
        return {}
    value = value.strip().strip("\"'")
    if "=" not in value:
        return {"xf_user": value}
    cookies = {}
    for part in value.split(";"):
        key, _, val = part.partition("=")
        if key.strip():
            cookies[key.strip()] = val.strip()
    return cookies


def thread_slug(real_url: str) -> str:
    if match := THREAD_SLUG_REGEX.search(real_url):
        return match.group(1)
    return ""


def page_url(thread_id: int, page: int, sort: str | None) -> str:
    if page <= 1:
        url = REVIEWS_URL.format(thread=thread_id)
    else:
        url = REVIEWS_PAGE_URL.format(thread=thread_id, page=page)
    if sort:
        url += "?sort=" + urllib.parse.quote(sort)
    return url


def _error_from_index(index_error: f95zone.IndexerError) -> ReviewsError:
    return ReviewsError(
        index_error.error_flag.replace("_", " ").capitalize(),
        hint=ERROR_HINTS.get(index_error.error_flag, ""),
        details=index_error.details,
    )


def guest_mode() -> bool:
    return "xf_user" not in f95zone.cookies


# f95zone.check_error() logs "Logged out of F95zone" for the login banner every guest page
# has, which is expected when running without a cookie, so those requests use a quiet logger
_quiet_logger = logging.getLogger(__name__ + ".guest")
_quiet_logger.propagate = False
_quiet_logger.addHandler(logging.NullHandler())


def _check_response(res: bytes) -> None:
    """Raise ReviewsError for F95zone error pages, but tolerate the guest login banner."""
    index_error = f95zone.check_error(res, _quiet_logger if guest_mode() else logger)
    if index_error is None:
        return
    if guest_mode() and index_error.error_flag == f95zone.ERROR_SESSION_LOGGED_OUT.error_flag:
        return
    raise _error_from_index(index_error)


async def _fetch(url: str, user_agent: str | None) -> tuple[bytes, str]:
    headers = {"User-Agent": user_agent} if user_agent else {}
    for attempt in range(MAX_RATELIMIT_RETRIES):
        async with f95zone.RATELIMIT:
            try:
                async with f95zone.session.get(
                    url,
                    cookies=f95zone.cookies,
                    headers=headers,
                ) as req:
                    if req.status == 429:
                        try:
                            retry_after = int(req.headers.get("Retry-After") or 0)
                        except ValueError:
                            retry_after = 0
                        sleep = retry_after or 5 * (attempt + 1)
                        logger.warning(f"Ratelimited by F95zone, sleeping {sleep} seconds")
                        await asyncio.sleep(sleep)
                        continue
                    res = await req.read()
                    return res, str(req.real_url)
            except Exception as exc:
                if index_error := f95zone.check_error(exc, logger):
                    raise _error_from_index(index_error)
                raise
    raise ReviewsError(
        "F95zone kept ratelimiting this request",
        hint=ERROR_HINTS["F95ZONE_RATELIMIT"],
    )


def served_page(real_url: str) -> int:
    """Which reviews page F95zone actually served us (1 if the URL has no page marker)."""
    if match := re.search(r"/reviews/page-(\d+)", real_url):
        return int(match.group(1))
    return 1


async def _fetch_page(
    thread_id: int,
    page: int,
    sort: str | None,
    user_agent: str | None,
    seen: set[int] | None = None,
) -> tuple[parser.ParsedReviewPage | None, str, bool]:
    """Fetch and parse one reviews page.

    Returns (page, real_url, bounced):
      page is None if reviews are disabled for the thread
      bounced is True when F95zone silently served an older page instead of the requested one,
      which happens when guests ask for anything past page 1
    """
    # A sort query on a paginated reviews URL can be dropped by F95zone's canonical redirect,
    # in which case we retry once without it instead of failing the whole scrape
    attempts = (sort, None) if (page > 1 and sort) else (sort,)
    for attempt in attempts:
        url = page_url(thread_id, page, attempt)
        res, real_url = await _fetch(url, user_agent)
        _check_response(res)
        if "/reviews" not in real_url:
            logger.info(f"Thread {thread_id} does not have reviews enabled")
            return None, "", False
        parsed = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(parser.review_page, res, page)
        )
        if isinstance(parsed, parser.ParserError):
            raise ReviewsError(
                f"Failed to parse reviews page {page}",
                hint=ERROR_HINTS["PARSING_FAILED"],
                details=parsed.message,
            )
        if page > 1 and served_page(real_url) != page:
            if seen and parsed.items and all(item.id in seen for item in parsed.items):
                # Bounced back to a page we already have, F95zone won't give us any more
                return parsed, real_url, True
            if attempt is not None:
                logger.warning(
                    f"Page {page} redirected to {real_url}, retrying without the sort query "
                    "(review order may differ, the final count tells if anything was missed)"
                )
                continue
            raise ReviewsError(
                f"F95zone refused to serve page {page} of the reviews",
                hint=LOGIN_HINT,
            )
        return parsed, real_url, False
    raise ReviewsError(f"Could not fetch page {page} of the reviews", hint=LOGIN_HINT)


async def scrape(
    thread_id: int,
    sort: str = "oldest",
    delay: float = 1.0,
    start_page: int = 1,
    max_pages: int | None = None,
    stop_at_ids: set[int] | None = None,
    known_ids: set[int] | None = None,
    user_agent: str | None = BROWSER_USER_AGENT,
    on_page=None,
) -> ScrapeResult:
    """Walk every reviews page of a thread.

    stop_at_ids: already known review ids, stop as soon as a page has nothing new
                 (only useful with sort="newest")
    known_ids: review ids already stored locally, used to detect F95zone bouncing us
               back to an earlier page
    on_page: called as on_page(page, new_items, collected, total) after every page
    """
    items: list[parser.ParsedReviewItem] = []
    seen: set[int] = set()
    guest_limited = False
    total = 0
    average = 0.0
    title = ""
    slug = ""
    pages = 0
    last_page = 0
    page = max(1, start_page)

    while True:
        # known ids are used to tell "F95zone bounced us back to page 1" from "it dropped our sort"
        known = set(known_ids or ()) | set(stop_at_ids or ()) | seen
        parsed, real_url, bounced = await _fetch_page(thread_id, page, sort, user_agent, known)
        if bounced:
            if total and len(items) < total:
                logger.warning(
                    f"F95zone served an older reviews page instead of page {page}, "
                    "stopping here (guests only get the first page)"
                )
                guest_limited = True
            else:
                logger.info(f"F95zone has no reviews past page {page}")
            break
        if parsed is None:
            return ScrapeResult(
                thread_id=thread_id,
                url=f"{f95zone.HOST}/threads/{thread_id}/",
                title=title,
                slug=slug,
                sort=sort,
                total=0,
                average=0.0,
                pages=pages,
                last_page=last_page,
                items=items,
                disabled=True,
            )

        pages += 1
        last_page = page
        slug = slug or thread_slug(real_url)
        title = title or parsed.title
        total = total or parsed.total
        average = average or parsed.average

        new_items = []
        for item in parsed.items:
            if item.id in seen:
                continue
            seen.add(item.id)
            new_items.append(item)
        items.extend(new_items)

        if on_page is not None:
            result = on_page(page, new_items, len(items), total)
            if asyncio.iscoroutine(result):
                await result

        if parsed.guest_limited and total and len(items) < total:
            # No point in hammering the remaining pages, they just redirect back here
            logger.warning(
                f"Only {len(items)} of {total} reviews are visible without logging in, stopping here"
            )
            guest_limited = True
            break
        if not parsed.items:
            break
        if total and len(items) >= total:
            break
        if stop_at_ids is not None and not new_items:
            break
        if len(parsed.items) < REVIEWS_PER_PAGE:
            break
        if max_pages is not None and pages >= max_pages:
            break

        page += 1
        if delay > 0:
            await asyncio.sleep(delay + random.uniform(0, 0.25))

    return ScrapeResult(
        thread_id=thread_id,
        url=f"{f95zone.HOST}/threads/{thread_id}/",
        title=title,
        slug=slug,
        sort=sort,
        total=total or len(items),
        average=average,
        pages=pages,
        last_page=last_page,
        items=items,
        guest_limited=guest_limited,
    )


async def search_threads(
    query: str,
    limit: int = 10,
    user_agent: str | None = BROWSER_USER_AGENT,
) -> list[SearchHit]:
    """Search F95zone Latest Updates by title, same endpoint the app uses to add games.

    Only threads tracked by Latest Updates are found, which is most games but not
    tools/mods/old threads. Use --id or --url for those.
    """
    query = f95zone.latest_updates_search_sanitize_query(query)
    if not query:
        raise ReviewsError(
            f"Nothing left of the title after sanitizing it ({query!r})",
            hint="Try a shorter, simpler title, or pass --id / --url",
        )

    hits: dict[int, SearchHit] = {}
    for category in f95zone.LATEST_UPDATES_CATEGORIES:
        url = f95zone.LATEST_UPDATES_SEARCH_URL.format(
            cmd="list",
            cat=category,
            page=1,
            search="search",
            query=urllib.parse.quote(query),
            sort="likes",
            rows=90,
            ts=int(time.time()),
        )
        res, _ = await _fetch(url, user_agent)
        _check_response(res)
        try:
            data = json.loads(res)
        except ValueError:
            raise ReviewsError("F95zone search returned invalid JSON", details=str(res)[:1000])
        if index_error := f95zone.check_error(data, logger):
            raise _error_from_index(index_error)

        for entry in data.get("msg", {}).get("data", []):
            try:
                thread_id = int(entry["thread_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if thread_id in hits:
                continue
            title = str(entry.get("title") or "")
            hits[thread_id] = SearchHit(
                thread_id=thread_id,
                title=title,
                version=str(entry.get("version") or ""),
                creator=str(entry.get("creator") or ""),
                rating=float(entry.get("rating") or 0.0),
                likes=int(entry.get("likes") or 0),
                category=category,
                url=f"{f95zone.HOST}/threads/{thread_id}/",
            )
        if len(hits) >= limit:
            break
        await asyncio.sleep(0.3)

    return list(hits.values())[:limit]

