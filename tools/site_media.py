#!/usr/bin/env python3
"""The home page's videos and pictures (web/media/), from capture output.

    tools/site_media.py --shots DIR --lang-shots DIR --videos DIR [--out DIR]
    tools/site_media.py --shots DIR --lang-shots DIR --videos DIR --readme [--out DIR]

--shots       desktop and phone screenshots (PNG), named as in GALLERY
--lang-shots  phone screenshots of the language versions (PNG), LANG_SHOTS
--videos      the captured clips (MP4), named as in VIDEOS

Writes every file under web/media/ with its content hash in the name (the
site serves /media/ as immutable) and web/media/media.json, which
web/generate.py reads: {"files": {logical name: {"file", "w", "h"}},
"gallery": [...], "signs": [...], "langs": [...], "credits": {...},
"video_credits": {clip: [credit, ...]}, "made_from": {logical name: a
digest of its capture and settings}}.

A file whose capture and settings are those of the run before ("made_from")
is kept as it is. An encoder does not give the same bytes on every machine
or in every version, so making it again would only rename it. Everything
new is made in a temporary folder and copied over at the end: a run that
fails leaves web/media/ as it was. A file with a hash in its name that
media.json no longer lists is then removed; other files are left alone.

--readme writes the README's pictures instead (docs/readme/, README_CLIPS
and README_STILLS): animated previews of three clips and a few stills, under
names that stay the same. It needs all three capture folders too. A picture
dropped from those tables is not removed: delete it by hand (a test fails
while it is there). docs/media-credits.md says whose work both sets show.

Needs ffmpeg with libx264 and libwebp, and ffprobe.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "media")

# The number of threads is fixed: x264 cuts a frame differently for each, and
# left to ffmpeg the number follows the machine's cores.
X264 = ["-c:v", "libx264", "-threads", "4", "-preset", "slow", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-movflags", "+faststart", "-an"]
# a file this tool wrote: a content hash before the extension
HASHED = re.compile(r"\.[0-9a-f]{8}\.(webp|mp4)$")

# logical name: (source clip, ffmpeg arguments after the input)
VIDEOS = {
    "tour-photo.mp4": ("photo-search-seven-scripts.mp4", ["-vf", "scale=1600:900:flags=lanczos", "-crf", "25"]),
    "tour-zoom.mp4": ("zoom-to-building-kyiv.mp4", ["-vf", "scale=1280:720:flags=lanczos", "-crf", "27"]),
    "tour-terrain.mp4": ("3d-terrain-tehran.mp4", ["-vf", "scale=1280:720:flags=lanczos", "-crf", "28"]),
    "tour-drive.mp4": ("driving-directions-kyiv.mp4", ["-vf", "scale=540:1168:flags=lanczos", "-crf", "28"]),
    # The home page's opening loop: the fly-in, tilt and turn of the zoom
    # clip, after its English caption has left the screen.
    "hero-loop.mp4": ("zoom-to-building-kyiv.mp4", [
        "-ss", "4.4", "-t", "16", "-vf",
        "fps=24,scale=960:540:flags=lanczos,fade=t=in:st=0:d=0.5:color=0x0b1220,"
        "fade=t=out:st=15.5:d=0.5:color=0x0b1220", "-crf", "30"]),
}
SIGN_SCRIPTS = ["ukrainian", "greek", "arabic", "persian", "japanese", "korean", "chinese", "english"]
for _s in SIGN_SCRIPTS:
    VIDEOS[f"sign-{_s}.mp4"] = (f"photo-search-{_s}.mp4", ["-vf", "scale=390:844:flags=lanczos", "-crf", "27"])

# logical name: (source, seconds into it or None for a still, width)
POSTERS = {
    "poster-photo.webp": ("shots:photo-search-seven-scripts.png", None, 1600),
    "poster-zoom.webp": ("videos:zoom-to-building-kyiv.mp4", 19.5, 1280),
    "poster-terrain.webp": ("videos:3d-terrain-tehran.mp4", 21, 1280),
    "poster-drive.webp": ("videos:driving-directions-kyiv.mp4", 20, 540),
    "poster-hero.webp": ("videos:zoom-to-building-kyiv.mp4", 4.9, 960),
}
for _s in SIGN_SCRIPTS:
    POSTERS[f"poster-sign-{_s}.webp"] = (f"shots:photo-search-{_s}-1-read.png", None, 390)

# The sign clips: script, the page language whose name labels it, the map.
SIGNS = [("ukrainian", "uk", "kyiv"), ("greek", "el", "athens"), ("arabic", "ar", "cairo"),
         ("persian", "fa", "tehran"), ("japanese", "ja", "osaka"), ("korean", "ko", "seoul"),
         ("chinese", "zh", "taipei"), ("english", "en", "washington-dc")]

# The gallery, in order: (source, kind, caption key in site.<lang>.json,
# the map's region id, the language of a language version or None, credits).
# "lang:" sources come from --lang-shots.
GALLERY = [
    ("kyiv-01-whole-city", "wide", "gal_city", "kyiv", None, []),
    ("kyiv-phone-01-search-ukrainian", "phone", "gal_search", "kyiv", None, []),
    ("kyiv-03-building-level-tilted", "wide", "gal_building", "kyiv", None, []),
    ("kyiv-05-place-card", "wide", "gal_place", "kyiv", None, []),
    ("lang:kyiv-ar-2-place", "phone", "gal_lang", "kyiv", "ar", []),
    ("kyiv-07-food-and-drink", "wide", "gal_find", "kyiv", None, []),
    ("kyiv-phone-03-route", "phone", "gal_route", "kyiv", None, []),
    ("kyiv-phone-04-directions-list", "phone", "gal_turns", "kyiv", None, []),
    ("kyiv-phone-05-driving", "phone", "gal_drive", "kyiv", None, []),
    ("kyiv-06-satellite", "wide", "gal_satellite", "kyiv", None, ["sat"]),
    ("kyiv-08-dark-theme", "wide", "gal_dark", "kyiv", None, []),
    ("tehran-hillshade", "wide", "gal_hillshade", "tehran", None, []),
    ("tehran-3d-terrain-satellite", "wide", "gal_terrain_sat", "tehran", None, ["sat"]),
    ("seoul-3d-terrain", "wide", "gal_terrain", "seoul", None, []),
    ("taipei-3d-terrain-satellite", "wide", "gal_terrain_sat", "taipei", None, ["sat"]),
    ("cairo-search-arabic", "wide", "gal_search", "cairo", None, []),
    ("lang:cairo-ar-3-route", "phone", "gal_lang", "cairo", "ar", []),
    ("lang:cairo-ar-4-directions", "phone", "gal_lang", "cairo", "ar", []),
    ("photo-search-arabic-1-read", "phone", "gal_photo", "cairo", None, ["arabic"]),
    ("photo-search-arabic-2-place", "phone", "gal_photo_found", "cairo", None, ["arabic"]),
    ("osaka-search-japanese", "wide", "gal_search", "osaka", None, []),
    ("photo-search-japanese-1-read", "phone", "gal_photo", "osaka", None, ["japanese"]),
    ("lang:kyiv-ja-2-place", "phone", "gal_lang", "kyiv", "ja", []),
    ("athens-search-greek", "wide", "gal_search", "athens", None, []),
    ("photo-search-greek-1-read", "phone", "gal_photo", "athens", None, ["greek"]),
    ("tehran-search-persian", "wide", "gal_search", "tehran", None, []),
    ("photo-search-korean-1-read", "phone", "gal_photo", "seoul", None, ["korean"]),
    ("lang:kyiv-he-2-place", "phone", "gal_lang", "kyiv", "he", []),
    ("lang:kyiv-hi-4-directions", "phone", "gal_lang", "kyiv", "hi", []),
    ("kyiv-10-wikipedia-layer", "wide", "gal_wiki_layer", "kyiv", None, []),
    ("kyiv-11-wikipedia-article", "wide", "gal_article", "kyiv", None, ["gate"]),
    ("lang:kyiv-th-4-directions", "phone", "gal_lang", "kyiv", "th", []),
    ("lang:kyiv-zh-2-place", "phone", "gal_lang", "kyiv", "zh", []),
]

# "The map in your language": two phone pictures of the Kyiv map per language.
LANG_SHOTS = [
    ("ar", ["kyiv-ar-2-place", "kyiv-ar-4-directions"]),
    ("he", ["kyiv-he-2-place", "kyiv-he-4-directions"]),
    ("hi", ["kyiv-hi-2-place", "kyiv-hi-4-directions"]),
    ("th", ["kyiv-th-3-route", "kyiv-th-4-directions"]),
    ("ja", ["kyiv-ja-2-place", "kyiv-ja-4-directions"]),
    ("zh", ["kyiv-zh-2-place", "kyiv-zh-4-directions"]),
]

SIZES = {"wide": (1600, 800), "phone": (780, 390)}

BY_SA_4 = ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/")
CREDITS = {
    "sat": {"kind": "imagery"},
    "gate": {"kind": "photo", "author": "George Chernilevsky", "licence": None,
             "source": "https://commons.wikimedia.org/wiki/File:Golden_Gate_Kiev_2018_G1.jpg"},
    "ukrainian": {"kind": "sign", "lang": "uk", "author": "AnatolyPm", "licence": None,
                  "source": "https://commons.wikimedia.org/wiki/File:"
                            "%D0%9A%D1%80%D0%B0%D1%81%D0%B8%D0%BB%D1%96%D0%B2%D1%81%D1%8C%D0%BA%D0%B0_1.jpg"},
    "greek": {"kind": "sign", "lang": "el", "author": "Chabe01", "licence": BY_SA_4,
              "source": "https://commons.wikimedia.org/wiki/File:"
                        "Plaque_Rue_Kodrou_-_Ath%C3%A8nes_(GRA1)_-_2022-03-25_-_1.jpg"},
    "arabic": {"kind": "sign", "lang": "ar", "author": "Warren LeMay",
               "licence": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
               "source": "https://commons.wikimedia.org/wiki/File:"
                         "Signage,_Egyptian_Museum,_al-Q%C4%81hirah,_CG,_EGY_(40942409173).jpg"},
    "persian": {"kind": "sign", "lang": "fa", "author": "BABAK1985", "licence": BY_SA_4,
                "source": "https://commons.wikimedia.org/wiki/File:Central_Mosque_of_Tehran3.jpg"},
    "japanese": {"kind": "sign", "lang": "ja", "author": "ねこねーちゃん", "licence": BY_SA_4,
                 "source": "https://api.panoramax.xyz/#focus=pic&pic=f7ff2e78-4eaf-4c58-95ee-7267ff520343"},
    "korean": {"kind": "sign", "lang": "ko", "author": "민지애",
               "licence": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
               "source": "https://gongu.copyright.or.kr/gongu/wrt/wrt/view.do?wrtSn=11077093&menuNo=200018"},
    "chinese": {"kind": "sign", "lang": "zh", "author": "Tianmu peter",
                "licence": ("CC BY 3.0", "https://creativecommons.org/licenses/by/3.0/"),
                "source": "https://commons.wikimedia.org/wiki/File:"
                          "Road_name_sign_of_Lane_53,_Zhulun_Street,_Taipei_20080816.jpg"},
    "english": {"kind": "sign", "lang": "en", "author": "Tim1965",
                "licence": ("CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0/"),
                "source": "https://commons.wikimedia.org/wiki/File:"
                          "Metropolitan_Square_-_Washington_DC_-_Old_Ebbitt_Grill.JPG"},
}
# Photos a clip shows that its poster or name does not say.
VIDEO_CREDITS = {"tour-photo.mp4": ["ukrainian", "greek", "arabic", "japanese", "korean", "chinese", "english"],
                 "tour-terrain.mp4": ["sat"]}

# The README's pictures. An animated preview: (source clip, ffmpeg arguments
# before the input, filter, quality); a still: (source, width).
README_OUT = os.path.join(ROOT, "docs", "readme")
README_CLIPS = {
    "photo-search.webp": ("photo-search-seven-scripts.mp4", [], "fps=8,scale=960:540:flags=lanczos", 62),
    "zoom.webp": ("zoom-to-building-kyiv.mp4", ["-t", "25"],
                  "setpts=PTS/1.6,fps=8,scale=640:360:flags=lanczos", 42),
    "driving.webp": ("driving-directions-kyiv.mp4", ["-ss", "8", "-t", "26"],
                     "fps=6,scale=280:606:flags=lanczos", 50),
}
README_STILLS = {
    "terrain.webp": ("shots:tehran-3d-terrain-satellite.png", 1200),
    "arabic.webp": ("lang:cairo-ar-4-directions.png", 600),
    "hindi.webp": ("lang:kyiv-hi-4-directions.png", 600),
    "japanese.webp": ("lang:kyiv-ja-2-place.png", 600),
}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        sys.exit(f"{' '.join(cmd[:6])} ...: {r.stderr.decode(errors='replace')[-600:]}")
    return r.stdout.decode()


def dims(path):
    w, h = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height", "-of", "csv=p=0", path]).strip().split(",")[:2]
    return int(w), int(h)


def webp(src, dst, width, at=None, quality=80):
    cmd = ["ffmpeg", "-loglevel", "error", "-y"]
    if at is not None:
        cmd += ["-ss", str(at)]
    cmd += ["-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2:flags=lanczos",
            "-c:v", "libwebp", "-quality", str(quality), dst]
    run(cmd)


def made_from(src, *settings):
    """What an output is made from: its capture's bytes and its settings."""
    h = hashlib.sha256(repr(settings).encode("utf-8"))
    with open(src, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def content_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:8]


class Store:
    """The files of web/media/, each named with the first 8 hex digits of its
    SHA-256. They are collected first and written together (write)."""

    def __init__(self, out, tmp):
        self.out, self.tmp = out, tmp
        self.files, self.paths, self.marks = {}, {}, {}
        try:
            with open(os.path.join(out, "media.json"), encoding="utf-8") as f:
                self.before = json.load(f)
        except (OSError, ValueError):
            self.before = {}

    def make(self, logical, src, settings, encode):
        """One output of a capture: the file of the run before when the
        capture and the settings are the same and the file is whole, else
        made again by encode(path)."""
        mark = made_from(src, *settings)
        was = (self.before.get("files") or {}).get(logical)
        if was and (self.before.get("made_from") or {}).get(logical) == mark:
            path = os.path.join(self.out, was["file"])
            stem, ext = os.path.splitext(logical)
            if os.path.isfile(path) and was["file"] == f"{stem}.{content_hash(path)}{ext}":
                self.files[logical], self.paths[logical], self.marks[logical] = was, path, mark
                return logical
        path = os.path.join(self.tmp, logical)
        encode(path)
        self.marks[logical] = mark
        return self.add(logical, path)

    def add(self, logical, path):
        stem, ext = os.path.splitext(logical)
        w, h = dims(path)
        self.files[logical] = {"file": f"{stem}.{content_hash(path)}{ext}", "w": w, "h": h}
        self.paths[logical] = path
        return logical

    def write(self, manifest):
        """Copy the new files and media.json into the folder, then remove
        the hashed files media.json no longer lists."""
        os.makedirs(self.out, exist_ok=True)
        for logical, f in self.files.items():
            put(self.paths[logical], os.path.join(self.out, f["file"]))
        part = os.path.join(self.out, "media.json.part")
        with open(part, "w", encoding="utf-8") as f:
            json.dump({**manifest, "files": self.files, "made_from": self.marks}, f,
                      ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(part, os.path.join(self.out, "media.json"))
        keep = {f["file"] for f in self.files.values()}
        for name in os.listdir(self.out):
            if HASHED.search(name) and name not in keep and os.path.isfile(os.path.join(self.out, name)):
                os.remove(os.path.join(self.out, name))


def put(src, dst):
    """Copy a file into place whole: a reader never sees half of it."""
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    shutil.copyfile(src, dst + ".part")
    os.replace(dst + ".part", dst)


def readme_pictures(args, source, tmp):
    out = args.out or README_OUT
    for name, (src, pre, vf, quality) in README_CLIPS.items():
        run(["ffmpeg", "-loglevel", "error", "-y", *pre, "-i", os.path.join(args.videos, src),
             "-vf", vf, "-an", "-c:v", "libwebp_anim", "-lossless", "0", "-quality", str(quality),
             "-compression_level", "6", "-loop", "0", os.path.join(tmp, name)])
    for name, (src, width) in README_STILLS.items():
        webp(source(src), os.path.join(tmp, name), width, quality=84)
    names = [*README_CLIPS, *README_STILLS]
    os.makedirs(out, exist_ok=True)
    for name in names:
        put(os.path.join(tmp, name), os.path.join(out, name))
    total = sum(os.path.getsize(os.path.join(out, n)) for n in names)
    print(f"{len(names)} files, {total / 1e6:.1f} MB in {out}")


def site_media(args, source, tmp):
    store = Store(args.out or OUT, tmp)
    for logical, (src, ff) in VIDEOS.items():
        clip = os.path.join(args.videos, src)
        pre = ff[:4] if ff[0] == "-ss" else []
        store.make(logical, clip, (ff, X264), lambda path, clip=clip, pre=pre, ff=ff: run(
            ["ffmpeg", "-loglevel", "error", "-y", *pre, "-i", clip, *ff[len(pre):], *X264, path]))
    for logical, (src, at, width) in POSTERS.items():
        store.make(logical, source(src), ("poster", at, width),
                   lambda path, src=src, at=at, width=width: webp(source(src), path, width, at))

    def picture(src, kind, small=True):
        d, _, name = src.rpartition(":")
        png = os.path.join(args.lang_shots if d == "lang" else args.shots, name + ".png")
        made = {}
        for label, width in zip(("large", "small") if small else ("large",), SIZES[kind]):
            logical = f"{name}-{width}.webp"
            quality = 82 if label == "large" else 80
            if logical not in store.files:
                store.make(logical, png, ("picture", width, quality),
                           lambda path, width=width, quality=quality: webp(png, path, width, quality=quality))
            made[label] = logical
        return made

    gallery = []
    for src, kind, caption, region, lang, credits in GALLERY:
        gallery.append({**picture(src, kind), "kind": kind, "caption": caption, "region": region,
                        "lang": lang, "credits": credits})
    # a language's first screen is on the page; the rest open from it, full size
    langs = [{"lang": lang, "shots": [picture("lang:" + s, "phone", small=i == 0) for i, s in enumerate(shots)]}
             for lang, shots in LANG_SHOTS]
    signs = [{"id": s, "lang": lang, "region": region, "video": f"sign-{s}.mp4",
              "poster": f"poster-sign-{s}.webp", "credits": [s]} for s, lang, region in SIGNS]
    store.write({"gallery": gallery, "langs": langs, "signs": signs,
                 "video_credits": VIDEO_CREDITS, "credits": CREDITS})
    hashed = [f["file"] for f in store.files.values()]
    total = sum(os.path.getsize(os.path.join(store.out, n)) for n in hashed)
    kept = sum(1 for p in store.paths.values() if os.path.dirname(p) != tmp)
    print(f"{len(hashed)} files ({kept} kept as they were), {total / 1e6:.1f} MB in {store.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", required=True)
    ap.add_argument("--lang-shots", required=True)
    ap.add_argument("--videos", required=True)
    ap.add_argument("--out", help="where to write (web/media/, or docs/readme/ with --readme)")
    ap.add_argument("--readme", action="store_true", help="write the README's pictures and nothing else")
    args = ap.parse_args()

    def source(spec):
        kind, _, name = spec.partition(":")
        return os.path.join({"shots": args.shots, "videos": args.videos, "lang": args.lang_shots}[kind], name)

    tmp = tempfile.mkdtemp(prefix="site-media-")
    try:
        (readme_pictures if args.readme else site_media)(args, source, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
