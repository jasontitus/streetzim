#!/usr/bin/env python3
"""Check the website's translations (web/i18n/) against the English source.

  python3 tools/check_site_i18n.py [LANG ...]     (default: every site.<lang>.json)
  python3 tools/check_site_i18n.py --dir DIR LANG (translations in DIR, the source in web/i18n)

site.<lang>.json: the same keys as site.en.json; each string keeps the
English one's {placeholders} and HTML tags (name and attributes, any order;
a {wikipedia} href counts as the same) and its VERBATIM text (the satellite
imagery's attribution); "months" is 12 non-empty names; no string is empty. regions.<lang>.json: the same map ids, each with a
non-empty title and description and no HTML. Exit 1 on any problem, each
printed as "<file>: <key>: <problem>"."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web" / "i18n"
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
TAG = re.compile(r"<\s*/?\s*[a-zA-Z!?]")
# Text a translation keeps word for word: the satellite imagery's required
# attribution (EOX's licence names its form).
VERBATIM = {"src_sat": ["https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2021)"]}


class _Markup(HTMLParser):
    """Everything in a string that is not text, as a browser parses it: tags
    with every attribute (any quoting), and comments, declarations and
    processing instructions."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: Counter = Counter()

    def handle_starttag(self, tag, attrs):
        self.out[("", tag, tuple(sorted((k, v or "") for k, v in attrs)))] += 1

    def handle_startendtag(self, tag, attrs):
        self.out[("/>", tag, tuple(sorted((k, v or "") for k, v in attrs)))] += 1

    def handle_endtag(self, tag):
        self.out[("/", tag, ())] += 1

    def handle_comment(self, data):
        self.out[("!--", "comment", ())] += 1

    def handle_decl(self, decl):
        self.out[("!", "decl", ())] += 1

    def unknown_decl(self, data):
        self.out[("!", "decl", ())] += 1

    def handle_pi(self, data):
        self.out[("?", "pi", ())] += 1


def tags(s: str) -> Counter:
    """The markup of a string (_Markup), counted. An unfinished tag at the end
    is not in it (the parser reads it as text): check_site counts the "<"s."""
    m = _Markup()
    m.feed(s)
    m.close()
    return m.out


def check_site(en: dict, tr: dict, label: str) -> list[str]:
    problems = []
    keys = {k for k in en if not k.startswith("_")}
    have = {k for k in tr if not k.startswith("_")}
    for k in sorted(keys - have):
        problems.append(f"{label}: {k}: missing")
    for k in sorted(have - keys):
        problems.append(f"{label}: {k}: not in site.en.json")
    for k in sorted(keys & have):
        e, t = en[k], tr[k]
        if k == "months":
            if not (isinstance(t, list) and len(t) == 12 and all(isinstance(m, str) and m.strip() for m in t)):
                problems.append(f"{label}: months: not 12 names")
            continue
        if not isinstance(t, str) or not t.strip():
            problems.append(f"{label}: {k}: empty")
            continue
        if sorted(PLACEHOLDER.findall(e)) != sorted(PLACEHOLDER.findall(t)):
            problems.append(f"{label}: {k}: placeholders {sorted(PLACEHOLDER.findall(t))}, "
                            f"not {sorted(PLACEHOLDER.findall(e))}")
        for v in VERBATIM.get(k, []):
            if v not in t:
                problems.append(f"{label}: {k}: must keep {v!r} word for word")
        if len(TAG.findall(t)) != sum(tags(t).values()):
            problems.append(f"{label}: {k}: a tag that does not close (it would run on into the page)")
        if tags(e) != tags(t):
            missing = tags(e) - tags(t)
            extra = tags(t) - tags(e)
            problems.append(f"{label}: {k}: HTML tags differ (missing {sorted(missing)}, extra {sorted(extra)})")
    return problems


def check_regions(en: dict, tr: dict, label: str, maps_only: bool = False) -> list[str]:
    """A maps-only language (its site.<lang>.json says "_maps_only": true)
    names only the maps it has versions of: a map it lacks is no problem."""
    problems = []
    ids = {k for k in en if not k.startswith("_")}
    have = {k for k in tr if not k.startswith("_")}
    for k in sorted(ids - have):
        if not maps_only:
            problems.append(f"{label}: {k}: missing")
    for k in sorted(have - ids):
        problems.append(f"{label}: {k}: not in regions.en.json")
    for k in sorted(ids & have):
        v = tr[k]
        if not isinstance(v, dict):
            problems.append(f"{label}: {k}: not an object")
            continue
        for f in ("title", "description"):
            s = v.get(f)
            if not isinstance(s, str) or not s.strip():
                problems.append(f"{label}: {k}.{f}: empty")
            elif TAG.search(s):
                problems.append(f"{label}: {k}.{f}: HTML in plain text")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("langs", nargs="*")
    ap.add_argument("--dir", type=Path, default=SRC)
    a = ap.parse_args(argv)
    langs = a.langs or sorted(p.name[5:-5] for p in a.dir.glob("site.*.json") if p.name != "site.en.json")
    en_site = json.loads((SRC / "site.en.json").read_text(encoding="utf-8"))
    en_regions = json.loads((SRC / "regions.en.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for lang in langs:
        try:
            maps_only = json.loads((a.dir / f"site.{lang}.json").read_text(encoding="utf-8")).get("_maps_only") is True
        except (OSError, ValueError, AttributeError):
            maps_only = False
        for kind, en, check in (("site", en_site, check_site),
                                ("regions", en_regions, lambda e, t, lab, mo=maps_only: check_regions(e, t, lab, mo))):
            p = a.dir / f"{kind}.{lang}.json"
            try:
                tr = json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:
                problems.append(f"{p.name}: missing")
                continue
            except json.JSONDecodeError as e:
                problems.append(f"{p.name}: not JSON ({e})")
                continue
            problems += check(en, tr, p.name)
    for p in problems:
        print(p)
    print(f"{len(langs)} languages: {'ok' if not problems else f'{len(problems)} problems'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
