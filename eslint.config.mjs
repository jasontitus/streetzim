// ESLint flat config for the viewer JavaScript. Run it with
//   npm run lint:viewer        (tools/lint_viewer.mjs)
// which lints routing-worker.js and the inline <script> blocks of
// resources/viewer/index.html and places.html, reporting index.html
// findings against the part in resources/viewer/src/index/ they come from.
// Plain `npx eslint resources/viewer/routing-worker.js` works too.
//
// The rules are @eslint/js "recommended" minus the ones that only flag
// style or intentional idioms in this code; see the comments below. Rules
// that find real bugs (no-undef, no-dupe-keys, no-unreachable, ...) stay on.
import js from "@eslint/js";
import globals from "globals";

const relaxed = {
  // Unused arguments and caught errors are part of callback signatures
  // and `catch (e) {}` fallbacks throughout; unused locals are still
  // errors. Page-level globals (vars: "local") may be used by another
  // script block, an inline handler or the console, so they are exempt.
  "no-unused-vars": ["error", { vars: "local", args: "none", caughtErrors: "none" }],
  // `var x` declared again in another branch of the same function
  // (`if (a) { var ring = ... } else { var ring = ... }`) is legal and used
  // on purpose throughout; each of the 18 hits when this was set up was
  // checked and none was a bug. Duplicate let/const/function-scope
  // collisions are syntax errors and still fail the parse.
  "no-redeclare": "off",
  // Flags `var p = 0; if (...) p = 1; else p = 2;` and a deliberate
  // defensive re-read in routing-worker.js; no bugs among the 3 hits.
  "no-useless-assignment": "off",
  // `catch (e) {}` is the deliberate "best effort" idiom (storage, Kiwix
  // quirks); an empty block anywhere else is still reported.
  "no-empty": ["error", { allowEmptyCatch: true }],
  // Redundant escapes in regexes and strings are harmless; fixing them
  // would churn hundreds of lines in the fixed-size viewer slots.
  "no-useless-escape": "off",
};

export default [
  {
    ignores: ["node_modules/**", "web/**", "tmp/**", "preview-proxy/**", "rust/**"],
  },
  js.configs.recommended,
  {
    // The main viewer: classic browser scripts sharing one global scope,
    // with MapLibre loaded by <script src="maplibre-gl.js">.
    files: ["resources/viewer/index.html.js", "resources/viewer/places.html.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, maplibregl: "readonly" },
    },
    rules: relaxed,
  },
  {
    files: ["resources/viewer/routing-worker.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.worker },
    },
    rules: relaxed,
  },
];
