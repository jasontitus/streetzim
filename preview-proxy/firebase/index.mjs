// Optional: run the range proxy as a Firebase Cloud Function (2nd gen,
// i.e. Cloud Run) behind a Hosting rewrite, so the preview streams from
// streetzim.web.app/ia/<item>/<file> instead of a workers.dev host.
// Needs the Blaze plan; Hosting bills the data transfer and Cloud
// Functions the outbound bytes, which Cloudflare Workers do not. See
// docs/online-preview.md, "Running it on the StreetZim site itself".
//
// firebase.json additions (the predeploy copy is what puts the shared
// handler inside this directory — Firebase zips only the source dir):
//
//   "functions": {
//     "source": "preview-proxy/firebase",
//     "codebase": "preview-proxy",
//     "predeploy": ["cp preview-proxy/ia-range-proxy.js preview-proxy/node-adapter.mjs preview-proxy/firebase/"]
//   },
//   "hosting": { "rewrites": [{ "source": "/ia/**", "function": { "functionId": "iaRange", "region": "us-central1" } }], ... }
//
// Deploy: firebase deploy --only functions:preview-proxy,hosting
// Then set window.STREETZIM_PREVIEW_PROXY = 'https://streetzim.web.app/ia'.
import { onRequest } from 'firebase-functions/v2/https';
import { nodeAdapter } from './node-adapter.mjs';

export const iaRange = onRequest(
  { region: 'us-central1', memory: '256MiB', timeoutSeconds: 300, cors: false,
    invoker: 'public' },
  (req, res) => {
    // Hosting forwards /ia/<item>/<file>?bytes=… — strip the mount point
    // so the handler sees /<item>/<file>.
    req.url = req.url.replace(/^\/ia(?=\/)/, '');
    return nodeAdapter(req, res, 'https://streetzim.web.app');
  });
