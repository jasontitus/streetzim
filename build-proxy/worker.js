// Cloudflare Workers entry point of the build worker (streetzim-build-proxy):
// the /build/ page's range proxy and the language versions' webseed, apart
// from the preview proxy so either can be switched off alone.
//
//   /merged/<region>/<lang>/<uuid>/<file>.zim   a language version, served
//                                               virtually (merged-webseed.js)
//   /<archive.org item>/<file>.zim              a ranged read of one archive.org
//                                               file (the preview proxy's handler)
//
// Deploy: README.md. Switch off: set MERGED=0 (webseed only) or OFF=1
// (everything) in wrangler.toml [vars] or the dashboard, or delete the worker.
import { handleRequest } from '../preview-proxy/ia-range-proxy.js';
import { createMergedHandler } from './merged-webseed.js';

let merged = null;

export default {
  fetch(request, env, ctx) {
    env = env || {};
    if (env.OFF === '1') return new Response('switched off\n', { status: 503, headers: { 'Access-Control-Allow-Origin': '*' } });
    const path = new URL(request.url).pathname;
    if (path.startsWith('/merged/')) {
      if (env.MERGED === '0') return new Response('language-version downloads are switched off\n', { status: 503, headers: { 'Access-Control-Allow-Origin': '*' } });
      merged = merged || createMergedHandler({ site: env.SITE_ORIGIN, upstream: env.UPSTREAM });
      return merged(request, ctx);
    }
    return handleRequest(request);
  },
};
