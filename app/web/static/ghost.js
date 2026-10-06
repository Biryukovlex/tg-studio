/* TGhost shared mascot lifecycle (T50).
 *
 * One interactive paper ghost lives in the header/sidebar junction
 * (`#ghost-trigger` in `app/web/templates/base.html`). The frozen artwork is
 * served from `app/web/static/brand/tgstudio-ghost-*.svg` (approved mouthless
 * face, open eyes, coral cheek) and injected inline so this module can drive
 * the single-play lifecycle without duplicating the character:
 *
 * - Overview: 6.2s raised clear magnifier, 144-unit diameter, eye
 *   magnification only while aligned, lens clears before lowering.
 * - Studio: 7s independent floating notebook/pencil writing.
 * - Settings: 8.6s two tightening strokes, lift/reseat, completion cue,
 *   put-away. Arms stay connected; flight/bank and hand rings/sparks kept.
 * - Idle: 3.4s downward scan after 11-19s of rest, interrupted by actions.
 *
 * Main action runs once after accepted page entry; click/Enter/Space on the
 * native button replays it (rapid pokes replace rather than queue).
 * Same-page navigation (hash changes) never replays. OS reduced motion and
 * hidden tabs stop/pause work; timers and listeners are cleaned up and
 * repeat initialisation is a no-op so remounts cannot duplicate actions.
 *
 * Decorative mascot timers only. This module never fabricates agent progress.
 */
(() => {
  "use strict";

  var GHOST_DESCRIPTIONS = {
    overview: "inspect through a magnifying glass",
    studio: "write by telekinesis",
    settings: "adjust a gear by telekinesis",
  };

  // Idle gaze scheduling bounds (ms): 11s fixed + up to 8s of jitter.
  var IDLE_REST_BASE_MS = 11000;
  var IDLE_REST_JITTER_MS = 8000;

  function motionReduced() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function describeMode(mode) {
    return GHOST_DESCRIPTIONS[mode] || GHOST_DESCRIPTIONS.overview;
  }

  function setTriggerLabel(trigger, mode) {
    var label = "Replay ghost animation: " + describeMode(mode);
    trigger.setAttribute("aria-label", label);
    trigger.setAttribute("title", "Poke to replay: " + describeMode(mode));
  }

  function parseSvg(text) {
    var parser = new DOMParser();
    var doc = parser.parseFromString(text, "image/svg+xml");
    var node = doc.querySelector("svg.ghost-scene");
    if (!node) return null;
    return document.importNode(node, true);
  }

  function neutralise(svg, mode) {
    svg.setAttribute("data-mode", mode);
    svg.setAttribute("data-playing", "false");
    svg.setAttribute("data-looking", "false");
    svg.setAttribute("aria-hidden", "true");
    svg.removeAttribute("width");
    svg.removeAttribute("height");
  }

  function initGhost(trigger) {
    if (!trigger || trigger.dataset.ghostReady === "true") return null;
    trigger.dataset.ghostReady = "true";

    var mode = trigger.dataset.ghostMode || "overview";
    // Jinja form submissions reload the document without switching screens.
    // Persist the last screen in this tab so filtering/reselecting stays idle.
    var enteredPage = true;
    try {
      var previousPage = window.sessionStorage.getItem("tg-studio:ghost-page");
      enteredPage = previousPage !== mode;
      window.sessionStorage.setItem("tg-studio:ghost-page", mode);
    } catch (err) { /* Storage restrictions keep normal entry behaviour. */ }

    var svg = trigger.querySelector("svg.ghost-scene");
    var lookTimer = null;
    var destroyed = false;
    var pendingFetch = null;

    function stopLook() {
      if (lookTimer !== null) {
        window.clearTimeout(lookTimer);
        lookTimer = null;
      }
      if (svg) svg.dataset.looking = "false";
    }

    function scheduleLook() {
      if (lookTimer !== null) {
        window.clearTimeout(lookTimer);
        lookTimer = null;
      }
      if (destroyed || !svg) return;
      if (document.hidden || motionReduced()) return;
      if (svg.dataset.playing === "true") return;
      lookTimer = window.setTimeout(function () {
        lookTimer = null;
        if (destroyed || !svg) return;
        if (document.hidden || motionReduced()) return;
        if (svg.dataset.playing === "true") return;
        svg.dataset.looking = "true";
      }, IDLE_REST_BASE_MS + Math.random() * IDLE_REST_JITTER_MS);
    }

    function stopGhost() {
      if (!svg) return;
      svg.dataset.playing = "false";
      scheduleLook();
    }

    function playGhost() {
      if (destroyed || !svg) return;
      stopLook();
      // Replace, never queue: restart from neutral even mid-action.
      svg.dataset.playing = "false";
      if (motionReduced()) return;
      void svg.getBoundingClientRect();
      svg.dataset.playing = "true";
    }

    function setMode(next) {
      if (!next || next === mode) return;
      mode = next;
      setTriggerLabel(trigger, mode);
      if (svg) {
        stopGhost();
        stopLook();
        svg.dataset.mode = mode;
        playGhost();
        return;
      }
      var src = trigger.dataset.ghostSrc;
      if (src) loadScene(src, true);
    }

    function bindScene(nextSvg) {
      if (svg && svg.parentNode === trigger) {
        trigger.removeChild(svg);
      }
      svg = nextSvg;
      neutralise(svg, mode);
      trigger.insertBefore(svg, trigger.firstChild);
      svg.addEventListener("animationend", onAnimationEnd);
    }

    function onAnimationEnd(event) {
      if (!event || !event.target || !event.target.classList) return;
      if (event.target.classList.contains("paper-float")) stopGhost();
      if (event.target.classList.contains("paper-look")) {
        stopLook();
        scheduleLook();
      }
    }

    function onTriggerClick() {
      playGhost();
    }

    function onVisibilityChange() {
      document.documentElement.toggleAttribute(
        "data-mascot-paused",
        document.hidden
      );
      if (destroyed) return;
      if (document.hidden) {
        stopLook();
      } else {
        scheduleLook();
      }
    }

    function onHashChange() {
      // Same-page navigation stays idle: never replay the action.
    }

    function loadScene(src, autoplay) {
      if (pendingFetch && typeof pendingFetch.abort === "function") {
        try {
          pendingFetch.abort();
        } catch (err) {
          /* ignore abort failures and continue with the newest source */
        }
      }
      var controller = null;
      if (typeof AbortController !== "undefined") {
        controller = new AbortController();
        pendingFetch = controller;
      }
      var options = { credentials: "same-origin" };
      if (controller) options.signal = controller.signal;
      window
        .fetch(src, options)
        .then(function (response) {
          if (!response.ok) throw new Error("ghost asset unavailable");
          return response.text();
        })
        .then(function (text) {
          if (destroyed) return;
          var next = parseSvg(text);
          if (!next) return;
          bindScene(next);
          if (autoplay) playGhost();
          else scheduleLook();
        })
        .catch(function () {
          // Keep the static fallback mark; never throw from decoration.
          if (!destroyed) scheduleLook();
        });
    }

    function destroy() {
      destroyed = true;
      if (lookTimer !== null) {
        window.clearTimeout(lookTimer);
        lookTimer = null;
      }
      trigger.removeEventListener("click", onTriggerClick);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("hashchange", onHashChange);
      if (svg) svg.removeEventListener("animationend", onAnimationEnd);
      if (pendingFetch && typeof pendingFetch.abort === "function") {
        try {
          pendingFetch.abort();
        } catch (err) {
          /* ignore */
        }
      }
      delete trigger.dataset.ghostReady;
      document.documentElement.removeAttribute("data-mascot-paused");
    }

    setTriggerLabel(trigger, mode);
    trigger.addEventListener("click", onTriggerClick);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("hashchange", onHashChange);
    document.documentElement.toggleAttribute(
      "data-mascot-paused",
      document.hidden
    );

    if (svg) {
      neutralise(svg, mode);
      svg.addEventListener("animationend", onAnimationEnd);
      // Accepted page entry plays once; reduced motion stays neutral.
      if (enteredPage && !document.hidden) playGhost();
      else scheduleLook();
    } else {
      var src = trigger.dataset.ghostSrc;
      if (src && typeof window.fetch === "function") {
        loadScene(src, enteredPage && !document.hidden && !motionReduced());
        if (motionReduced() || document.hidden) scheduleLook();
      }
    }

    return { play: playGhost, stop: stopGhost, setMode: setMode, destroy: destroy };
  }

  function initAll(root) {
    var scope = root || document;
    var found = [];
    scope.querySelectorAll("#ghost-trigger").forEach(function (trigger) {
      var handle = initGhost(trigger);
      if (handle) found.push(handle);
    });
    return found;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function onReady() {
      document.removeEventListener("DOMContentLoaded", onReady);
      initAll(document);
    });
  } else {
    initAll(document);
  }

  window.TgGhost = { init: initGhost, initAll: initAll };
})();
