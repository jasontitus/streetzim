// BEGIN polar
// A polar map: one whose tiles, search records and routing graph are not in
// real longitude and latitude. Antarctica cannot be drawn whole in Web
// Mercator (it ends at 85 S), so that map's data is moved, at build time, to
// Antarctic polar stereographic (EPSG:3031) metres read as Web Mercator
// metres: the pole sits at "map" position (0, 0), 0 E is up, 90 E is right.
// map-config.json says so with "projection": "antarctic-polar".
//
// Everything inside the viewer stays in map positions. Only what crosses to
// the real world goes through here: a GPS fix, coordinates shown or shared,
// distances and compass directions.
//   szToMap(lon, lat)   real -> map, [lon, lat]
//   szToTrue(lon, lat)  map -> real, [lon, lat]
// On every other map both return their input.
// BEGIN shared-polar  (the same block is in places.html; tests/viewer_ui_js.test.mjs)
var _szPolar = false;
var _SZ_POLAR = (function () {
  var A = 6378137, F = 1 / 298.257223563, E2 = F * (2 - F), E = Math.sqrt(E2);
  var RAD = Math.PI / 180;
  function t(phi) {   // phi: latitude counted towards the pole, radians
    var s = Math.sin(phi);
    return Math.tan(Math.PI / 4 - phi / 2) / Math.pow((1 - E * s) / (1 + E * s), E / 2);
  }
  var PHI_C = 71 * RAD;
  var K = A * Math.cos(PHI_C) / Math.sqrt(1 - E2 * Math.sin(PHI_C) * Math.sin(PHI_C)) / t(PHI_C);
  return {
    // Real places north of this have no position on the map.
    NORTH_LIMIT: -45,
    toMap: function (lon, lat) {
      var rho = K * t(-lat * RAD), lam = lon * RAD;
      var x = rho * Math.sin(lam), y = rho * Math.cos(lam);
      var sh = Math.sinh(y / A);
      return [x / A / RAD, Math.atan(sh) / RAD];
    },
    toTrue: function (mlon, mlat) {
      var x = A * mlon * RAD, y = A * Math.asinh(Math.tan(mlat * RAD));
      var rho = Math.sqrt(x * x + y * y);
      if (rho === 0) return [0, -90];
      var tt = rho / K, phi = Math.PI / 2 - 2 * Math.atan(tt);
      for (var i = 0; i < 8; i++) {
        var s = Math.sin(phi);
        phi = Math.PI / 2 - 2 * Math.atan(tt * Math.pow((1 - E * s) / (1 + E * s), E / 2));
      }
      return [Math.atan2(x, y) / RAD, -phi / RAD];
    },
    // Real metres per map (Web Mercator, at this map latitude) metre: what a
    // distance measured on the map as if it were real is to be multiplied by.
    scaleAt: function (mlon, mlat) {
      var phi = Math.min(-this.toTrue(mlon, mlat)[1] * RAD, Math.PI / 2 - 1e-7);
      var m = Math.cos(phi) / Math.sqrt(1 - E2 * Math.sin(phi) * Math.sin(phi));
      var k = K * t(phi) / (A * m);       // the projection's scale there
      return 1 / (k * Math.cos(mlat * RAD));
    }
  };
})();
function szToMap(lon, lat) { return _szPolar ? _SZ_POLAR.toMap(lon, lat) : [lon, lat]; }
function szToTrue(lon, lat) { return _szPolar ? _SZ_POLAR.toTrue(lon, lat) : [lon, lat]; }
// What a distance measured between two map positions as if they were real
// has to be multiplied by, to be the real distance.
function szMapScale(lat1, lon1, lat2, lon2) {
  return _szPolar ? _SZ_POLAR.scaleAt((lon1 + lon2) / 2, (lat1 + lat2) / 2) : 1;
}
// "lat, lon" of a map position as the reader knows coordinates: real ones.
function szCoordText(lat, lon, digits) {
  var t = szToTrue(lon, lat);
  return t[1].toFixed(digits) + ', ' + t[0].toFixed(digits);
}
// END shared-polar
// A GPS fix as a map position, or null for one that is not on the map. The
// heading turns with it: real north points away from the pole, which on the
// map is the direction of the fix's own longitude.
function _szPolarFix(pos) {
  var c = pos && pos.coords;
  if (!c || !(c.latitude <= _SZ_POLAR.NORTH_LIMIT)) return null;
  var m = _SZ_POLAR.toMap(c.longitude, c.latitude);
  var heading = c.heading;
  if (typeof heading === 'number' && isFinite(heading)) {
    heading = ((heading + c.longitude) % 360 + 360) % 360;
  }
  return {
    timestamp: pos.timestamp,
    coords: {
      latitude: m[1], longitude: m[0],
      accuracy: c.accuracy / _SZ_POLAR.scaleAt(m[0], m[1]),
      altitude: c.altitude, altitudeAccuracy: c.altitudeAccuracy,
      heading: heading, speed: c.speed
    }
  };
}
// Every reader of the device's position (the locate button, Directions, the
// driving mode) asks `geo`; on a polar map each gets map positions from it.
function _szPolarGps(geo) {
  if (!geo || geo._szPolarGps) return;
  var get = geo.getCurrentPosition, watch = geo.watchPosition;
  function moved(ok, err) {
    return function (pos) {
      var fix = _szPolarFix(pos);
      if (fix) { if (ok) ok(fix); return; }
      if (err) err({ code: 2, message: 'This position is not on this map.',
                     PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3 });
    };
  }
  try {
    geo.getCurrentPosition = function (ok, err, opts) { return get.call(geo, moved(ok, err), err, opts); };
    geo.watchPosition = function (ok, err, opts) { return watch.call(geo, moved(ok, err), err, opts); };
    geo._szPolarGps = true;
  } catch (e) { /* a frozen geolocation object: positions stay unconverted */ }
}
function szUsePolar(config, geo) {
  _szPolar = !!config && config.projection === 'antarctic-polar';
  if (_szPolar) _szPolarGps(geo);
}
// Meridians and parallels, with their names: on this map north is not up,
// and these are what a reader takes direction and position from.
function _szPolarGraticule() {
  var feats = [], lon, lat, i, ring;
  function at(lo, la) { return _SZ_POLAR.toMap(lo, la); }
  for (lon = -180; lon < 180; lon += 15) {
    feats.push({ type: 'Feature', properties: { w: lon % 90 === 0 ? 2 : 1 }, geometry: {
      type: 'LineString',
      coordinates: [at(lon, lon % 90 === 0 ? -90 : (lon % 30 === 0 ? -85 : -80)), at(lon, -60)]
    } });
    if (lon % 30 === 0) {
      feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: at(lon, -62) }, properties: {
        label: lon === 0 ? '0°' : lon === -180 ? '180°' : Math.abs(lon) + '°' + (lon > 0 ? 'E' : 'W')
      } });
    }
  }
  for (lat = -85; lat <= -60; lat += 5) {
    ring = [];
    for (i = 0; i <= 180; i++) ring.push(at(i * 2, lat));
    feats.push({ type: 'Feature', properties: { w: lat === -60 ? 2 : 1 },
                 geometry: { type: 'LineString', coordinates: ring } });
    if (lat < -60) {
      for (lon = 45; lon < 360; lon += 90) {
        feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: at(lon, lat) },
                     properties: { label: (-lat) + '°S' } });
      }
    }
  }
  return { type: 'FeatureCollection', features: feats };
}
function _szAddGraticule(style) {
  if (!_szPolar) return;
  style.sources['polar-graticule'] = { type: 'geojson', data: _szPolarGraticule() };
  style.layers.push({
    id: 'polar-graticule', type: 'line', source: 'polar-graticule',
    filter: ['==', ['geometry-type'], 'LineString'],
    paint: { 'line-color': '#5f7f95', 'line-opacity': 0.45,
             'line-width': ['case', ['==', ['get', 'w'], 2], 1, 0.6] }
  });
  style.layers.push({
    id: 'polar-graticule-label', type: 'symbol', source: 'polar-graticule',
    filter: ['==', ['geometry-type'], 'Point'],
    layout: { 'text-field': ['get', 'label'], 'text-font': ['OpenSansRegular'], 'text-size': 11 },
    paint: { 'text-color': '#4f6f85', 'text-halo-color': 'rgba(255,255,255,0.8)', 'text-halo-width': 1.2 }
  });
}
// The scale bar. MapLibre's measures along the map as if it were real ground.
function SzPolarScale(unit) { this._unit = unit; }
SzPolarScale.prototype.onAdd = function (map) {
  var self = this;
  this._map = map;
  this._el = document.createElement('div');
  this._el.className = 'maplibregl-ctrl maplibregl-ctrl-scale';
  this._draw = function () { self._update(); };
  map.on('move', this._draw);
  this._update();
  return this._el;
};
SzPolarScale.prototype.onRemove = function () {
  this._map.off('move', this._draw);
  if (this._el.parentNode) this._el.parentNode.removeChild(this._el);
  this._map = null;
};
SzPolarScale.prototype.setUnit = function (unit) { this._unit = unit; this._update(); };
SzPolarScale.prototype._update = function () {
  var map = this._map, c = map.getContainer(), y = c.clientHeight / 2, max = 100;
  var a = map.unproject([0, y]), b = map.unproject([max, y]);
  var metres = a.distanceTo(b) * szMapScale(a.lat, a.lng, b.lat, b.lng);
  var per = this._unit === 'imperial' ? (metres > 1609.344 ? 1609.344 : 0.3048) : (metres >= 1000 ? 1000 : 1);
  var name = this._unit === 'imperial' ? (per > 1 ? 'mi' : 'ft') : (per > 1 ? 'km' : 'm');
  var n = metres / per, p = Math.pow(10, Math.floor(Math.log(n) / Math.LN10)), d = n / p;
  var nice = (d >= 5 ? 5 : d >= 2 ? 2 : 1) * p;
  this._el.style.width = (max * nice / n) + 'px';
  this._el.textContent = nice + ' ' + name;
};
// END polar
