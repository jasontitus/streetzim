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
# 3.2.0: the tilemaker the world tiles were made with (master facdf93 = v3.2.0
# plus a version bump), so a region cut here (ops: the multilingual tile
# step) matches them. 3.0.0 copied a polygon's label into every tile under
# it, and did not merge or declutter points (docs/language-ready.md).
ARG TILEMAKER_REF=v3.2.0
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
# The official zimcheck (zim-tools' static build, checked against its
# SHA-256), which streetzim-langpack's gates require (docs/zimfarm-langpack.md).
# x86_64 only, like the image's CI build.
ARG ZIM_TOOLS_VERSION=3.8.0
ARG ZIM_TOOLS_SHA256=9d4cbf68675670176fe72f096364c7f4ecf09950340e769bdb81dcbb5030da00
RUN curl -fsSL --retry 3 -o /tmp/zim-tools.tgz \
        "https://download.openzim.org/release/zim-tools/zim-tools_linux-x86_64-${ZIM_TOOLS_VERSION}.tar.gz" \
    && echo "${ZIM_TOOLS_SHA256}  /tmp/zim-tools.tgz" | sha256sum -c - \
    && tar -xzf /tmp/zim-tools.tgz -C /usr/local/bin --strip-components=1 \
        "zim-tools_linux-x86_64-${ZIM_TOOLS_VERSION}/zimcheck" \
    && rm /tmp/zim-tools.tgz && zimcheck --version | head -n 1

WORKDIR /app
# Runtime dependencies only (no pytest, no internetarchive; see
# requirements-dev.txt and requirements-ops.txt).
COPY requirements.txt /app/
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.txt
# DuckDB's spatial and httpfs extensions (--overture) in the image, so a task
# does not fetch them from extensions.duckdb.org (about 100 MB) every run.
# In /opt, readable by any user: under ~/.duckdb (root's) a
# `docker run --user` build could not load them. streetzim/overture.py
# duckdb_connect() points DuckDB here through STREETZIM_DUCKDB_EXT.
ENV STREETZIM_DUCKDB_EXT=/opt/duckdb-ext
RUN mkdir -p /opt/duckdb-ext \
    && /venv/bin/python -c "import duckdb; c = duckdb.connect(); \
c.execute(\"SET extension_directory = '/opt/duckdb-ext'\"); \
c.execute('INSTALL spatial; INSTALL httpfs; LOAD spatial; LOAD httpfs')" \
    && chmod -R a+rX /opt/duckdb-ext
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
