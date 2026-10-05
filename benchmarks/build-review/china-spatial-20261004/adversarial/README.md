# Adversarial review of the China memory changes

See the [complete findings and fixes](../../../../docs/build-review.md#fresh-adversarial-pass-requested-after-the-memory-changes).

The review covered the memory-change production diff, regression tests,
benchmark tools and claims. It found and fixed MBTiles fact selection,
shared-cache mode/group/ownership and ACL handling, staging access,
benchmark output collisions and runtime attribution, and an existing
spatial-preparation retry bug. The measured spatial algorithm and cache
loader remain unchanged; historical measurement files are preserved.

- `source.json`: final reviewed production, regression-test and tool hashes.
- `validation.json`, `linux-tests.txt`, `native-tests.txt`: final validation
  and explicit skips/limitations; 2,227 Linux tests passed, 72 skipped.
- `spatial-review.json`: independent differential, mapping-lifetime, geometry
  and dense-cell checks, plus the prepared-marker failure reproduction/fix.
- `*-before-fix*`: deliberate failure reproductions, not final test failures.
- `shared-group-tests.json`, `lock-race-tests.json`: actual Linux writers and
  forced concurrent lock creation; later ownership/ACL cases are in the suite.
- `wikidata-*-tests.json`: helper safeguards, runtime limits and tiny Linux
  reproduction; these do not replace the earlier large cache benchmark.
- `measurement-code-comparison.json`: AST equality of the measured loader
  and spatial functions before/after this review.
- `monaco-*`: final Docker/libzim full-profile build and validation; 6,440
  identical content entries, only two expected Xapian differences, zimcheck Pass.

Remaining compatibility constraint: ACL-only writers that cannot preserve the
original file UID/GID fail clearly before publication. Use a common writer UID
or a writer permitted to retain ownership. Ordinary shared-group and
world-writable cache cases have separate real-user tests. Cache publication
is per file; later failures do not undo earlier completed bucket updates.

No complete China production build was rerun. The synthetic capacity result
and these correctness checks do not establish a full-build limit below 10 GB.
Unrelated local deletions, operational scripts and scratch outputs were not
modified. Nothing was committed, pushed or deployed by this review.
