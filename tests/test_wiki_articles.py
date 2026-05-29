"""Unit tests for cloud/wiki_articles.py — network mocked.

Run: python tests/test_wiki_articles.py   (or via pytest)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cloud import wiki_articles as wa


class CleanArticleTests(unittest.TestCase):
    def test_trims_noise_keeps_prose_and_adds_attribution(self):
        # Real-ish lead: nested IPA in rt-commentedText, an infobox table, a
        # reference sup, an edit-section span, and an internal link.
        html = """
        <div class="mw-parser-output">
        <table class="infobox"><tr><td>Pop.</td><td>827,526</td></tr></table>
        <p><b>Nevada</b> (<span class="rt-commentedText nowrap">
        <span class="IPA">/nəˈvædə/</span>
        <span class="ext-phonos">ⓘ</span></span>) is a
        <a href="./State">state</a> in the Western United States.<sup class="reference">[5]</sup>
        <span class="mw-editsection">[edit]</span></p>
        <h2>History</h2><p>It became a state in 1864.</p>
        <ol class="references"><li>Some citation.</li></ol>
        </div>
        """
        out = wa.clean_article_html(html, "Nevada",
                                    "https://en.wikipedia.org/wiki/Nevada")
        self.assertIn("is a state in the Western United States", out)
        self.assertIn("It became a state in 1864", out)
        self.assertIn("<h2>History</h2>", out)
        # Noise gone:
        self.assertNotIn("827,526", out)          # infobox table
        self.assertNotIn("ⓘ", out)           # ⓘ listen glyph
        self.assertNotIn("væd", out)         # IPA
        self.assertNotIn("[edit]", out)           # edit section
        self.assertNotIn("Some citation", out)    # reference list
        body = out.split("<footer>")[0]
        self.assertNotIn("href", body)            # body links unwrapped
        self.assertNotIn("./State", body)         # the internal link is gone
        self.assertNotIn("class=", body)          # attributes stripped
        # The footer keeps its attribution links (CC BY-SA requirement).
        self.assertIn("href", out)
        # Attribution footer present (CC BY-SA).
        self.assertIn("CC BY-SA", out)
        self.assertIn("en.wikipedia.org/wiki/Nevada", out)
        self.assertTrue(out.startswith("<!DOCTYPE html>"))


class BundleTests(unittest.TestCase):
    def test_bundles_distinct_titles_at_wiki_article_path(self):
        stored = {}
        def add_item(path, title, mimetype, content):
            stored[path] = (title, mimetype, content)

        fake_html = '<div class="mw-parser-output"><p>Body text here.</p></div>'
        with mock.patch.object(wa, "_fetch_online", return_value=fake_html) as m:
            stats = wa.bundle_wiki_articles(
                ["en:Camarillo Ranch House", "en:Camarillo Ranch House",  # dup
                 "Lake Tahoe"],
                add_item, cache_dir="/tmp/ignored", sleep=0, log=lambda *_: None)

        self.assertEqual(stats["bundled"], 2)            # de-duped
        self.assertEqual(stats["failed"], 0)
        self.assertIn("wiki-article/Camarillo_Ranch_House", stored)
        self.assertIn("wiki-article/Lake_Tahoe", stored)
        title, mt, content = stored["wiki-article/Camarillo_Ranch_House"]
        self.assertEqual(title, "Camarillo Ranch House")
        self.assertEqual(mt, "text/html")
        self.assertIn(b"Body text here", content)
        self.assertEqual(m.call_count, 2)                # fetched once per distinct

    def test_missing_article_counted_not_stored(self):
        stored = {}
        with mock.patch.object(wa, "_fetch_online", return_value=None):
            stats = wa.bundle_wiki_articles(
                ["en:No Such Place"], lambda *a: stored.setdefault(a[0], a),
                cache_dir="/tmp/ignored", sleep=0, log=lambda *_: None)
        self.assertEqual(stats["bundled"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stored, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
