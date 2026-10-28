"""Markdown / JSONL output and incremental state for the bulk review scraper."""
import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
import typing

if typing.TYPE_CHECKING:
    from indexer import reviews

STATE_VERSION = 1
STATE_DIR = ".reviews-state"
STAR_FULL = "★"
STAR_EMPTY = "☆"
# Fields that, if changed, mean an already known review was edited or got new votes
COMPARE_FIELDS = ("hash", "score", "helpful_yes", "helpful_no")


class ReviewStore:
    """Accumulated reviews of one thread, persisted as JSON, rendered as Markdown."""

    def __init__(self, out_dir: pathlib.Path | str, thread_id: int):
        self.out_dir = pathlib.Path(out_dir)
        self.thread_id = thread_id
        self.state_path = self.out_dir / STATE_DIR / f"{thread_id}.json"
        self.data = {
            "version": STATE_VERSION,
            "thread_id": thread_id,
            "title": "",
            "url": f"https://f95zone.to/threads/{thread_id}/",
            "slug": "",
            "sort": "",
            "total": 0,
            "average": 0.0,
            "last_page": 0,
            "last_scraped": 0,
            "reviews": {},
        }

    @classmethod
    def load(cls, out_dir: pathlib.Path | str, thread_id: int) -> "ReviewStore":
        store = cls(out_dir, thread_id)
        if not store.state_path.is_file():
            return store
        try:
            data = json.loads(store.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return store
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return store
        data.setdefault("reviews", {})
        if not isinstance(data["reviews"], dict):
            data["reviews"] = {}
        store.data = {**store.data, **data}
        return store

    @property
    def reviews(self) -> dict[str, dict]:
        return self.data["reviews"]

    @property
    def collected(self) -> int:
        return len(self.data["reviews"])

    @property
    def known_ids(self) -> set[int]:
        return {int(key) for key in self.data["reviews"] if str(key).isdigit()}

    def merge_items(self, items: list["reviews.parser.ParsedReviewItem"]) -> tuple[int, int, int]:
        """Merge scraped reviews, returns (new, updated, unchanged)."""
        new = updated = unchanged = 0
        for item in items:
            record = dataclasses.asdict(item)
            record.pop("page", None)  # scrape metadata, not review data
            record["hash"] = hashlib.sha1(record["message"].encode("utf-8")).hexdigest()
            key = str(item.id)
            previous = self.data["reviews"].get(key)
            if previous is None:
                new += 1
            elif any(previous.get(field) != record[field] for field in COMPARE_FIELDS):
                updated += 1
            else:
                unchanged += 1
            self.data["reviews"][key] = record
        return new, updated, unchanged

    def merge_result(self, result: "reviews.ScrapeResult") -> tuple[int, int, int]:
        if result.title:
            self.data["title"] = result.title
        if result.url:
            self.data["url"] = result.url
        if result.slug:
            self.data["slug"] = result.slug
        if result.sort:
            self.data["sort"] = result.sort
        if result.total:
            self.data["total"] = result.total
        if result.average:
            self.data["average"] = result.average
        self.data["last_page"] = max(self.data["last_page"], result.last_page)
        self.data["last_scraped"] = int(time.time())
        return self.merge_items(result.items)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_path)

    def sorted_reviews(self) -> list[dict]:
        return sorted(
            self.data["reviews"].values(),
            key=lambda review: (int(review.get("timestamp") or 0), int(review.get("id") or 0)),
        )

    def markdown_filename(self) -> str:
        slug = self.data.get("slug") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", str(self.data.get("title") or "").lower()).strip("-")
        return f"{self.thread_id}-{slug}.md" if slug else f"{self.thread_id}-reviews.md"

    def markdown_path(self) -> pathlib.Path:
        return self.out_dir / self.markdown_filename()

    def jsonl_path(self) -> pathlib.Path:
        return self.markdown_path().with_suffix(".jsonl")

    def render_markdown(self) -> str:
        stored = self.sorted_reviews()
        total = int(self.data.get("total") or len(stored))
        average = float(self.data.get("average") or 0.0)
        if not average and stored:
            average = sum(float(review.get("score") or 0) for review in stored) / len(stored)
        title = self.data.get("title") or f"Thread {self.thread_id}"
        scraped = dt.datetime.fromtimestamp(int(self.data.get("last_scraped") or time.time()), dt.UTC)
        pages = int(self.data.get("last_page") or 1)
        total_text = f"{total} review{'' if total == 1 else 's'}"
        page_text = f"{pages} page{'' if pages == 1 else 's'}"

        lines = [
            "---",
            f"thread_id: {self.thread_id}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"url: {self.data.get('url')}",
            f"rating: {average:.2f}/5",
            f"reviews_total: {total}",
            f"reviews_exported: {len(stored)}",
            f"scraped: {scraped.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "source: f95zone.to",
            "---",
            "",
            f"# {title} — reviews",
            "",
            f"- thread: {self.data.get('url')}",
            f"- rating: {average:.1f}/5 · {total_text}",
            f"- scraped: {scraped.strftime('%Y-%m-%d %H:%M UTC')} · "
            f"{len(stored)}/{total} reviews · {page_text}",
            "- order: chronological",
            "",
        ]
        if len(stored) < total:
            lines += [
                f"> ⚠️ Only {len(stored)} of {total} reviews were collected, re-run with a logged in "
                "cookie (`--cookie` or `COOKIE_XF_USER`) to get the rest.",
                "",
            ]
        if not stored:
            lines += ["_No reviews found._", ""]
        for review in stored:
            lines += self.render_review(review)
        return "\n".join(lines).rstrip() + "\n"

    def render_review(self, review: dict) -> list[str]:
        score = float(review.get("score") or 0.0)
        filled = max(0, min(5, int(round(score))))
        timestamp = int(review.get("timestamp") or 0)
        if timestamp:
            date_text = dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y-%m-%d")
        else:
            date_text = str(review.get("iso_date") or "unknown date")[:10]

        record = review.get("author_record") or ""
        if not record and review.get("author_average"):
            record = f"gives {float(review['author_average']):.1f}★ on average"
        meta = []
        if record:
            meta.append(record)
        meta.append(f"👍 {int(review.get('helpful_yes') or 0)} / 👎 {int(review.get('helpful_no') or 0)}")
        if permalink := review.get("permalink"):
            meta.append(f"[permalink]({permalink})")

        lines = [
            f"## {STAR_FULL * filled}{STAR_EMPTY * (5 - filled)} {score:.1f}/5 — "
            f"{review.get('user') or 'unknown'} — {date_text}",
            "",
            "> " + " · ".join(meta),
            "",
        ]
        paragraphs = self.paragraphs(str(review.get("message") or ""))
        if not paragraphs:
            lines += ["_No text._", ""]
        for paragraph in paragraphs:
            lines += [paragraph, ""]
        return lines

    @staticmethod
    def paragraphs(message: str) -> list[str]:
        paragraphs = []
        for line in message.splitlines():
            line = line.strip()
            if not line:
                continue
            if line[0] in "#>":
                line = "\\" + line  # don't let review text break the document structure
            paragraphs.append(line)
        return paragraphs

    def write_markdown(self) -> pathlib.Path:
        path = self.markdown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_markdown(), encoding="utf-8")
        return path

    def write_jsonl(self) -> pathlib.Path:
        path = self.jsonl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            for review in self.sorted_reviews():
                record = {
                    "thread_id": self.thread_id,
                    "thread_title": self.data.get("title") or "",
                    **review,
                }
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

