#!/usr/bin/env python3
"""Regenerate the site's pages from Archive.org.

Queries Archive.org for all items with identifiers starting with "streetzim-",
merges with the REGIONS registry, and writes, in English and every language
of web/i18n/: the home page (web/index.html, web/<lang>/index.html, from
web/template.html), a page per map (web/maps/<id>/, web/<lang>/maps/<id>/,
from web/map_template.html), web/site.css and web/sitemap.xml. Then
(optionally) runs `firebase deploy`.

Usage:
    python3 web/generate.py          # generate only
    python3 web/generate.py --deploy # generate and deploy to Firebase
"""
import argparse
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from html import escape, unescape

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "index.html")
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

# Static registry of regions with their display names and descriptions.
# The "order" field controls display order. New regions go here.
# `zim_file` must match the filename we upload to Archive.org.
# Tier display order. Each entry is (tier_id, header_text). REGIONS are
# rendered grouped by tier in this order, with the header_text emitted
# as a full-width row inside the .maps grid (`.maps-tier-header` CSS,
# grid-column: 1 / -1).
TIERS = [
    ("continent", "Continents & continent-scale regions"),
    ("multi-country", "Multi-country regions"),
    ("country", "Individual countries"),
    ("us-region", "United States — sub-regions"),
    ("local", "States, cities & islands"),
]


# Page languages. English is the template's language, at /. Every language
# with a translation in web/i18n/ has its own pages: site.<lang>.json (the
# page text, keyed as site.en.json; tools/check_site_i18n.py checks one) and
# regions.<lang>.json (the maps' names and descriptions). Each language has a
# home page at /<lang>/ (English: /) and a page per map at /<lang>/maps/<id>/
# (English: /maps/<id>/). A region with "language" set is a map made in that
# language: listed on that language's home page only, with no map page.
I18N_DIR = os.path.join(SCRIPT_DIR, "i18n")
SITE_URL = "https://streetzim.web.app/"
RTL_LANGS = {"ar", "fa", "he", "ur"}
_I18N = {}


def _i18n_file(kind, lang):
    key = (kind, lang)
    if key not in _I18N:
        path = os.path.join(I18N_DIR, f"{kind}.{lang}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: not a JSON object")
        _I18N[key] = {k: v for k, v in data.items() if not k.startswith("_")}
    return _I18N[key]


def site_text(lang):
    """One language's page text: site.<lang>.json over site.en.json. A key
    a translation lacks (one added after it was made) stays English."""
    en = _i18n_file("site", "en")
    if lang == "en":
        return en
    tr = _i18n_file("site", lang)
    missing = sorted(set(en) - set(tr))
    if missing and ("missing", lang) not in _I18N:
        _I18N[("missing", lang)] = missing
        print(f"  [{lang}] {len(missing)} strings not translated yet, in English: {missing[:8]}")
    return {**en, **tr}


def t(lang, key, **values):
    """A page string (HTML) with its {placeholders} filled."""
    s = site_text(lang)[key]
    for k, v in values.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def t_attr(lang, key, **values):
    """A page string inside an attribute value (double-quoted)."""
    return t(lang, key, **values).replace('"', "&quot;")


def _check_translations(lang):
    """Stop on a translation whose markup, placeholders or required text
    differ from the English (tools/check_site_i18n.py): its HTML goes into the
    pages as it is. A key or map it lacks is fine (it stays English)."""
    spec = importlib.util.spec_from_file_location(
        "check_site_i18n", os.path.join(PROJECT_DIR, "tools", "check_site_i18n.py"))
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    problems = chk.check_site(_i18n_file("site", "en"), _i18n_file("site", lang), f"site.{lang}.json")
    problems = [p for p in problems if not p.endswith((": missing", ": not in site.en.json"))]
    if problems:
        raise ValueError(f"web/i18n/site.{lang}.json: " + "; ".join(problems[:5])
                         + " (tools/check_site_i18n.py)")


# A language whose site.<lang>.json says "_maps_only": true (the languages
# of maps in India and their neighbours, which have no page of their own
# otherwise) gets a home page listing only the maps that have a version in
# it, and pages for those maps alone; while no map has one, no pages at all
# (build_page drops it from LANGUAGES, so no picker, alternate link or
# sitemap entry points at a page that is not there).
MAPS_ONLY = set()
# {lang: ids of the maps with a version in it}, for the maps-only languages
# (build_page): the map pages they have.
_MAPS_ONLY_PAGES: dict = {}


def _maps_only_flag(lang):
    """Whether site.<lang>.json says "_maps_only": true (the _ keys are
    dropped from site_text, so read the file itself)."""
    try:
        with open(os.path.join(I18N_DIR, f"site.{lang}.json"), encoding="utf-8") as f:
            return json.load(f).get("_maps_only") is True
    except (OSError, ValueError, AttributeError):
        return False


def has_page(lang, path):
    """Whether the site has `path` ("" for the home page, "maps/<id>/") in
    `lang`: every language has every page, but a maps-only one only its own
    maps' pages."""
    if lang not in MAPS_ONLY or not path.startswith("maps/"):
        return True
    return path.split("/")[1] in _MAPS_ONLY_PAGES.get(lang, ())


def site_languages():
    """[(code, name in that language, path under web/)]: English first, then
    each language with both translation files, by code (each checked:
    _check_translations); a maps-only one (MAPS_ONLY) as well."""
    out = [("en", site_text("en")["lang_name"], "")]
    try:
        names = sorted(os.listdir(I18N_DIR))
    except OSError:
        names = []
    for n in names:
        m = re.fullmatch(r"site\.([a-z]{2,3})\.json", n)
        if m and m.group(1) != "en" and os.path.isfile(os.path.join(I18N_DIR, f"regions.{m.group(1)}.json")):
            _check_translations(m.group(1))
            if _maps_only_flag(m.group(1)):
                MAPS_ONLY.add(m.group(1))
            out.append((m.group(1), site_text(m.group(1))["lang_name"], f"{m.group(1)}/"))
    return out


LANGUAGES = site_languages()
# Every language with a translation; LANGUAGES is it less the maps-only
# languages with no map yet (build_page sets it for each build).
ALL_LANGUAGES = list(LANGUAGES)


def region_text(lang, region):
    """A map's (title, description HTML) in a language: regions.<lang>.json
    (plain text), else the English of REGIONS."""
    if lang != "en" and region.get("language", "en") == "en":
        tr = _i18n_file("regions", lang).get(region["id"])
        if isinstance(tr, dict) and tr.get("title") and tr.get("description"):
            return tr["title"], escape(tr["description"], quote=False)
    return region["title"], region["description"]


def fmt_size(lang, n):
    """human_size in a language: its decimal separator and units."""
    s = human_size(n)
    if lang == "en":
        return s
    num, _, unit = s.partition(" ")
    unit = {"GB": t(lang, "unit_gb"), "MB": t(lang, "unit_mb")}.get(unit, unit)
    return num.replace(".", t(lang, "decimal_sep")) + " " + unit


def fmt_date(lang, iso, human=None):
    """A date (YYYY-MM-DD) as a language writes it; English: `human`."""
    d = datetime.date.fromisoformat(iso)
    if lang == "en" and human:
        return human
    return t(lang, "date_format", day=d.day, month=site_text(lang)["months"][d.month - 1], year=d.year)


REGIONS = [
    # ── Continents & continent-scale regions ─────────────────────────
    {
        "id": "europe",
        "tier": "continent",
        "title": "Europe",
        "zim_file": "osm-europe.zim",
        "description": "United Kingdom, Ireland, France, Spain, Portugal, Germany, Italy, Netherlands, Belgium, Switzerland, Austria, Poland, Greece, Sweden, Norway, Finland, Denmark, the Baltics, Ukraine, Georgia, Armenia, Azerbaijan, Cyprus, and dozens more.",
    },
    {
        "id": "united-states",
        "tier": "continent",
        "title": "United States",
        "zim_file": "osm-united-states.zim",
        "description": "Continental United States &mdash; all 48 contiguous states and Washington, D.C.",
    },
    {
        "id": "south-america",
        "tier": "continent",
        "title": "South America",
        "zim_file": "osm-south-america.zim",
        "description": "Continental South America &mdash; Colombia, Venezuela, Guyana, Suriname, French Guiana, Ecuador, Peru, Bolivia, Paraguay, Uruguay, Chile, the Amazon, the Andes, the Atacama, and Patagonia. (Brazil and Argentina ship as their own dedicated ZIMs.)",
    },
    {
        "id": "africa",
        "tier": "continent",
        "title": "Africa",
        "zim_file": "osm-africa.zim",
        "description": "All of Africa &mdash; Algeria, Egypt, Ethiopia, Kenya, Morocco, Nigeria, South Africa, Tanzania, and 40+ more countries.",
    },
    {
        "id": "indian-subcontinent",
        "tier": "continent",
        "title": "Indian Subcontinent",
        "zim_file": "osm-indian-subcontinent.zim",
        "description": "India, Pakistan, Bangladesh, Sri Lanka, Nepal, Bhutan, and the Maldives.",
    },
    {
        "id": "china",
        "tier": "continent",
        "title": "China",
        "zim_file": "osm-china.zim",
        "description": "China &mdash; Beijing, Shanghai, Guangzhou, Shenzhen, Chengdu, Chongqing, Xi&rsquo;an, Wuhan, Hangzhou, Hong Kong, and Macau, plus the Great Wall, the Yangtze, the Tibetan Plateau, and the Gobi.",
    },
    {
        "id": "russia",
        "tier": "continent",
        "title": "Russia",
        "zim_file": "osm-russia.zim",
        "description": "Russia &mdash; Moscow, Saint Petersburg, Novosibirsk, Yekaterinburg, Kazan, Nizhny Novgorod, Sochi, Vladivostok; the Volga, the Urals, the Caucasus, Lake Baikal; the Trans-Siberian Railway from the Baltic to the Sea of Japan.",
    },
    {
        "id": "australia-nz",
        "tier": "continent",
        "title": "Australia & New Zealand",
        "zim_file": "osm-australia-nz.zim",
        "description": "Australia and New Zealand &mdash; Sydney, Melbourne, Brisbane, Perth, Auckland, Wellington, the Great Barrier Reef, Outback, and Southern Alps.",
    },
    {
        "id": "southeast-asia",
        "tier": "continent",
        "title": "Southeast Asia",
        "zim_file": "osm-southeast-asia.zim",
        "description": "Thailand, Vietnam, Myanmar, Cambodia, Laos, Malaysia, Singapore, Indonesia, the Philippines, Brunei, and Timor-Leste &mdash; Bangkok, Hanoi, Ho Chi Minh City, Yangon, Kuala Lumpur, Jakarta, Manila, Bali, Borneo, the Mekong Delta, and the South China Sea.",
    },
    {
        "id": "thailand",
        "tier": "country",
        "title": "Thailand",
        "zim_file": "osm-thailand.zim",
        "description": "Thailand &mdash; Bangkok, Chiang Mai, Phuket, Pattaya, Ayutthaya, Khon Kaen and Hat Yai, with the islands of the Andaman Sea and the Gulf of Thailand.",
    },
    {
        "id": "canada",
        "tier": "continent",
        "title": "Canada",
        "zim_file": "osm-canada.zim",
        "description": "All of Canada &mdash; Toronto, Montreal, Vancouver, Calgary, Ottawa, Quebec City, Edmonton, Halifax, the Rockies, Banff, the Great Lakes, the Maritimes, and the Yukon and Northwest Territories.",
    },
    # ── Multi-country regions ────────────────────────────────────────
    {
        "id": "west-asia",
        "tier": "multi-country",
        "title": "West Asia",
        "zim_file": "osm-west-asia.zim",
        "description": "Turkey, Syria, Lebanon, Israel, Palestine, Jordan, Iraq, Iran, Kuwait, Saudi Arabia, Bahrain, Qatar, UAE, Oman, Yemen, and parts of Egypt, Afghanistan, and Pakistan.",
    },
    {
        "id": "central-asia",
        "tier": "multi-country",
        "title": "Central Asia",
        "zim_file": "osm-central-asia.zim",
        "description": "Central Asia &mdash; Kazakhstan, Uzbekistan, Turkmenistan, Tajikistan, Kyrgyzstan, Afghanistan, and the Caucasus. Almaty, Tashkent, Bishkek, Ashgabat, Dushanbe, Kabul, the Pamirs, and the Silk Road.",
    },
    {
        "id": "korea-mongolia",
        "tier": "multi-country",
        "title": "Korea & Mongolia",
        "zim_file": "osm-korea-mongolia.zim",
        "description": "The Korean Peninsula and Mongolia &mdash; Seoul, Busan, Daegu, Incheon, Pyongyang, Ulaanbaatar, Darkhan; the Gobi, the Khangai mountains, the DMZ, and the Sea of Japan coast.",
    },
    {
        "id": "caucasus",
        "tier": "multi-country",
        "title": "Caucasus",
        "zim_file": "osm-caucasus.zim",
        "description": "The Caucasus &mdash; Georgia, Armenia, and Azerbaijan, with the southern Russian Caucasus and eastern Turkey. Tbilisi, Yerevan, Baku, Batumi, the Greater Caucasus range, Mount Elbrus, Lake Sevan, and the Caspian coast.",
    },
    {
        "id": "pacific-islands",
        "tier": "multi-country",
        "title": "Pacific Islands",
        "zim_file": "osm-pacific-islands.zim",
        "description": "Papua New Guinea, Fiji, Solomon Islands, Vanuatu, New Caledonia, Palau, Micronesia, the Marshall Islands, Nauru, Tuvalu, and Kiribati&rsquo;s Gilbert Islands &mdash; Port Moresby, Suva, Nadi, Honiara, Port Vila, Nouméa, and Majuro.",
    },
    {
        "id": "west-africa",
        "tier": "multi-country",
        "title": "West Africa",
        "zim_file": "osm-west-africa.zim",
        "description": "Nigeria, Ghana, Côte d'Ivoire, Senegal, Mali, Burkina Faso, Guinea, Benin, Togo, Sierra Leone, Liberia, and Niger &mdash; Lagos, Accra, Abidjan, Dakar, Abuja, Bamako, Ouagadougou, and the Gulf of Guinea coast.",
    },
    {
        "id": "southern-africa",
        "tier": "multi-country",
        "title": "Southern Africa",
        "zim_file": "osm-southern-africa.zim",
        "description": "South Africa, Namibia, Botswana, Zimbabwe, Mozambique, Zambia, Malawi, Lesotho, and Eswatini &mdash; Johannesburg, Cape Town, Durban, Pretoria, Windhoek, Gaborone, Harare, Maputo, Lusaka, the Kalahari, Kruger, and the Cape.",
    },
    {
        "id": "east-africa",
        "tier": "multi-country",
        "title": "East Africa",
        "zim_file": "osm-east-africa.zim",
        "description": "Kenya, Tanzania, Uganda, Ethiopia, Rwanda, Burundi, Somalia, and South Sudan &mdash; Nairobi, Dar es Salaam, Addis Ababa, Kampala, Kigali, Mombasa, Zanzibar, Mount Kilimanjaro, the Serengeti, and Lake Victoria.",
    },
    {
        "id": "central-america-caribbean",
        "tier": "multi-country",
        "title": "Central America & Caribbean",
        "zim_file": "osm-central-america-caribbean.zim",
        "description": "Central America and the Caribbean &mdash; Yucatán, Belize, Guatemala, Honduras, El Salvador, Nicaragua, Costa Rica, Panama, Cuba, Jamaica, Hispaniola, Cayman Islands, Puerto Rico, the Lesser Antilles, and the southern Bahamas.",
    },
    {
        "id": "himalayas",
        "tier": "multi-country",
        "title": "Himalayas",
        "zim_file": "osm-himalayas.zim",
        "description": "The Himalayas, Karakoram, Hindu Kush, and Pamir &mdash; Nepal, Bhutan, Tibet, Sikkim, Ladakh, Kashmir, the Indus and Brahmaputra valleys. Kathmandu, Pokhara, Lhasa, Thimphu, Leh, Hunza, Everest, K2, Kanchenjunga, and Annapurna.",
    },
    {
        "id": "baltics",
        "tier": "multi-country",
        "title": "Baltics",
        "zim_file": "osm-baltics.zim",
        "description": "Estonia, Latvia, and Lithuania &mdash; Tallinn, Riga, Vilnius, and the Baltic Sea coast.",
    },
    # ── Individual countries ─────────────────────────────────────────
    {
        "id": "brazil",
        "tier": "country",
        "title": "Brazil",
        "zim_file": "osm-brazil.zim",
        "description": "All of Brazil &mdash; São Paulo, Rio de Janeiro, Brasília, Salvador, Belo Horizonte, Fortaleza, Manaus, the Amazon, Pantanal, Iguaçu Falls, and the Atlantic coast.",
    },
    {
        "id": "argentina",
        "tier": "country",
        "title": "Argentina",
        "zim_file": "osm-argentina.zim",
        "description": "Argentina &mdash; Buenos Aires, Córdoba, Rosario, Mendoza, Bariloche, the Pampas, the Andes, Patagonia, and Tierra del Fuego.",
    },
    {
        "id": "mexico",
        "tier": "country",
        "title": "Mexico",
        "zim_file": "osm-mexico.zim",
        "description": "All of Mexico &mdash; Mexico City, Guadalajara, Monterrey, Puebla, Tijuana, Cancún and the Riviera Maya, Oaxaca, the Yucatán Peninsula, Baja California, the Sierra Madre, and both the Pacific and Gulf coasts.",
    },
    {
        "id": "ukraine",
        "tier": "country",
        "title": "Ukraine, Moldova & Western Russia",
        "zim_file": "osm-ukraine.zim",
        "description": "Ukraine and Moldova in full (including Crimea), plus the western Russian frontier &mdash; Volgograd, Rostov, Sochi, Krasnodar, the Black Sea coast, and the Caucasus foothills.",
    },
    {
        "id": "japan",
        "tier": "country",
        "title": "Japan",
        "zim_file": "osm-japan.zim",
        "description": "Japan &mdash; all four main islands (Honshu, Hokkaido, Kyushu, Shikoku), Okinawa, and the Ryukyu archipelago. Tokyo, Osaka, Kyoto, Nagoya, Sapporo, Fukuoka, Hiroshima, and more.",
    },
    {
        "id": "iran",
        "tier": "country",
        "title": "Iran",
        "zim_file": "osm-iran.zim",
        "description": "Iran &mdash; from the Caspian Sea to the Persian Gulf, including Tehran, Isfahan, Shiraz, and Mashhad.",
    },
    {
        "id": "bahrain",
        "tier": "country",
        "title": "Bahrain",
        "zim_file": "osm-bahrain.zim",
        "description": "Bahrain &mdash; Manama, Muharraq, Riffa, Isa Town, the King Fahd Causeway and the Hawar Islands; search a street or shop sign with a photo.",
    },
    {
        "id": "egypt",
        "tier": "country",
        "title": "Egypt",
        "zim_file": "osm-egypt.zim",
        "description": "Egypt &mdash; Cairo, Alexandria, Giza, Luxor, Aswan, the Nile Valley, Sinai Peninsula, and the Red Sea coast.",
    },
    {
        "id": "switzerland",
        "tier": "country",
        "title": "Switzerland & the Alps",
        "zim_file": "osm-switzerland.zim",
        "description": "Switzerland and the surrounding Alps &mdash; Z\u00fcrich, Geneva, Bern, Basel, Lausanne, Lucerne, Lugano, St. Moritz, Zermatt and the Matterhorn, Interlaken and the Jungfrau, plus Chamonix and Mont Blanc, Aosta, Lake Como, Liechtenstein, Lake Constance and western Tirol.",
    },
    {
        "id": "switzerland-de",
        "tier": "country",
        "language": "de",
        "title": "Schweiz und die Alpen",
        "zim_file": "osm-switzerland-de.zim",
        "description": "Die Schweiz und die umliegenden Alpen auf Deutsch &mdash; Zürich, Genf, Bern, Basel, Lausanne, Luzern, Lugano, St. Moritz, Zermatt und das Matterhorn, Interlaken und die Jungfrau, Chamonix und der Mont Blanc, Aosta, der Comer See, Liechtenstein, der Bodensee und Westtirol. Mit deutschen Ortsnamen, deutscher Wikipedia und deutscher Bedienoberfläche.",
    },
    {
        "id": "switzerland-de-ultralight",
        "tier": "country",
        "language": "de",
        "title": "Schweiz und die Alpen (Ultralight)",
        "zim_file": "osm-switzerland-de-ultralight.zim",
        "description": "Die kleine Ausgabe der deutschen Schweiz-Karte: dieselben Orte, Straßen, Wege und Routen zu Fuß, mit dem Rad und dem Auto, mit deutschen Ortsnamen und deutscher Bedienoberfläche &mdash; aber ohne Satellitenbilder, ohne Wikipedia-Artikel und ohne die Kiwix-eigene Volltextsuche (die Suche in der Karte findet weiterhin alle Orte und Adressen), und die Karte reicht bis Zoomstufe 13 statt 14. Rund ein Drittel so groß.",
    },
    {
        "id": "switzerland-border-de-ultralight",
        "tier": "country",
        "language": "de",
        "title": "Schweiz und Liechtenstein (Ultralight)",
        "zim_file": "osm-switzerland-border-de-ultralight.zim",
        "description": "Nur die Schweiz und Liechtenstein: Orte, Adressen, Straßen, Wege und Routen zu Fuß, mit dem Rad und dem Auto enden 10&nbsp;km hinter der Landesgrenze, darüber hinaus zeigt die Karte nur noch eine grobe, grau abgedeckte Übersicht. Sonst wie &bdquo;Schweiz und die Alpen (Ultralight)&ldquo;: deutsche Ortsnamen und deutsche Bedienoberfläche, keine Satellitenbilder, keine Wikipedia-Artikel und keine Kiwix-eigene Volltextsuche (die Suche in der Karte findet alle Orte und Adressen), und die Karte reicht bis Zoomstufe 13. Rund halb so groß.",
    },
    {
        "id": "switzerland-light",
        "tier": "country",
        "title": "Switzerland & the Alps (Light)",
        "zim_file": "osm-switzerland-light.zim",
        "description": "A smaller Switzerland for constrained devices and slow connections &mdash; same coverage and search as the full build, but without satellite imagery and with vector map detail capped one zoom level shallower (z13 instead of z14). 1.4 GB instead of 2.1 GB. Streets, places, addresses, routing, terrain and Wikipedia are unchanged; the map simply stops sharpening at building-footprint scale.",
    },
    {
        "id": "iceland",
        "tier": "country",
        "title": "Iceland",
        "zim_file": "osm-iceland.zim",
        "description": "Iceland &mdash; Reykjavík, Akureyri, the Ring Road, Þingvellir, Geysir, Gullfoss, Vík, Jökulsárlón, Snæfellsnes, the Westfjords, and Vatnajökull.",
    },
    {
        "id": "turkey",
        "tier": "country",
        "title": "Turkey",
        "zim_file": "osm-turkey.zim",
        "description": "Turkey &mdash; Istanbul, Ankara, İzmir, Bursa, Antalya, Konya, Gaziantep, Trabzon, Cappadocia, Pamukkale, Ephesus, the Bosporus, the Aegean coast, and the Anatolian plateau.",
    },
    {
        "id": "south-korea",
        "tier": "country",
        "title": "South Korea",
        "zim_file": "osm-south-korea.zim",
        "description": "South Korea &mdash; Seoul, Busan, Incheon, Daegu, Daejeon, Gwangju, Suwon, Jeju Island, the DMZ, Seoraksan, and Hallasan.",
    },
    {
        "id": "taiwan",
        "tier": "country",
        "title": "Taiwan",
        "zim_file": "osm-taiwan.zim",
        "description": "Taiwan &mdash; Taipei, New Taipei, Taichung, Kaohsiung, Tainan, Taoyuan, Hsinchu, Keelung, Hualien; Taroko Gorge, Sun Moon Lake, Alishan, Yushan, Kenting, and the islands of Penghu, Kinmen, Matsu, Green Island, and Orchid Island.",
    },
    # ── United States — sub-regions ──────────────────────────────────
    {
        "id": "east-coast-us",
        "tier": "us-region",
        "title": "East Coast US",
        "zim_file": "osm-east-coast-us.zim",
        "description": "U.S. East Coast from Maine to Florida &mdash; New York, Boston, Philadelphia, Washington D.C., Atlanta, Miami, and the entire Eastern Seaboard.",
    },
    {
        "id": "midwest-us",
        "tier": "us-region",
        "title": "Midwest United States",
        "zim_file": "osm-midwest-us.zim",
        "description": "Ohio, Indiana, Illinois, Michigan, Wisconsin, Minnesota, Iowa, Missouri, North Dakota, South Dakota, Nebraska, and Kansas.",
    },
    {
        "id": "west-coast-us",
        "tier": "us-region",
        "title": "West Coast US",
        "zim_file": "osm-west-coast-us.zim",
        "description": "U.S. West Coast: Washington, Oregon, and California &mdash; Seattle, Portland, San Francisco, Los Angeles, San Diego, and everything in between.",
    },
    {
        "id": "central-us",
        "tier": "us-region",
        "title": "Central US",
        "zim_file": "osm-central-us.zim",
        "description": "The Mountain West and surrounds &mdash; Utah, Colorado, Wyoming, Montana, Idaho, Nevada, Arizona, and New Mexico. Salt Lake City, Denver, Phoenix, Albuquerque, Yellowstone, Grand Canyon, and the Rockies.",
    },
    # ── States, cities & islands ─────────────────────────────────────
    {
        "id": "california",
        "tier": "local",
        "title": "California",
        "zim_file": "osm-california.zim",
        "description": "All of California &mdash; from the Oregon border to Mexico, the Pacific coast to the Sierra Nevada.",
    },
    {
        "id": "hawaii",
        "tier": "local",
        "title": "Hawaii",
        "zim_file": "osm-hawaii.zim",
        "description": "The Hawaiian Islands &mdash; Honolulu, Hilo, Kahului; O&lsquo;ahu, Maui, Hawai&lsquo;i (Big Island), Kaua&lsquo;i, Moloka&lsquo;i, and Lāna&lsquo;i; Mauna Kea, Mauna Loa, Haleakalā, Pearl Harbor, and the Nā Pali coast.",
    },
    {
        "id": "alaska",
        "tier": "local",
        "title": "Alaska",
        "zim_file": "osm-alaska.zim",
        "description": "Alaska &mdash; Anchorage, Fairbanks, Juneau, Sitka, Ketchikan, Nome, Barrow; Denali, the Brooks Range, the Aleutians, the Inside Passage, Prince William Sound, the Yukon, and the Arctic.",
    },
    {
        "id": "new-york-state",
        "tier": "local",
        "title": "New York State",
        "zim_file": "osm-new-york-state.zim",
        "description": "All of New York State &mdash; New York City, Albany, Buffalo, Rochester, Syracuse, Yonkers; Long Island, the Hudson Valley, the Catskills, the Adirondacks, the Finger Lakes, Niagara Falls, and the Thousand Islands.",
    },
    {
        "id": "florida",
        "tier": "local",
        "title": "Florida",
        "zim_file": "osm-florida.zim",
        "description": "All of Florida &mdash; Miami, Orlando, Tampa, Jacksonville, St. Petersburg, Tallahassee, Fort Lauderdale; the Everglades, the Keys, Cape Canaveral, the Atlantic and Gulf coasts, and Walt Disney World.",
    },
    {
        "id": "carolinas",
        "tier": "local",
        "title": "The Carolinas",
        "zim_file": "osm-carolinas.zim",
        "description": "North and South Carolina &mdash; Charlotte, Raleigh, Durham, Greensboro, Winston-Salem, Columbia, Charleston, Myrtle Beach, Wilmington, and Asheville; the Blue Ridge and Great Smoky Mountains, the Research Triangle, and the Outer Banks and Atlantic coast.",
    },
    {
        "id": "nyc-metro",
        "tier": "local",
        "title": "New York Metro",
        "zim_file": "osm-nyc-metro.zim",
        "description": "The NYC tri-state metropolitan area &mdash; Manhattan, Brooklyn, Queens, the Bronx, Staten Island, Long Island, Westchester, northern New Jersey, and southwestern Connecticut.",
    },
    {
        "id": "chicago-metro",
        "tier": "local",
        "title": "Chicago Metro",
        "zim_file": "osm-chicago-metro.zim",
        "description": "Greater Chicago &mdash; the city and Cook, DuPage, Lake, Will, Kane, McHenry counties, plus northwest Indiana and southeast Wisconsin along Lake Michigan.",
    },
    {
        "id": "greater-la",
        "tier": "local",
        "title": "Greater Los Angeles",
        "zim_file": "osm-greater-la.zim",
        "description": "The Los Angeles metropolitan area &mdash; LA, Long Beach, Anaheim, Santa Monica, Pasadena, Riverside, San Bernardino, Orange County, and the Inland Empire, plus Catalina Island and the Channel Islands.",
    },
    {
        "id": "texas",
        "tier": "local",
        "title": "Texas",
        "zim_file": "osm-texas.zim",
        "description": "Texas, USA &mdash; from the Gulf Coast to the Rio Grande, including Houston, Dallas, San Antonio, Austin, Fort Worth, and El Paso.",
    },
    {
        "id": "colorado",
        "tier": "local",
        "title": "Colorado",
        "zim_file": "osm-colorado.zim",
        "description": "The Rocky Mountain state &mdash; Denver, Aspen, Vail, Rocky Mountain National Park, and the Continental Divide.",
    },
    {
        "id": "silicon-valley",
        "tier": "local",
        "title": "Silicon Valley",
        "zim_file": "osm-silicon-valley.zim",
        "description": "San Francisco Bay Area &mdash; San Francisco, Oakland, Palo Alto, Mountain View, Stanford, Cupertino, San Jose, and the Peninsula.",
    },
    {
        "id": "taipei",
        "tier": "local",
        "title": "Taipei",
        "zim_file": "osm-taipei.zim",
        "description": "Taipei &mdash; Taipei City and the heart of New Taipei: Ximending, Zhongshan, Xinyi and Taipei 101, Beitou, Tamsui, Banqiao, Xindian and Yangmingshan; search a street sign or shop name with a photo.",
    },
    {
        "id": "osaka",
        "tier": "local",
        "title": "Osaka",
        "zim_file": "osm-osaka.zim",
        "description": "Osaka &mdash; central Osaka: Umeda, Namba and Dotonbori, Shinsaibashi, Tennoji, Osaka Castle, Shin-Osaka and the bay; search a street sign or shop name with a photo.",
    },
    {
        "id": "tehran",
        "tier": "local",
        "title": "Tehran",
        "zim_file": "osm-tehran.zim",
        "description": "Tehran &mdash; Tehran city: Azadi Tower, the Grand Bazaar and Golestan Palace, Valiasr Street, Tajrish and the north, the University of Tehran; search a street sign or shop name with a photo.",
    },
    {
        "id": "cairo",
        "tier": "local",
        "title": "Cairo",
        "zim_file": "osm-cairo.zim",
        "description": "Cairo &mdash; Cairo and Giza: Tahrir Square and the Egyptian Museum, Islamic Cairo and Al-Muizz Street, Khan el-Khalili, Zamalek, Heliopolis and the pyramids of Giza; search a street sign or shop name with a photo.",
    },
    {
        "id": "athens",
        "tier": "local",
        "title": "Athens",
        "zim_file": "osm-athens.zim",
        "description": "Athens &mdash; Athens and Piraeus: the Acropolis, Plaka and Monastiraki, Syntagma, Kolonaki, Exarchia and the port of Piraeus; search a street sign or shop name with a photo.",
    },
    {
        "id": "moscow",
        "tier": "local",
        "title": "Moscow",
        "zim_file": "osm-moscow.zim",
        "description": "Moscow &mdash; Moscow inside the ring road: the Kremlin and Red Square, Tverskaya, Arbat, Zamoskvorechye, Sparrow Hills and Moscow State University; search a street sign or shop name with a photo.",
    },
    {
        "id": "kyiv",
        "tier": "local",
        "title": "Kyiv",
        "zim_file": "osm-kyiv.zim",
        "description": "Kyiv &mdash; Kyiv on both banks of the Dnipro: Maidan Nezalezhnosti, Khreshchatyk, St Sophia's, Podil, the Pechersk Lavra and the left bank; search a street sign or shop name with a photo.",
    },
    {
        "id": "seoul",
        "tier": "local",
        "title": "Seoul",
        "zim_file": "osm-seoul.zim",
        "description": "Seoul &mdash; Seoul: Gyeongbokgung and Insadong, Myeongdong, Itaewon, Hongdae, the Han River, Gangnam and Jamsil; search a street sign or shop name with a photo.",
    },
    {
        "id": "washington-dc",
        "tier": "local",
        "title": "Washington, D.C.",
        "zim_file": "osm-washington-dc.zim",
        "description": "Washington, D.C. &mdash; the U.S. capital and surrounding metro area.",
    },
    {
        "id": "hispaniola",
        "tier": "local",
        "title": "Hispaniola",
        "zim_file": "osm-hispaniola.zim",
        "description": "The Caribbean island of Hispaniola &mdash; Haiti and the Dominican Republic.",
    },
    # ── Added 2026-09-22: the European country split + africa-light ──
    # europe is 61 GB; these carve the same data so nobody downloads a
    # continent to get one country. Registry rows in cloud/regions.tsv.
    {
        "id": "france", "tier": "country", "title": "France",
        "zim_file": "osm-france.zim",
        "description": "France &mdash; Paris, Lyon, Marseille, Toulouse, Bordeaux, Nice, Strasbourg; the Alps, the Pyrenees, the Loire and Rhône valleys, Corsica.",
    },
    {
        "id": "germany", "tier": "country", "title": "Germany",
        "zim_file": "osm-germany.zim",
        "description": "Germany &mdash; Berlin, Hamburg, Munich, Cologne, Frankfurt, Stuttgart; the Rhine and Danube, the Black Forest, the Bavarian Alps, the Baltic and North Sea coasts.",
    },
    {
        "id": "italy", "tier": "country", "title": "Italy",
        "zim_file": "osm-italy.zim",
        "description": "Italy &mdash; Rome, Milan, Naples, Turin, Florence, Venice, Bologna; the Alps and Dolomites, the Apennines, Sicily and Sardinia.",
    },
    {
        "id": "poland", "tier": "country", "title": "Poland",
        "zim_file": "osm-poland.zim",
        "description": "Poland &mdash; Warsaw, Kraków, Łódź, Wrocław, Poznań, Gdańsk; the Baltic coast, the Masurian lakes, the Tatras.",
    },
    {
        "id": "greece", "tier": "country", "title": "Greece",
        "zim_file": "osm-greece.zim",
        "description": "Greece &mdash; Athens, Thessaloniki, Patras, Heraklion; the Peloponnese, Crete, the Cyclades and Dodecanese, Mount Olympus.",
    },
    {
        "id": "britain-ireland", "tier": "multi-country", "title": "Britain & Ireland",
        "zim_file": "osm-britain-ireland.zim",
        "description": "The British Isles &mdash; London, Manchester, Birmingham, Glasgow, Edinburgh, Dublin, Cardiff, Belfast; the Highlands, Snowdonia, the Lake District, the Wild Atlantic Way.",
    },
    {
        "id": "iberia", "tier": "multi-country", "title": "Iberia",
        "zim_file": "osm-iberia.zim",
        "description": "Spain, Portugal &amp; Andorra &mdash; Madrid, Barcelona, Valencia, Seville, Lisbon, Porto; the Pyrenees, the Meseta, the Algarve, the Balearics.",
    },
    {
        "id": "benelux", "tier": "multi-country", "title": "Benelux",
        "zim_file": "osm-benelux.zim",
        "description": "Netherlands, Belgium &amp; Luxembourg &mdash; Amsterdam, Rotterdam, The Hague, Brussels, Antwerp, Ghent, Luxembourg City; the Randstad, the Ardennes, the North Sea coast.",
    },
    {
        "id": "nordics", "tier": "multi-country", "title": "Nordics",
        "zim_file": "osm-nordics.zim",
        "description": "Norway, Sweden, Finland &amp; Denmark &mdash; Oslo, Stockholm, Helsinki, Copenhagen, Bergen, Gothenburg; the fjords, Lapland, the Baltic archipelagos.",
    },
    {
        "id": "austria-czech", "tier": "multi-country", "title": "Austria, Czechia & Slovakia",
        "zim_file": "osm-austria-czech.zim",
        "description": "Austria, Czechia &amp; Slovakia &mdash; Vienna, Prague, Bratislava, Brno, Salzburg, Graz, Innsbruck; the Eastern Alps, Bohemia, the Tatras.",
    },
    {
        "id": "balkans", "tier": "multi-country", "title": "Balkans",
        "zim_file": "osm-balkans.zim",
        "description": "Slovenia, Croatia, Bosnia, Serbia, Montenegro, Albania, North Macedonia &amp; Kosovo &mdash; Belgrade, Zagreb, Sarajevo, Ljubljana, Split, Tirana; the Dalmatian coast, the Dinaric Alps.",
    },
    {
        "id": "carpathians", "tier": "multi-country", "title": "Romania, Hungary & Bulgaria",
        "zim_file": "osm-carpathians.zim",
        "description": "Romania, Hungary &amp; Bulgaria &mdash; Bucharest, Budapest, Sofia, Cluj-Napoca, Plovdiv, Timișoara; the Carpathians, the Danube delta, the Black Sea coast.",
    },
    {
        "id": "africa-light", "tier": "continent", "title": "Africa (Light)",
        "zim_file": "osm-africa-light.zim",
        "description": "The whole of Africa without satellite imagery and capped at zoom 13 &mdash; the same coverage as Africa in roughly half the download. Built for slower connections and smaller devices.",
    },
    {
        "id": "north-africa", "tier": "multi-country", "title": "North Africa",
        "zim_file": "osm-north-africa.zim",
        "description": "Morocco, Algeria, Tunisia, Libya, Mauritania &amp; Western Sahara &mdash; Casablanca, Algiers, Tunis, Tripoli, Marrakesh, Fes; the Atlas mountains, the Mediterranean coast, the northern Sahara.",
    },
    {
        "id": "horn-of-africa", "tier": "multi-country", "title": "Horn of Africa",
        "zim_file": "osm-horn-of-africa.zim",
        "description": "Ethiopia, Somalia, Eritrea, Djibouti, South Sudan &amp; northern Kenya &mdash; Addis Ababa, Mogadishu, Asmara, Djibouti City, Juba; the Ethiopian highlands, the Rift Valley, the Red Sea coast.",
    },
    {
        "id": "central-africa", "tier": "multi-country", "title": "Central Africa",
        "zim_file": "osm-central-africa.zim",
        "description": "DR Congo, Cameroon, Chad, Central African Republic, Gabon, Congo, Equatorial Guinea, Angola &amp; Zambia &mdash; Kinshasa, Luanda, Douala, Yaound&eacute;, N'Djamena, Lusaka; the Congo basin, the Albertine Rift.",
    },
    {
        # Not Africa. Grouped into the same build queue only because its OSM
        # extract was prepared in the same pass; the queue's name is a
        # misnomer, the region is not.
        "id": "east-polynesia", "tier": "multi-country", "title": "East Polynesia",
        "zim_file": "osm-east-polynesia.zim",
        "description": "French Polynesia, the Cook Islands &amp; the Austral and Tuamotu archipelagos &mdash; Papeete, Bora Bora, Mo&lsquo;orea, Rarotonga; atolls and volcanic islands across 3,000 km of the South Pacific.",
    },
    # ── India in six zones (2026-11-05), each with a Light variant ─────
    {
        "id": "india-northwest", "tier": "local", "title": "Northwest India",
        "zim_file": "osm-india-northwest.zim",
        "description": "Delhi, Punjab, Haryana, Chandigarh, Rajasthan, Himachal Pradesh, Jammu &amp; Kashmir and Ladakh &mdash; New Delhi, Jaipur, Amritsar, Ludhiana, Chandigarh, Shimla, Srinagar, Leh, Jodhpur and Udaipur; the Thar Desert, the Punjab plains and the western Himalaya.",
    },
    {
        "id": "india-central", "tier": "local", "title": "Uttar Pradesh & Central India",
        "zim_file": "osm-india-central.zim",
        "description": "Uttar Pradesh, Uttarakhand, Madhya Pradesh and Chhattisgarh &mdash; Lucknow, Kanpur, Varanasi, Agra and the Taj Mahal, Prayagraj, Dehradun, Rishikesh, Bhopal, Indore, Gwalior and Raipur; the Ganges plain, the Garhwal and Kumaon Himalaya, and the forests of Kanha.",
    },
    {
        "id": "india-east", "tier": "local", "title": "East India",
        "zim_file": "osm-india-east.zim",
        "description": "West Bengal, Bihar, Jharkhand and Odisha &mdash; Kolkata, Patna, Ranchi, Bhubaneswar, Cuttack, Puri, Gaya, Siliguri and Darjeeling; the Ganges delta and the Sundarbans.",
    },
    {
        "id": "india-northeast", "tier": "local", "title": "Northeast India",
        "zim_file": "osm-india-northeast.zim",
        "description": "Assam, Meghalaya, Arunachal Pradesh, Nagaland, Manipur, Mizoram, Tripura and Sikkim &mdash; Guwahati, Shillong, Gangtok, Itanagar, Kohima, Imphal, Aizawl and Agartala; the Brahmaputra valley, Kaziranga and the eastern Himalaya.",
    },
    {
        "id": "india-south", "tier": "local", "title": "South India",
        "zim_file": "osm-india-south.zim",
        "description": "Tamil Nadu, Kerala, Karnataka, Andhra Pradesh, Telangana and Puducherry, with Lakshadweep and the Andaman and Nicobar Islands &mdash; Chennai, Bengaluru, Hyderabad, Kochi, Thiruvananthapuram, Mysuru, Madurai, Visakhapatnam and Port Blair; the Western Ghats and the Kerala backwaters.",
    },
    {
        "id": "india-west", "tier": "local", "title": "West India",
        "zim_file": "osm-india-west.zim",
        "description": "Maharashtra, Gujarat, Goa, and Dadra and Nagar Haveli and Daman and Diu &mdash; Mumbai, Pune, Ahmedabad, Surat, Nagpur, Nashik, Vadodara and Panaji; the Konkan coast, the Western Ghats and the Rann of Kutch.",
    },
    {
        "id": "india-northwest-light", "tier": "local", "title": "Northwest India (Light)",
        "zim_file": "osm-india-northwest-light.zim",
        "description": "Northwest India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
    {
        "id": "india-central-light", "tier": "local", "title": "Uttar Pradesh & Central India (Light)",
        "zim_file": "osm-india-central-light.zim",
        "description": "Uttar Pradesh &amp; Central India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
    {
        "id": "india-east-light", "tier": "local", "title": "East India (Light)",
        "zim_file": "osm-india-east-light.zim",
        "description": "East India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
    {
        "id": "india-northeast-light", "tier": "local", "title": "Northeast India (Light)",
        "zim_file": "osm-india-northeast-light.zim",
        "description": "Northeast India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
    {
        "id": "india-south-light", "tier": "local", "title": "South India (Light)",
        "zim_file": "osm-india-south-light.zim",
        "description": "South India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
    {
        "id": "india-west-light", "tier": "local", "title": "West India (Light)",
        "zim_file": "osm-india-west-light.zim",
        "description": "West India without satellite imagery and capped at zoom 13 &mdash; the same places in a smaller download, for slower connections and smaller devices.",
    },
]


def human_size(bytes_count):
    """Format bytes as 'X.X GB' or 'X MB'.

    Binary units (1024-based) to match what Finder / Explorer show for
    the downloaded file; archive.org's own listing uses decimal, so its
    numbers read ~7% larger.
    """
    if bytes_count is None or bytes_count <= 0:
        return ""
    gb = bytes_count / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = bytes_count / (1024 ** 2)
    if mb < 1:
        return "<1 MB"  # was "0 MB" for anything under a mebibyte
    return f"{int(round(mb))} MB"


def _all_listed(data, docs):
    """archive.org lists at most `rows` items and says how many matched
    (numFound). A region past the cap would render as not live, with no
    error (the cap was 100 until 2026-10-13, with 78 items), so stop."""
    found = (data.get("response") or {}).get("numFound")
    if isinstance(found, int) and found > len(docs):
        raise SystemExit(f"archive.org listed {len(docs)} of {found} streetzim-* items: raise rows")


def fetch_archive_items():
    """Query Archive.org for all streetzim-* items. Returns {region_id: metadata}."""
    # NOTE: don't include `&fl[]=title` here. archive.org's search index
    # populates fields incrementally for new items — `title` can lag the
    # `identifier`/`item_size` fields by hours. With `title` in the
    # field list, a brand-new item is silently filtered OUT of the
    # response. We don't actually use the search-side title (live-card
    # rendering uses the static REGIONS[].title), so dropping it makes
    # generate.py see new items as soon as their identifier indexes.
    # Bug seen 2026-04-25 with Egypt — first ~hour after upload it had
    # a title in metadata but not in the search index.
    url = (
        "https://archive.org/advancedsearch.php?"
        "q=identifier%3Astreetzim-*"
        "&fl%5B%5D=identifier"
        "&fl%5B%5D=item_size"
        "&fl%5B%5D=publicdate"
        "&rows=1000"
        "&output=json"
    )
    print(f"Querying Archive.org: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim-generate/1.0"})
    data = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            break
        except Exception as exc:  # noqa: BLE001 — transient 5xx/timeouts
            if attempt == 3:
                raise
            wait = 5 * attempt
            print(f"  search attempt {attempt} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    docs = data.get("response", {}).get("docs", [])
    _all_listed(data, docs)
    by_id = {}
    for doc in docs:
        identifier = doc.get("identifier", "")
        if not identifier.startswith("streetzim-"):
            continue
        region_id = identifier.replace("streetzim-", "", 1)
        by_id[region_id] = doc
    print(f"Found {len(by_id)} items on Archive.org")
    return by_id


_ITEM_DETAILS = {}     # identifier -> its listing, fetched once per build
STALE_LISTINGS = {}    # identifier -> when the last good listing used for it was fetched
# The last good listing of each item, for a build while archive.org's metadata
# API fails for it: up to LISTING_MAX_AGE_S old.
LISTING_MAX_AGE_S = 7 * 86400


def _listing_cache(identifier):
    d = os.environ.get("STREETZIM_IA_LISTINGS") or os.path.join(SCRIPT_DIR, "..", ".cache", "ia-listings")
    return os.path.join(d, identifier + ".json")


def _keep_listing(identifier, data):
    """The listing archive.org just gave, kept (best effort) for a build
    while its metadata API fails for the item."""
    p = _listing_cache(identifier)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched": time.time(), "listing": data}, fh)
        os.replace(tmp, p)
    except OSError as e:
        print(f"  Warning: could not keep {identifier}'s listing: {e}")


def _last_good_listing(identifier):
    """(listing, fetched) of the item's last good listing, or (None, None)
    when there is none, it cannot be read or it is too old."""
    try:
        with open(_listing_cache(identifier), encoding="utf-8") as fh:
            kept = json.load(fh)
        listing, fetched = kept["listing"], float(kept["fetched"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(listing, dict) or not isinstance(listing.get("files"), list):
        return None, None
    if not 0 <= time.time() - fetched <= LISTING_MAX_AGE_S:
        return None, None
    return listing, fetched


def fetch_item_details(identifier, *, retries=3):
    """Fetch the file list for an item to find the actual ZIM file size.

    Retries on transient network errors (SSL handshake timeouts,
    connection resets, etc.) with exponential backoff. Without this,
    a single TLS hiccup during the upload's site-regen step caused
    the dated-filename detection to fall back to the undated
    canonical URL — which doesn't exist on the item, so users got
    "page not found" when clicking Download.

    Returns None only after every attempt fails. Callers MUST treat
    a None return as a hard failure (don't render that region's
    card with a guessed URL — the URL won't work).

    Every real item lists files (its _meta.xml at least): an answer
    without a file list ({"error": "item metadata may be invalid"} while
    the item's server is down, {} for an identifier archive.org does not
    know) is a failed fetch, never an item without a ZIM, which would take
    a live map's card off the site as "upcoming" (streetzim-africa,
    2026-10-31). A listing is fetched once per build, so every language's
    page is made from the same one.

    Each listing archive.org gives is kept (.cache/ia-listings/, or
    $STREETZIM_IA_LISTINGS); when every attempt fails, the item's last good
    listing, up to a week old, is used instead (and named in
    STALE_LISTINGS): archive.org's metadata API failed for Africa, Europe and
    Kyiv for over a day (2026-10-31 to 11-01) while their files downloaded,
    and the refusal held every site deploy back, the builder page's new
    recipes included. Without one, None as before.
    """
    if identifier in _ITEM_DETAILS:
        return _ITEM_DETAILS[identifier]
    url = f"https://archive.org/metadata/{identifier}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "streetzim-generate/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            if not isinstance(data, dict) or not isinstance(data.get("files"), list):
                raise RuntimeError(f"archive.org gave no file list: {str(data)[:120]}")
            _ITEM_DETAILS[identifier] = data
            _keep_listing(identifier, data)
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
                print(
                    f"  Warning: fetch {identifier} attempt "
                    f"{attempt}/{retries} failed: {e} "
                    f"(retrying in {wait}s)"
                )
                time.sleep(wait)
    listing, fetched = _last_good_listing(identifier)
    if listing is not None:
        hours = (time.time() - fetched) / 3600
        print(f"  WARNING: archive.org did not give {identifier}'s listing after {retries} attempts ({last_err}); "
              f"using the last good one, {hours:.0f} h old")
        _ITEM_DETAILS[identifier] = listing
        STALE_LISTINGS[identifier] = fetched
        return listing
    print(f"  ERROR: failed to fetch {identifier} after {retries} attempts: {last_err}")
    return None


# Per-feature badge rendered on each region card. Keys mirror the
# metadata fields `cloud/stamp_item_metadata.py` sets on archive.org
# items after a successful upload. When a field is absent from the
# item metadata the badge is hidden — users on older pre-stamping
# ZIMs get no false advertising.
FEATURE_BADGES = [
    ("streetzim_routing",   "Routing & Directions", "nav"),
    ("streetzim_overture",  "Rich place info",      "overture"),
    ("streetzim_terrain",   "3D Terrain",           "terrain"),
    ("streetzim_satellite", "Satellite",            "satellite"),
    ("streetzim_wikidata",  "Wikipedia links",      "wiki"),
    ("streetzim_photo_search", "Photo search",      "photo"),
]


def render_feature_badges(item_meta, lang="en"):
    """Return an HTML fragment listing the features an item advertises.

    `item_meta` is the top-level `metadata` block of an archive.org
    metadata response (each field is a string). We treat "yes" (case-
    insensitive) as enabled and anything else — including the field
    being absent entirely — as disabled. No badges → empty string so
    the card doesn't get an empty row on stamping-less older items.
    """
    if not item_meta:
        return ""
    pills = []
    for key, label, css in FEATURE_BADGES:
        # English: the label as here; another language: its site.<lang>.json
        # badge_<css> (HTML, as the page key's badges)
        label = escape(label) if lang == "en" else t(lang, f"badge_{css}")
        val = str(item_meta.get(key, "")).lower()
        if val == "yes":
            pills.append(
                f'<span class="map-badge badge-{css}">{label}</span>'
            )
    if not pills:
        return ""
    return '\n        <div class="map-card-badges">' + "".join(pills) + "</div>"


def check_filter_css(html: str) -> None:
    """Refuse to write a page whose filter box cannot hide anything.

    The filter sets `card.hidden = true`; `.maps [hidden] { display: none
    !important }` is what makes that visible, because `.map-card` sets
    `display: flex` and so outranks the UA stylesheet's own [hidden] rule.
    On 2026-09-27 that override had been written inside
    `.maps-tier-header:first-child { ... }`, which browsers read as CSS
    nesting -- it resolved to a selector that matches nothing, and typing in
    the box visibly did nothing at all for as long as that shipped.

    Nothing in the pipeline noticed, because the JS was correct and the
    attribute really was set. So check the stylesheet's shape here, where it
    costs nothing, and gate the behaviour in cloud/site_filter_gate.mjs.
    """
    if ".maps-filter" not in html:            # no filter box on this page
        return
    if not re.search(r"^  \.maps \[hidden\] \{[^}]*display:\s*none", html, re.M):
        raise ValueError(
            "the `.maps [hidden]` override is missing or indented as a nested "
            "rule; the download-page filter would set `hidden` on cards that "
            "stay visible (see cloud/site_filter_gate.mjs)")
    block = re.search(r"\n  \.maps-tier-header:first-child \{(.*?)\n  \}",
                      html, re.S)
    if block and re.search(r"^\s+[.#][\w-]+.*\{", block.group(1), re.M):
        raise ValueError(
            "a selector is nested inside `.maps-tier-header:first-child`; "
            "that is how the filter CSS was disabled on 2026-09-27")


def _bdecode(data, pos=0):
    """Minimal bencode decoder (ints, byte strings, lists, dicts)."""
    c = data[pos:pos + 1]
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if c == b"l":
        out, pos = [], pos + 1
        while data[pos:pos + 1] != b"e":
            v, pos = _bdecode(data, pos)
            out.append(v)
        return out, pos + 1
    if c == b"d":
        out, pos = {}, pos + 1
        while data[pos:pos + 1] != b"e":
            k, pos = _bdecode(data, pos)
            v, pos = _bdecode(data, pos)
            out[k] = v
        return out, pos + 1
    colon = data.index(b":", pos)
    n = int(data[pos:colon])
    return data[colon + 1:colon + 1 + n], colon + 1 + n


def torrent_file_name(region_id):
    """`info.name` of web/torrents/<id>.torrent, or None if unreadable."""
    path = os.path.join(SCRIPT_DIR, "torrents", f"{region_id}.torrent")
    try:
        with open(path, "rb") as f:
            meta, _ = _bdecode(f.read())
        name = meta.get(b"info", {}).get(b"name")
        return name.decode("utf-8", "replace") if name else None
    except (OSError, ValueError, KeyError, AttributeError):
        return None


_DATED_ZIM_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})[a-z]?\.zim$")


def zim_build_date(zim_filename, file_meta=None):
    """When this ZIM's data is from, as (iso, human) — or None.

    The filename's date is what a reader actually wants: it is the day the
    ZIM was built, i.e. the vintage of the OSM/Overture data inside. Fall
    back to archive.org's per-file mtime (upload time) for the handful of
    legacy files with no date in the name.
    """
    m = _DATED_ZIM_RE.search(zim_filename or "")
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.isoformat(), d.strftime("%-d %b %Y")
        except ValueError:
            pass
    try:
        ts = int((file_meta or {}).get("mtime"))
        d = datetime.datetime.utcfromtimestamp(ts).date()
        return d.isoformat(), d.strftime("%-d %b %Y")
    except (TypeError, ValueError):
        return None


PREVIEW_CONFIG_PATH = os.path.join(SCRIPT_DIR, "drive", "preview-config.js")


def preview_proxy_url(path=PREVIEW_CONFIG_PATH):
    """The range proxy configured in web/drive/preview-config.js, or "".

    A Preview button only makes sense when the /drive/ picker can really
    stream an archive.org ZIM, and that takes the proxy: archive.org's
    download servers send no CORS headers (docs/online-preview.md). So the
    catalog reads the same setting the picker uses instead of a second
    flag that could drift from it.
    """
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return ""
    m = re.search(r"STREETZIM_PREVIEW_PROXY\s*=\s*(['\"])(.*?)\1", src)
    return m.group(2).strip() if m else ""


def build_recipes_dir():
    """web/build/recipes/ (where ops/langpack/publish_site.sh puts recipes)."""
    return os.path.join(SCRIPT_DIR, "build", "recipes")


FRESH_FORMAT = "streetzim-fresh-viewer/1"
FRESH_PATH = re.compile(r"^/merged/([a-z0-9-]+)-viewer/[a-z]{2,3}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                        r"-[0-9a-f]{12}/[A-Za-z0-9._-]+\.zim$")


def merged_origin():
    """The build worker's origin, as the site names it to the pages
    (web/drive/preview-config.js STREETZIM_MERGED_ORIGIN); '' without one."""
    try:
        with open(os.path.join(SCRIPT_DIR, "drive", "preview-config.js"), encoding="utf-8") as f:
            m = re.search(r"STREETZIM_MERGED_ORIGIN\s*=\s*'(https://[A-Za-z0-9.-]+)'", f.read())
    except OSError:
        return ""
    return m.group(1) if m else ""


def fresh_viewer(region_id, zim_filename, zim_md5, recipes_dir=None, origin=None):
    """The map with today's viewer that the build worker serves
    (tools/langpack/fresh_viewer.py: build/recipes/<region>-viewer/fresh.json
    and its segment index), when it was made from this very live file (name and
    MD5): {"url", "file", "viewer"}; else None (a newer map is live and has
    no fresh viewer yet, or there is none: the card offers archive.org's
    file, as before). docs/fresh-viewer.md."""
    d = os.path.join(recipes_dir or build_recipes_dir(), f"{region_id}-viewer")
    try:
        with open(os.path.join(d, "fresh.json"), encoding="utf-8") as f:
            fr = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(fr, dict) or fr.get("format") != FRESH_FORMAT or fr.get("region") != region_id:
        return None
    base = fr.get("base") or {}
    if base.get("file") != zim_filename or not zim_md5 or str(base.get("md5") or "").lower() != str(zim_md5).lower():
        return None
    if not os.path.isfile(os.path.join(d, f"{fr.get('lang')}.ws.json")):
        return None
    url = str(fr.get("webseed") or "")
    u = urllib.parse.urlsplit(url)
    origin = merged_origin() if origin is None else origin
    m = FRESH_PATH.match(u.path)
    if (not origin or f"{u.scheme}://{u.netloc}" != origin or u.query or u.fragment or not m
            or m.group(1) != region_id or not url.endswith("/" + str(fr.get("file")))):
        return None
    return {"url": url, "file": fr["file"], "viewer": fr.get("viewer")}


def build_page_regions(recipes_dir=None):
    """{region id: {"file", "md5"}} of the base of every region published
    for the builder page (/build/): web/build/recipes/<id>/region.json, as
    ops/langpack/publish_site.sh leaves it; {} when there is none.

    A region is offered only while archive.org serves that very file (name
    and MD5) as one of the item's two newest ZIMs (builder_link): the
    builder downloads the base, archive.org's keep-2 cleanup deletes older
    ones, and an in-place re-upload keeps the name.
    """
    out = {}
    recipes_dir = recipes_dir or build_recipes_dir()
    try:
        names = sorted(os.listdir(recipes_dir))
    except OSError:
        return out
    for rid in names:
        try:
            with open(os.path.join(recipes_dir, rid, "region.json"), encoding="utf-8") as f:
                r = json.load(f)
            if r["id"] == rid and r["menu"]:
                out[rid] = {"file": str(r["base"]["file"]), "md5": str(r["base"].get("md5") or "")}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


# A ZIM file name's date (osm-greece-2026-10-15.zim, a same-day re-roll
# 2026-10-15b), as the live-file pick and cloud/cleanup_old_zims.py read it.
ZIM_DATE = re.compile(r'-(\d{4}-\d{2}(?:-\d{2})?[a-z]?)\.zim$')
# The ZIMs an item keeps (cloud/cleanup_old_zims.py --keep 2; a round keeps
# 3, so 2 is the safe count): an older one is being deleted, though the
# listing may name it until archive.org's delete task has run.
BUILDER_KEEP = 2


def newest_zims(item_files, keep=BUILDER_KEEP):
    """The names of the `keep` newest dated ZIMs of an item's listing."""
    dated = []
    for f in item_files or []:
        name = str(f.get("name") or "") if isinstance(f, dict) else ""
        m = ZIM_DATE.search(name)
        if m and "history/" not in name:
            dated.append((m.group(1), name))
    return {name for _, name in sorted(dated)[-keep:]}


def builder_link(builder, region_id, zim_filename, zim_md5, item_files=None):
    """Whether a live card links to the builder page: the region's recipes
    (build_page_regions) were made from this very ZIM, by name and MD5, or
    from one archive.org still serves and keeps (`item_files`, the item's
    listing: by name and MD5, one of its two newest ZIMs): a newer upload (a
    viewer swap, a rebuild) keeps the previous base until keep-2 deletes it,
    so the region stays on the builder page with its working recipes until
    ship_packs.sh publishes new ones."""
    b = builder.get(region_id)
    if not b or not b["md5"]:
        return False
    if zim_filename and zim_md5 and b["file"] == zim_filename and b["md5"] == str(zim_md5).lower():
        return True
    return b["file"] in newest_zims(item_files) and any(
        isinstance(f, dict) and f.get("name") == b["file"] and str(f.get("md5") or "").lower() == b["md5"]
        for f in item_files or [])


# region id -> True when the builder may offer it (filled while the pages are
# rendered; write_build_index writes the page's region list from it)
BUILDER_LIVE = {}
# region id -> the live ZIM, where the builder still offers the one before it
BUILDER_NEWER = {}
# region id -> whether the builder's base map has search with a photo: the
# item's streetzim_photo_search (cloud/stamp_item_metadata.py), for the live
# ZIM, so only where the recipes are for that ZIM (not BUILDER_NEWER)
BUILDER_PHOTO = {}


# Every live map as the site's cards show it, for web/library/catalog.json
# (write_library_catalog): filled by _write_page, one entry per region.
LIBRARY: dict[str, dict] = {}
PLACES_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "cloud", "region-places.tsv")
M49_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "cloud", "m49-countries.tsv")


def library_dir():
    """web/library/, read when called: tests point SCRIPT_DIR elsewhere, and
    a path fixed at import wrote their made-up maps into the real catalog."""
    return os.path.join(SCRIPT_DIR, "library")


def _tsv_rows(path, ncols):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) != ncols:
                raise ValueError(f"{path}: want {ncols} columns, got {len(cols)}: {line!r}")
            yield [c.strip() for c in cols]


def load_m49(path=M49_PATH):
    """{country: (UN M49 region, continent)} from cloud/m49-countries.tsv."""
    return {c: (r, k) for c, r, k in _tsv_rows(path, 3)}


def load_places(path=PLACES_PATH, m49=None):
    """{region id: {continents, regions, countries, subregion, areas}} from
    cloud/region-places.tsv: "@Europe" / "@Western Africa" expanded to the
    countries of cloud/m49-countries.tsv, the regions and continents those
    countries are in (plus the row's "also"). A country the table does not
    know is an error."""
    m49 = load_m49() if m49 is None else m49
    all_regions = {r for r, _ in m49.values()}
    all_continents = {k for _, k in m49.values()}
    split = lambda v: [x.strip() for x in v.split(";") if x.strip()]  # noqa: E731
    out: dict[str, dict] = {}
    for rid, countries, sub, areas, also in _tsv_rows(path, 5):
        names: list[str] = []
        for c in split(countries):
            if c.startswith("@"):            # every country of a continent or region
                if c[1:] not in all_regions | all_continents:
                    raise ValueError(f"{path}: {rid}: no continent or M49 region {c[1:]!r}")
                names += sorted(n for n, (r, k) in m49.items() if c[1:] in (r, k))
            elif c in m49:
                names.append(c)
            else:
                raise ValueError(f"{path}: {rid}: {c!r} is not in {os.path.basename(M49_PATH)}")
        names = list(dict.fromkeys(names))
        extra = split(also)
        bad = [x for x in extra if x not in all_regions | all_continents]
        if bad:
            raise ValueError(f"{path}: {rid}: also {bad} is no continent or M49 region")
        regs = {m49[c][0] for c in names} | (set(extra) & all_regions)
        conts = {m49[c][1] for c in names} | (set(extra) & all_continents)
        out[rid] = {"continents": sorted(conts), "regions": sorted(regs), "countries": names,
                    "subregion": sub, "areas": split(areas)}
    return out


def plain_text(html_text: str) -> str:
    """A card description (HTML entities, the odd tag) as plain text."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", html_text))).strip()


def builder_versions(recipes_dir=None):
    """{region id: [{lang, label, size, webseed}]}: the language versions
    the builder page offers, from the build/recipes/index.json this site
    build wrote (write_build_index); {} without one."""
    try:
        with open(os.path.join(recipes_dir or build_recipes_dir(), "index.json"), encoding="utf-8") as f:
            regions = json.load(f).get("regions") or []
    except (OSError, ValueError):
        return {}
    return {r["id"]: [{"lang": m["lang"], "label": m.get("label") or m["lang"], "size": m.get("size"),
                       "webseed": m.get("webseed")} for m in r.get("menu") or []]
            for r in regions if isinstance(r, dict) and r.get("id")}


def write_library_catalog(library, versions, places=None, out_dir=None, m49=None):
    """web/library/catalog.json: every live map (`library`, from
    _write_page) with where it is (cloud/region-places.tsv) and the
    builder's versions of it in other languages (`versions`, from
    builder_versions), and the UN M49 region and continent of every country
    named: the whole of what the library page (web/library/index.html)
    reads, one small file for a phone. A live map without a places row is
    listed without places (search still finds it) and named in a warning:
    the main page must not wait for this one. Returns the ids."""
    m49 = load_m49() if m49 is None else m49
    places = load_places(m49=m49) if places is None else places
    nowhere = {"continents": [], "regions": [], "countries": [], "subregion": "", "areas": []}
    missing = sorted(rid for rid in library if rid not in places)
    if missing:
        print(f"WARNING: no row in cloud/region-places.tsv for live maps {missing}: the library page "
              "lists them without places; add a row (id, countries, subregion, areas, also)")
    maps = [{**library[rid], "size_label": human_size(library[rid].get("size")),
             "places": places.get(rid, nowhere), "builder": bool(versions.get(rid)),
             "versions": [{**v, "size_label": human_size(v.get("size"))} for v in versions.get(rid) or []]}
            for rid in sorted(library, key=lambda r: (library[r]["title"].casefold(), r))]
    named = sorted({c for m in maps for c in m["places"]["countries"]})
    countries = {c: m49[c][0] for c in named if c in m49}
    regions = dict(m49.values())          # M49 region -> continent
    out_dir = out_dir or library_dir()
    os.makedirs(out_dir, exist_ok=True)
    doc = {"format": "streetzim-library/1", "generated": datetime.date.today().isoformat(),
           "countries": countries, "regions": regions, "maps": maps}
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="catalog.json.", suffix=".tmp")
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(out_dir, "catalog.json"))
    return [m["id"] for m in maps]


def write_build_index(live, recipes_dir=None, newer=None, photo=None):
    """web/build/recipes/index.json: the published regions whose base
    archive.org serves (`live`: {region id: True}; builder_link), by title,
    each with "newer": the live ZIM where that is a newer one (`newer`) than
    the recipes' base. Written at every site build, so a pruned or
    re-uploaded base leaves the builder page with the next deploy. Each
    region in `photo` ({id: bool}) gets "photo_search": whether its base
    map has search with a photo (the builder page marks it). Returns the
    region ids listed."""
    recipes_dir = recipes_dir or build_recipes_dir()
    if not os.path.isdir(recipes_dir):
        return []
    regions = []
    for rid in sorted(os.listdir(recipes_dir)):
        try:
            with open(os.path.join(recipes_dir, rid, "region.json"), encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("id") == rid and r.get("menu") and live.get(rid):
            if (newer or {}).get(rid):
                r["newer"] = newer[rid]
            if rid in (photo or {}):
                r["photo_search"] = bool(photo[rid])
            regions.append(r)
    regions.sort(key=lambda r: (str(r.get("title", "")).casefold(), r["id"]))
    idx = {"format": "streetzim-build-index/1", "generated": datetime.date.today().isoformat(),
           "regions": regions}
    fd, tmp = tempfile.mkstemp(dir=recipes_dir, prefix="index.json.", suffix=".tmp")   # a name of its own
    os.fchmod(fd, 0o644)                  # a site file: mkstemp's 0600 otherwise
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(recipes_dir, "index.json"))
    return [r["id"] for r in regions]


def card_text(lang):
    """A card's button labels and titles in a language (site.<lang>.json card_*)."""
    return {k: t(lang, "card_" + k) for k in ("download", "preview", "preview_title", "info", "updated",
                                             "building", "languages", "languages_title")}


def render_live_card(region, size_label, item_meta=None, torrent_ok=True,
                     build_date=None, preview=False, lang="en", build_page=False, fresh=None, map_page=None):
    """Render a map card with active download/torrent/details buttons.

    `torrent_ok=False` drops the Torrent button: the committed .torrent
    describes a different (older) file than the Download button, so
    handing it out would seed a build that archive.org's keep-2 cleanup
    is about to delete.

    `preview=True` adds a Preview button that opens the ZIM in the /drive/
    viewer streamed off archive.org (see preview_proxy_url).

    `build_page=True` adds "Build in another language", the builder page
    for this region (see build_page_regions).

    `fresh` (fresh_viewer): Download is the map with today's viewer, which
    the build worker serves; data-fallback names archive.org's file, which
    the page downloads instead when the worker cannot serve it (template.html).

    `map_page`: the map's own page (write_map_pages), which its title links to.
    """
    tx = card_text(lang)
    item_id = f"streetzim-{region['id']}"
    zim_file = region["zim_file"]
    zim_file_attr = escape(urllib.parse.quote(zim_file), quote=True)
    title_attr = escape(region["title"], quote=True)
    # Quoted once, as a whole: the picker decodes the query value once
    # and matches it against the archive.org download URL shape.
    preview_target = urllib.parse.quote(
        f"https://archive.org/download/{item_id}/{zim_file}", safe=":/")
    preview_html = (
        f'\n          <a class="btn btn-secondary" '
        f'href="/drive/?zim={escape(preview_target, quote=True)}" '
        f'data-track="preview" data-region="{region["id"]}" data-title="{title_attr}" '
        f'title="{escape(tx["preview_title"], quote=True)}">{tx["preview"]}</a>'
        if preview else ""
    )
    build_html = (
        f'\n          <a class="btn btn-secondary" '
        f'href="/build/?region={escape(urllib.parse.quote(region["id"]), quote=True)}" '
        f'data-track="build" data-region="{region["id"]}" data-title="{title_attr}" '
        f'title="{escape(tx["languages_title"], quote=True)}">{tx["languages"]}</a>'
        if build_page else ""
    )
    badges_html = render_feature_badges(item_meta, lang)
    # Without this the only way to tell a refreshed ZIM from the one you
    # already downloaded was to fetch it and look inside.
    date_html = ""
    if build_date:
        iso, human = build_date
        human = fmt_date(lang, iso, human)
        date_html = (f'\n        <div class="map-card-date">{tx["updated"]} '
                     f'<time datetime="{iso}">{escape(human)}</time></div>')
    torrent_html = (
        f'\n          <a class="btn btn-secondary" href="/torrents/{region["id"]}.torrent" '
        f'data-track="torrent" data-region="{region["id"]}" data-title="{title_attr}">Torrent</a>'
        if torrent_ok else ""
    )
    archive_url = f"https://archive.org/download/{item_id}/{zim_file_attr}"
    title_html = escape(region["title"])
    if map_page:
        title_html = f'<a href="{escape(map_page, quote=True)}">{title_html}</a>'
    if fresh:
        download_html = (
            f'<a class="btn btn-primary" href="{escape(fresh["url"], quote=True)}" data-fallback="{archive_url}" '
            f'data-track="download" data-region="{region["id"]}" data-title="{title_attr}">{tx["download"]}</a>')
    else:
        download_html = (
            f'<a class="btn btn-primary" href="{archive_url}" '
            f'data-track="download" data-region="{region["id"]}" data-title="{title_attr}">{tx["download"]}</a>')
    return f"""      <div class="map-card">
        <div class="map-card-head">
          <div class="map-card-title">{title_html}</div>
          <div class="map-card-size">{size_label}</div>
        </div>{date_html}
        <p class="map-card-desc">{region["description"]}</p>{badges_html}
        <div class="map-card-links">
          {download_html}{preview_html}{build_html}{torrent_html}
          <a class="btn btn-secondary" href="https://archive.org/details/{item_id}" data-track="details" data-region="{region["id"]}" data-title="{title_attr}">{tx["info"]}</a>
        </div>
      </div>"""


def render_upcoming_card(region, lang="en"):
    """Render a dimmed card for regions that haven't been uploaded yet."""
    title, desc = region_text(lang, region)
    return f"""      <div class="map-card upcoming">
        <div class="map-card-head">
          <div class="map-card-title">{escape(title)}</div>
          <div class="map-card-size">{t(lang, "card_building")}</div>
        </div>
        <p class="map-card-desc">{desc}</p>
      </div>"""


def render_tier_header(label: str) -> str:
    """A full-width row inside the .maps grid that visually separates
    one tier of regions from the next. ``grid-column: 1 / -1`` makes
    the header span every column regardless of viewport width."""
    return (
        f'      <h3 class="maps-tier-header">{escape(label)}</h3>'
    )


def maps_only_pages(maps):
    """Set _MAPS_ONLY_PAGES (each maps-only language's maps: live English maps
    with a version in it, _has_version) and return the maps-only languages
    with no page this build: no such map and no live map made in it."""
    for lang in MAPS_ONLY:
        _MAPS_ONLY_PAGES[lang] = {e["region"]["id"] for e in maps
                                  if e["live"] and e["region"].get("language", "en") == "en" and _has_version(e, lang)}
        unnamed = sorted(rid for rid in _MAPS_ONLY_PAGES[lang] if rid not in _i18n_file("regions", lang))
        if unnamed:
            print(f"  WARNING [{lang}] maps with a version but no name in regions.{lang}.json "
                  f"(listed in English): {unnamed}")
    made_in = {e["region"].get("language") for e in maps if e["live"]}
    dropped = [code for code, _n, _s in ALL_LANGUAGES
               if code in MAPS_ONLY and not _MAPS_ONLY_PAGES.get(code) and code not in made_in]
    if dropped:
        print(f"Maps-only languages with no map in them yet (no pages): {dropped}")
    return dropped


def build_page():
    LIBRARY.clear()                       # this build's live maps only
    archive_items = fetch_archive_items()

    # Validate that every region declares a known tier, fail loud if
    # not — silently rendering an unknown-tier region in a default
    # bucket would mask a config typo for weeks.
    valid_tiers = {tid for tid, _ in TIERS}
    for region in REGIONS:
        if region.get("tier") not in valid_tiers:
            raise ValueError(
                f"region {region['id']!r} has invalid tier "
                f"{region.get('tier')!r}; valid tiers: {sorted(valid_tiers)}")
        # Titles go through html.escape(); an entity written into one
        # renders literally ("Britain &amp; Ireland" shipped that way).
        if re.search(r"&(#?\w+);", region["title"]):
            raise ValueError(
                f"region {region['id']!r} title {region['title']!r} contains "
                "an HTML entity; write the plain character, titles are escaped")

    # REGIONS here and cloud/regions.tsv are two lists of the same fact, and
    # nothing used to check they agreed. africa-light shipped to archive.org
    # and never appeared on the site; the twelve European country builds
    # would have done the same. A region that has an archive.org item but no
    # card is invisible to every user, so fail the build rather than deploy
    # a page that silently omits it.
    registry_ids = set()
    tsv = os.path.join(os.path.dirname(SCRIPT_DIR), "cloud", "regions.tsv")
    if os.path.exists(tsv):
        with open(tsv, encoding="utf-8") as fh:
            tsv_lines = fh.read().splitlines()
        for line in tsv_lines:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) > 3:
                registry_ids.add(parts[0])
    page_ids = {r["id"] for r in REGIONS}
    # `rid`, NOT f"streetzim-{rid}": fetch_archive_items() strips the prefix
    # when it builds the dict (see region_id above), so the prefixed form was
    # never a key and this gate was unconditionally false from the day it was
    # written. It did not protect the four african regions added in 0d93c58 --
    # horn-of-africa and central-africa both went public before that commit
    # and would have deployed cardless in silence. Proven against the live
    # index: prefixed -> [], bare -> ['greece'] with greece's card removed.
    shipped_missing = sorted(
        rid for rid in registry_ids - page_ids
        if rid in archive_items)
    if shipped_missing:
        raise ValueError(
            "these regions are live on archive.org but have no card in "
            f"REGIONS, so the site would not list them: {shipped_missing}. "
            "Add an entry to web/generate.py REGIONS.")
    unshipped = sorted(registry_ids - page_ids)
    if unshipped:
        print(f"note: in regions.tsv but not yet on the site (not shipped "
              f"yet, fine): {unshipped}")

    preview = bool(preview_proxy_url())
    print("Preview buttons: " + ("on (proxy " + preview_proxy_url() + ")" if preview
          else "off (no proxy in web/drive/preview-config.js)"))
    # The builder page streams through the same proxy.
    builder = build_page_regions() if preview else {}
    print(f"Builder links: {len(builder)} regions published in web/build/recipes/"
          + ("" if preview else " (off: no proxy)"))
    maps = collect_maps(archive_items, builder, preview)
    dropped = maps_only_pages(maps)
    LANGUAGES[:] = [x for x in ALL_LANGUAGES if x[0] not in dropped]
    for lang, _name, sub in LANGUAGES:
        _write_page(maps, lang, sub, preview, builder_on=bool(builder))
    write_map_pages(maps, preview, builder_on=bool(builder))
    remove_dropped_languages()
    write_site_css()
    write_sitemap(maps)
    listed = []
    if os.path.isdir(build_recipes_dir()):
        listed = write_build_index(BUILDER_LIVE, newer=BUILDER_NEWER, photo=BUILDER_PHOTO)
        print(f"Builder page lists {len(listed)} regions whose base archive.org serves: {listed}")
    if os.path.isfile(os.path.join(library_dir(), "index.html")):
        n = write_library_catalog(LIBRARY, builder_versions() if listed else {})
        print(f"Library catalog: {len(n)} maps")


def _sub(lang):
    """A language's path under web/ ("" for English, "de/")."""
    return "" if lang == "en" else f"{lang}/"


def render_lang_picker(lang, path=""):
    """The language links at the top of each page (the current one marked):
    the same page (`path` after the language's folder, "" for its home page)
    in every language."""
    links = []
    for code, name, sub in LANGUAGES:
        if not has_page(code, path):
            continue
        cur = ' aria-current="page"' if code == lang else ""
        links.append(f'<a href="/{sub}{path}" hreflang="{code}" lang="{code}"{cur}>{escape(name)}</a>')
    # Folded: 22 names take seven rows of a phone's screen. The links stay in
    # the page (search engines follow them), shown when it is opened.
    return (f'    <details class="lang-menu"><summary>{escape(site_text(lang)["lang_name"])}</summary>\n'
            f'    <nav class="lang-picker" aria-label="{t_attr(lang, "lang_picker_aria")}">'
            + "".join(links) + "</nav></details>")


def render_head_links(lang, path="", title="", description=""):
    """The <head> links of a page (`path` after the language's folder): its
    canonical URL, <link rel=alternate hreflang> for every language and
    x-default (English), and Open Graph title, description and URL."""
    url = f"{SITE_URL}{_sub(lang)}{path}"
    out = [f'<link rel="canonical" href="{url}">']
    out += [f'<link rel="alternate" hreflang="{code}" href="{SITE_URL}{sub}{path}">' for code, _n, sub in LANGUAGES
            if has_page(code, path)]
    out.append(f'<link rel="alternate" hreflang="x-default" href="{SITE_URL}{path}">')
    out.append('<meta property="og:type" content="website">')
    out.append('<meta property="og:site_name" content="StreetZim">')
    out.append(f'<meta property="og:url" content="{url}">')
    if title:
        out.append(f'<meta property="og:title" content="{title}">')
    if description:
        out.append(f'<meta property="og:description" content="{description}">')
    return "\n".join(out)


# The English strings a page in another language replaces with its own (the
# map list's intro and step 2 say what its downloads are: the map in that language).
LANG_VARIANTS = {"maps_intro": "maps_intro_lang", "step2_p": "step2_p_lang"}


def fill_text(template, lang, **values):
    """{{t:key}} (HTML) and {{a:key}} (inside an attribute) in a template, in a language."""
    def one(m):
        key = m.group(2)
        if lang != "en":
            key = LANG_VARIANTS.get(key, key)
        return t_attr(lang, key, **values) if m.group(1) == "a" else t(lang, key, **values)
    return re.sub(r"\{\{([ta]):([a-z0-9_]+)\}\}", one, template)


def _dir_attr(lang):
    return ' dir="rtl"' if lang in RTL_LANGS else ""


def _int(v):
    # archive.org occasionally reports size as None / "" for a file still
    # being processed; a bare int() here aborted the whole build.
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _zim_sort_key(f):
    """A listed ZIM's place among an item's files: dated ones over undated,
    newest date first, size only to break ties among same-date uploads.
    Rebuilds can shrink a ZIM (Overture deduping trims OSM duplicates); size
    alone stale-stuck a 5.5 GB April-13 ZIM over a fresh 210 MB April-22 one.
    A single-letter same-day re-roll suffix (b, c, d...) sorts after the plain
    date: `2026-04-26 < 2026-04-26b < 2026-04-26c`; without [a-z]? the c file
    read as undated and the plain one won on size."""
    name = f.get("name", "")
    m = re.search(r'-(\d{4}-\d{2}(?:-\d{2})?[a-z]?)\.zim$', name)
    if m:
        # (dated=1, date_str, size). YYYY-MM < YYYY-MM-DD lexicographically.
        return (1, m.group(1), _int(f.get("size")))
    return (0, "", _int(f.get("size")))


def collect_maps(archive_items, builder, preview=False):
    """Every region of REGIONS as the pages show it, read from archive.org
    once for all the languages: [{"region" (its "zim_file" the live one),
    "live", "size", "meta" (the item's metadata), "torrent_ok", "built"
    ((iso, human) or None), "fresh", "archive_url", "build_ok"}]. Fills
    LIBRARY and the BUILDER_* tables. Exits (2) when an item's metadata could
    not be read: a card with an undated canonical URL (the 2026-05-10
    California "page not found") is worse than no deploy."""
    out = []
    # Items whose metadata fetch failed even after retries. We KNOW these
    # exist (they showed up in the search results): a transient network
    # issue, not a missing item.
    failed_metadata = []
    for region in REGIONS:
        e = {"region": region, "live": False}
        out.append(e)
        if not archive_items.get(region["id"]):
            continue
        # The ZIM's name + size from archive.org: names are dated
        # (osm-europe-2026-04.zim), so found in the listing, not hardcoded.
        details = fetch_item_details(f"streetzim-{region['id']}")
        if details is None:
            failed_metadata.append(region["id"])
            continue
        zim_size = zim_filename = zim_file_meta = None
        if details:
            zim_files = [f for f in details.get("files", [])
                         if f.get("name", "").endswith(".zim") and "history/" not in f.get("name", "")]
            if zim_files:
                best = max(zim_files, key=_zim_sort_key)
                zim_filename = best.get("name")
                zim_file_meta = best
                zim_size = _int(best.get("size")) or None
        if not zim_filename:
            # Item exists (dark / just created / mid-upload) but lists no
            # .zim yet. Rendering it live linked the undated osm-<id>.zim,
            # which never exists on a dated item (2026-05-10 California).
            print(f"  {region['id']}: no .zim in item listing yet, treating as upcoming")
            continue
        region = {**region, "zim_file": zim_filename}
        e["region"] = region
        if zim_size is None:
            zim_size = _int(archive_items[region["id"]].get("item_size")) or None
        if not zim_size or zim_size <= 0:
            # The file is still uploading / being processed: upcoming, so the
            # page doesn't show "0 MB" until the upload finalizes.
            print(f"  {region['id']}: file not yet finalized, treating as upcoming")
            continue
        # The Torrent button must point at the same file as the Download
        # button. Torrents are regenerated separately and drift behind
        # rebuilds; a stale one seeds a ZIM the keep-2 cleanup will delete.
        torrent_name = torrent_file_name(region["id"])
        torrent_ok = torrent_name == zim_filename
        if not torrent_ok:
            print(f"  {region['id']}: torrent names {torrent_name!r} but the "
                  f"live ZIM is {zim_filename!r} — omitting Torrent button "
                  f"(regenerate with cloud/generate_all_torrents.py)")
        item_id = f"streetzim-{region['id']}"
        md5 = (zim_file_meta or {}).get("md5")
        fresh = fresh_viewer(region["id"], zim_filename, md5)
        archive_url = f"https://archive.org/download/{item_id}/{urllib.parse.quote(zim_filename)}"
        built = zim_build_date(zim_filename, zim_file_meta)
        meta = (details or {}).get("metadata") if details else None
        build_ok = builder_link(builder, region["id"], zim_filename, md5, (details or {}).get("files"))
        e.update(live=True, size=zim_size, meta=meta, torrent_ok=torrent_ok, built=built, fresh=fresh,
                 archive_url=archive_url, build_ok=build_ok)
        LIBRARY[region["id"]] = {
            "id": region["id"], "title": region["title"], "description": plain_text(region["description"]),
            "tier": region["tier"], "language": region.get("language", "en"),
            "file": zim_filename, "size": zim_size, "date": built[0] if built else None,
            "download": fresh["url"] if fresh else archive_url,
            "fallback": archive_url if fresh else None,
            "details": f"https://archive.org/details/{item_id}",
            "torrent": f"/torrents/{region['id']}.torrent" if torrent_ok else None,
            "preview": ("/drive/?zim=" + urllib.parse.quote(f"https://archive.org/download/{item_id}/{zim_filename}", safe=":/"))
                       if preview else None}
        if region["id"] in builder:
            BUILDER_LIVE[region["id"]] = BUILDER_LIVE.get(region["id"], False) or build_ok
            if build_ok and (builder.get(region["id"]) or {}).get("file") == zim_filename:
                BUILDER_PHOTO[region["id"]] = str((meta or {}).get("streetzim_photo_search", "")).lower() == "yes"
            if not build_ok:
                print(f"  {region['id']}: the builder's recipes are for {builder[region['id']]}, which archive.org "
                      f"no longer serves (live ZIM {zim_filename!r}) — off the builder page "
                      f"(run ops/langpack/ship_packs.sh)")
            elif builder[region["id"]]["file"] != zim_filename:
                BUILDER_NEWER[region["id"]] = zim_filename
                print(f"  {region['id']}: the builder's recipes are for {builder[region['id']]['file']}, still on "
                      f"archive.org beside the live {zim_filename!r}: kept on the builder page until new ones")
    if failed_metadata:
        print(f"\nERROR: {len(failed_metadata)} item(s) failed metadata "
              f"fetch even after retries: {failed_metadata}", file=sys.stderr)
        print("Refusing to write site — re-run when archive.org's "
              "metadata API is reachable.", file=sys.stderr)
        sys.exit(2)
    return out


def _on_page(region, lang):
    """Whether a language's home page lists a region: English lists the
    English maps; another language those (in its words) and the maps made in it."""
    rl = region.get("language", "en")
    return rl == lang or (lang != "en" and rl == "en")


_VERSIONS = {}


def lang_versions(region_id, recipes_dir=None, origin=None):
    """The region's language versions on the builder page, from its recipes
    (web/build/recipes/<id>/region.json): {lang: {"size", "url" (the build
    worker's download of the whole file, when it can serve it: its segment
    index <lang>.ws.json published, on the site's worker), "builder" (the
    builder page, building it in the browser), "torrent" (or None), "built"
    (the date of the map it was made from, (iso, human), or None)}}."""
    recipes_dir = recipes_dir or build_recipes_dir()
    key = (recipes_dir, region_id)
    if key in _VERSIONS:
        return _VERSIONS[key]
    out = {}
    d = os.path.join(recipes_dir, region_id)
    origin = merged_origin() if origin is None else origin
    try:
        with open(os.path.join(d, "region.json"), encoding="utf-8") as f:
            r = json.load(f)
        menu = r.get("menu") or []
        base_file = str((r.get("base") or {}).get("file") or "")
    except (OSError, ValueError, AttributeError):
        menu, base_file = [], ""
    for m in menu:
        if not isinstance(m, dict) or not re.fullmatch(r"[a-z]{2,3}", str(m.get("lang", ""))):
            continue
        lang = m["lang"]
        url = str(m.get("webseed") or "")
        u = urllib.parse.urlsplit(url)
        if not (origin and f"{u.scheme}://{u.netloc}" == origin and not u.query and not u.fragment
                and u.path.startswith(f"/merged/{region_id}/{lang}/") and u.path.endswith(".zim")
                and os.path.isfile(os.path.join(d, f"{lang}.ws.json"))):
            url = ""
        out[lang] = {
            "size": _int(m.get("size")),
            "built": zim_build_date(base_file) if base_file else None,
            "url": url,
            "builder": f"/build/?region={urllib.parse.quote(region_id)}&lang={lang}&go=1",
            "torrent": (f"/build/recipes/{region_id}/{lang}.torrent"
                        if os.path.isfile(os.path.join(d, f"{lang}.torrent")) else None)}
    _VERSIONS[key] = out
    return out


def _version(e, lang):
    """A live map's version in a language (lang_versions), or None."""
    return lang_versions(e["region"]["id"]).get(lang) if e.get("build_ok") else None


def _has_version(e, lang):
    """Whether a live map has a version in `lang` a page can offer: the map
    in that language with its size (the map page offers none without)."""
    v = _version(e, lang)
    return bool(v and v.get("size"))


def _original(e):
    """(href, fallback or None) of a live map's own file: the build worker's
    copy with today's viewer where there is one (fresh_viewer), archive.org's
    file as its fallback; else archive.org's."""
    if e["fresh"]:
        return e["fresh"]["url"], e["archive_url"]
    return e["archive_url"], None


def _a(href, label, cls, fallback=None, title=None, track=None, rid="", name="", lang=None):
    attrs = [f'class="btn {cls}"', f'href="{escape(href, quote=True)}"']
    if fallback:
        attrs.append(f'data-fallback="{escape(fallback, quote=True)}"')
    if track:
        attrs.append(f'data-track="{track}" data-region="{rid}" data-title="{escape(name, quote=True)}"')
    if lang:
        attrs.append(f'data-lang="{lang}"')
    if title:
        attrs.append(f'title="{title}"')
    return f'<a {" ".join(attrs)}>{label}</a>'


def _preview_href(e):
    return "/drive/?zim=" + urllib.parse.quote(
        f"https://archive.org/download/streetzim-{e['region']['id']}/{e['region']['zim_file']}", safe=":/")


def render_lang_card(e, lang, preview=False, builder_on=True):
    """An English map's card on another language's home page: the map in that
    language (lang_versions: the build worker's file, the builder page when it
    cannot serve it), the original file beside it; without a version in the
    language, the original and a note saying so. The title links to the
    map's page in the language."""
    region = e["region"]
    rid = region["id"]
    name, desc = region_text(lang, region)
    v = _version(e, lang)
    page = f"/{_sub(lang)}maps/{rid}/"
    href, fb = _original(e)
    date_html = ""
    if e["built"]:
        iso, human = e["built"]
        date_html = (f'\n        <div class="map-card-date">{t(lang, "card_updated")} '
                     f'<time datetime="{iso}">{escape(fmt_date(lang, iso, human))}</time></div>')
    links = []
    note = ""
    if v:
        links.append(_a(v["url"] or v["builder"], t(lang, "card_download_lang"), "btn-primary",
                        fallback=v["builder"] if v["url"] else None, title=t_attr(lang, "card_download_lang_title"),
                        track="download_lang", rid=rid, name=name, lang=lang))
        links.append(_a(href, t(lang, "card_original"), "btn-secondary", fallback=fb,
                        title=t_attr(lang, "card_original_title"), track="download", rid=rid, name=name))
    else:
        links.append(_a(href, t(lang, "card_download"), "btn-primary", fallback=fb, track="download", rid=rid, name=name))
        if builder_on:
            note = f'\n        <p class="map-card-note">{t(lang, "card_no_lang")}</p>'
    if preview:
        links.append(_a(_preview_href(e), t(lang, "card_preview"), "btn-secondary",
                        title=t_attr(lang, "card_preview_title"), track="preview", rid=rid, name=name))
    links.append(f'<a class="btn btn-secondary" href="{page}">{t(lang, "card_map_page")}</a>')
    size = fmt_size(lang, v["size"] if v and v["size"] else e["size"])
    return f"""      <div class="map-card">
        <div class="map-card-head">
          <div class="map-card-title"><a href="{page}">{escape(name)}</a></div>
          <div class="map-card-size">{size}</div>
        </div>{date_html}
        <p class="map-card-desc">{desc}</p>{render_feature_badges(e["meta"], lang)}{note}
        <div class="map-card-links">
          {"".join(links)}
        </div>
      </div>"""


def _write_page(maps, lang, sub, preview=False, builder_on=True):
    """Render and write one language's home page (web/<sub>index.html). An
    English map whose name in the language is that of a map made in it (the
    German "Schweiz und die Alpen") is listed once, as the map made in it; its
    page in the language (/<lang>/maps/<id>/) still offers its version."""
    def title_key(e):
        return re.sub(r"&[a-z]+;", " ", region_text(lang, e["region"])[0]).strip().casefold()
    by_tier: dict[str, list] = {tid: [] for tid, _ in TIERS}
    native = {region_text(lang, e["region"])[0] for e in maps
              if lang != "en" and e["region"].get("language", "en") == lang}
    for e in maps:
        if not _on_page(e["region"], lang):
            continue
        if lang in MAPS_ONLY and e["region"]["id"] not in _MAPS_ONLY_PAGES.get(lang, ()) \
                and e["region"].get("language", "en") != lang:
            continue                      # a maps-only language lists its maps alone
        if e["region"].get("language", "en") != lang and region_text(lang, e["region"])[0] in native:
            print(f"  [{lang}] {e['region']['id']}: listed as the map made in {lang} of the same name")
            continue
        by_tier[e["region"]["tier"]].append(e)
    cards = []
    live_count = upcoming_count = 0
    for tier_id, tier_label in TIERS:
        group = sorted(by_tier.get(tier_id) or [], key=title_key)
        if not group:
            continue
        cards.append(render_tier_header(tier_label if lang == "en"
                                        else t(lang, "tier_" + tier_id.replace("-", "_"))))
        for e in group:
            region = e["region"]
            if not e["live"]:
                cards.append(render_upcoming_card(region, lang))
                upcoming_count += 1
                continue
            live_count += 1
            if lang != "en" and region.get("language", "en") == "en":
                cards.append(render_lang_card(e, lang, preview, builder_on))
                continue
            cards.append(render_live_card(
                region, fmt_size(lang, e["size"]), item_meta=e["meta"], torrent_ok=e["torrent_ok"],
                build_date=e["built"], preview=preview, lang=lang, build_page=e["build_ok"], fresh=e["fresh"],
                map_page=f"/maps/{region['id']}/" if lang == "en" and region.get("language", "en") == "en" else None))
    print(f"[{lang}] Rendered {live_count} live, {upcoming_count} upcoming")

    # An empty-but-200 search response (archive.org index hiccup) would
    # otherwise render every card as "upcoming" and --deploy would wipe
    # the download links from the live site.
    if live_count == 0:
        print("\nERROR: archive.org search returned no live items — "
              "refusing to write a page with zero download links",
              file=sys.stderr)
        sys.exit(2)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    updated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = fill_text(template, lang, updated=updated, wikipedia=f"https://{lang}.wikipedia.org/")
    html = html.replace("{{LANG}}", lang).replace("{{DIR}}", _dir_attr(lang))
    html = html.replace("{{LANG_PICKER}}", render_lang_picker(lang))
    html = html.replace("{{HEAD_LINKS}}", render_head_links(
        lang, "", t_attr(lang, "html_title"), t_attr(lang, "meta_description")))
    rest = html.replace("{{MAPS}}", "")
    if "{{" in rest:
        raise ValueError(f"[{lang}] the page has an unfilled placeholder: "
                         f"{rest[rest.index('{{'):rest.index('{{') + 60]!r}")
    html = html.replace("{{MAPS}}", "\n".join(cards))
    check_filter_css(html)

    # Explicit UTF-8: the template and region descriptions carry
    # non-cp1252 characters (Lāna, İzmir, Þingvellir), so the locale
    # default raised UnicodeEncodeError on Windows / C-locale hosts.
    out_path = os.path.join(SCRIPT_DIR, sub, "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path}")


MAP_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "map_template.html")


def template_parts(path=None):
    """What the map pages share with the home page (template.html): its
    stylesheet, its analytics script and its download-fallback script."""
    with open(path or TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    css = re.search(r"<style>\n(.*?)</style>", tpl, re.S)
    analytics = re.search(r'<script type="module">.*?</script>', tpl, re.S)
    fallback = [m.group(0) for m in re.finditer(r"<script>.*?</script>", tpl, re.S) if "a[data-fallback]" in m.group(0)]
    if not (css and analytics and len(fallback) == 1):
        raise ValueError("template.html: its <style>, analytics <script type=module> or download-fallback "
                         "<script> (a[data-fallback]) is not where write_map_pages looks for it")
    return {"css": css.group(1), "analytics": analytics.group(0), "fallback": fallback[0]}


def write_site_css():
    """web/site.css: the home page's stylesheet, for the map pages."""
    path = os.path.join(SCRIPT_DIR, "site.css")
    with open(path, "w", encoding="utf-8") as f:
        f.write(template_parts()["css"])


def render_kiwix_guide(lang):
    """How to install Kiwix and open a map in it, per platform."""
    def steps(*keys):
        return "<ol>" + "".join(f"<li>{t(lang, k)}</li>" for k in keys) + "</ol>"
    return (f'<h2>{t(lang, "kg_h2")}</h2>\n<div class="kiwix-guide">\n'
            f'<h3>{t(lang, "kg_android_h3")}</h3>{steps("kg_android_1", "kg_android_2", "kg_android_3")}\n'
            f'<h3>{t(lang, "kg_ios_h3")}</h3>{steps("kg_ios_1", "kg_ios_2", "kg_ios_3")}\n'
            f'<h3>{t(lang, "kg_mac_h3")}</h3><p>{t(lang, "kg_mac_1")}</p>\n'
            f'<h3>{t(lang, "kg_pc_h3")}</h3><p>{t(lang, "kg_pc_1")}</p>\n'
            f'<p>{t(lang, "kg_offline")}</p>\n</div>')


def render_map_page(e, lang, template, parts, preview=False, builder_on=True):
    """A live English map's own page in a language (/<lang>/maps/<id>/). Its
    main download: in English the map's own file (as the home page's card);
    in another language the map in that language (lang_versions), the own
    file beside it. Each download has its torrent beside it and the date of
    the data it was made from."""
    region = e["region"]
    rid = region["id"]
    name, desc = region_text(lang, region)
    ename = escape(name)
    v = _version(e, lang) if lang != "en" else None
    if v and not v["size"]:
        v = None
    href, fb = _original(e)
    body = [f'<p class="lead">{t(lang, "mp_lead", name=ename)}</p>', f"<p>{desc}</p>",
            f'<h2>{t(lang, "mp_download_h2")}</h2>', '<div class="map-dl">']

    def when(built):
        if not built:
            return ""
        iso, human = built
        return t(lang, "mp_updated", date=f'<time datetime="{iso}">{escape(fmt_date(lang, iso, human))}</time>') + " "

    def torrent(url):
        return ('<div class="more">' + _a(url, t(lang, "mp_dl_torrent"), "btn-secondary", track="torrent",
                                          rid=rid, name=name) + "</div>") if url else ""
    if v:
        body.append(_a(v["url"] or v["builder"], t(lang, "mp_dl_lang", size=fmt_size(lang, v["size"])), "btn-primary",
                       fallback=v["builder"] if v["url"] else None, track="download_lang", rid=rid, name=name, lang=lang))
        body.append(f'<p>{when(v["built"])}{t(lang, "mp_dl_lang_note")}</p>{torrent(v["torrent"])}')
    elif lang != "en" and builder_on:
        body.append(f'<p>{t(lang, "card_no_lang")}</p>')
    own = (t(lang, "card_download") + f' ({fmt_size(lang, e["size"])})' if lang == "en"
           else t(lang, "mp_dl_original", size=fmt_size(lang, e["size"])))
    body.append(_a(href, own, "btn-secondary" if v else "btn-primary", fallback=fb, track="download", rid=rid, name=name))
    body.append(f'<p>{when(e["built"])}{t(lang, "mp_dl_original_note")}</p>'
                f'{torrent(f"/torrents/{rid}.torrent" if e["torrent_ok"] else None)}')
    more = []
    if preview:
        more.append(_a(_preview_href(e), t(lang, "mp_dl_preview"), "btn-secondary", track="preview", rid=rid, name=name))
    if lang == "en" and e.get("build_ok"):
        more.append(_a(f"/build/?region={urllib.parse.quote(rid)}", t(lang, "card_languages"), "btn-secondary",
                       title=t_attr(lang, "card_languages_title"), track="build", rid=rid, name=name))
    if more:
        body.append('<div class="more">' + "".join(more) + "</div>")
    body.append(f'<p>{t(lang, "mp_space_note")}</p>')
    body.append("</div>")
    feats = [t(lang, "mp_feat_search")] + [
        t(lang, f"mp_feat_{css}") for key, _l, css in FEATURE_BADGES
        if str((e["meta"] or {}).get(key, "")).lower() == "yes"]
    body.append(f'<h2>{t(lang, "mp_inside_h2")}</h2>')
    body.append('<ul class="map-feats">' + "".join(f"<li>{f}</li>" for f in feats) + "</ul>")
    body.append(render_kiwix_guide(lang))
    others = [f'<li><a href="/{sub}maps/{rid}/" hreflang="{code}" lang="{code}">{escape(lname)}</a></li>'
              for code, lname, sub in LANGUAGES if code != lang and has_page(code, f"maps/{rid}/")]
    body.append(f'<h2>{t(lang, "mp_other_langs_h2")}</h2>')
    body.append('<ul class="other-langs">' + "".join(others) + "</ul>")
    path = f"maps/{rid}/"
    title = t_attr(lang, "mp_html_title", name=ename)
    # the description promises the map in the language: only where there is that version
    description = t_attr(lang, "mp_meta_description" if v or lang == "en" else "mp_lead", name=ename)
    html = template
    for k, val in (("{{LANG}}", lang), ("{{DIR}}", _dir_attr(lang)), ("{{TITLE}}", title),
                   ("{{DESCRIPTION}}", description), ("{{HEAD_LINKS}}", render_head_links(lang, path, title, description)),
                   ("{{ANALYTICS}}", parts["analytics"]), ("{{LANG_PICKER}}", render_lang_picker(lang, path)),
                   ("{{HOME}}", f"/{_sub(lang)}"), ("{{ALL_MAPS}}", t(lang, "mp_all_maps")),
                   ("{{H1}}", t(lang, "mp_h1", name=ename)), ("{{FOOTER}}", t(lang, "footer")),
                   ("{{FALLBACK}}", parts["fallback"])):
        html = html.replace(k, val)
    if "{{" in html.replace("{{BODY}}", ""):
        rest = html.replace("{{BODY}}", "")
        raise ValueError(f"[{lang}] {rid}: the map page has an unfilled placeholder: "
                         f"{rest[rest.index('{{'):rest.index('{{') + 60]!r}")
    return html.replace("{{BODY}}", "\n".join(body))


def write_map_pages(maps, preview=False, builder_on=True):
    """A page per live English map in every language: web/maps/<id>/ and
    web/<lang>/maps/<id>/. A folder for a map no longer in REGIONS goes; one
    for a map not live just now stays (an archive.org hiccup must not take
    its pages down)."""
    with open(MAP_TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    parts = template_parts()
    live = [e for e in maps if e["live"] and e["region"].get("language", "en") == "en"]
    known = {r["id"] for r in REGIONS if r.get("language", "en") == "en"}
    n = 0
    for lang, _name, sub in LANGUAGES:
        root = os.path.join(SCRIPT_DIR, sub, "maps")
        mine = [e for e in live if has_page(lang, f"maps/{e['region']['id']}/")]
        for e in mine:
            d = os.path.join(root, e["region"]["id"])
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                f.write(render_map_page(e, lang, template, parts, preview, builder_on))
            n += 1
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for old in names:
            p = os.path.join(root, old)
            if old not in known and os.path.isfile(os.path.join(p, "index.html")):
                shutil.rmtree(p)
                print(f"  [{lang}] removed the page of {old} (no longer in REGIONS)")
            elif (lang in MAPS_ONLY and old in known and not has_page(lang, f"maps/{old}/")
                  and old in {e["region"]["id"] for e in live} and os.path.isfile(os.path.join(p, "index.html"))):
                shutil.rmtree(p)
                print(f"  [{lang}] removed the page of {old} (no version in {lang} now)")
    print(f"Map pages: {len(live)} maps in {len(LANGUAGES)} languages ({n} pages)")


def remove_dropped_languages():
    """The pages of a language no longer in web/i18n/: web/<code>/ holding
    only what this script writes there (index.html, maps/) goes; anything
    else in it keeps the folder."""
    # ALL_LANGUAGES: a maps-only language with no map just now (an archive.org
    # hiccup) keeps its pages; the picker, hreflang and sitemap leave it out.
    keep = {code for code, _n, _s in ALL_LANGUAGES}
    for code in sorted(os.listdir(SCRIPT_DIR)):
        d = os.path.join(SCRIPT_DIR, code)
        if (re.fullmatch(r"[a-z]{2,3}", code) and code not in keep and os.path.isdir(d)
                and not os.path.islink(d) and os.path.isfile(os.path.join(d, "index.html"))
                and set(os.listdir(d)) <= {"index.html", "maps"}):
            shutil.rmtree(d)
            print(f"  removed web/{code}/ (no translation in web/i18n/)")


def write_sitemap(maps):
    """web/sitemap.xml: every language's home page and map pages, each with
    its versions in the other languages (xhtml:link) and, for a map page,
    the date of the map's data (lastmod)."""
    pages = [("", None)] + [(f"maps/{e['region']['id']}/", (e["built"] or (None,))[0])
                            for e in maps if e["live"] and e["region"].get("language", "en") == "en"]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for path, lastmod in pages:
        alts = [f'    <xhtml:link rel="alternate" hreflang="{code}" href="{SITE_URL}{sub}{path}"/>'
                for code, _n, sub in LANGUAGES if has_page(code, path)]
        alts.append(f'    <xhtml:link rel="alternate" hreflang="x-default" href="{SITE_URL}{path}"/>')
        for code, _n, sub in LANGUAGES:
            if not has_page(code, path):
                continue
            out.append("  <url>")
            out.append(f"    <loc>{SITE_URL}{sub}{path}</loc>")
            if lastmod:
                out.append(f"    <lastmod>{lastmod}</lastmod>")
            out += alts
            out.append("  </url>")
    out.append("</urlset>")
    with open(os.path.join(SCRIPT_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"Sitemap: {sum(has_page(c, p) for p, _l in pages for c, _n, _s in LANGUAGES)} pages")


def deploy():
    print("Deploying to Firebase...")
    subprocess.run(
        ["firebase", "deploy", "--only", "hosting"],
        cwd=PROJECT_DIR,
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", action="store_true", help="Deploy to Firebase after generating")
    args = parser.parse_args()
    build_page()
    if args.deploy:
        deploy()
