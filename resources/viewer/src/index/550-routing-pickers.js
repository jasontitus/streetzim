  // Queue a pick when the graph isn't loaded yet. Previously these setters
  // silently `return`ed on `!graph`, which is why tapping the routing-panel
  // GPS button or picking a typeahead result before graph load finished
  // left the UI stuck at "Getting location…" with no visible progress.
  // Loads the graph if needed, then re-fires the pick with the original
  // label. Pass a 'which' of 'origin' or 'dest' so the retry dispatches
  // to the correct setter.
  function queueGraphPick(which, lat, lon, label) {
    if (graph) return false;
    // Show the picked label in the input IMMEDIATELY so the user can
    // see what they chose — graph load can take 3-5 s, and an empty
    // input plus "loading routing data…" status reads as "the click
    // did nothing." The retry below will then snap to the nearest
    // node and overwrite the marker once the graph is up.
    var visible = label || coordLabel(lat, lon);
    if (which === 'origin') originInput.value = visible;
    else                    destInput.value   = visible;
    statusEl.textContent = 'Loading routing data…';
    loadGraph();
    var tries = 0;
    var retry = setInterval(function() {
      tries++;
      if (graph) {
        clearInterval(retry);
        if (which === 'origin') setOriginFromLatLon(lat, lon, label);
        else setDestFromLatLon(lat, lon, label);
      } else if (!loadGraphInflight || tries > 6000) {
        // The load settled without producing a graph (fetch/parse
        // failed) — or a 10-minute hard cap. Used to give up after a
        // fixed 30 s, which was shorter than a cold country-scale
        // index load over Kiwix on iOS, so the pick was dropped even
        // though the graph arrived a few seconds later.
        clearInterval(retry);
        statusEl.textContent = 'Could not load routing data';
      }
    }, 100);
    return true;
  }

  // Set origin from any source (map click, typeahead, GPS). `label` is what
  // to show in the input; if omitted, a lat/lon coord label is used.
  var originSnapSeq = 0;
  var destSnapSeq = 0;
  // The points as picked (before snapping): a travel-mode change snaps
  // them again for the new mode, which may use different vertices.
  var originPick = null;
  var destPick = null;

  function snappingText() {
    return travelMode === 'drive' ? 'Finding nearest road...' : 'Finding nearest path...';
  }

  async function setOriginFromLatLon(lat, lon, label) {
    // Race guard: a "Directions to here" click on a place-detail
    // popup fires getCurrentPosition, then on resolve calls back into
    // this with label='Current location'. If the user has clicked
    // into the origin field and started typing in the meantime, we'd
    // clobber their input AND auto-fire a route from current location
    // before they even finished picking their actual origin. Skip
    // GPS auto-fills (the only callers that pass 'Current location')
    // when the input is being edited.
    if (label === 'Current location'
        && window.__streetzim_originBeingEdited) {
      return;
    }
    if (queueGraphPick('origin', lat, lon, label)) return;
    originPick = { lat: lat, lon: lon };
    navPlanSeq++;
    var snapSeq = ++originSnapSeq;
    statusEl.textContent = snappingText();
    var snapped;
    try {
      snapped = await nearestNode(lat, lon, 'origin');
    } catch (err) {
      if (snapSeq === originSnapSeq) statusEl.textContent = 'Could not find a nearby road';
      console.error('[streetzim] origin snap failed:', err);
      return;
    }
    if (snapSeq !== originSnapSeq) return;
    originNode = snapped.node;
    var oLat = snapped.lat;
    var oLon = snapped.lon;
    originCoordE7 = [Math.round(oLat * 1e7), Math.round(oLon * 1e7)];
    originInput.value = label || coordLabel(oLat, oLon);
    // Origin is now committed (typeahead pick, map click, GPS button,
    // or queued-pick replay). Clear the editing flag so future GPS
    // auto-fills aren't blocked indefinitely.
    window.__streetzim_originBeingEdited = false;
    if (originMarker) originMarker.remove();
    originMarker = new maplibregl.Marker({ element: makeMarkerEl('routing-marker-origin'), anchor: 'center' })
      .setLngLat([oLon, oLat]).addTo(map);
    if (destNode !== -1) {
      computeAndDrawRoute();
    } else {
      statusEl.textContent = 'Enter destination';
    }
  }

  async function setDestFromLatLon(lat, lon, label) {
    if (queueGraphPick('dest', lat, lon, label)) return;
    destPick = { lat: lat, lon: lon };
    navPlanSeq++;
    var snapSeq = ++destSnapSeq;
    statusEl.textContent = snappingText();
    var snapped;
    try {
      snapped = await nearestNode(lat, lon, 'dest');
    } catch (err) {
      if (snapSeq === destSnapSeq) statusEl.textContent = 'Could not find a nearby road';
      console.error('[streetzim] destination snap failed:', err);
      return;
    }
    if (snapSeq !== destSnapSeq) return;
    destNode = snapped.node;
    var dLat = snapped.lat;
    var dLon = snapped.lon;
    destCoordE7 = [Math.round(dLat * 1e7), Math.round(dLon * 1e7)];
    destInput.value = label || coordLabel(dLat, dLon);
    if (destMarker) destMarker.remove();
    destMarker = new maplibregl.Marker({ element: makeMarkerEl('routing-marker-dest'), anchor: 'center' })
      .setLngLat([dLon, dLat]).addTo(map);
    if (originNode !== -1) {
      computeAndDrawRoute();
    } else {
      statusEl.textContent = 'Enter start location';
    }
  }

  // Track the latest route-compute call so earlier (slower) calls can't
  // overwrite results from a newer destination change.
  var routeSeq = 0;

  function removeRouteLine() {
    if (!routeDrawn) return;
    if (map.getLayer('route-line')) map.removeLayer('route-line');
    if (map.getLayer('route-outline')) map.removeLayer('route-outline');
    if (map.getSource('route')) map.removeSource('route');
    routeDrawn = false;
  }

  // Switch the travel mode: snap both picked points again for the new
  // mode and route once both are in (whichever snap lands second
  // routes). Navigation in progress ends — its route was for the old mode.
  // `remember` false: a host-driven change (enterDriveMode) that must not
  // replace the user's own saved choice.
  function setTravelMode(m, remember) {
    if (routingModes.indexOf(m) < 0 || m === travelMode) return false;
    travelMode = m;
    navPlanSeq++;
    if (remember !== false) {
      try { localStorage.setItem('streetzim.travelMode', m); } catch (e) {}
    }
    syncTravelButtons();
    if (driveMode.active) driveMode.exit();
    resetGoButtons();
    var o = originPick, d = destPick;
    var oLabel = originInput.value, dLabel = destInput.value;
    if (o) originNode = -1;
    if (d) destNode = -1;
    if (o || d) {
      routeSeq++;                       // drop a route still computing
      if (typeof cancelInFlightRoute === 'function') cancelInFlightRoute();
      stopRouteProgressIndicator();
      resultEl.style.display = 'none';
      setExpandHint();
      goRow.classList.remove('visible');
      lastRoute = null;
      removeRouteLine();
    }
    if (o) setOriginFromLatLon(o.lat, o.lon, oLabel);
    if (d) setDestFromLatLon(d.lat, d.lon, dLabel);
    return true;
  }
  Object.keys(travelBtns).forEach(function(m) {
    if (!travelBtns[m]) return;
    travelBtns[m].addEventListener('click', function() { setTravelMode(m); });
  });

  function computeAndDrawRoute() {
    resultEl.style.display = 'none';
    var seq = ++routeSeq;
    var routeTravel = travelMode;
    // Compute crow-fly distance to feed the ETA heuristic, then start
    // the spinner-and-elapsed-time indicator immediately. Ticks every
    // 100 ms whether or not the A* loop has yielded — closes the
    // "is it stuck?" gap that drove the user to give up on long
    // routes.
    var crowKm = 0;
    if (graph && originNode >= 0 && destNode >= 0) {
      var oLatE7 = originCoordE7[0];
      var oLonE7 = originCoordE7[1];
      var dLatE7 = destCoordE7[0];
      var dLonE7 = destCoordE7[1];
      crowKm = haversine(oLatE7 / 1e7, oLonE7 / 1e7,
                         dLatE7 / 1e7, dLonE7 / 1e7) / 1000;
    }
    __routeDebugLabel = 'Calculating route';
    __routeDebugPops = 0;
    startRouteProgressIndicator(crowKm);
    setTimeout(async function() {
      // A stale callback (newer route already kicked off via a second
      // setOrigin/setDest in the same tick) must NOT call
      // stopRouteProgressIndicator — that would clear the spinner the
      // newer call just installed. Only the live callback owns the
      // indicator. Same logic everywhere else seq is rechecked below.
      if (seq !== routeSeq) return;
      var result;
      try {
        result = await findRoute(originNode, destNode, routeTravel,
                                 { origin: originPick, dest: destPick });
      } catch (err) {
        console.error('[streetzim] findRoute failed:', err);
        if (seq === routeSeq) {
          stopRouteProgressIndicator();
          statusEl.textContent = 'Routing failed';
        }
        return;
      }
      if (seq !== routeSeq) return;  // newer route owns the spinner now
      stopRouteProgressIndicator();
      if (result && result._cancelled) {
        // Cancelled by the user (field focus) with no newer route yet:
        // leave the map as it was. Used to fall into the "No route
        // found" branch and erase the drawn route.
        return;
      }
      if (result) {
        // Sanity check: any coord obviously off the map? Logs first
        // offender — helps diagnose "crazy lines" bugs after the fact.
        var bounds = map.getMaxBounds && map.getMaxBounds();
        var bad = null;
        for (var ci = 0; ci < result.coords.length; ci++) {
          var c = result.coords[ci];
          if (!isFinite(c[0]) || !isFinite(c[1])) { bad = { ci: ci, c: c, reason: 'NaN' }; break; }
        }
        if (bad) console.warn('[streetzim] route coord:', bad);
        console.log('[streetzim] route', {
          coords: result.coords.length,
          km: (result.distance / 1000).toFixed(1),
          first: result.coords[0],
          mid: result.coords[Math.floor(result.coords.length / 2)],
          last: result.coords[result.coords.length - 1],
        });
        // An end the router had to move out of a sealed pocket (a road
        // stub cut off by the map's edge): put its marker where the route
        // really starts / ends.
        if (result.endMoved && typeof result.endMoved.lat === 'number') {
          destNode = result.endMoved.node;
          destCoordE7 = [Math.round(result.endMoved.lat * 1e7), Math.round(result.endMoved.lon * 1e7)];
          if (destMarker) destMarker.setLngLat([result.endMoved.lon, result.endMoved.lat]);
        }
        if (result.startMoved && typeof result.startMoved.lat === 'number') {
          originNode = result.startMoved.node;
          originCoordE7 = [Math.round(result.startMoved.lat * 1e7), Math.round(result.startMoved.lon * 1e7)];
          if (originMarker) originMarker.setLngLat([result.startMoved.lon, result.startMoved.lat]);
        }
        drawRoute(unwrapLngs(result.coords));
        distEl.textContent = formatDistance(result.distance);
        timeEl.textContent = formatTime(result.time);
        // The folded header carries the summary, so comparing modes on
        // a phone needs no unfold.
        setExpandHint('— ' + formatTime(result.time) + ' · ' + formatDistance(result.distance));
        renderRoads(result.roads);
        resultEl.style.display = 'block';
        clearBtn.style.display = 'block';
        goRow.classList.add('visible');
        resetGoButtons();
        lastRoute = augmentRouteForDriving(result);
        lastRoute.travel = routeTravel;
        // Expose for the smoke harness: the user's standing rule is
        // "highway-only for the sketch is fine but it still needs to
        // get me from where I start to where I am going." Smoke
        // checks the first/last coords of the drawn route against
        // the requested origin/destination.
        try { window.__streetzim_lastRoute = lastRoute; } catch (e) {}
        if (driveMode.active) driveMode.setRoute(lastRoute);
        statusEl.textContent = '';
        // In drive mode, the next GPS fix re-centers at the user's
        // location; fitBounds would yank the camera out to the whole
        // route and blow up the follow-cam.
        if (!driveMode.active) {
          var shown = unwrapLngs(result.coords);
          var bounds = shown.reduce(function(b, c) { return b.extend(c); },
            new maplibregl.LngLatBounds(shown[0], shown[0]));
          map.fitBounds(bounds, { padding: 60, duration: 1000 });
        }
        // Auto-minimize when the routing panel would otherwise cover
        // most of the route — common on phones where the panel can
        // be 80%+ of viewport height. Threshold: panel takes more
        // than half the viewport height. The user can tap the
        // header stripe to expand again.
        try {
          var panelH = panel.getBoundingClientRect().height;
          var vh = window.innerHeight || document.documentElement.clientHeight;
          if (vh > 0 && panelH / vh > 0.5
              && !panel.classList.contains('minimized')) {
            panel.classList.add('minimized');
            if (minBtn) {
              minBtn.title = 'Unfold (show directions)';
            }
            try { map.resize(); } catch (e) {}
          }
        } catch (e) {}
      } else {
        removeRouteLine();
        setExpandHint();
        statusEl.textContent = routeTravel === 'walk' ? 'No walking route found'
                             : routeTravel === 'bike' ? 'No cycling route found'
                             : 'No route found';
        clearBtn.style.display = 'block';
      }
    }, 10);
  }

  // Typeahead: wire each input to map._queryPlaces and render a dropdown.
  // `target` is 'origin' or 'dest' — determines which handler runs on pick.
  function attachTypeahead(inputEl, resultsEl, target) {
    var timer;
    var activeIdx = -1;
    var lastMatches = [];

    function pickMatch(m) {
      resultsEl.style.display = 'none';
      var lat = m.item.a;
      var lon = m.item.o;
      var label = m.item.n;
      if (target === 'origin') setOriginFromLatLon(lat, lon, label);
      else setDestFromLatLon(lat, lon, label);
      inputEl.blur();
    }

    function renderDropdown(matches) {
      while (resultsEl.firstChild) resultsEl.removeChild(resultsEl.firstChild);
      lastMatches = matches;
      activeIdx = -1;
      if (!matches.length) { resultsEl.style.display = 'none'; return; }
      for (var i = 0; i < matches.length; i++) {
        (function(m) {
          var row = document.createElement('div');
          row.className = 'routing-result-row';

          var textCol = document.createElement('div');
          textCol.className = 'routing-result-text';

          var nameSpan = document.createElement('div');
          nameSpan.className = 'routing-result-name';
          nameSpan.textContent = m.item.n;
          textCol.appendChild(nameSpan);

          // Secondary line: location (e.g. "Sacramento, California") + type,
          // so ambiguous names like "Main St" reveal which city's they're in.
          var subParts = [];
          if (m.item.l) subParts.push(m.item.l);
          var typeLabel = m.item.t === 'addr' ? 'address'
                          : m.item.t === 'street' ? 'street'
                          : m.item.t;
          if (typeLabel) subParts.push(typeLabel);
          if (subParts.length) {
            var sub = document.createElement('div');
            sub.className = 'routing-result-sub';
            sub.textContent = subParts.join(' · ');
            textCol.appendChild(sub);
          }

          row.appendChild(textCol);
          if (m.dist) {
            var meta = document.createElement('span');
            meta.className = 'routing-result-meta';
            meta.textContent = m.dist;
            row.appendChild(meta);
          }
          // mousedown firing pickMatch on touch was the
          // "scrolling the dropdown selects an item" bug — touch
          // devices dispatch mousedown at touchstart, before the
          // OS knows whether it's a tap or a scroll. Split:
          //   - mousedown: only preventDefault to keep focus on
          //     the input (so the blur handler doesn't hide the
          //     dropdown out from under the click).
          //   - click:     actually pick. Click is suppressed by
          //     the browser when a scroll gesture moves past its
          //     slop threshold, which is exactly what we want.
          row.addEventListener('mousedown', function(e) { e.preventDefault(); });
          (function(match) {
            row.addEventListener('click', function(e) {
              e.preventDefault();
              pickMatch(match);
            });
          })(m);
          resultsEl.appendChild(row);
        })(matches[i]);
      }
      resultsEl.style.display = 'block';
    }

    // Large chunks (e.g. `sh.json` at 15 MB on Japan) take 20-40s to
    // fetch+parse over Kiwix's SW on iOS. Without feedback the input
    // feels hung — show a spinner row so the user knows the fetch is
    // in flight. Tracked with a sequence id so a stale response from
    // an earlier keystroke doesn't overwrite the dropdown.
    var seq = 0;
    function renderLoading() {
      while (resultsEl.firstChild) resultsEl.removeChild(resultsEl.firstChild);
      var row = document.createElement('div');
      row.className = 'routing-result-row routing-result-loading';
      row.style.pointerEvents = 'none';
      row.style.opacity = '0.7';
      row.textContent = 'Searching…';
      resultsEl.appendChild(row);
      resultsEl.style.display = 'block';
    }
    inputEl.addEventListener('input', function() {
      clearTimeout(timer);
      var q = inputEl.value.trim();
      if (q.length < 2) { resultsEl.style.display = 'none'; return; }
      if (!map._queryPlaces) return;
      timer = setTimeout(function() {
        var mySeq = ++seq;
        renderLoading();
        map._queryPlaces(q).then(function(matches) {
          if (mySeq !== seq) return;  // newer keystroke already in flight
          renderDropdown(matches);
        });
      }, 150);
    });

    inputEl.addEventListener('keydown', function(e) {
      var rows = resultsEl.querySelectorAll('.routing-result-row');
      if (e.key === 'ArrowDown' && rows.length) {
        e.preventDefault();
        activeIdx = Math.min(activeIdx + 1, rows.length - 1);
        updateActive(rows);
      } else if (e.key === 'ArrowUp' && rows.length) {
        e.preventDefault();
        activeIdx = Math.max(activeIdx - 1, 0);
        updateActive(rows);
      } else if (e.key === 'Enter') {
        if (activeIdx >= 0 && lastMatches[activeIdx]) {
          e.preventDefault();
          pickMatch(lastMatches[activeIdx]);
        }
      } else if (e.key === 'Escape') {
        resultsEl.style.display = 'none';
      }
    });

    function updateActive(rows) {
      for (var i = 0; i < rows.length; i++) {
        rows[i].classList.toggle('active', i === activeIdx);
      }
      if (activeIdx >= 0) rows[activeIdx].scrollIntoView({ block: 'nearest' });
    }

    inputEl.addEventListener('blur', function() {
      // Delay so mousedown on row still fires
      setTimeout(function() { resultsEl.style.display = 'none'; }, 150);
    });
  }

  attachTypeahead(originInput, originResultsEl, 'origin');
  attachTypeahead(destInput, destResultsEl, 'dest');

  // Focus behaviour: select existing text so the next keystroke
  // overwrites it cleanly, AND clear the synthetic "Current location"
  // label so the user doesn't have to hand-delete it. setTimeout(0)
  // works around iOS Safari's habit of un-selecting on the focus
  // event tick. Hooks both focus AND click — clicking on an
  // already-focused input still re-selects, which matches Google
  // Maps' behaviour.
  function autoSelectOnFocus(input) {
    // First focus on the input (transitioning from elsewhere) selects
    // all so the next keystroke replaces — and clears the synthetic
    // "Current location" label specifically. Subsequent clicks while
    // already focused let the user position the caret normally; we
    // don't re-select on every click.
    input.addEventListener('focus', function() {
      setTimeout(function() {
        if (input.value === 'Current location') {
          input.value = '';
        } else if (document.activeElement === input) {
          try { input.select(); } catch (e) {}
        }
      }, 0);
    });
  }
  autoSelectOnFocus(originInput);
  autoSelectOnFocus(destInput);

  // Race guard: when "Directions to here" is clicked, the popup
  // handlers fire `getCurrentPosition` and on resolve overwrite the
  // origin input with the cached current location. If the user
  // started typing a custom origin in the meantime, the GPS resolve
  // would clobber what they typed (and the auto-fired route would
  // be from current location instead of their pick). Track active
  // editing so the GPS callbacks can skip auto-fill.
  function markOriginEditing() {
    window.__streetzim_originBeingEdited = true;
  }
  originInput.addEventListener('focus', markOriginEditing);
  originInput.addEventListener('input', markOriginEditing);
  // When the user clicks into either input, cancel any route still
  // computing in the worker. They're clearly about to change one of
  // the endpoints, so the in-flight result is moot. Fixes the
  // "Directions to here auto-fires a route from current location
  // and grinds while I try to type San Diego" complaint.
  function cancelOnFieldFocus() {
    if (typeof window.__streetzim_cancelInFlightRoute === 'function') {
      window.__streetzim_cancelInFlightRoute();
    }
    if (typeof statusEl !== 'undefined' && statusEl) {
      statusEl.textContent = '';
    }
    stopRouteProgressIndicator();
  }
  originInput.addEventListener('focus', cancelOnFieldFocus);
  destInput.addEventListener('focus', cancelOnFieldFocus);
  // Cleared by any code path that successfully sets origin (GPS
  // button, typeahead pick, map click). setOriginFromLatLon below
  // is the funnel — clear there.
  // Hook all "Directions to here" callers via this small bridge.
  window.__streetzimGpsAutofillOk = function() {
    return !window.__streetzim_originBeingEdited;
  };

  // Current location button (origin only). Uses the standard Geolocation API.
  // Falls back gracefully if not available or permission denied.
  // Just fills the origin — does NOT recenter the map. The user picked their
  // destination on purpose; yanking the camera back to "you are here" hides
  // what they were looking at. If a destination is also set,
  // computeAndDrawRoute will fitBounds the route once both ends are pinned.
  gpsBtn.addEventListener('click', function() {
    var cached = window.__streetzimLastLoc;
    var fresh = cached && (Date.now() - cached.ts) < 10 * 60 * 1000;
    if (fresh) {
      setOriginFromLatLon(cached.lat, cached.lon, 'Current location');
      return;
    }
    if (!navigator.geolocation) {
      statusEl.textContent = 'Geolocation not available';
      return;
    }
    statusEl.textContent = 'Getting location…';
    navigator.geolocation.getCurrentPosition(
      function(pos) {
        var lat = pos.coords.latitude;
        var lon = pos.coords.longitude;
        window.__streetzimLastLoc = { lat: lat, lon: lon, ts: Date.now() };
        setOriginFromLatLon(lat, lon, 'Current location');
      },
      function(err) {
        statusEl.textContent = 'Location denied or unavailable';
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  });

  // Map click — fallback picker. Fills origin if empty, else updates
  // destination (preserving the origin so users can iterate on the target
  // without wiping the route). Clear button resets both. Disabled while
  // drive mode is active — a stray tap on the map while navigating
  // would otherwise silently reroute to wherever the finger landed.
  map.on('click', function(e) {
    if (!active || !graph) return;
    if (driveMode && driveMode.active) return;
    var lat = e.lngLat.lat;
    var lon = e.lngLat.lng;
    if (originNode === -1) {
      setOriginFromLatLon(lat, lon);
    } else {
      setDestFromLatLon(lat, lon);
    }
  });

