/*
 * BrainPoint options: the server address and the token, kept in chrome.storage.local.
 *
 *     serverUrl  ws://127.0.0.1:8765/ws (the default), or wss://<name>.trycloudflare.com/ws
 *                through a cloudflared tunnel
 *     token      the token `python -m brainsync.server` prints (empty when it runs --no-auth)
 *
 * The service worker reads both when it opens a connection and reconnects every running tab
 * when either changes. The token is shown masked and never logged. checkUrl and cleanToken
 * are exported for clients/chrome/test/options.test.mjs.
 */
(function (root) {
  "use strict";

  const DEFAULT_URL = "ws://127.0.0.1:8765/ws";

  /** {url} for a usable server address, or {error} with a sentence that says how to fix it. */
  function checkUrl(text) {
    const s = String(text === undefined || text === null ? "" : text).trim();
    if (!s) return { url: DEFAULT_URL };
    let u;
    try { u = new URL(s); } catch (e) {
      return { error: "That is not an address. Use ws://127.0.0.1:8765/ws, or wss://<host>/ws for a remote server." };
    }
    if (u.protocol !== "ws:" && u.protocol !== "wss:") {
      return { error: "The address must start with ws:// or wss://, for example ws://127.0.0.1:8765/ws." };
    }
    if (u.username || u.password) {
      return { error: "Leave the user name and password out of the address; put the token in the Token field." };
    }
    if (u.hash) return { error: "Leave the #fragment out of the address; put the token in the Token field." };
    if (u.pathname === "/" || u.pathname === "") u.pathname = "/ws";
    return { url: u.toString() };
  }

  /** The token without surrounding spaces, or null for none. */
  function cleanToken(text) {
    const s = String(text === undefined || text === null ? "" : text).trim();
    return s ? s : null;
  }

  function start(doc, chromeApi) {
    const urlEl = doc.getElementById("serverUrl");
    const tokenEl = doc.getElementById("token");
    const statusEl = doc.getElementById("status");
    const say = (text, bad) => { statusEl.textContent = text; statusEl.className = bad ? "bad" : "ok"; };

    chromeApi.storage.local.get(["serverUrl", "token"]).then((got) => {
      urlEl.value = typeof got.serverUrl === "string" ? got.serverUrl : DEFAULT_URL;
      tokenEl.value = typeof got.token === "string" ? got.token : "";
    });

    doc.getElementById("form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const checked = checkUrl(urlEl.value);
      if (checked.error) { say(checked.error, true); return; }
      const token = cleanToken(tokenEl.value);
      const save = { serverUrl: checked.url };
      const done = () => {
        urlEl.value = checked.url;
        say("Saved. Running tabs reconnect to " + checked.url + (token ? " with the new token." : " without a token."), false);
      };
      const store = chromeApi.storage.local;
      const work = token === null
        ? store.set(save).then(() => store.remove("token"))
        : store.set(Object.assign(save, { token }));
      work.then(done, (e) => say("Could not save: " + e.message, true));
    });

    doc.getElementById("reset").addEventListener("click", () => {
      urlEl.value = DEFAULT_URL;
      say("Press Save to use the default address.", false);
    });
  }

  const api = Object.freeze({ DEFAULT_URL, checkUrl, cleanToken, start });
  root.BrainPointOptions = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (root.document && root.chrome && root.chrome.storage) {
    start(root.document, root.chrome);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
