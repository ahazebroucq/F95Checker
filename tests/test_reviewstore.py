import json
import pathlib
import tempfile
import unittest

from common import parser
from indexer.reviewstore import ReviewStore


def fake_item(review_id: int, timestamp: int, score: float = 5.0, message: str = "Nice game."):
    return parser.ParsedReviewItem(
        id=review_id,
        user="alice",
        user_id=42,
        permalink=f"https://f95zone.to/thread-reviews/{review_id}/",
        score=score,
        message=message,
        helpful_yes=3,
        helpful_no=1,
        author_record="12 reviews · gives 4.5★ on average",
        author_reviews=12,
        author_average=4.5,
        timestamp=timestamp,
        iso_date="",
        page=1,
    )


class ReviewStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_dedupes_and_detects_edits(self):
        store = ReviewStore.load(self.out, 1234)
        self.assertEqual(store.merge_items([fake_item(1, 1000), fake_item(2, 2000)]), (2, 0, 0))
        self.assertEqual(store.merge_items([fake_item(1, 1000), fake_item(2, 2000)]), (0, 0, 2))
        self.assertEqual(store.merge_items([fake_item(2, 2000, score=4.0)]), (0, 1, 0))
        self.assertEqual(store.collected, 2)
        self.assertEqual(store.known_ids, {1, 2})

    def test_state_roundtrip(self):
        store = ReviewStore.load(self.out, 1234)
        store.data["title"] = "Test Game"
        store.data["slug"] = "test-game"
        store.data["total"] = 2
        store.merge_items([fake_item(1, 1000)])
        store.save()

        reloaded = ReviewStore.load(self.out, 1234)
        self.assertEqual(reloaded.data["title"], "Test Game")
        self.assertEqual(reloaded.collected, 1)
        self.assertEqual(reloaded.data["reviews"]["1"]["user"], "alice")

    def test_markdown_output(self):
        store = ReviewStore.load(self.out, 1234)
        store.data.update({"title": "Test Game [v1.0]", "slug": "test-game", "total": 3, "average": 4.5})
        store.merge_items([
            fake_item(2, 2000, score=4.0, message="Second review.\n\n# not a heading"),
            fake_item(1, 1000, score=5.0, message="First review."),
        ])
        store.data["last_scraped"] = 1700000000
        store.data["last_page"] = 1
        text = store.render_markdown()

        # frontmatter
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('title: "Test Game [v1.0]"', text)
        self.assertIn("thread_id: 1234", text)
        self.assertIn("reviews_total: 3", text)
        self.assertIn("reviews_exported: 2", text)
        # header
        self.assertIn("# Test Game [v1.0] — reviews", text)
        # chronological order regardless of scrape order
        self.assertLess(text.index("First review."), text.index("Second review."))
        # headers with stars and metadata
        self.assertIn("## ★★★★★ 5.0/5 — alice — 1970-01-01", text)
        self.assertIn("## ★★★★☆ 4.0/5 — alice — 1970-01-01", text)
        self.assertIn("> 12 reviews · gives 4.5★ on average · 👍 3 / 👎 1", text)
        self.assertIn("[permalink](https://f95zone.to/thread-reviews/1/)", text)
        # review text must not break the document structure
        self.assertIn("\\# not a heading", text)
        # incomplete export warning
        self.assertIn("Only 2 of 3 reviews were collected", text)

    def test_markdown_and_jsonl_files(self):
        store = ReviewStore.load(self.out, 1234)
        store.data.update({"title": "Test Game", "slug": "test-game", "total": 1})
        store.merge_items([fake_item(1, 1000)])
        store.save()

        markdown_path = store.write_markdown()
        jsonl_path = store.write_jsonl()
        self.assertEqual(markdown_path, self.out / "1234-test-game.md")
        self.assertTrue(markdown_path.is_file())
        self.assertTrue(store.state_path.is_file())

        records = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["thread_id"], 1234)
        self.assertEqual(records[0]["id"], 1)
        self.assertEqual(records[0]["thread_title"], "Test Game")


if __name__ == "__main__":
    unittest.main()
