// StreetZim online preview — configuration.
//
// Base URL of the byte-range proxy that fronts archive.org for the
// /drive/ viewer (see preview-proxy/ and docs/online-preview.md).
// archive.org's download servers honour Range requests but send no CORS
// headers, so a browser can only stream a ZIM off them through this.
//
// Leave it empty to keep the feature off: the picker then refuses
// archive.org URLs and web/generate.py renders no Preview buttons.
window.STREETZIM_PREVIEW_PROXY = 'https://streetzim-preview-proxy.tiltastech.workers.dev';

// Origin of the build worker (build-proxy/, streetzim-build-proxy): its
// /merged/<region>/<lang>/<uuid>/<file>.zim URLs serve each map's language
// versions (the /build/ page's entries) with Range and CORS, so the viewer
// can stream one like an archive.org file. Only that exact path shape on
// this origin is accepted. Empty: no language-version previews.
window.STREETZIM_MERGED_ORIGIN = 'https://streetzim-build-proxy.tiltastech.workers.dev';
