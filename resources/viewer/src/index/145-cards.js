// BEGIN sz-cards
// One panel at a time over the map, as the big map apps do: Nearby
// (Wikipedia), Directions, and Find results each fill the same room, and
// opened together they lay over each other (Nearby's sheet over the
// Directions panel's Start button, a Find strip over both). Opening one
// hides the one showing and keeps it underneath; closing the top one brings
// back the one under it, as it was left. A hidden panel keeps its state:
// only the class .sz-card-hidden takes it off the screen.
//
//   szCardDefine(name, { el: () => Element, suspend(), resume() })
//     el: the panel's element (read each time: some are made anew);
//     suspend/resume: what else goes or comes back with it (the
//     Directions panel hides the search box while it shows)
//   szCardOpen(name)    the panel opened or was asked for again: on top
//   szCardClosed(name)  the panel closed itself: out of the stack, and the
//                       one under it comes back if it was on top
//   szCardHidden(name)  open but under another panel
var _szCards = { order: [], defs: {} };
function szCardDefine(name, def) { _szCards.defs[name] = def || {}; }
function _szCardShow(name, shown, hooks) {
  var d = _szCards.defs[name];
  if (!d) return;
  var el = null;
  try { el = d.el ? d.el() : null; } catch (e) {}
  if (el && el.classList) {
    if (shown) el.classList.remove('sz-card-hidden'); else el.classList.add('sz-card-hidden');
  }
  var f = hooks ? (shown ? d.resume : d.suspend) : null;
  if (typeof f === 'function') { try { f(); } catch (e) {} }
}
function szCardOpen(name) {
  var o = _szCards.order, at = o.indexOf(name);
  if (at === o.length - 1 && at >= 0) { _szCardShow(name, true, false); return; }
  if (o.length) _szCardShow(o[o.length - 1], false, true);
  if (at >= 0) o.splice(at, 1);
  o.push(name);
  _szCardShow(name, true, at >= 0);
}
function szCardClosed(name) {
  var o = _szCards.order, at = o.indexOf(name);
  if (at < 0) return;
  var wasTop = at === o.length - 1;
  o.splice(at, 1);
  _szCardShow(name, true, false);     // no class left on a closed panel
  if (wasTop && o.length) _szCardShow(o[o.length - 1], true, true);
}
function szCardHidden(name) {
  var o = _szCards.order, at = o.indexOf(name);
  return at >= 0 && at < o.length - 1;
}
// END sz-cards
