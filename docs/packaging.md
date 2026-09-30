# Packaging, PyPI and coverage

## The wheel

`python -m build` makes `dist/streetzim-<version>.tar.gz` and
`dist/streetzim-<version>-py3-none-any.whl` (the wheel is built from the
sdist). The wheel holds everything the `streetzim` command reads at run time:

| in the wheel | from the checkout |
|---|---|
| `streetzim/`, `create_osm_zim.py`, `wikidata_cache.py`, `download_overture_data.py` | the same paths |
| `cloud/`: only the modules the builder imports (`CLOUD_MODULES` in `streetzim/paths.py`) | the same paths; the rest of `cloud/` is operations code and is left out |
| `streetzim/resources/viewer/{index.html,places.html,routing-worker.js}` | `resources/viewer/` (the built files, not `src/`) |
| `streetzim/resources/tilemaker/` (config, Lua profile, `fetch-shapefiles.sh`) | `resources/tilemaker/` |
| `streetzim/resources/vendor/` (MapLibre GL JS, RTL text plugin, their licences) | `resources/vendor/` |
| `streetzim/resources/viewer-assets.lock.json` | `resources/viewer-assets.lock.json` |
| `cloud/regions.tsv` | the same path |

`pyproject.toml` maps `resources/` to the `streetzim.resources` package and
lists these files as package data. `streetzim/paths.py` resolves
`RESOURCES_DIR` to the installed copy when there is one, else to the
checkout's `resources/` (a checkout or `pip install -e .`), and names the
files (`RUNTIME_FILES`). `streetzim` stops with an error naming any that are
missing. `tests/test_packaging.py` fails if the package data and
`RUNTIME_FILES` disagree, if a new `streetzim` subpackage is missing from
the package list, or if `CLOUD_MODULES` is not what an import scan of the
builder finds. `setup.py` applies `CLOUD_MODULES` to every build (`python -m
build`, `pip install .`, `pip wheel .`).

The wheel's dependencies are the runtime set, listed in `pyproject.toml`
(not read from `requirements.txt`, which lists the same set; test tools are
in `requirements-dev.txt`, the `ia` client in `requirements-ops.txt`);
`tests/test_packaging.py` checks the two lists agree.

Download caches (satellite, DEM, Wikidata, Wikipedia, font glyphs) go to
`$STREETZIM_CACHE_DIR`, which the `streetzim` command sets to `<--dl>/cache`.
Without it, a checkout keeps them in the repository as before, and an
installed wheel uses `$XDG_CACHE_HOME/streetzim` or `~/.cache/streetzim`,
never site-packages (`cache_root` in `streetzim/paths.py`).

A build from the wheel still needs what a build from a checkout needs outside
Python: `tilemaker` (3.x) and `osmium` on `PATH`, and the coastline and
Natural Earth shapefiles (`--shapefiles DIR`; fetched into it with `curl`
and `unzip` when missing).
The Docker image remains the complete environment. Fonts are downloaded,
checked against the lock file and cached under `--dl` on first use.

CI's `wheel` job builds both files, installs the wheel into a fresh venv,
and from `/tmp` runs `streetzim --help` and `tools/check_wheel_install.py`.
That check needs no network (the job points the proxy variables at a closed
port): it checks that the package and its files come from the venv and not
the checkout, that `cloud/` holds exactly `CLOUD_MODULES`, verifies the vendored MapLibre and RTL plugin against their
pinned hashes, and reads the tilemaker config, the font list and
`cloud/regions.tsv` as a build does. The built files are kept as the `dist`
artifact.

To try it locally:

```bash
python -m build
python -m venv /tmp/v && /tmp/v/bin/pip install dist/streetzim-*.whl
(cd /tmp && /tmp/v/bin/python "$OLDPWD/tools/check_wheel_install.py")
```

## Publishing to PyPI (not set up yet)

`.github/workflows/publish.yml` publishes with PyPI trusted publishing, so no
API token is stored. It runs only by hand, and only in
`jasontitus/streetzim`. Before the first run the maintainer:

1. On PyPI, adds a trusted publisher for the project `streetzim` (a
   "pending publisher" if the project does not exist yet): owner
   `jasontitus`, repository `streetzim`, workflow `publish.yml`,
   environment `pypi`.
2. In the GitHub repository settings, creates the environment `pypi`, with
   deployment tags restricted to `v*` (Deployment branches and tags,
   Selected, a tag rule) and a required reviewer, so each upload waits for
   approval.
3. Sets the version in `streetzim/__about__.py`, then tags it: `v1.0.0` for
   `1.0.0`.

Then Actions, "Publish to PyPI", Run workflow, with the tag. The build job
checks that the input is an existing tag and matches the version, builds and checks the wheel as CI does,
and the publish job uploads it. To publish on every pushed `v*` tag instead,
uncomment the `push: tags` trigger in the workflow.

**Blocker before any PyPI release: top-level names.** The wheel installs
`cloud` (a namespace package, with no `__init__.py`), `create_osm_zim`,
`wikidata_cache` and `download_overture_data` at the top level of
site-packages, next to `streetzim`.
Another distribution could ship the same names, and a namespace `cloud`
would merge with or be shadowed by any other `cloud` on the path. The plan:
move these under `streetzim.*` (for example `streetzim.builder`,
`streetzim.wikidata_cache`, `streetzim.cloud.*`), with small shims at the
old paths in the checkout so the build host, ops scripts and `python
create_osm_zim.py` keep working, and ship only the `streetzim` package.
Not done yet; do not publish until it is.

## Coverage

CI's `checks` job runs `tests/` and `ops/tests/` under pytest-cov
(`pytest --cov`; settings in `[tool.coverage]` in `pyproject.toml`). The
report lists the files the tests import, plus unimported modules coverage
can find inside the repository's packages (at 0%); scripts no test imports
are not listed, and code the tests run in a subprocess is not counted, so
the total (45% when this was set up) is a rough guide, not a target. The
table is in the job summary; `coverage.xml` and an HTML report are the `coverage` artifact.

Locally: `pip install -e ".[test]"`, then
`python -m pytest tests -q --cov --cov-report=term` (or `--cov-report=html`).

Codecov is optional. The upload step is skipped until a token exists; to
turn it on, sign in to codecov.io with GitHub, add the repository, and save
its upload token as the repository secret `CODECOV_TOKEN` (Settings,
Secrets and variables, Actions). The next CI run uploads `coverage.xml`; an
upload failure does not fail CI.
