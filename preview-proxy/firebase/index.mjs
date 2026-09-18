// Optional: run the range proxy as a Firebase Cloud Function (2nd gen,
// i.e. Cloud Run) behind a Hosting rewrite, so the preview streams from
// streetzim.web.app/ia/… instead of a workers.dev host. Needs the Blaze
// plan; Cloud Run bills outbound bytes (~$0.12/GB), which Cloudflare
// Workers do not. See docs/online-preview.md, "Running it on the
// StreetZim site itself".
//
// functions/ layout (not committed — create it when you want this):
//   functions/package.json   {"type":"module","main":"index.mjs",
//                             "dependencies":{"firebase-functions":"^6"}}
//   functions/index.mjs      copy of this file
//   functions/ia-range-proxy.js, node-adapter.mjs   copied from preview-proxy/
// firebase.json additions:
//   "functions": {"source": "functions"},
//   "hosting": {"rewrites": [{"source": "/ia/**", "function": "iaRange"}], ...}
import { onRequest } from 'firebase-functions/v2/https';
import { nodeAdapter } from './node-adapter.mjs';

export const iaRange = onRequest(
  { region: 'us-central1', memory: '256MiB', timeoutSeconds: 300, cors: false },
  (req, res) => {
    // Hosting forwards /ia/<item>/<file> — strip the mount point so the
    // handler sees /<item>/<file>.
    req.url = req.url.replace(/^\/ia(?=\/)/, '');
    return nodeAdapter(req, res, 'https://streetzim.web.app');
  });
