import pathlib
import unittest

from common import parser

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ReviewPageParserTest(unittest.TestCase):
    def test_page_metadata(self):
        page = parser.review_page(fixture("reviews_page.html"))
        self.assertIsInstance(page, parser.ParsedReviewPage)
        self.assertEqual(page.total, 2)
        self.assertEqual(page.page, 1)
        self.assertEqual(page.per_page, 2)
        self.assertAlmostEqual(page.average, 4.5)
        # Forum labels must not leak into the title
        self.assertEqual(page.title, "Test Game [v1.0.0]")
        self.assertFalse(page.guest_limited)

    def test_review_fields(self):
        page = parser.review_page(fixture("reviews_page.html"))
        review = page.items[0]
        self.assertEqual(review.id, 111)
        self.assertEqual(review.user, "alice")
        self.assertEqual(review.user_id, 42)
        self.assertEqual(review.permalink, "https://f95zone.to/thread-reviews/111/")
        self.assertAlmostEqual(review.score, 5.0)
        self.assertEqual(review.helpful_yes, 3)
        self.assertEqual(review.helpful_no, 1)
        self.assertEqual(review.timestamp, 1700000000)
        self.assertEqual(review.iso_date, "2023-11-14T22:13:20+0000")
        self.assertEqual(review.author_reviews, 12)
        self.assertAlmostEqual(review.author_average, 4.5)
        self.assertEqual(review.page, 1)
        # <br> newlines and paragraphs survive, zero width chars are stripped
        self.assertIn("First line.\nSecond line", review.message)
        self.assertIn("Second paragraph", review.message)
        self.assertIn('"quote"', review.message)
        self.assertNotIn("\u200b", review.message)

    def test_second_review(self):
        page = parser.review_page(fixture("reviews_page.html"))
        review = page.items[1]
        self.assertEqual(review.id, 222)
        self.assertEqual(review.user, "bob")
        self.assertAlmostEqual(review.score, 4.0)
        self.assertEqual((review.helpful_yes, review.helpful_no), (0, 2))
        self.assertEqual(review.author_record, "")

    def test_guest_limited(self):
        page = parser.review_page(fixture("reviews_page_guest.html"))
        self.assertTrue(page.guest_limited)
        self.assertEqual(page.total, 2)

    def test_legacy_reviews_wrapper(self):
        # The cache indexer still consumes the reduced schema
        reviews = parser.reviews(fixture("reviews_page.html"))
        self.assertIsInstance(reviews, parser.ParsedReviews)
        self.assertEqual(reviews.total, 2)
        self.assertEqual(len(reviews.items), 2)
        self.assertEqual(reviews.items[0].score, 5)
        self.assertIsInstance(reviews.items[0].score, int)
        self.assertEqual(reviews.items[0].likes, 3)
        self.assertEqual(reviews.items[1].likes, 0)

    def test_not_a_f95zone_payload(self):
        self.assertIsInstance(parser.review_page(b"<html><body>nope</body></html>"), parser.ParserError)
        self.assertIsInstance(parser.reviews(b"<html><body>nope</body></html>"), parser.ParserError)

    def test_page_without_reviews(self):
        # Thread with reviews disabled still parses, just with no items
        html = (
            b'<html><body><div class="p-body-pageContent">'
            b'<div class="tabs">Reviews (0)</div>'
            b"</div></body></html>"
        )
        page = parser.review_page(html)
        self.assertIsInstance(page, parser.ParsedReviewPage)
        self.assertEqual(page.items, [])
        self.assertEqual(page.total, 0)


if __name__ == "__main__":
    unittest.main()
