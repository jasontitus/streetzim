"""The satellite imagery a ZIM can include, with each source's licence.

Stdlib only: streetzim/cli.py imports it before STREETZIM_CACHE_DIR is set.

Both sources are EOX's Sentinel-2 cloudless mosaics ("EOxCloudless"), served
from EOX's public WMTS. EOX licenses each year separately; as published on
2026-09-29 (docs/zimfarm.md, "Satellite imagery", quotes the pages):

- 2016: CC BY 4.0. The WMTS layer `s2cloudless_3857` ("Sentinel-2 cloudless
  layer for 2016 by EOX"; its abstract: "... released under Creative Commons
  Attribution 4.0 International License"), and
  https://cloudless.eox.at/license-non-commercial: "For the year 2016,
  EOxCloudless is licensed under the Creative Commons Attribution 4.0
  International License."
- 2018 to 2025: CC BY-NC-SA 4.0 ("For the years 2018 to 2025, EOxCloudless
  WM(T)S layers is licensed under the Creative Commons
  Attribution-NonCommercial-ShareAlike 4.0 International License"). Using
  them commercially needs a licence bought from EOX.

The attribution strings are the ones that page requires for each year.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SatelliteSource:
    key: str               # what --satellite-source takes
    layer: str             # EOX WMTS layer id (Web Mercator)
    year: str              # the mosaic's year, as EOX names it
    license: str           # short licence name
    license_url: str
    noncommercial: bool
    attribution: str       # the text EOX requires wherever the imagery is shown

    @property
    def tile_url(self) -> str:
        return (f"https://tiles.maps.eox.at/wmts/1.0.0/{self.layer}"
                "/default/g/{z}/{y}/{x}.jpg")

    @property
    def license_metadata(self) -> str:
        """The satellite part of the ZIM's License metadata."""
        use = ", non-commercial use only" if self.noncommercial else ""
        return f"Satellite imagery: {self.license}{use} ({self.attribution})"


_BY = "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH"

SOURCES: dict[str, SatelliteSource] = {s.key: s for s in (
    SatelliteSource(
        key="s2cloudless-2016", layer="s2cloudless_3857", year="2016",
        license="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        noncommercial=False,
        attribution=f"{_BY} (Contains modified Copernicus Sentinel data 2016 & 2017)"),
    SatelliteSource(
        key="s2cloudless-2021", layer="s2cloudless-2021_3857", year="2021",
        license="CC BY-NC-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-nc-sa/4.0/",
        noncommercial=True,
        attribution=f"{_BY} (Contains modified Copernicus Sentinel data 2021)"),
)}

# The builder's default (create_osm_zim.py --satellite) stays the 2021 mosaic
# its caches hold. `streetzim` defaults to the freely licensed one.
BUILDER_DEFAULT = "s2cloudless-2021"
OPENZIM_DEFAULT = "s2cloudless-2016"

# Flavour metadata of a `streetzim` ZIM with satellite imagery. Kiwix keys a
# book on Name + Flavour, so each variant is a separate book.
FLAVOUR_FREE = "satellite"
FLAVOUR_NONCOMMERCIAL = "satellite-nc"
TAG = "satellite"
TAG_NONCOMMERCIAL = "non-commercial"


def get(key: str) -> SatelliteSource:
    try:
        return SOURCES[key]
    except KeyError:
        raise ValueError(f"unknown satellite source {key!r}; known: "
                         + ", ".join(SOURCES)) from None


def flavour(src: SatelliteSource) -> str:
    return FLAVOUR_NONCOMMERCIAL if src.noncommercial else FLAVOUR_FREE


def tags(src: SatelliteSource) -> list[str]:
    return [TAG, TAG_NONCOMMERCIAL] if src.noncommercial else [TAG]


def restricted_note(src: SatelliteSource) -> str:
    """The sentence a restricted ZIM's LongDescription and About panel carry."""
    return (f"Restricted: the satellite imagery ({src.attribution}) is licensed "
            f"{src.license} and may be used for non-commercial purposes only; "
            "the rest of this map is openly licensed.")


def map_config(src: SatelliteSource) -> dict[str, str | bool]:
    """What the viewer needs to credit the imagery (map-config.json)."""
    return {"satelliteSource": src.key, "satelliteYear": src.year,
            "satelliteLicense": src.license, "satelliteLicenseUrl": src.license_url,
            "satelliteAttribution": src.attribution,
            "satelliteNonCommercial": src.noncommercial}
