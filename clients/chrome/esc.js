/*
 * BrainPoint Esc: the stop key, in every frame of every page, from the first moment.
 *
 *     const off = BrainPointEsc.on(() => overlay.escape())   // top frame, point_overlay.js
 *     // a sub-frame forwards Esc to the worker: chrome.runtime.sendMessage({type: "_esc"})
 *
 * PROTOCOL-v3 section 4, rule 1: Esc always stops the flicker locally and sends `pause`. The
 * overlay lives in the top frame only, and a key pressed while an iframe has the focus (an
 * embedded editor, a widget, a field BrainPoint itself clicked inside a same-origin frame)
 * never reaches the top window. So this script runs in every frame (all_frames, about:blank
 * frames too) at document_start, before any page script, and listens to keydown in the
 * capture phase on both the window and the document. A page listener added later cannot stop
 * the event before ours runs, since listeners on one target run in the order they were added
 * and the window's capture listeners run first.
 *
 * In the top frame, Esc calls every handler registered with on(): the overlay's. In a
 * sub-frame it sends {type: "_esc"} to the service worker, which sends `pause` to the server
 * on this tab's connection and forwards {type: "_esc"} to the top frame's content script. One
 * key press is handled once, whichever listener sees it first. The script does nothing else:
 * it reads no page content and sends nothing but that message.
 *
 * clients/chrome/test/esc.test.mjs runs install() on plain objects in node.
 */
(function (root) {
  "use strict";

  const ESC_MESSAGE = "_esc";

  function isEsc(e) {
    return Boolean(e) && (e.key === "Escape" || e.key === "Esc" || e.keyCode === 27);
  }

  /**
   * Listen for Esc in one frame. opts: win, doc, top (true in the top frame), send(msg) (used
   * in a sub-frame). Returns {on(fn) -> off, handle(e), remove()}.
   */
  function install(opts) {
    const win = opts.win, doc = opts.doc;
    const top = Boolean(opts.top);
    const send = opts.send || (() => {});
    const handlers = new Set();
    const seen = new WeakSet();

    function handle(e) {
      if (!isEsc(e)) return false;
      if (typeof e === "object") {
        if (seen.has(e)) return false;
        seen.add(e);
      }
      if (top) {
        for (const f of Array.from(handlers)) {
          try { f(e); } catch (err) { /* one broken handler must not stop the others */ }
        }
      } else {
        try { send({ type: ESC_MESSAGE }); } catch (err) { /* the extension was reloaded */ }
      }
      return true;
    }

    if (win && typeof win.addEventListener === "function") win.addEventListener("keydown", handle, true);
    if (doc && typeof doc.addEventListener === "function") doc.addEventListener("keydown", handle, true);

    return {
      top,
      on(fn) {
        handlers.add(fn);
        return () => { handlers.delete(fn); };
      },
      handle,
      remove() {
        if (win && typeof win.removeEventListener === "function") win.removeEventListener("keydown", handle, true);
        if (doc && typeof doc.removeEventListener === "function") doc.removeEventListener("keydown", handle, true);
        handlers.clear();
      },
    };
  }

  function isTop(win) {
    try { return win.top === win; } catch (e) { return false; }
  }

  const api = { ESC_MESSAGE, isEsc, install, isTop };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.freeze(api);
  } else if (root.chrome && root.chrome.runtime && root.document && !root.BrainPointEsc) {
    const chromeApi = root.chrome;
    const hub = install({
      win: root, doc: root.document, top: isTop(root),
      send: (msg) => chromeApi.runtime.sendMessage(msg, () => { void chromeApi.runtime.lastError; }),
    });
    root.BrainPointEsc = Object.freeze(Object.assign({}, api, { on: hub.on, handle: hub.handle }));
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
