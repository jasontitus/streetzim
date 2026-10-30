"""Drop the viewer's comments and indentation from the built index.html.

The viewer lives in a fixed 1 MiB slot in every published ZIM
(docs/viewer-slots.md) and roughly a third of its source is comments,
which the reader's browser only downloads and skips. compact() removes, in
the built file only (the parts keep them):

- inside inline <script> (JavaScript): whole lines that are a `//` comment,
  and `/* … */` blocks that start and end a line of their own;
- inside <style>: those `/* … */` blocks;
- in both: blank lines and leading indentation;
- in the HTML: `<!-- … -->` comments that start and end lines of their own.

Only WHOLE lines go. A comment after code, a `//` inside a string, regex
or template literal on a code line, and any line holding a tag are left as
they are. A line inside a multi-line template literal could look like a
comment line, so the result is checked rather than trusted:
tools/lint_viewer.mjs compares every inline script's token stream before
and after (espree), and tests/test_viewer_compact.py the CSS and HTML.

Kept on purpose:
- `// BEGIN name` … `// END name` blocks shared with places.html
  (search-shards, chip-shards, the i18n runtime …), verbatim: tests require
  every copy to be identical. `keep` names them (tools/build_viewer.py: the
  blocks places.html has); the others are compacted like any code, their
  BEGIN and END lines kept;
- part 000-head.html, untouched: the rollout and driver gates read marker
  phrases that sit in its comments ("Kiwix iOS safe-area shim", "aspect
  instead") back out of shipped ZIMs.
"""
from __future__ import annotations

import re

_KEEP_JS = re.compile(r"//\s*(BEGIN|END)\b")
_BEGIN = re.compile(r"//\s*BEGIN\s+([\w-]+)")
_SCRIPT_OPEN = re.compile(r"<script(\s[^>]*)?>", re.I)


def _script_is_js(attrs: str) -> bool:
    if re.search(r"\bsrc\s*=", attrs, re.I):
        return False
    m = re.search(r"""\btype\s*=\s*["']?([^"'\s>]+)""", attrs, re.I)
    return not m or m.group(1).lower() in ("text/javascript", "application/javascript", "module")


def _context_after(line: str, ctx: str) -> str:
    """The context ('html', 'js', 'css', 'raw' for a non-JS script) after a
    line that holds tags."""
    pos = 0
    while True:
        if ctx == "html":
            ms = _SCRIPT_OPEN.search(line, pos)
            mst = re.search(r"<style(\s[^>]*)?>", line[pos:], re.I)
            cands = []
            if ms:
                cands.append((ms.start(), "script", ms))
            if mst:
                cands.append((pos + mst.start(), "style", mst))
            if not cands:
                return ctx
            start, kind, m = min(cands, key=lambda c: c[0])
            if kind == "script":
                ctx = "js" if _script_is_js(m.group(1) or "") else "raw"
                pos = m.end()
            else:
                ctx = "css"
                pos = pos + mst.end() if mst else pos
        else:
            close = "</style>" if ctx == "css" else "</script>"
            i = line.lower().find(close, pos)
            if i < 0:
                return ctx
            ctx, pos = "html", i + len(close)


def _has_tag(line: str) -> bool:
    return bool(re.search(r"</?(script|style)\b", line, re.I))


def compact(text: str, keep: set[str] | frozenset[str] | None = None) -> str:
    """`keep`: the names of the BEGIN/END blocks to copy verbatim; None for
    all of them."""
    lines = text.split("\n")
    out: list[str] = []
    ctx = "html"
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        t = line.strip()
        if _has_tag(line):
            out.append(line)
            ctx = _context_after(line, ctx)
            i += 1
            continue
        if ctx == "js" and _BEGIN.match(t) and (keep is None or _BEGIN.match(t).group(1) in keep):
            # A shared block: copy it as it is, up to its END line.
            name = _BEGIN.match(t).group(1)
            end = next((j for j in range(i + 1, n) if lines[j].strip().startswith("// END " + name)), None)
            if end is not None:
                out.extend(lines[i:end + 1])
                i = end + 1
                continue
        if ctx in ("js", "css"):
            if not t:
                i += 1
                continue
            if ctx == "js" and t.startswith("//") and not _KEEP_JS.match(t):
                i += 1
                continue
            if t.startswith("/*"):
                j = _block_end(lines, i, "/*", "*/")
                if j is not None:
                    i = j + 1
                    continue
            out.append(line.lstrip())
            i += 1
            continue
        if ctx == "html" and t.startswith("<!--"):
            j = _block_end(lines, i, "<!--", "-->")
            if j is not None:
                i = j + 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _block_end(lines: list[str], i: int, opener: str, closer: str) -> int | None:
    """Index of the line that closes the comment opened at the start of
    lines[i], if the comment covers whole lines (nothing before the opener,
    nothing after the closer, no second opener); else None."""
    first = lines[i].strip()
    if first.count(opener) != 1:
        return None
    for j in range(i, len(lines)):
        t = lines[j].strip()
        k = t.find(closer, len(opener) if j == i else 0)
        if k < 0:
            continue
        if k + len(closer) != len(t) or t.count(closer) != 1:
            return None
        return j
    return None


def compact_build(parts: list[tuple[str, str]], keep: set[str] | frozenset[str] | None = None) -> str:
    """Join the parts, compacting all but 000-head.html (see above). The
    context (inside a script or not) carries across parts. `keep`: as
    compact()."""
    head = [s for name, s in parts if name.startswith("000-")]
    rest = "".join(s for name, s in parts if not name.startswith("000-"))
    # 000-head ends inside <head> outside any script/style: compact the
    # rest from an HTML context.
    return "".join(head) + compact(rest, keep)
