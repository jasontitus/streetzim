// Cloudflare Workers entry point. Deploy: `npx wrangler deploy` in this
// directory, then put the worker URL into web/drive/preview-config.js.
import { handleRequest } from './ia-range-proxy.js';

export default {
  fetch(request) {
    return handleRequest(request);
  },
};
