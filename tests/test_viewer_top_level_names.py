"""The viewer's index parts (resources/viewer/src/index/*.js) are one
script once built: a top-level function or var declared in two parts is
one name, and the later declaration silently replaces the earlier (a
`var x = null` cache in one part turned another part's `function x()`
into null, and every photo search threw)."""
import collections
import re
from pathlib import Path

PARTS = Path(__file__).resolve().parent.parent / "resources" / "viewer" / "src" / "index"
DECL = re.compile(r"(?:function\s+([\w$]+)\s*\(|(?:var|let|const)\s+([\w$]+)\s*[=;,])")


def top_level_names(text):
    """Names declared at column 0 (the parts' top level)."""
    return [m.group(1) or m.group(2) for line in text.splitlines() if (m := DECL.match(line))]


def test_no_top_level_name_is_declared_twice():
    seen = collections.defaultdict(list)
    for part in sorted(PARTS.glob("*.js")):
        for name in top_level_names(part.read_text(encoding="utf-8")):
            seen[name].append(part.name)
    assert seen, "no declarations found"
    twice = {k: v for k, v in seen.items() if len(v) > 1}
    assert not twice, twice


def test_the_rule_sees_a_var_shadowing_a_function():
    assert top_level_names("function _a(x) {\n}\nvar _a = null;\n  var inner = 1;\n") == ["_a", "_a"]
