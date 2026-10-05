  // Keep HUD units in sync with the scale bar unit toggle
  map.on('streetzim.units', function() {
    // Next position tick will pick up new unit. No-op here; kept for future.
  });

  // Host-container hook — lets a wrapper like the mcpzim iOS chat view
  // drive routing + drive-mode from injected JS. All routing state
  // (lastRoute, setOriginFromLatLon, modeBtns, graph) is function-scoped
  // inside `initRouting`, so this tiny window object is the public seam.
  // No-op cost when nothing calls it.
  //
  // `graph` is lazy-loaded on first user interaction with the routing
  // panel (`toggleBtn` click above), so a host that calls `setOrigin`
  // cold would hit `if (!graph) return;` and silently no-op. Expose
  // `loadGraph` directly so the host can kick off the fetch itself,
  // then poll `graphReady` before calling `setOrigin / setDest`.
  window.streetzimRouting = {
    // Bump when a new capability lands so host containers can
    // feature-detect. Callers that need a given method should test
    // for it (`typeof window.streetzimRouting.foo === "function"`),
    // but the version helps diagnostics attribute "host vs ZIM" skew.
    version: 6,
    /// Travel mode routes are planned for: 'drive', 'walk' or 'bike'
    /// (version 6). `travelModes` lists what this ZIM supports;
    /// setTravelMode re-plans the current route and returns false for an
    /// unsupported or unchanged mode.
    get travelMode() { return travelMode; },
    get travelModes() { return routingModes.slice(); },
    setTravelMode: function(m) { return setTravelMode(m, false); },
    /// True while the routing panel is open: a map tap then picks a route
    /// point, so the place popups stay out of the way.
    get panelActive() { return !!active; },
    setOrigin: setOriginFromLatLon,
    setDest:   setDestFromLatLon,
    /// Reset the routing panel to "no route picked" — clears both
    /// endpoints, the result panel, and any drawn route. Mirrors the
    /// `Clear route` button. Useful for test harnesses that swap
    /// origin/dest pairs across runs.
    clear:     clearRoute,
    clickMode: function(mode) {
      var btn = modeBtns[mode];
      if (btn) btn.click();
    },
    /// Open the routing panel programmatically. Used by the search pin
    /// "Directions" popup and places.html's "Directions" CTA — both
    /// entry points now that the Route button is gone from the right
    /// rail. No-op when already open.
    open: function() {
      if (!active) activate();
    },
    /// Close the routing panel + clear route state. Mirrors the panel's
    /// `×` button.
    close: function() {
      deactivate();
    },
    loadGraph: loadGraph,
    get graphReady() { return !!graph; },
    get hasRoute()   { return !!lastRoute; },
    get driveActive() { return !!driveMode.active; },
    get lastEnteredMode() { return window.streetzimRouting.__lastEnteredMode || null; },

    /// Atomic "enter drive mode with these endpoints". Handles the full
    /// sequence internally so host containers (e.g. the mcpzim iOS chat
    /// view) don't have to orchestrate it across the JS bridge:
    ///   1. Kick off loadGraph if needed, poll graphReady.
    ///   2. Call setOriginFromLatLon / setDestFromLatLon.
    ///   3. Poll lastRoute (set asynchronously by computeAndDrawRoute).
    ///   4. Call driveMode.enter(mode) *directly* — not via modeBtn.click(),
    ///      which toggles and can flip the HUD off on repeated calls.
    ///   5. Mark modeBtns UI to match (active-mode class on selected,
    ///      hidden-mode on the others + Exit label) so the visual state
    ///      agrees with the internal state when the user eventually
    ///      returns to the routing panel.
    /// Returns a Promise that resolves when drive mode is active or
    /// rejects on timeout / error. Safe to call multiple times —
    /// subsequent calls while already active are a no-op.
    /// Clear any active route. Matches the "Clear route" button in the
    /// routing panel — removes markers, line, go-row, and exits drive
    /// mode if active.
    clearRoute: function() {
      clearRoute();
    },

    /// Explicit drive-mode exit — inverse of `enterDriveMode`. Also
    /// resets the go-row chrome so mode buttons return to their
    /// pre-activated visual state.
    exitDriveMode: function() {
      if (!driveMode.active) return;
      driveMode.exit();
      resetGoButtons();
      window.streetzimRouting.__lastEnteredMode = null;
    },

    /// Switch the active drive mode without fully leaving. Useful if
    /// the host wants the user to toggle Drive↔Walk↔Bike from its
    /// own chat-bubble pills while the fullscreen view is already up.
    /// Exits the current mode (if any) then enters the new one —
    /// re-uses the existing `lastRoute`, so no re-compute needed.
    switchDriveMode: function(newMode) {
      if (!modeBtns[newMode]) throw new Error("unknown mode: " + newMode);
      if (driveMode.active) {
        driveMode.exit();
        resetGoButtons();
      }
      if (!lastRoute) return false;
      // A ZIM that plans walk / bike routes re-plans for the new mode
      // first, then starts navigation once that route lands — unless the
      // plan is overtaken (clear, another mode, new endpoints: navPlanSeq).
      // Returns true when planning started; navigation follows only if a
      // route is found.
      if (multiModal && routingModes.indexOf(newMode) >= 0
          && (lastRoute.travel || 'drive') !== newMode) {
        setTravelMode(newMode, false);
        var plan = navPlanSeq;
        var polls = 0;
        var poll = setInterval(function() {
          polls++;
          if (plan !== navPlanSeq || polls > 600) {
            clearInterval(poll);
          } else if (lastRoute && lastRoute.travel === newMode) {
            clearInterval(poll);
            window.streetzimRouting.switchDriveMode(newMode);
          }
        }, 100);
        return true;
      }
      driveMode.enter(newMode);
      window.streetzimRouting.__lastEnteredMode = newMode;
      Object.keys(modeBtns).forEach(function(m) {
        if (m === newMode) {
          modeBtns[m].classList.add("active-mode");
          modeBtns[m].textContent = szT('drive.exit_button', 'Exit');
        } else {
          modeBtns[m].classList.add("hidden-mode");
        }
      });
      return true;
    },

    /// Camera control. Host can frame the map (pan / zoom / pitch / bearing)
    /// without entering drive mode — useful for "show me this place" intents
    /// where we just want to centre the map on a coordinate.
    flyTo: function(opts) {
      if (!map) return;
      opts = opts || {};
      var easeOpts = {};
      if (typeof opts.lat === "number" && typeof opts.lon === "number") {
        easeOpts.center = [opts.lon, opts.lat];
      }
      if (typeof opts.zoom === "number")    easeOpts.zoom = opts.zoom;
      if (typeof opts.pitch === "number")   easeOpts.pitch = opts.pitch;
      if (typeof opts.bearing === "number") easeOpts.bearing = opts.bearing;
      easeOpts.duration = typeof opts.duration === "number" ? opts.duration : 800;
      map.easeTo(easeOpts);
    },

    /// Display an array of places on the map as pins. Replaces any
    /// previous set from a prior `showPlaces` call (use `clearPlaces`
    /// to take them off without a new set). Useful for host-driven
    /// intents like "interesting places near you" / "bars in North Beach"
    /// where the LLM returns several candidates and we want them all
    /// visible at once rather than one pin at a time.
    ///
    /// places: [{ lat, lon, label, description?, color? }]
    ///   - lat / lon:       required, decimal degrees
    ///   - label:           required, shown as a popup on tap
    ///   - description:     optional, shown below the label in popup
    ///   - color:           optional, CSS color (default #e11d48)
    ///
    /// opts: { fitBounds?: true, padding?: 40 }
    ///   - fitBounds: pan+zoom to frame the pins (default: true)
    ///   - padding:   fitBounds padding in px (default: 40)
    showPlaces: function(places, opts) {
      opts = opts || {};
      if (!Array.isArray(places) || places.length === 0) {
        window.streetzimRouting.clearPlaces();
        return;
      }
      var src = "mcpzim-places";
      var layerDots = "mcpzim-places-dots";
      var layerLabels = "mcpzim-places-labels";
      var features = places
        .map(function(p, i) {
          if (typeof p.lat !== "number" || typeof p.lon !== "number") return null;
          return {
            type: "Feature",
            geometry: { type: "Point", coordinates: [p.lon, p.lat] },
            properties: {
              idx: i,
              label: p.label || "",
              description: p.description || "",
              color: p.color || "#e11d48"
            }
          };
        })
        .filter(Boolean);
      if (features.length === 0) return;
      var fc = { type: "FeatureCollection", features: features };
      if (map.getSource(src)) {
        map.getSource(src).setData(fc);
      } else {
        map.addSource(src, { type: "geojson", data: fc });
        map.addLayer({
          id: layerDots, type: "circle", source: src,
          paint: {
            "circle-radius": 8,
            "circle-color": ["get", "color"],
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 2
          }
        });
        map.addLayer({
          id: layerLabels, type: "symbol", source: src,
          layout: {
            "text-field": ["get", "label"],
            // Must match the fontstack baked into the ZIM (see makeStyle);
            // "Open Sans Bold" 404'd every glyph request and labels never drew.
            "text-font": ["OpenSansBold"],
            "text-size": 12,
            "text-offset": [0, 1.2],
            "text-anchor": "top",
            "text-optional": true,
            "text-max-width": 10
          },
          paint: {
            "text-color": "#111827",
            "text-halo-color": "#ffffff",
            "text-halo-width": 1.5
          }
        });
        // Tap-to-popup with label + optional description.
        // Handlers are kept on the api object so clearPlaces can
        // detach them; registering afresh on every showPlaces →
        // clearPlaces → showPlaces cycle stacked N popups per tap.
        var onDotClick = function(e) {
          var f = e.features && e.features[0];
          if (!f) return;
          var props = f.properties;
          var html = '<div style="font-family:var(--ui-font,system-ui);max-width:220px;">'
            + '<div style="font-weight:600;font-size:14px;margin-bottom:2px;">'
            + (props.label || "").replace(/</g, "&lt;") + "</div>";
          if (props.description) {
            html += '<div style="font-size:12px;color:var(--szd-fg-2, #555);">'
              + props.description.replace(/</g, "&lt;") + "</div>";
          }
          html += "</div>";
          new maplibregl.Popup({ offset: 14 })
            .setLngLat(f.geometry.coordinates)
            .setHTML(html)
            .addTo(map);
        };
        var onDotEnter = function() { map.getCanvas().style.cursor = "pointer"; };
        var onDotLeave = function() { map.getCanvas().style.cursor = ""; };
        map.on("click", layerDots, onDotClick);
        map.on("mouseenter", layerDots, onDotEnter);
        map.on("mouseleave", layerDots, onDotLeave);
        window.streetzimRouting.__placesHandlers = {
          click: onDotClick, mouseenter: onDotEnter, mouseleave: onDotLeave
        };
      }
      if (opts.fitBounds !== false && features.length >= 1) {
        var b = new maplibregl.LngLatBounds(
          features[0].geometry.coordinates,
          features[0].geometry.coordinates
        );
        features.forEach(function(f) { b.extend(f.geometry.coordinates); });
        var padding = typeof opts.padding === "number" ? opts.padding : 40;
        // For a single pin we can't fitBounds, just centre it.
        if (features.length === 1) {
          map.easeTo({ center: features[0].geometry.coordinates, zoom: 15, duration: 800 });
        } else {
          // maxZoom: 17 — was 15, which capped tight clusters (a few
          // bars on one block) at a zoom level that shows a whole
          // neighbourhood, leaving the pins as a tiny red clump in the
          // middle. 17 still maintains context but zooms in appropriately.
          map.fitBounds(b, { padding: padding, duration: 800, maxZoom: 17 });
        }
      }
    },

    /// Remove the pins set by the most recent `showPlaces` call.
    /// Safe to call when nothing is set.
    clearPlaces: function() {
      var src = "mcpzim-places";
      var h = window.streetzimRouting.__placesHandlers;
      if (h) {
        Object.keys(h).forEach(function(type) {
          try { map.off(type, "mcpzim-places-dots", h[type]); } catch (e) {}
        });
        window.streetzimRouting.__placesHandlers = null;
      }
      ["mcpzim-places-labels", "mcpzim-places-dots"].forEach(function(id) {
        if (map.getLayer(id)) map.removeLayer(id);
      });
      if (map.getSource(src)) map.removeSource(src);
    },

    /// Draw a translucent "coverage radius" circle centred on a point.
    /// Useful for "near me" style tool calls so the user sees *why* the
    /// returned list of places is what it is — the circle visualises the
    /// `radius_km` arg the tool was invoked with.
    ///
    /// opts: { lat, lon, radiusKm, color? }
    ///   - lat / lon:  required, decimal degrees, centre of the circle
    ///   - radiusKm:   required, radius in kilometres
    ///   - color:      optional, ring + fill color (default #2563eb)
    ///
    /// We approximate the circle as a 64-point polygon in lon/lat — that's
    /// close enough at city scale and doesn't need a projection-aware
    /// geodesic path. For larger radii (>100 km) distortion becomes
    /// visible at high latitudes; host should clamp the display radius
    /// if it cares.
    showRadiusRing: function(opts) {
      opts = opts || {};
      if (typeof opts.lat !== "number" || typeof opts.lon !== "number"
          || typeof opts.radiusKm !== "number" || opts.radiusKm <= 0) {
        return;
      }
      var src = "mcpzim-radius";
      var fillId = "mcpzim-radius-fill";
      var lineId = "mcpzim-radius-line";
      var color = opts.color || "#2563eb";
      // Equirectangular approximation: 1° lat ≈ 111.32 km everywhere,
      // 1° lon ≈ 111.32 * cos(lat) km. Good enough for the rendered
      // polygon at city-to-region scale.
      var kmPerDegLat = 111.32;
      var kmPerDegLon = 111.32 * Math.cos(opts.lat * Math.PI / 180);
      var steps = 64;
      var ring = [];
      for (var i = 0; i <= steps; i++) {
        var theta = (i / steps) * 2 * Math.PI;
        var dLat = (opts.radiusKm / kmPerDegLat) * Math.sin(theta);
        var dLon = (opts.radiusKm / kmPerDegLon) * Math.cos(theta);
        ring.push([opts.lon + dLon, opts.lat + dLat]);
      }
      var polygon = {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [ring] },
        properties: {}
      };
      if (map.getSource(src)) {
        map.getSource(src).setData(polygon);
      } else {
        map.addSource(src, { type: "geojson", data: polygon });
        // Fill under everything interactive so taps still hit pins.
        map.addLayer({
          id: fillId, type: "fill", source: src,
          paint: { "fill-color": color, "fill-opacity": 0.08 }
        });
        map.addLayer({
          id: lineId, type: "line", source: src,
          paint: { "line-color": color, "line-width": 2, "line-opacity": 0.5,
                   "line-dasharray": [2, 2] }
        });
      }
    },

    clearRadiusRing: function() {
      var src = "mcpzim-radius";
      ["mcpzim-radius-line", "mcpzim-radius-fill"].forEach(function(id) {
        if (map.getLayer(id)) map.removeLayer(id);
      });
      if (map.getSource(src)) map.removeSource(src);
    },

    /// Show / hide the viewer's own chrome (search bar, zoom+layer controls,
    /// routing panel). Host wrappers that overlay their own UI can suppress
    /// the on-page chrome for a cleaner look. Pass an object like
    /// `{search: false, controls: false, panel: false}`; omit keys to leave
    /// those elements untouched.
    setChromeVisibility: function(opts) {
      opts = opts || {};
      function setDisplay(el, visible) {
        if (!el) return;
        el.style.display = visible ? "" : "none";
      }
      if ("search" in opts)   setDisplay(document.getElementById("search-container"), opts.search);
      if ("controls" in opts) setDisplay(document.getElementById("controls"),         opts.controls);
      if ("panel" in opts)    setDisplay(document.getElementById("routing-panel"),    opts.panel);
    },

    enterDriveMode: function(mode, origLat, origLon, destLat, destLon) {
      return new Promise(function(resolve, reject) {
        if (!modeBtns[mode]) {
          return reject(new Error("unknown mode: " + mode));
        }
        // `driveMode.active` is exposed via the IIFE's returned api;
        // `state.mode` isn't, so we cache the last-entered mode ourselves.
        if (driveMode.active && window.streetzimRouting.__lastEnteredMode === mode) {
          return resolve({ alreadyActive: true });
        }
        function waitFor(test, cb, label, tries) {
          tries = tries || 0;
          if (test()) return cb();
          if (tries > 300) return reject(new Error("timeout waiting for " + label));
          setTimeout(function() { waitFor(test, cb, label, tries + 1); }, 100);
        }
        if (!graph) loadGraph();
        waitFor(function() { return !!graph; }, function() {
          // Drop any previous route AND endpoints first. Nulling only
          // lastRoute left the old destNode in place, so the origin
          // snap could compute origin→old-dest and the poll below would
          // resolve on that before the new destination was routed.
          clearRoute();
          // Plan the route for the navigation mode when the ZIM can.
          if (multiModal && routingModes.indexOf(mode) >= 0 && travelMode !== mode) {
            setTravelMode(mode, false);
          }
          try {
            setOriginFromLatLon(origLat, origLon, "Start");
            setDestFromLatLon(destLat, destLon, "Destination");
          } catch (e) { return reject(e); }
          waitFor(function() { return !!lastRoute; }, function() {
            try {
              driveMode.enter(mode);
              window.streetzimRouting.__lastEnteredMode = mode;
              // Mirror what the mode-button click handler does to
              // the go-row so the chrome agrees with the state.
              Object.keys(modeBtns).forEach(function(m) {
                if (m === mode) {
                  modeBtns[m].classList.add("active-mode");
                  modeBtns[m].textContent = szT('drive.exit_button', 'Exit');
                } else {
                  modeBtns[m].classList.add("hidden-mode");
                }
              });
              resolve({ alreadyActive: false, mode: mode });
            } catch (e) { reject(e); }
          }, "lastRoute");
        }, "graphReady");
      });
    }
  };
}
