"""Unit tests for cloud/wiki_articles.py — network mocked.

Run: python tests/test_wiki_articles.py   (or via pytest)
"""
import re
import os
import sys
import tempfile
import urllib.error
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

    def test_transient_5xx_does_not_poison_cache(self):
        err = urllib.error.HTTPError(
            url="https://example.test",
            code=500,
            msg="server error",
            hdrs=None,
            fp=None,
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(wa.urllib.request, "urlopen",
                                   side_effect=err), \
                    mock.patch.object(wa.time, "sleep"):
                self.assertIsNone(wa._fetch_online("No_Such_Page", td, "ua"))
            self.assertEqual(os.listdir(td), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeWikiSource:
    """Stands in for _OfflineZim: article HTML plus resolvable image bytes."""
    def __init__(self, pages, images):
        self.pages, self.images = pages, images

    def html(self, title_us):
        return self.pages.get(title_us)

    def image(self, src, max_bytes=None):
        got = self.images.get(src)
        if got and max_bytes is not None and len(got[0]) > max_bytes:
            return None                       # like _OfflineZim: dirent size, no read
        return got


BIG = b"\x89PNG" + b"\0" * 300_000
PIC_A = b"RIFFWEBPVP8 " + b"\1" * 5000
PIC_B = b"RIFFWEBPVP8 " + b"\2" * 7000
ICON = b"RIFFWEBPVP8 " + b"\3" * 400

ARTICLE = (
    '<div class="mw-parser-output">'
    '<table class="infobox"><tr><td><img src="./_assets_/h/Town_Hall.jpg" width="220" alt="Town hall"></td></tr></table>'
    '<p>Intro paragraph.</p>'
    '<figure><img src="./_assets_/h/River.jpg" width="220"><figcaption>The <b>river</b> at dusk</figcaption></figure>'
    '<img src="./_assets_/h/OOjs_UI_icon_edit.svg.png" width="12">'
    '<img src="./_assets_/h/Huge_panorama.png" width="600">'
    '<p>More prose.</p></div>'
)
IMAGES = {
    "./_assets_/h/Town_Hall.jpg": (PIC_A, "image/webp"),
    "./_assets_/h/River.jpg": (PIC_B, "image/webp"),
    "./_assets_/h/OOjs_UI_icon_edit.svg.png": (ICON, "image/png"),
    "./_assets_/h/Huge_panorama.png": (BIG, "image/png"),
}


class ImageBundlingTests(unittest.TestCase):
    def _run(self, mode, titles=("Exampleville",), pages=None):
        stored = {}
        src = FakeWikiSource(pages or {t: ARTICLE for t in titles}, IMAGES)
        stats = wa.bundle_wiki_articles(
            list(titles), lambda p, t, m, c: stored.__setitem__(p, (t, m, c)),
            sleep=0, log=lambda *_: None, images=mode, image_max_kb=128, source=src)
        return stored, stats

    def test_none_stores_no_images(self):
        stored, stats = self._run("none")
        self.assertEqual([p for p in stored if p.startswith("wiki-image/")], [])
        self.assertEqual(stats["images"], 0)
        self.assertNotIn(b"<img", stored["wiki-article/Exampleville"][2])

    def test_lead_stores_the_infobox_picture_after_the_title(self):
        stored, stats = self._run("lead")
        imgs = [p for p in stored if p.startswith("wiki-image/")]
        self.assertEqual(len(imgs), 1)
        self.assertEqual(stats["images"], 1)
        _, mt, data = stored[imgs[0]]
        self.assertEqual((mt, data), ("image/webp", PIC_A))
        page = stored["wiki-article/Exampleville"][2].decode()
        self.assertRegex(page, r"<h1>Exampleville</h1>\s*<figure class=\"lead\"><img src=\"\.\./"
                               + imgs[0].replace("wiki-image/", "wiki-image/") + r"\"")
        self.assertNotIn('<section class="gallery">', page)

    def test_all_skips_icons_and_oversize_and_keeps_captions(self):
        stored, stats = self._run("all")
        imgs = sorted(p for p in stored if p.startswith("wiki-image/"))
        self.assertEqual(len(imgs), 2, imgs)          # icon (<1500 B) and 300 KB panorama dropped
        self.assertEqual(stats["images"], 2)
        page = stored["wiki-article/Exampleville"][2].decode()
        self.assertIn('<section class="gallery">', page)
        self.assertIn("<figcaption>The river at dusk</figcaption>", page)   # tags stripped
        self.assertNotIn("OOjs", page)
        self.assertNotIn("Huge_panorama", page)
        for p in imgs:
            self.assertIn(f'src="../{p}"', page)

    def test_shared_image_stored_once_across_articles(self):
        stored, stats = self._run("all", titles=("Town_A", "Town_B"),
                                  pages={"Town_A": ARTICLE, "Town_B": ARTICLE})
        self.assertEqual(stats["bundled"], 2)
        self.assertEqual(stats["images"], 2)          # not 4
        self.assertEqual(len([p for p in stored if p.startswith("wiki-image/")]), 2)

    def test_images_need_an_offline_source(self):
        stored = {}
        msgs = []
        fake_html = '<div class="mw-parser-output"><p>x</p><img src="//upload.wikimedia.org/a.jpg"></div>'
        with mock.patch.object(wa, "_fetch_online", return_value=fake_html):
            stats = wa.bundle_wiki_articles(
                ["Online_Only"], lambda p, t, m, c: stored.__setitem__(p, c),
                cache_dir="/tmp/ignored", sleep=0, log=msgs.append, images="all")
        self.assertEqual(stats["images"], 0)
        self.assertTrue(any("text only" in m for m in msgs))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            wa.bundle_wiki_articles(["X"], lambda *a: None, images="some", source=FakeWikiSource({}, {}))


class ReviewRegressionTests(unittest.TestCase):
    """Pinned from the 2026-09-05 adversarial review of image bundling."""

    def _bundle(self, titles, pages, mode="all"):
        stored = {}
        wa.bundle_wiki_articles(
            list(titles), lambda p, t, m, c: stored.__setitem__(p, (t, m, c)),
            sleep=0, log=lambda *_: None, images=mode, image_max_kb=128,
            source=FakeWikiSource(pages, IMAGES))
        return stored

    def test_slash_title_links_images_at_the_right_depth(self):
        # wiki-article/Expo_Park/USC_station is two levels deep, so
        # ../wiki-image/ would resolve to wiki-article/wiki-image/ (dangling,
        # and a failed validate gate). 108 of California's 11,613 titles.
        stored = self._bundle(["Expo Park/USC station", "Plain_Town"],
                              {"Expo_Park/USC_station": ARTICLE, "Plain_Town": ARTICLE})
        deep = stored["wiki-article/Expo_Park/USC_station"][2].decode()
        flat = stored["wiki-article/Plain_Town"][2].decode()
        self.assertIn('src="../../wiki-image/', deep)
        self.assertNotIn('src="../wiki-image/', deep)
        self.assertIn('src="../wiki-image/', flat)

    def test_captions_are_not_double_escaped(self):
        page_html = ARTICLE.replace("The <b>river</b> at dusk", "Walker &amp; Eisen &quot;Bandstand&quot;")
        stored = self._bundle(["Cap_Town"], {"Cap_Town": page_html})
        page = stored["wiki-article/Cap_Town"][2].decode()
        self.assertIn("<figcaption>Walker &amp; Eisen &quot;Bandstand&quot;</figcaption>", page)
        self.assertNotIn("&amp;amp;", page)
        self.assertNotIn("&amp;quot;", page)

    def test_disambiguation_page_is_skipped_not_bundled(self):
        dab = ('<div class="mw-parser-output"><p><b>Roma</b> or <b>ROMA</b> may refer to:</p>'
               '<ul><li>Rome</li></ul></div>')
        stored = {}
        stats = wa.bundle_wiki_articles(
            ["it:Roma", "Real_Place"], lambda p, t, m, c: stored.__setitem__(p, c),
            sleep=0, log=lambda *_: None, images="none",
            source=FakeWikiSource({"Roma": dab, "Real_Place": ARTICLE}, {}))
        self.assertNotIn("wiki-article/Roma", stored)
        self.assertIn("wiki-article/Real_Place", stored)
        self.assertEqual(stats["disambiguation_skipped"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_oversize_image_is_rejected_by_size_before_read(self):
        class CountingSource(FakeWikiSource):
            reads = 0
            def image(self, src, max_bytes=None):
                if src == "./_assets_/h/Huge_panorama.png" and max_bytes and len(BIG) > max_bytes:
                    return None                       # dirent-size check, no blob read
                CountingSource.reads += 1
                return FakeWikiSource.image(self, src)
        stored = {}
        wa.bundle_wiki_articles(["X"], lambda p, t, m, c: stored.__setitem__(p, c),
                                sleep=0, log=lambda *_: None, images="all",
                                source=CountingSource({"X": ARTICLE}, IMAGES))
        self.assertEqual(len([p for p in stored if p.startswith("wiki-image/")]), 2)
        # town hall + river read; the icon is filtered by name before any
        # lookup, and the 300 KB panorama is refused on size without a read.
        self.assertEqual(CountingSource.reads, 2)

    def test_per_article_cap(self):
        many = '<div class="mw-parser-output"><p>x</p>' + "".join(
            f'<figure><img src="./_assets_/h/p{i}.jpg" width="200"></figure>' for i in range(30)) + "</div>"
        imgs = {f"./_assets_/h/p{i}.jpg": (b"RIFFWEBPVP8 " + bytes([i]) * 3000, "image/webp") for i in range(30)}
        stored = {}
        wa.bundle_wiki_articles(["Listy"], lambda p, t, m, c: stored.__setitem__(p, c),
                                sleep=0, log=lambda *_: None, images="all",
                                max_images_per_article=5, source=FakeWikiSource({"Listy": many}, imgs))
        self.assertEqual(len([p for p in stored if p.startswith("wiki-image/")]), 5)


class EscapedMarkupTests(unittest.TestCase):
    """Escaped markup in article text used to fail the release gate.

    Some articles carry `<a href="...">` typed literally into the
    wikitext, so Kiwix serves `&lt;a href="..."&lt;/a&gt`. The unwrap pass
    only matches real tags, so the text survived, rendered as garbage, and
    zimcheck's link scanner read the bare href out of it and reported a
    dangling internal link — failing nyc-metro and new-york-state on
    2026-09-06.
    """

    def _body(self, inner):
        return wa.clean_article_html(
            f'<div class="mw-parser-output">{inner}</div>', "T",
            "https://en.wikipedia.org/wiki/T")

    def _internal_hrefs(self, page):
        return [h for h in re.findall(r'href="([^"]+)"', page)
                if "creativecommons.org" not in h and "en.wikipedia.org" not in h]

    def test_escaped_anchor_leaves_no_scannable_href(self):
        page = self._body('<p>Moving &lt;a href="yzppassaic.org"&lt;/a&gt) soon.</p>')
        self.assertEqual(self._internal_hrefs(page), [])
        self.assertIn("Moving", page)

    def test_escaped_img_leaves_no_scannable_src(self):
        page = self._body('<p>See &lt;img src="Foo.jpg"&gt; here.</p>')
        self.assertNotIn('src="Foo.jpg"', page)
        self.assertIn("here", page)

    def test_plain_prose_with_angle_brackets_survives(self):
        page = self._body('<p>If a &lt; b and x &lt;= y then f(a) &lt; f(b).</p>')
        self.assertIn("a &lt; b", page)
        self.assertIn("&lt;= y", page)
