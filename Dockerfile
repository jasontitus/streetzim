# StreetZim builder image: tilemaker 3, osmium and the Python stack (libzim),
# on Python 3.14 like openZIM's scrapers, so zimscraperlib is installed and
# the `streetzim` command uses it (see streetzim/scraperlib.py).
#
#   docker build -t streetzim .
#   docker run --rm -v "$PWD/out:/output" streetzim \
#       create_osm_zim.py --area monaco --routing -o /output/osm-monaco.zim
#
# The image fetches nothing at run time except what a build needs (OSM
# extract, DEM, Wikidata). MapLibre is vendored and the font glyphs are
# fetched and checked against their pinned sha256s when the image is built
# (docs/viewer-supply-chain.md). The coastline / Natural Earth shapefiles
# (~900 MB) are not baked in; fetch them once into the output volume, which
# is also the working directory tilemaker reads them from:
#   docker run --rm -v "$PWD/out:/output" streetzim scripts/fetch-shapefiles.sh /output
# Builds from OpenFreeMap tiles (--mbtiles) need neither tilemaker nor shapefiles.

FROM debian:trixie-slim AS tilemaker
ARG TILEMAKER_REF=v3.0.0
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
        libboost-dev libboost-filesystem-dev libboost-iostreams-dev \
        libboost-program-options-dev libboost-system-dev \
        liblua5.1-0-dev libshp-dev libsqlite3-dev rapidjson-dev zlib1g-dev \
    && git clone --depth 1 --branch "$TILEMAKER_REF" https://github.com/systemed/tilemaker.git /src/tilemaker \
    && cmake -S /src/tilemaker -B /src/tilemaker/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/tilemaker/build -j"$(nproc)"

# Same Debian release as the tilemaker stage, so its shared libraries match.
FROM python:3.14-slim-trixie
LABEL org.opencontainers.image.source=https://github.com/jasontitus/streetzim
# libmagic and cairo are for zimscraperlib (file types, SVG illustrations).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl unzip osmium-tool \
        libboost-filesystem1.83.0 libboost-iostreams1.83.0 \
        libboost-program-options1.83.0 liblua5.1-0 libshp4 libsqlite3-0 \
        libmagic1t64 libcairo2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tilemaker /src/tilemaker/build/tilemaker /usr/local/bin/tilemaker

WORKDIR /app
# Runtime dependencies only (no pytest, no internetarchive; see
# requirements-dev.txt and requirements-ops.txt).
COPY requirements.txt /app/
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.txt
# DuckDB's spatial and httpfs extensions (--overture) in the image, so a task
# does not fetch them from extensions.duckdb.org (about 100 MB) every run.
RUN /venv/bin/python -c "import duckdb; duckdb.connect().execute('INSTALL spatial; INSTALL httpfs')"
ENV PATH=/venv/bin:$PATH

COPY . /app
# The `streetzim` command (openZIM-style flags; see offliner-definition.json).
RUN /venv/bin/pip install --no-cache-dir --no-deps -e /app && mkdir -p /output
# The pinned glyph ranges, verified, where the builder looks after its cache.
RUN python tools/pin_viewer_assets.py --check \
    && python tools/pin_viewer_assets.py --prefetch /app/viewer-assets
WORKDIR /output
# Download caches (satellite, DEM, Wikidata, Wikipedia) go to the mounted
# volume, so they survive `docker run --rm` and work with a non-root --user.
ENV STREETZIM_CACHE_DIR=/output/cache
ENV PATH=/app:/app/scripts:$PATH
# Unbuffered stdout/stderr: Zimfarm (like `docker logs` or a pipe) reads a
# non-TTY stdout, where Python block-buffers, so the scraper's own lines came
# out after its subprocesses' output and Zimfarm's live log lagged.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT []
CMD ["create_osm_zim.py", "--help"]
