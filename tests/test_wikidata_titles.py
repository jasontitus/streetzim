"""Unit tests for cloud/wikidata_titles.py — all network mocked.

Run: python tests/test_wikidata_titles.py   (or via pytest)
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cloud import wikidata_titles as wt


def _fake_entities(mapping: dict) -> dict:
    """Build a wbgetentities-shaped response. mapping: qid -> title|None."""
    entities = {}
    for q, title in mapping.items():
        entities[q] = {"sitelinks": {"enwiki": {"title": title}} if title else {}}
    return {"entities": entities}


@contextmanager
def _resp(payload: dict):
    yield io.BytesIO(json.dumps(payload).encode("utf-8"))


class ResolveQidsTests(unittest.TestCase):
    def test_returns_only_titles_with_sitelinks(self):
        payload = _fake_entities({"Q42": "Douglas Adams", "Q999": None})
        with mock.patch.object(wt.urllib.request, "urlopen",
                               return_value=_resp(payload)):
            out = wt.resolve_qids(["Q42", "Q999"], sleep=0)
        self.assertEqual(out, {"Q42": "Douglas Adams"})

    def test_batches_over_50(self):
        qids = [f"Q{i}" for i in range(1, 121)]  # 120 -> 3 batches
        calls = []

        def fake_urlopen(req, timeout=30):
            # Parse the ids out of the query string to size each batch.
            ids = req.full_url.split("ids=")[1].split("&")[0]
            batch = ids.split("%7C") if "%7C" in ids else ids.split("|")
            calls.append(len(batch))
            return _resp(_fake_entities({q: f"T{q}" for q in batch}))

        with mock.patch.object(wt.urllib.request, "urlopen", fake_urlopen):
            out = wt.resolve_qids(qids, sleep=0)
        self.assertEqual(len(out), 120)
        self.assertEqual(calls, [50, 50, 20])

    def test_cache_persists_hits_and_misses(self):
        payload = _fake_entities({"Q42": "Douglas Adams", "Q999": None})
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "c.json")
            with mock.patch.object(wt.urllib.request, "urlopen",
                                   return_value=_resp(payload)) as m:
                wt.resolve_qids(["Q42", "Q999"], cache_path=cache, sleep=0)
                self.assertEqual(m.call_count, 1)
            saved = json.load(open(cache))
            self.assertEqual(saved["Q42"], "Douglas Adams")
            self.assertEqual(saved["Q999"], "")  # known-miss cached
            # Second run hits cache only — no API call.
            with mock.patch.object(wt.urllib.request, "urlopen") as m2:
                out = wt.resolve_qids(["Q42", "Q999"], cache_path=cache, sleep=0)
                m2.assert_not_called()
            self.assertEqual(out, {"Q42": "Douglas Adams"})

    def test_offline_map_dict_skips_network(self):
        with mock.patch.object(wt.urllib.request, "urlopen") as m:
            out = wt.resolve_qids(["Q42", "Q7"],
                                  offline_map={"Q42": "Douglas Adams"})
            m.assert_not_called()
        self.assertEqual(out, {"Q42": "Douglas Adams"})

    def test_offline_map_tsv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.tsv")
            open(p, "w").write("Q42\tDouglas Adams\nQbad\n\nQ7\tFoo Bar\n")
            out = wt.resolve_qids(["Q42", "Q7", "Q999"], offline_map=p)
        self.assertEqual(out, {"Q42": "Douglas Adams", "Q7": "Foo Bar"})


class AugmentTests(unittest.TestCase):
    def test_fills_wikipedia_from_wikidata_with_provenance(self):
        xref = {
            "a": {"wikidata": "Q42"},                       # resolvable
            "b": {"wikipedia": "en:Hand_Tag", "wikidata": "Q1"},  # already tagged
            "c": {"wikidata": "Q999"},                      # no enwiki article
            "d": {},                                        # nothing
        }
        payload = _fake_entities({"Q42": "Douglas Adams", "Q999": None})
        with mock.patch.object(wt.urllib.request, "urlopen",
                               return_value=_resp(payload)):
            stats = wt.augment_wiki_cross_refs(xref, log=lambda *_: None)

        # 'a' upgraded with underscored title + provenance.
        self.assertEqual(xref["a"]["wikipedia"], "en:Douglas_Adams")
        self.assertEqual(xref["a"]["wikipedia_src"], "wd")
        # 'b' (OSM-tagged) untouched — no provenance flag, original title kept.
        self.assertEqual(xref["b"]["wikipedia"], "en:Hand_Tag")
        self.assertNotIn("wikipedia_src", xref["b"])
        # 'c' (no article) and 'd' (no tag) stay unlinked.
        self.assertNotIn("wikipedia", xref["c"])
        self.assertNotIn("wikipedia", xref["d"])

        self.assertEqual(stats["distinct_qids"], 2)   # Q42, Q999 (Q1 already tagged)
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["entries_upgraded"], 1)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(wt.augment_wiki_cross_refs(None)["entries_upgraded"], 0)
        self.assertEqual(wt.augment_wiki_cross_refs({})["entries_upgraded"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
