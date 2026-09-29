"""map-config.json carries what the viewer's About panel shows (title,
description, date, the streetzim release that built it), matching the ZIM
metadata, without overriding anything the config already sets."""
import json
import re

from streetzim import zim_writer
from streetzim.__about__ import __version__


class FakeItem:
    def __init__(self, path, title, mimetype, content, is_front=False, compress=True, namespace=None):
        self.path, self.content = path, content


class FakeCreator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item


def _config(map_config, **about):
    c = FakeCreator()
    zim_writer._add_map_config(c, FakeItem, map_config=map_config, has_wiki_articles=False,
                               about=zim_writer._about_fields(**about))
    return json.loads(c.items["map-config.json"].content)


def test_defaults_follow_the_zim_metadata_defaults():
    cfg = _config({"name": "Monaco"}, name="OSM - Monaco", description="Offline map of Monaco.")
    assert cfg["title"] == "OSM - Monaco"
    assert cfg["description"] == "Offline map of Monaco."
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", cfg["date"])
    assert cfg["generator"] == f"streetzim {__version__}"
    assert cfg["name"] == "Monaco"


def test_metadata_flags_win_and_config_keys_are_kept():
    cfg = _config({"name": "M", "title": "Kept"}, name="n", description="d",
                  metadata={"Title": "Flag title", "Description": "Flag desc"})
    assert cfg["title"] == "Kept"
    assert cfg["description"] == "Flag desc"


def test_without_about_the_config_is_unchanged():
    c = FakeCreator()
    zim_writer._add_map_config(c, FakeItem, map_config={"name": "t"}, has_wiki_articles=False)
    cfg = json.loads(c.items["map-config.json"].content)
    assert not {"title", "description", "date", "generator"} & set(cfg)
