import dataclasses
import datetime as dt
import functools
import json
import re

from lxml import etree
import bs4

from common.structs import (
    Status,
    Tag,
    Type,
)
from external import error

html = functools.partial(bs4.BeautifulSoup, features="lxml")
_html = html

@dataclasses.dataclass(slots=True)
class ParsedThread:
    name: str
    thread_version: str
    developer: str
    type: Type
    status: Status
    last_updated: int
    score: float
    votes: int
    description: str
    changelog: str
    tags: list[Tag]
    unknown_tags: list[str]
    image_url: str
    previews_urls: list[str]
    downloads: list[tuple[str, list[tuple[str, str]]]]

@dataclasses.dataclass(slots=True)
class ParsedReview:
    user: str
    score: int
    message: str
    likes: int
    timestamp: int

@dataclasses.dataclass(slots=True)
class ParsedReviews:
    total: int
    items: list[ParsedReview]


# Rich review records, only used by the bulk review scraper (reviews-main.py).
# The cache indexer keeps using ParsedReview above, which is what the client consumes.
@dataclasses.dataclass(slots=True)
class ParsedReviewItem:
    id: int
    user: str
    user_id: int
    permalink: str
    score: float
    message: str
    helpful_yes: int
    helpful_no: int
    author_record: str
    author_reviews: int
    author_average: float
    timestamp: int
    iso_date: str
    page: int


@dataclasses.dataclass(slots=True)
class ParsedReviewPage:
    total: int
    page: int
    per_page: int
    average: float
    title: str
    guest_limited: bool
    items: list[ParsedReviewItem]


reviews_per_page = 20
f95_host = "https://f95zone.to/"

# [^\S\r\n] = whitespace but not newlines
sanitize_whitespace = lambda text: re.sub(r" *(?:\r\n?|\n)", r"\n", re.sub(r"(?:[^\S\r\n]|\u200b)", " ", text))
fixed_newlines = lambda text: re.sub(r"(?: *\n){2}(?: *\n)+", r"\n\n", text).strip()
fixed_spaces = lambda text: re.sub(r" +", r" ", text).strip()
clean_text = lambda text: fixed_spaces(fixed_newlines(sanitize_whitespace(text)))


def is_text(text: str):
    def _is_text(elem: bs4.element.Tag):
        if not hasattr(elem, "text"):
            return False
        val = sanitize_whitespace(elem.text.lower())
        return val == text or val.startswith(text + ":")
    return _is_text


def is_class(name: str):
    def _is_class(elem: bs4.element.Tag):
        return hasattr(elem, "get_attribute_list") and name in elem.get_attribute_list("class")
    return _is_class


def datestamp(timestamp: int | float):
    utc_time = dt.datetime.fromtimestamp(timestamp, dt.UTC)
    utc_date = utc_time.replace(hour=12, minute=0, second=0, microsecond=0)
    return int(utc_date.timestamp())


def attachment(url: str):
    if url.startswith("https://preview.f95zone.to/"):
        url =  "https://attachments.f95zone.to/" + url.removeprefix("https://preview.f95zone.to/")
    if url.startswith("https://attachments.f95zone.to/") and "?h=" in url:
        url = url.split("?h=")[0]
    return url


class ParserError(Exception):
    def __init__(self, message: str, dump=None):
        super().__init__()
        self.message = message
        self.dump = dump


def thread(res: bytes) -> ParsedThread | ParserError:
    def game_has_prefixes(*names: list[str]):
        for name in names:
            if head.find("span", text=f"{name}"):
                return True
        return False
    def get_game_attr(*names: list[str]):
        for name in names:
            if match := re.search(r"^ *" + name + r" *(?: *\n? *:|: *\n? *) *(.*)", plain, flags=re.RegexFlag.MULTILINE | re.RegexFlag.IGNORECASE):
                return fixed_spaces(match.group(1))
        return ""
    def get_long_game_attr(*names: list[str]):
        value_regex = ""
        for name in names:
            if match := re.search(r"^ *" + name + r" *:? *\n? *:? *((?:.|\n)*)", plain, flags=re.RegexFlag.MULTILINE | re.RegexFlag.IGNORECASE):
                value_regex = re.sub(
                    r"(?:(?: *\n){7}|(?:\n *[A-Z a-z]+:(?:.|\n)+?){2}|\n *(?:DOWNLOAD|Download) *(?:\n|:))(?:.|\n)*", r"", match.group(1),
                    flags=re.RegexFlag.MULTILINE
                )
                value_regex = fixed_newlines(value_regex)
        value_html = ""
        for name in names:
            if elem := post.find(is_text(name)):
                break
        if elem:
            while children := list(getattr(elem, "children", [])):
                elem = children[0]
            while not (is_class("bbWrapper")(elem) or elem.parent.name == "article"):
                if elem.next_sibling:
                    elem = elem.next_sibling
                else:
                    elem = elem.parent
                    continue
                if elem.name == "b" or (hasattr(elem, "get") and "center" in elem.get("style", "")):
                    break
                text = sanitize_whitespace(elem.text)
                if text.strip() in (":", ""):
                    continue
                value_html += text
            value_html = fixed_newlines(value_html)
        return value_html, value_regex
    def get_game_downloads(*names: list[str]):
        for name in names:
            if elem := post.find(is_text(name)):
                break
        if not elem:
            return []
        while not is_class("link")(elem) and (children := list(getattr(elem, "children", []))):
            elem = children[0]
        downloads = []
        download_name = ""
        download_mirrors = []
        def add_downloads():
            nonlocal download_name, download_mirrors
            download_name = clean_text(download_name)
            lines = download_name.split("\n")
            download_name = clean_text(lines.pop()).strip(":")
            while lines:
                if line := clean_text(lines.pop(0)).strip(":"):
                    downloads.append((line, []))
            if download_name or download_mirrors:
                downloads.append((download_name, download_mirrors))
                download_name = ""
                download_mirrors = []
        post_tree = etree.fromstring(post.encode(), etree.HTMLParser())
        while not (is_class("bbWrapper")(elem) or elem.parent.name == "article"):
            if elem.next_sibling:
                elem = elem.next_sibling
            else:
                elem = elem.parent
                continue
            while not (is_link := is_class("link")(elem)) and (children := list(getattr(elem, "children", []))):
                elem = children[0]
            if is_link and (link_url := elem.get("href")):
                if not link_url.startswith(f95_host):
                    # Cache API is public, to prevent abuse we replace naked links with XPath
                    # expressions to allow client to automatically find the link in main post
                    link_host = link_url[:link_url.find("/", len("https://")) + 1]
                    xpath_expr = f"//{elem.name}[starts-with(@href,{link_host!r})]"
                    xpath_results = post_tree.xpath(xpath_expr)
                    for xpath_i, xpath_result in enumerate(xpath_results):
                        if xpath_result.get("href") == link_url:
                            link_url = f"{xpath_expr}[{xpath_i + 1}]"
                            break
                    else:  # Did not break so no match for some reason, just redact it
                        link_url = ""
                download_mirrors.append((clean_text(elem.text), link_url))
            else:
                if elem.name in ("img", "video"):
                    break
                text = sanitize_whitespace(elem.text)
                if not text.strip("-,*:/ "):
                    continue
                if download_mirrors:
                    add_downloads()
                download_name += text
        add_downloads()
        downloads = tuple(downloads)
        return downloads

    try:

        html = _html(res)
        head = html.find(is_class("p-body-header"))
        post = html.find(is_class("message-threadStarterPost"))
        if head is None or post is None:
            logo = html.select_one(".p-header-logo img")
            if logo and logo.attrs.get("alt", "").startswith("F95zone"):
                e = ParserError(
                    message="Thread structure missing",
                    dump=res,
                )
            else:
                e = ParserError(
                    message="Not an F95zone payload",
                    dump=res,
                )
            return e
        for spoiler in post.find_all(is_class("bbCodeSpoiler-button")):
            try:
                next(spoiler.span.span.children).replace_with(html.new_string(""))
            except Exception:
                pass
        for div in post.find_all("div"):
            div.insert_after(html.new_string("\n"))
        plain = sanitize_whitespace(post.find("article").get_text(separator="", strip=False))

        name = ""
        for elem in html.find(is_class("p-title-value")).children:
            if not is_class("p-title-prefix")(elem) and not is_class("labelLink")(elem) and not is_class("label-append")(elem):
                name += elem.text
        name = fixed_spaces(sanitize_whitespace(re.search(r"^\s*(.*?)(?:\s*\[.*?\]\s*)*$", name, re.DOTALL).group(1)).replace("\n", " "))

        thread_version = get_game_attr("version", "mod version", "game version")
        if not thread_version:
            if match := re.search(r"(?:\[.+?\] - )*.+?\[(.+?)\]", html.title.text):
                thread_version = fixed_spaces(sanitize_whitespace(match.group(1)))

        developer = get_game_attr(
            "developer/publisher",
            "developer & publisher",
            "developer / publisher",
            "original developer",
            "developers",
            "developer",
            "publisher",
            "artist",
            "animator",
            "producer",
            "modder",
            "remake by",
            "game by",
            "posted by"
        )
        for separator in developer_chop_separators:
            developer = developer.split(separator)[0]
        while True:
            prev_developer = developer
            developer = re.sub(r"(" + r"|".join(developer_remove_patterns) + r")", r"", developer, flags=re.IGNORECASE)
            if not developer:
                developer = prev_developer
                break
            if developer == prev_developer:
                break
        developer = fixed_spaces(developer.strip(developer_strip_chars))

        # Content Types
        if game_has_prefixes("Cheat Mod"):
            type = Type.Cheat_Mod
        elif game_has_prefixes("Mod"):
            type = Type.Mod
        elif game_has_prefixes("Tool"):
            type = Type.Tool
        # Post Types
        elif game_has_prefixes("READ ME"):
            type = Type.READ_ME
        elif game_has_prefixes("Request"):
            type = Type.Request
        elif game_has_prefixes("Tutorial"):
            type = Type.Tutorial
        # Animation Types
        elif game_has_prefixes("Video"):
            type = Type.Video
        elif game_has_prefixes("GIF"):
            type = Type.GIF
        # Comic Types
        elif game_has_prefixes("CG"):
            type = Type.CG
        elif game_has_prefixes("Manga"):
            type = Type.Manga
        elif game_has_prefixes("Comics"):
            type = Type.Comics
        elif game_has_prefixes("Pinup"):
            type = Type.Pinup
        # Game Engines
        elif game_has_prefixes("ADRIFT"):
            type = Type.ADRIFT
        elif game_has_prefixes("Flash"):
            type = Type.Flash
        elif game_has_prefixes("Godot"):
            type = Type.Godot
        elif game_has_prefixes("HTML"):
            type = Type.HTML
        elif game_has_prefixes("Java"):
            type = Type.Java
        elif game_has_prefixes("Others"):
            type = Type.Others
        elif game_has_prefixes("QSP"):
            type = Type.QSP
        elif game_has_prefixes("RAGS"):
            type = Type.RAGS
        elif game_has_prefixes("RPGM"):
            type = Type.RPGM
        elif game_has_prefixes("Ren'Py"):
            type = Type.RenPy
        elif game_has_prefixes("Tads"):
            type = Type.Tads
        elif game_has_prefixes("Unity"):
            type = Type.Unity
        elif game_has_prefixes("Unreal Engine"):
            type = Type.Unreal_Eng
        elif game_has_prefixes("WebGL"):
            type = Type.WebGL
        elif game_has_prefixes("Wolf RPG"):
            type = Type.Wolf_RPG
        else:
            type = Type.Misc

        if game_has_prefixes("Completed"):
            status = Status.Completed
        elif game_has_prefixes("Onhold"):
            status = Status.OnHold
        elif game_has_prefixes("Abandoned"):
            status = Status.Abandoned
        else:
            status = Status.Normal

        last_updated = 0
        text = get_game_attr("thread updated", "updated", "release date").replace("/", "-")
        try:
            last_updated = dt.datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
        if not last_updated:
            try:
                if elem := post.find(is_class("message-lastEdit")):
                    last_updated = int(elem.find("time").get("data-timestamp"))
                else:
                    last_updated = int(post.find(is_class("message-attribution-main")).find("time").get("data-timestamp"))
            except Exception:
                pass
        last_updated = datestamp(last_updated)

        score = None
        votes = None
        for ldjson in html.find_all("script", type="application/ld+json"):
            try:
                schema = json.loads(ldjson.get_text())
                if schema["@context"] != "http://schema.org/" or "aggregateRating" not in schema:
                    continue
                score = float(schema["aggregateRating"]["ratingValue"])
                votes = int(schema["aggregateRating"]["ratingCount"])
                break
            except Exception:
                pass

        if score is None:
            score = 0.0

        if votes is None:
            votes = 0
            if elem := html.find(is_class("tabs")):
                if match := re.search(r"reviews\s*\(([\d,]+)\)", elem.get_text(), re.M | re.I):
                    try:
                        votes = int(match.group(1).replace(",", ""))
                    except Exception:
                        pass

        description_html, description_regex = get_long_game_attr("overview", "story")
        changelog_html, changelog_regex = get_long_game_attr("changelog", "change-log", "change log")
        if len(description_regex) > len(description_html):
            if description_html and description_regex in changelog_html + changelog_regex:
                description = description_html
            else:
                description = description_regex
        else:
            if description_regex and description_html in changelog_html + changelog_regex:
                description = description_regex
            else:
                description = description_html
        changelog = changelog_regex if len(changelog_regex) > len(changelog_html) else changelog_html

        tags = []
        unknown_tags = []
        if (taglist := head.find(is_class("js-tagList"))) is not None:
            for child in taglist.children:
                if hasattr(child, "get") and "/tags/" in (tag := child.get("href", "")):
                    tag = tag.replace("/tags/", "").strip("/")
                    if tag not in Tag._member_names_:
                        unknown_tags.append(tag)
                    else:
                        tags.append(Tag[tag])
        tags = tuple(sorted(tags, key=lambda tag: tag.name))

        elem = post.find(is_class("bbWrapper")).find(lambda elem: elem.name == "div" and "data-src" in elem.attrs)
        if elem:
            image_url = attachment(elem.get("data-src"))
        else:
            image_url = "missing"

        # Find all preview images in the starter post, prefer the full-size URL
        previews_urls = []
        for img in post.find(is_class("bbWrapper")).find_all(is_class("js-lbImage")):
            url = img.get("href", "")
            if not url.startswith("https://attachments.f95zone.to/"):
                continue
            url = attachment(url)
            # Skip duplicates
            if url != image_url and url not in previews_urls:
                previews_urls.append(url)

        downloads = get_game_downloads("downloads", "download")

    except Exception:
        e = ParserError(
            message=f"Unhandled exception: {error.text()}",
            dump=error.traceback()
        )
        return e

    ret = ParsedThread(
        name=name,
        thread_version=thread_version,
        developer=developer,
        type=type,
        status=status,
        last_updated=last_updated,
        score=score,
        votes=votes,
        description=description,
        changelog=changelog,
        tags=tags,
        unknown_tags=unknown_tags,
        image_url=image_url,
        previews_urls=previews_urls,
        downloads=downloads,
    )
    return ret


developer_strip_chars = "-–|｜\\/':,)(][}{ "

developer_chop_separators = [
    "is creating",
    "and h",
    " - ",
    " – ",
    " | ",
    " ｜ ",
    " \\ ",
    " / ",
    "'s ",
    ": ",
    ", ",
    "(",
    "[",
    "{"
]

developer_remove_patterns = [
    r"commissions?",
    r"blogspot",
    r"profile",
    r"website",
    r"trailer",
    r"weebly",
    r" blog",
    r" page",
    r" web",
    r"linktr(\.| *)?ee",
    r"instagram",
    r"facebook",
    r"youtube",
    r"discord",
    r"twitter",
    r"reddit",
    r"tumblr",
    r"buy *me *a *coffee",
    r"( |-)itch(\.io)?",
    r"subscr?ibe?star",
    r"kickstarter",
    r"tip *jar",
    r"indiegogo",
    r"gumroad",
    r"patreon",
    r"ko-?fi",
    r"fanbox",
    r"fantia",
    r"slushe",
    r"naughty *machinima",
    r"hypnopics *collective",
    r"affect3d(store)?",
    r"rule34(video)?",
    r"picarto(\.tv)?",
    r"furaffinity",
    r"newgrounds",
    r"deviantart",
    r"artstation",
    r"(porn)?3dx",
    r"redgifs?",
    r"pornhub",
    r"xvideos",
    r"pixiv",
    r"hentai-?foundry",
    r"hentaiengine",
    r"lewdpixels",
    r"waifu\.nl",
    r"bowlroll",
    r"2dmarket",
    r"ci-en",
    r"iwara",
    r"fakku",
    r" vndb",
    r" dmm",
    r"tfgames?site",
    r"f95(zone)?",
    r"gamejolt",
    r"dlsite",
    r"fenoxo",
    r"boosty",
    r"steam",
    r" enty",
    r"https?://\S*",
]


def _review_page_title(html: bs4.BeautifulSoup) -> str:
    if (elem := html.find(is_class("p-title-value"))) is None:
        return ""
    title = ""
    for child in elem.children:
        if is_class("label")(child) or is_class("label-append")(child) or is_class("p-title-prefix")(child):
            continue
        title += child.text
    return fixed_spaces(sanitize_whitespace(title))


def _review_page_total(html: bs4.BeautifulSoup) -> int:
    if (banner := html.find(is_class("f95rv-banner-count"))) is not None:
        if match := re.search(r"([\d,]+)\s+reviews?", banner.get_text(" ", strip=True), re.M | re.I):
            return int(match.group(1).replace(",", ""))
    if (tabs := html.find(is_class("tabs"))) is not None:
        if match := re.search(r"reviews\s*\(([\d,]+)\)", tabs.get_text(), re.M | re.I):
            return int(match.group(1).replace(",", ""))
    return 0


def _review_page_average(html: bs4.BeautifulSoup) -> float:
    if (elem := html.find(is_class("f95rv-stats-average"))) is not None:
        if match := re.match(r"\s*([\d.]+)", elem.get_text(" ", strip=True)):
            try:
                return float(match.group(1))
            except ValueError:
                pass
    return 0.0


def _review_message(card: bs4.element.Tag) -> str:
    body = card.find(is_class("f95rv-card-body"))
    if body is None:
        return ""
    # The actual post body is wrapped in a nested bbWrapper
    if (wrapper := body.find(is_class("bbWrapper"))) is not None:
        body = wrapper
    message = body.get_text()
    for goofy_unicode in "\u2063\u200c\u200b\u2064":
        message = message.replace(goofy_unicode, "")
    return clean_text(message)


def review_page(res: bytes, page: int = 1) -> ParsedReviewPage | ParserError:
    """Parse a full /threads/{id}/reviews/ page, keeping all metadata of that page's reviews."""
    try:
        html = _html(res)
        body = html.find(is_class("p-body-pageContent"))
        if body is None:
            logo = html.select_one(".p-header-logo img")
            if logo and logo.attrs.get("alt", "").startswith("F95zone"):
                return ParserError(message="Thread structure missing", dump=res)
            return ParserError(message="Not an F95zone payload", dump=res)
        items = []
        if (cards := body.find(is_class("f95rv-cards"))) is not None:
            items = [_review_card(card, page) for card in cards.find_all(is_class("f95rv-card"))]
        return ParsedReviewPage(
            total=_review_page_total(html),
            page=page,
            per_page=len(items),
            average=_review_page_average(html),
            title=_review_page_title(html),
            # Guests only get the first page of reviews, F95zone hides the rest
            guest_limited=body.find(is_class("f95rv-guestNote")) is not None,
            items=items,
        )
    except Exception:
        return ParserError(
            message=f"Unhandled exception: {error.text()}",
            dump=error.traceback()
        )



def _review_card(card: bs4.element.Tag, page: int) -> ParsedReviewItem:
    review_id = 0
    if (content := card.get("data-content")) and (match := re.search(r"review-(\d+)", content)):
        review_id = int(match.group(1))
    elif (anchor := card.find(is_class("u-anchorTarget"))) is not None:
        if match := re.match(r"review-(\d+)$", anchor.get("id", "")):
            review_id = int(match.group(1))

    user_id = 0
    if (member := card.find("a", attrs={"data-user-id": True})) is not None:
        try:
            user_id = int(member["data-user-id"])
        except ValueError:
            pass

    permalink = ""
    if (link := card.find("a", href=re.compile(r"^/thread-reviews/\d+/?$"))) is not None:
        permalink = f95_host + link["href"].removeprefix("/")
    elif review_id:
        permalink = f"{f95_host}thread-reviews/{review_id}/"

    score = 0.0
    try:
        if (rating := card.find(is_class("f95rv-card-rating"))) is not None:
            score = float(rating.get_text(strip=True))
        elif (stars := card.find(is_class("f95rv-stars"))) is not None:
            if match := re.search(r"([\d.]+)", stars.get("aria-label", "")):
                score = float(match.group(1))
    except ValueError:
        score = 0.0

    helpful_yes = 0
    helpful_no = 0
    for vote in card.find_all(is_class("f95rv-vote")):
        label, _, count = vote.get_text(" ", strip=True).partition("·")
        count = re.sub(r"[^\d]", "", count) or "0"
        if label.strip().lower().startswith("yes"):
            helpful_yes = int(count)
        else:
            helpful_no = int(count)

    author_record = ""
    author_reviews = 0
    author_average = 0.0
    if (record := card.find(is_class("f95rv-record"))) is not None:
        author_record = fixed_spaces(sanitize_whitespace(record.get_text(" ", strip=True)))
        if match := re.search(r"([\d,]+)\s+reviews?", author_record, re.I):
            try:
                author_reviews = int(match.group(1).replace(",", ""))
            except ValueError:
                pass
        if match := re.search(r"gives\s+([\d.]+)", author_record, re.I):
            try:
                author_average = float(match.group(1))
            except ValueError:
                pass

    timestamp = 0
    iso_date = ""
    if (elem := card.find(is_class("u-dt"))) is not None:
        try:
            timestamp = int(elem.get("data-timestamp") or 0)
        except ValueError:
            timestamp = 0
        iso_date = elem.get("datetime") or ""

    return ParsedReviewItem(
        id=review_id,
        user=card.get("data-author", ""),
        user_id=user_id,
        permalink=permalink,
        score=score,
        message=_review_message(card),
        helpful_yes=helpful_yes,
        helpful_no=helpful_no,
        author_record=author_record,
        author_reviews=author_reviews,
        author_average=author_average,
        timestamp=timestamp,
        iso_date=iso_date,
        page=page,
    )



def reviews(res: bytes) -> ParsedReviews | ParserError:
    """What the cache indexer consumes: first page of reviews, reduced to the client schema."""
    page = review_page(res, 1)
    if isinstance(page, ParserError):
        return page
    return ParsedReviews(
        total=page.total,
        items=[
            ParsedReview(
                user=item.user,
                score=int(round(item.score)),
                message=item.message,
                likes=item.helpful_yes,
                timestamp=item.timestamp,
            )
            for item in page.items
        ],
    )
