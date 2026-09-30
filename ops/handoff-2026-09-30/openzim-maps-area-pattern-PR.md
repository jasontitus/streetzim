# PR for openzim/maps

**Branch:** fork openzim/maps, then `git am openzim-maps-area-pattern.patch` on top of main (707fc44).

**Title:** Fix area pattern accepting values other than planet or monaco

**Body:**

The `area` pattern in `offliner-definition.json` is `^planet|monaco$`. Alternation binds looser than the anchors, so it means `(^planet)|(monaco$)`. Both Zimfarm's backend (pydantic `pattern`) and the recipe editor (`RegExp.test`) search anywhere in the string, so values like `planetx`, `planet-monaco` or `xmonaco` pass validation today.

A recipe with such a value only fails at run time, in `_fetch_mbtiles` ("Could not find tiles.mbtiles for area …"), after the task has already downloaded fonts, Natural Earth and sprites.

Changes:
- pattern becomes `^(planet|monaco)$`
- `--area` gets `choices=["planet", "monaco"]`, so the CLI fails at startup. The default (planet when the flag is omitted) is unchanged.
- CHANGELOG entry under Unreleased / Fixed.

Verified by building the maps offliner model with Zimfarm's `build_offliner_model`:
- old pattern: accepts `planetx`, `planet-monaco` and `xmonaco`
- new pattern: accepts only `planet` and `monaco`
- same results with `new RegExp(p).test()`

If you'd prefer, the flag could instead become a `string-enum` with `choices: ["planet", "monaco"]`. That would show a dropdown in the recipe editor and keep existing recipes valid. Happy to switch.
