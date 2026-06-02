#!/usr/bin/env python3
"""Real-browser smoke test of the drive viewer against a local ZIM.

Serves web/ locally, loads /drive/ in headless chromium, injects the ZIM via
the fallback file input, navigates into the viewer, and checks: page loads
(no hang), no console errors, map renders, locate button works (with a faked
GPS fix), and the Wikipedia list opens + yields an in-ZIM "Read full article".
"""
import sys, threading, functools, http.server, socketserver, time
from playwright.sync_api import sync_playwright

ZIM = "/storage/streetzim/osm-california-geoviewer.zim"
PORT = 8765
ROOT = "/storage/streetzim/web"

# --- local static server (range requests needed for the ZIM-less shell) ---
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
httpd = Srv(("127.0.0.1", PORT), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print(f"serving {ROOT} at :{PORT}")

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(
        geolocation={"latitude": 37.7749, "longitude": -122.4194},  # SF
        permissions=["geolocation"],
        viewport={"width": 1100, "height": 850},
    )
    # Force the fallback <input type=file> path (no native picker in headless).
    ctx.add_init_script("window.showOpenFilePicker = undefined;")
    page = ctx.new_page()

    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))
    page.on("filechooser", lambda fc: fc.set_files(ZIM))

    base = f"http://127.0.0.1:{PORT}"
    # --- 1. picker loads + SW registers ---
    page.goto(f"{base}/drive/", wait_until="load", timeout=30000)
    page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.ready", timeout=20000)
    check("picker page + service worker ready", True)

    # --- 2. pick the ZIM, wait for navigation into the viewer (the 'hang' step) ---
    try:
        page.click("#pick-btn", timeout=8000)
    except Exception:
        # some builds auto-wire; try the fallback input directly
        page.set_input_files("#file-fallback", ZIM)
    try:
        page.wait_for_url("**/drive/viewer/**", timeout=60000)
        check("viewer loaded (did NOT hang on ZIM load)", True, page.url)
    except Exception as e:
        check("viewer loaded (did NOT hang on ZIM load)", False, str(e)[:120])
        print("RESULT: hang/failure at ZIM load"); httpd.shutdown(); sys.exit(1)

    # --- 3. map renders ---
    try:
        page.wait_for_selector(".maplibregl-canvas", state="visible", timeout=45000)
        time.sleep(4)  # let tiles + controls settle
        check("map canvas rendered", True)
    except Exception as e:
        check("map canvas rendered", False, str(e)[:120])

    # --- 4. locate button present + works with faked GPS ---
    has_locate = page.query_selector(".maplibregl-ctrl-geolocate") is not None
    check("locate button present", has_locate)
    if has_locate:
        try:
            page.click(".maplibregl-ctrl-geolocate", timeout=5000)
            page.wait_for_selector(".maplibregl-user-location-dot", timeout=10000)
            check("locate button drops a location dot (geolocation works)", True)
        except Exception as e:
            check("locate button drops a location dot (geolocation works)", False, str(e)[:120])

    # --- 5. Wikipedia panel opens + lists entries ---
    wiki_toggle = page.query_selector("#wiki-toggle")
    check("Wiki toggle present", wiki_toggle is not None)
    if wiki_toggle:
        try:
            page.click("#wiki-toggle", timeout=5000)
            page.wait_for_selector("#wiki-panel-list .wiki-item", timeout=15000)
            n = len(page.query_selector_all("#wiki-panel-list .wiki-item"))
            check("Wiki list populated", n > 0, f"{n} entries")
            # --- 6. click an entry -> in-panel detail with article link ---
            page.click("#wiki-panel-list .wiki-item", timeout=5000)
            page.wait_for_selector(".wiki-article-btn", timeout=8000)
            check("'Read full article' button shown on entry detail", True)
            # --- 7. the article actually exists in the ZIM (no 404) ---
            href_ok = page.evaluate("""async () => {
                // find the article path the button would open
                var base = document.baseURI.replace(/[^/]*$/, '');
                // probe a known bundled article via the SW
                var r = await fetch(base + 'wiki-geo-index.json');
                if (!r.ok) return 'geo-index fetch ' + r.status;
                var g = await r.json();
                var t = Object.keys(g)[0];
                var a = await fetch(base + 'wiki-article/' + encodeURIComponent(t));
                return a.ok ? 'ok:' + t : ('article ' + a.status);
            }""")
            check("bundled article fetch via SW returns 200", str(href_ok).startswith("ok:"), str(href_ok))
        except Exception as e:
            check("Wiki list / article-link interaction", False, str(e)[:160])

    # --- 5b. map markers actually render (HTML 📖 markers) ---
    nm = len(page.query_selector_all('.wiki-map-marker'))
    check("map markers render (HTML 📖)", nm > 0, f"{nm} markers")
    check("no console errors", len(errors) == 0, ("; ".join(errors[:4]))[:300] if errors else "")
    browser.close()

httpd.shutdown()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== {passed}/{len(results)} checks passed ===")
sys.exit(0 if passed == len(results) else 2)
