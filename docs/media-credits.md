# Whose work the site's and the README's pictures show

`web/media/` holds the home page's clips and screenshots and `docs/readme/`
the README's. Both are made by `tools/site_media.py` from captures of real
maps: nothing in them is drawn or mocked up. They are not under the code's
MIT licence: every one shows other people's work, under the licences below.
The home page lists the same credits under "Photos and imagery in the clips
and screenshots", in every language of the site.

## The maps themselves

Every picture shows map data © [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors (ODbL 1.0), in the [OpenMapTiles](https://openmaptiles.org/)
schema (CC BY 4.0). Hillshade and 3D terrain come from the Copernicus DEM
(© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided
under COPERNICUS by the European Union and ESA). Place cards and articles
quote [Wikipedia](https://www.wikipedia.org/) (CC BY-SA 4.0). The README's
licence table has the full list of what a map contains.

## Satellite imagery: non-commercial use only

EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains
modified Copernicus Sentinel data 2021),
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
Pictures that show it carry the same licence:

- `docs/readme/terrain.webp`
- `web/media/`: `kyiv-06-satellite-*`, `tehran-3d-terrain-satellite-*`,
  `taipei-3d-terrain-satellite-*`, `tour-terrain.*.mp4`, `poster-terrain.*`,
  `hero-loop.*.mp4`

## Photos

The photo searches read a photo of a sign, and the Wikipedia article shows a
photo of a landmark. The sign photos were cropped.

| shown in | photo | by | licence |
|---|---|---|---|
| `sign-ukrainian`, `tour-photo`, `docs/readme/photo-search.webp` | [street sign, Kyiv](https://commons.wikimedia.org/wiki/File:%D0%9A%D1%80%D0%B0%D1%81%D0%B8%D0%BB%D1%96%D0%B2%D1%81%D1%8C%D0%BA%D0%B0_1.jpg) | AnatolyPm | public domain |
| `sign-greek`, `photo-search-greek-*`, `tour-photo`, `docs/readme/photo-search.webp` | [street sign, Athens](https://commons.wikimedia.org/wiki/File:Plaque_Rue_Kodrou_-_Ath%C3%A8nes_(GRA1)_-_2022-03-25_-_1.jpg) | Chabe01 | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| `sign-arabic`, `photo-search-arabic-*`, `tour-photo`, `docs/readme/photo-search.webp` | [museum sign, Cairo](https://commons.wikimedia.org/wiki/File:Signage,_Egyptian_Museum,_al-Q%C4%81hirah,_CG,_EGY_(40942409173).jpg) | Warren LeMay | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `sign-persian` | [mosque sign, Tehran](https://commons.wikimedia.org/wiki/File:Central_Mosque_of_Tehran3.jpg) | BABAK1985 | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| `sign-japanese`, `photo-search-japanese-*`, `tour-photo`, `docs/readme/photo-search.webp` | [market front, Osaka](https://api.panoramax.xyz/#focus=pic&pic=f7ff2e78-4eaf-4c58-95ee-7267ff520343) | ねこねーちゃん | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| `sign-korean`, `photo-search-korean-*`, `tour-photo`, `docs/readme/photo-search.webp` | [street sign, Seoul](https://gongu.copyright.or.kr/gongu/wrt/wrt/view.do?wrtSn=11077093&menuNo=200018) | 민지애 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `sign-chinese`, `tour-photo`, `docs/readme/photo-search.webp` | [street sign, Taipei: "Road name sign of Lane 53, Zhulun Street, Taipei 20080816"](https://commons.wikimedia.org/wiki/File:Road_name_sign_of_Lane_53,_Zhulun_Street,_Taipei_20080816.jpg) | Tianmu peter | [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) |
| `sign-english`, `tour-photo`, `docs/readme/photo-search.webp` | [restaurant front, Washington, DC: "Metropolitan Square - Washington DC - Old Ebbitt Grill"](https://commons.wikimedia.org/wiki/File:Metropolitan_Square_-_Washington_DC_-_Old_Ebbitt_Grill.JPG) | Tim1965 | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) |
| `kyiv-11-wikipedia-article-*` | [Golden Gate, Kyiv](https://commons.wikimedia.org/wiki/File:Golden_Gate_Kiev_2018_G1.jpg) | George Chernilevsky | public domain |

Each sign's poster (`poster-sign-*`, `poster-photo`) shows the same photo as
its clip. A clip or screenshot that shows a share-alike photo (CC BY-SA 3.0
or 4.0) may be reused under CC BY-SA 4.0, with the credits above.

`web/media/media.json` carries these credits for the site (`credits`,
`video_credits` and each gallery entry's `credits`); a test keeps this page
and that file in step.
