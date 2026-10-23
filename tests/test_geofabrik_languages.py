"""resources/geofabrik-languages.tsv, its generator
(tools/gen_geofabrik_languages.py) and streetzim/area_languages.py: the
region languages `streetzim --multilingual` records when a recipe gives
none."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import area_languages, languages  # noqa: E402
from tools import gen_geofabrik_languages as gen  # noqa: E402


def _country_info(rows):
    head = ("#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\tArea\tPopulation\tContinent"
            "\ttld\tCurrencyCode\tCurrencyName\tPhone\tPostal Code Format\tPostal Code Regex"
            "\tLanguages")
    return "\n".join([head] + ["\t".join([iso] + [""] * 14 + [langs]) for iso, langs in rows])


COUNTRY_INFO = _country_info([
    ("CH", "de-CH,fr-CH,it-CH,rm"), ("ES", "es-ES,ca,gl,eu,oc"), ("CA", "en-CA,fr-CA,iu"),
    ("US", "en-US,es-US,haw,fr"), ("CN", "zh-CN,yue,wuu,dta,ug,za"), ("TW", "zh-TW,zh,nan,hak"),
    ("PR", "en-PR,es-PR"), ("FR", "fr-FR,frp,br,co,ca,eu,oc"), ("NC", "fr-NC"),
    ("IL", "he,ar-IL,en-IL,"), ("PS", "ar-PS"), ("AQ", ""), ("BE", "nl-BE,fr-BE,de-BE"),
    ("GB", "en-GB,cy-GB,gd"), ("PF", "fr-PF,ty"), ("VU", "bi,en-VU,fr-VU"),
    ("NO", "no,nb,nn,se,fi"), ("PH", "tl,en-PH,fil,ceb"), ("MA", "ar-MA,ber,fr"),
    ("NZ", "en-NZ,mi"), ("ER", "aa-ER,ar-ER,tig,kun,ti-ER"), ("DE", "de"), ("AT", "de-AT"),
    ("ZA", "zu,xh,af,nso,en-ZA"), ("LS", "en-LS,st,zu,xh"), ("AS", "en-AS,sm,to"),
    ("GU", "en-GU,ch-GU"), ("MP", "fil,tl,zh,ch-MP,en-MP"), ("IQ", "ar-IQ,ku,hy"),
    ("MK", "mk,sq,tr,rmm,sr"), ("UA", "uk,ru-UA,rom,pl,hu"), ("PL", "pl"),
])


def _pop(**langs):
    return {k: {"_populationPercent": str(p), **({"_officialStatus": s} if s else {})}
            for k, (p, s) in langs.items()}


def _t(population, **langs):
    return {"_population": str(population), "languagePopulation": _pop(**langs)}


TERRITORIES = {"supplemental": {"territoryInfo": {
    "CH": _t(8700000, de=(76, "official"), fr=(39, "official"), gsw=(66, "de_facto_official"),
             it=(15, "official"), rm=(0.5, "official_regional")),
    "ES": {"languagePopulation": _pop(es=(99, "official"), ca=(17, "official_regional"),
                                      gl=(7, "official_regional"), eu=(2, "official_regional"))},
    "ZA": _t(60442600, en=(31, "official"), zu=(24, "official_regional"),
             xh=(18, "official_regional"), af=(13, "official_regional"),
             nso=(9.4, "official_regional")),
    "LS": _t(2227550, en=(27, "official"), st=(98, "official")),
    "AS": _t(43895, en=(97, "de_facto_official"), sm=(99, "official")),
    "GU": _t(169532, ch=(22, "official"), en=(91, "de_facto_official")),
    "MP": _t(51118, en=(97, "de_facto_official")),
    "IQ": _t(45000000, ar=(68, "official"), ckb=(20, "official_regional"),
             az_Arab=(1.8, "official_regional")),
    "MK": _t(1800000, mk=(68, "official"), sq=(25, "official_regional")),
    "UA": _t(38000000, uk=(65, "official"), ru=(46, "de_facto_official")),
    "PL": _t(37000000, pl=(97, "official"), de=(18, "official_regional")),
    "CA": {"languagePopulation": _pop(en=(87, "official"), fr=(29, "official"),
                                      iu=(0.1, "official_regional"))},
    "US": {"languagePopulation": _pop(en=(96, "de_facto_official"),
                                      es=(9.6, "official_regional"),
                                      haw=(0.01, "official_regional"))},
    "CN": {"languagePopulation": _pop(zh=(90, "official"), za=(0.3, "official_regional"),
                                      ug=(0.5, "official_regional"))},
    "TW": {"languagePopulation": _pop(zh_Hant=(95, "official"), nan_Hant=(57, "official"))},
    "PR": {"languagePopulation": _pop(es=(87, "official"), en=(49, "de_facto_official"))},
    "FR": {"languagePopulation": _pop(fr=(97, "official"), br=(0.5, None))},
    "NC": {"languagePopulation": _pop(fr=(99, "official"))},
    "IL": _t(8800000, he=(70, "official"), ar=(20, "official")),
    "PS": _t(4000000, ar=(98, "official")),
    "BE": {"languagePopulation": _pop(fr=(38, "official"), de=(22, "official"),
                                      nl=(55, "official"))},
    "GB": {"languagePopulation": _pop(en=(98, "official"), cy=(0.9, "official_regional"))},
    "PF": {"languagePopulation": _pop(fr=(94, "official"), ty=(3.6, "official"))},
    "VU": {"languagePopulation": _pop(bi=(95, "official"), en=(29, "official"))},
    "NO": {"languagePopulation": _pop(nb=(100, "official"), nn=(25, "official"),
                                      no=(100, "official"))},
    "PH": {"languagePopulation": _pop(en=(64, "official"), fil=(60, "official"),
                                      ceb=(24, "official_regional"))},
    "MA": {"languagePopulation": _pop(ar=(62, "official"), fr=(36, "de_facto_official"),
                                      tzm=(9.8, "official"))},
    "NZ": {"languagePopulation": _pop(en=(98, "de_facto_official"), mi=(2.8, "official"))},
    "ER": {"languagePopulation": _pop(ar=(4.9, "official"), en=(59, "official"),
                                      ti=(60, "de_facto_official"))},
    "DE": _t(83000000, de=(91, "official")),
    "AT": _t(9000000, de=(96, "official")),
}}}


def _feature(id_, path, parent=None, alpha2=None, sub=None):
    props = {"id": id_, "urls": {"pbf": f"https://download.geofabrik.de/{path}-latest.osm.pbf"}}
    if parent:
        props["parent"] = parent
    if alpha2:
        props["iso3166-1:alpha2"] = alpha2
    if sub:
        props["iso3166-2"] = sub
    return {"type": "Feature", "properties": props}


# As Geofabrik lists them: US states hang off north-america with only an
# iso3166-2 code; Taiwan, Puerto Rico and New Caledonia carry both their own
# alpha-2 code and another country's iso3166-2 code; polynesie-francaise
# carries Vanuatu's code and great-britain none (OVERRIDES fixes both).
FEATURES = [
    _feature("europe", "europe"),
    _feature("north-america", "north-america"),
    _feature("asia", "asia"),
    _feature("australia-oceania", "australia-oceania"),
    _feature("africa", "africa"),
    _feature("switzerland", "europe/switzerland", "europe", ["CH"]),
    _feature("germany", "europe/germany", "europe", ["DE"]),
    _feature("dach", "europe/dach", "europe"),
    _feature("alps", "europe/alps", "europe"),
    _feature("spain", "europe/spain", "europe", ["ES"]),
    _feature("andalucia", "europe/spain/andalucia", "spain"),
    _feature("canary-islands", "africa/canary-islands", "africa"),
    _feature("canada", "north-america/canada", "north-america", ["CA"]),
    _feature("quebec", "north-america/canada/quebec", "canada", sub=["CA-QC"]),
    _feature("us", "north-america/us", "north-america", ["US"]),
    _feature("us/california", "north-america/us/california", "north-america", sub=["US-CA"]),
    _feature("us-west", "north-america/us-west", "north-america"),
    _feature("us/puerto-rico", "north-america/us/puerto-rico", "north-america", ["PR"], ["US-PR"]),
    _feature("taiwan", "asia/taiwan", "asia", ["TW"], ["CN-TW"]),
    _feature("new-caledonia", "australia-oceania/new-caledonia", "australia-oceania",
             ["NC"], ["FR-NC"]),
    _feature("vanuatu", "australia-oceania/vanuatu", "australia-oceania", ["VU"]),
    _feature("polynesie-francaise", "australia-oceania/polynesie-francaise", "australia-oceania",
             ["VU"]),
    _feature("great-britain", "europe/great-britain", "europe"),
    _feature("ilps", "asia/israel-and-palestine", "asia", ["PS", "IL"]),
    _feature("belgium", "europe/belgium", "europe", ["BE"]),
    _feature("norway", "europe/norway", "europe", ["NO"]),
    _feature("philippines", "asia/philippines", "asia", ["PH"]),
    _feature("morocco", "africa/morocco", "africa", ["MA"]),
    _feature("new-zealand", "australia-oceania/new-zealand", "australia-oceania", ["NZ"]),
    _feature("eritrea", "africa/eritrea", "africa", ["ER"]),
    _feature("south-africa", "africa/south-africa", "africa", ["ZA"]),
    _feature("south-africa-and-lesotho", "africa/south-africa-and-lesotho", "africa"),
    _feature("american-oceania", "australia-oceania/american-oceania", "australia-oceania", ["VU"]),
    _feature("iraq", "asia/iraq", "asia", ["IQ"]),
    _feature("macedonia", "europe/macedonia", "europe", ["MK"]),
    _feature("ukraine", "europe/ukraine", "europe", ["UA"]),
    _feature("poland", "europe/poland", "europe", ["PL"]),
    _feature("antarctica", "antarctica", None, ["AQ"]),
]


def _generate(tmp_path, features=None):
    """Run the generator on fixture files; the table's rows by path, and its text."""
    (tmp_path / "index.json").write_text(json.dumps({"features": features or FEATURES}))
    (tmp_path / "territories.json").write_text(json.dumps(TERRITORIES))
    (tmp_path / "countries.txt").write_text(COUNTRY_INFO)
    out = tmp_path / "table.tsv"
    assert gen.main(["--index", str(tmp_path / "index.json"),
                     "--territories", str(tmp_path / "territories.json"),
                     "--countries", str(tmp_path / "countries.txt"), "-o", str(out)]) == 0
    text = out.read_text()
    rows = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            path, kind, countries, langs = line.split("\t")
            rows[path] = (kind, countries.split(","), langs.split(","))
    return rows, text


def test_countries_get_their_official_and_de_facto_languages(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["europe/switzerland"] == ("country", ["CH"], ["de", "fr", "it"])   # gsw is de
    assert rows["europe/spain"] == ("country", ["ES"], ["es"])           # not ca (regional)
    assert rows["europe/poland"] == ("country", ["PL"], ["pl"])          # not de (regional)
    assert rows["north-america/canada"] == ("country", ["CA"], ["en", "fr"])
    assert rows["north-america/us"] == ("country", ["US"], ["en"])
    assert rows["australia-oceania/new-zealand"] == ("country", ["NZ"], ["en", "mi"])
    assert rows["africa/eritrea"] == ("country", ["ER"], ["ti", "en", "ar"])
    assert rows["europe/belgium"] == ("country", ["BE"], ["nl", "fr", "de"])   # most spoken first



def test_country_languages_correct_cldr(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["africa/south-africa"] == ("country", ["ZA"], ["en", "zu", "xh", "af"])
    assert rows["asia/iraq"] == ("country", ["IQ"], ["ar", "ku"])
    assert rows["europe/macedonia"] == ("country", ["MK"], ["mk", "sq"])
    assert rows["europe/ukraine"] == ("country", ["UA"], ["uk"])          # not ru (de facto)


def test_country_languages_keep_their_given_order_and_cldr_shares():
    population, shares = gen.spoken_languages(TERRITORIES, {"ZA": ["af", "en", "xx"]})["ZA"]
    assert population == 60442600
    assert shares == [("af", 13.0), ("en", 31.0), ("xx", 0.0)]     # as given, not by share


def test_tags_that_become_one_code_keep_the_larger_share():
    population, shares = gen.spoken_languages(TERRITORIES)["CH"]
    assert population == 8700000
    assert shares == [("de", 76.0), ("fr", 39.0), ("it", 15.0)]     # gsw (66%) is de, not added


def test_several_countries_order_their_languages_by_speakers(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["australia-oceania/american-oceania"] == (
        "country", ["AS", "GU", "MP"], ["en", "sm", "ch"])     # not sm first, as in American Samoa
    assert rows["africa/south-africa-and-lesotho"] == (
        "country", ["ZA", "LS"], ["en", "zu", "xh", "af", "st"])
    # not an override: the index's order of countries
    assert rows["asia/israel-and-palestine"] == ("country", ["PS", "IL"], ["ar", "he"])
    assert "europe" not in rows and "antarctica" not in rows


def test_cldr_codes_become_streetzims(tmp_path):
    assert gen._codes(["nb", "nn", "no"]) == ["no"]                                  # noqa: SLF001
    assert gen._codes(["fil", "tzm", "gsw", "yue", "ckb"]) == ["tl", "zgh", "de", "zh", "ku"]  # noqa: SLF001
    rows, _ = _generate(tmp_path)
    assert rows["europe/norway"][2] == ["no"]
    assert rows["asia/philippines"][2] == ["en", "tl"]            # not ceb (regional)
    assert rows["africa/morocco"][2] == ["ar", "fr", "zgh"]


def test_an_extract_with_its_own_country_code_ignores_its_iso3166_2_code(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["asia/taiwan"] == ("country", ["TW"], ["zh"])               # not China's za, ug
    assert rows["north-america/us/puerto-rico"] == ("country", ["PR"], ["es", "en"])
    assert rows["australia-oceania/new-caledonia"] == ("country", ["NC"], ["fr"])


def test_parts_of_countries_get_only_their_countrys_first_language(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["north-america/us/california"] == ("part", ["US"], ["en"])  # its iso3166-2 code
    assert rows["europe/spain/andalucia"] == ("part", ["ES"], ["es"])       # its parent's
    assert rows["north-america/canada/quebec"] == ("part", ["CA"], ["en"])


def test_overrides_fix_wrong_and_missing_codes(tmp_path):
    rows, _ = _generate(tmp_path)
    assert rows["australia-oceania/polynesie-francaise"] == ("country", ["PF"], ["fr", "ty"])
    assert rows["australia-oceania/vanuatu"] == ("country", ["VU"], ["bi", "en"])
    assert rows["europe/great-britain"] == ("country", ["GB"], ["en"])
    assert rows["africa/canary-islands"] == ("part", ["ES"], ["es"])
    assert rows["north-america/us-west"] == ("part", ["US"], ["en"])
    assert rows["europe/dach"] == ("country", ["DE", "AT", "CH"], ["de", "fr", "it"])
    assert "europe/alps" not in rows                                         # None: no row


def test_two_extracts_with_one_code_stop_the_generator(tmp_path):
    features = FEATURES + [_feature("vanuatu-2", "australia-oceania/vanuatu-2",
                                    "australia-oceania", ["VU"])]
    with pytest.raises(ValueError, match="VU: vanuatu, vanuatu-2"):
        _generate(tmp_path, features)


def test_an_extract_left_without_a_row_stops_the_generator(tmp_path):
    features = FEATURES + [_feature("benelux", "europe/benelux", "europe")]
    with pytest.raises(ValueError, match="benelux"):
        _generate(tmp_path, features)


def test_render_names_sources_licences_and_changes(tmp_path):
    _, text = _generate(tmp_path)
    assert "europe/switzerland\tcountry\tCH\tde,fr,it\n" in text
    for needle in (gen.INDEX_URL, gen.TERRITORIES_URL, gen.COUNTRIES_URL, gen.CLDR_LICENSE,
                   gen.GEONAMES_LICENSE, gen.LICENCE_FILE, "CC BY 4.0", "Unicode License v3",
                   "Modified"):
        assert needle in text


def test_shipped_table_and_licence_notice():
    text = area_languages.GEOFABRIK_TABLE.read_text(encoding="utf-8")
    for needle in (gen.INDEX_URL, gen.TERRITORIES_URL, gen.COUNTRIES_URL, gen.CLDR_LICENSE,
                   gen.GEONAMES_LICENSE, gen.LICENCE_FILE, "Modified"):
        assert needle in text
    notice = (ROOT / gen.LICENCE_FILE).read_text(encoding="utf-8")
    assert notice.startswith("UNICODE LICENSE V3")
    assert "SPDX-License-Identifier: Unicode-3.0" in notice
    rows = {r[0]: r for r in area_languages._rows(area_languages.GEOFABRIK_TABLE)}  # noqa: SLF001
    assert len(rows) > 500
    for path, (_, kind, countries, langs) in rows.items():
        codes = langs.split(",")
        assert kind in ("country", "part") and countries, path
        assert languages.region_languages(langs) == codes, path
        assert kind == "country" or len(codes) == 1, path
    assert rows["asia/taiwan"][3] == "zh"
    assert rows["north-america/us/puerto-rico"][1:] == ["country", "PR", "es,en"]
    assert rows["north-america/us/california"][1:] == ["part", "US", "en"]
    assert rows["australia-oceania/new-zealand"][3] == "en,mi"
    assert rows["europe/ukraine"][3] == "uk"
    assert rows["europe/monaco"][3] == "fr"
    assert rows["africa/south-africa"][3] == "en,zu,xh,af"
    assert rows["africa/south-africa-and-lesotho"][3] == "en,zu,xh,af,st"
    assert rows["asia/iraq"][3] == "ar,ku" and rows["europe/macedonia"][3] == "mk,sq"
    for path, langs in (("europe/spain", "es"), ("europe/poland", "pl"), ("europe/italy", "it"),
                        ("europe/denmark", "da"), ("asia/philippines", "en,tl")):
        assert rows[path][3] == langs, path
    assert rows["australia-oceania/american-oceania"][3].startswith("en,")


def _tables(tmp_path):
    regions = tmp_path / "r.tsv"
    regions.write_text("# id\tlanguages\nbaltics\tlt,lv,et\ncalifornia\ten,es\n"
                       "ukraine\tru,uk,ro\nwashington-dc\ten,es\neurope\ten,de,ru\n"
                       "switzerland\tde,fr,it,rm\nalaska\ten,esu\n")
    outlines = tmp_path / "o.tsv"
    outlines.write_text("# id\tpaths\nbaltics\teurope/estonia+europe/latvia+europe/lithuania\n"
                        "ukraine\teurope/ukraine+europe/moldova+russia\n"
                        "europe\teurope+asia/armenia+asia/azerbaijan\n"
                        "nordics\teurope/norway+europe/sweden\n")
    registry = tmp_path / "regions.tsv"
    registry.write_text("# id\tname\tbbox\nwashington-dc\tWashington, D.C.\t"
                        "-77.12,38.79,-76.91,38.99\tlocal\n")
    geo = tmp_path / "g.tsv"
    geo.write_text("# x\neurope/switzerland\tcountry\tCH\tde,fr,it\n"
                   "europe/monaco\tcountry\tMC\tfr\neurope/liechtenstein\tcountry\tLI\tde\n"
                   "europe/ukraine\tcountry\tUA\tuk\neurope/moldova\tcountry\tMD\tro\n"
                   "russia\tcountry\tRU\tru\neurope/spain/andalucia\tpart\tES\tes\n")
    return {"region_table": regions, "outline_table": outlines, "registry_table": registry,
            "geofabrik_table": geo}


def test_derive_streetzim_regions_by_name_box_and_whole_outline(tmp_path):
    kw = _tables(tmp_path)
    d = area_languages.derive("california", ["north-america/us/california"], **kw)
    assert d.languages == ["en", "es"] and d.credit is None and d.labels
    d = area_languages.derive("dc", ["north-america/us/district-of-columbia"],
                              bbox="-77.12,38.79,-76.91,38.990", **kw)
    assert d.languages == ["en", "es"] and "StreetZim region washington-dc" in d.where
    d = area_languages.derive(None, ["europe/lithuania", "europe/estonia", "europe/latvia"], **kw)
    assert d.languages == ["lt", "lv", "et"] and "StreetZim region baltics" in d.where
    d = area_languages.derive(None, ["europe/ukraine", "europe/moldova", "russia"], **kw)
    assert d.languages == ["ru", "uk", "ro"] and "StreetZim region ukraine" in d.where


def test_derive_regions_without_an_outline_row_by_extract_name(tmp_path):
    kw = _tables(tmp_path)
    d = area_languages.derive(None, ["europe/switzerland"], **kw)
    assert d.languages == ["de", "fr", "it", "rm"] and d.credit is None
    assert d.where.startswith("StreetZim region switzerland, the extract europe/switzerland")
    d = area_languages.derive(None, ["north-america/us/alaska"], **kw)
    assert d.languages == ["en", "esu"] and "StreetZim region alaska" in d.where
    # a region with an outline row needs all of it, whatever its extracts are named
    assert area_languages.derive(None, ["europe/ukraine"], **kw).languages == ["uk"]
    assert area_languages.derive(None, ["europe/switzerland", "europe/monaco"],
                                 **kw).languages == ["de", "fr", "it"]


def test_derive_part_of_an_outline_is_no_region(tmp_path):
    kw = _tables(tmp_path)
    d = area_languages.derive(None, ["europe/ukraine"], **kw)
    assert d.languages == ["uk"] and d.credit and "StreetZim" not in d.where
    d = area_languages.derive(None, ["europe/ukraine", "europe/moldova"], **kw)
    assert d.languages == ["uk", "ro"] and "StreetZim" not in d.where
    d = area_languages.derive(None, ["europe"], **kw)                    # a continent alone
    assert d.languages == [] and "europe" in d.where
    d = area_languages.derive(None, ["europe/monaco"], **kw)             # no region: the country
    assert d.languages == ["fr"] and "StreetZim" not in d.where
    assert area_languages.streetzim_region(["europe/sweden", "europe/norway"],
                                           region_table=kw["region_table"],
                                           outline_table=kw["outline_table"]) is None


def test_derive_falls_back_to_the_geofabrik_table(tmp_path):
    kw = _tables(tmp_path)
    d = area_languages.derive("monaco", ["europe/monaco", "europe/liechtenstein"], **kw)
    assert d.languages == ["fr", "de"] and d.labels and d.hint is None
    assert d.credit == area_languages.GEOFABRIK_CREDIT
    assert d.where.startswith("the official languages of the Geofabrik extracts")
    d = area_languages.derive(None, ["europe/spain/andalucia"], **kw)
    assert d.languages == ["es"] and not d.labels and d.credit and "--region-languages" in d.hint
    assert d.where.startswith("the first language of the country of")
    assert area_languages.derive(None, ["europe/monaco", "africa/x"], **kw).languages == []
    d = area_languages.derive(None, None, **kw)                          # a polygon from elsewhere
    assert d.languages == [] and "--region-languages" in d.hint
    missing = {**kw, "region_table": tmp_path / "missing.tsv"}
    assert area_languages.derive("x", [], **missing).languages == []
