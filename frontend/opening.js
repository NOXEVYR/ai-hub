/* Decorative document opening only: no routing, storage, focus, or API work. */
(function () {
  'use strict';
  const layer = document.getElementById('opening');
  if (!layer) return;
  let ended = false, started = false, frame = 0, timer = 0, motion = null;
  const interactionEvents = ['pointerdown', 'click', 'keydown'];

  function finish() {
    if (ended) return;
    ended = true;
    // Hide first. Even a later cleanup failure must leave the workbench available.
    layer.hidden = true;
    layer.remove();
    window.clearTimeout(timer);
    window.cancelAnimationFrame(frame);
    window.removeEventListener('load', queueOpening);
    window.removeEventListener('pagehide', finish);
    document.removeEventListener('visibilitychange', onVisibility);
    for (const type of interactionEvents) document.removeEventListener(type, finish, true);
    if (motion?.removeEventListener) motion.removeEventListener('change', onMotion);
    else motion?.removeListener?.(onMotion);
  }
  function onVisibility() { if (document.hidden) finish(); }
  function onMotion() { if (motion.matches) finish(); }
  function start() {
    if (ended || started) return;
    const mark = layer.querySelector('img');
    // A failed stylesheet or image skips decoration instead of adding a raw layer.
    if (document.hidden || motion.matches || !mark?.complete || !mark.naturalWidth
        || window.getComputedStyle(layer).getPropertyValue('--lc-opening-ready').trim() !== '1') {
      finish(); return;
    }
    started = true;
    timer = window.setTimeout(finish, 1400);
    layer.classList.add('is-playing');
    layer.hidden = false;
  }
  function queueOpening() {
    if (ended) return;
    // Native WebView reveals after navigation completes; allow its first visible paint.
    frame = window.requestAnimationFrame(() => {
      if (!ended) frame = window.requestAnimationFrame(start);
    });
  }

  try {
    motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (document.hidden || motion.matches) { finish(); return; }
    // Capture only observes the event. The same click/key still reaches the workbench.
    for (const type of interactionEvents) document.addEventListener(type, finish, {capture: true, passive: true});
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', finish, {once: true});
    if (motion.addEventListener) motion.addEventListener('change', onMotion);
    else motion.addListener?.(onMotion);
    layer.addEventListener('animationend', event => {
      if (event.target === layer && event.animationName === 'lc-opening-unveil') finish();
    });
    layer.addEventListener('animationcancel', event => { if (event.target === layer) finish(); });
    if (document.readyState === 'complete') queueOpening();
    else window.addEventListener('load', queueOpening, {once: true});
  } catch (_) {
    finish();
  }
})();
