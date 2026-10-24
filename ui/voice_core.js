// Choosing the browser's fallback voice (speechSynthesis), used when a reply has no Cartesia
// clip. Pure, so node can test it (tests/js/voice_core.test.js). Loaded before speech.js.
"use strict";

const VoiceCore = (() => {
  // macOS novelty voices: never the user's voice.
  const NOVELTY = new Set(["albert", "bad news", "bahh", "bells", "boing", "bubbles", "cellos",
    "good news", "jester", "organ", "superstar", "trinoids", "whisper", "wobble", "zarvox",
    "fred", "junior", "ralph", "kathy", "grandma", "grandpa", "rocko", "shelley", "flo",
    "eddy", "reed", "sandy"]);

  // Higher is better. A local voice always beats a network one: Chrome's "Google" voices and
  // Edge's "Online (Natural)" voices need the internet, the very thing that is down when this
  // voice is used. Among local voices, macOS's downloaded Premium and Enhanced voices sound far
  // more natural than the compact defaults, and en-US matches the Cartesia voice.
  function score(v) {
    const name = (v.name || "").toLowerCase();
    const lang = (v.lang || "").replace("_", "-").toLowerCase();
    if (!lang.startsWith("en")) return -1;
    let s = 0;
    if (v.localService) s += 100;
    if (v.localService && /\bpremium\b/.test(name)) s += 30;
    else if (v.localService && /\benhanced\b/.test(name)) s += 20;
    else if (v.localService && /\b(natural|neural)\b/.test(name)) s += 15;
    if (lang === "en-us") s += 10;
    else if (lang === "en-gb" || lang === "en-au" || lang === "en-ca") s += 5;
    if (NOVELTY.has(name.replace(/\s*\(.*\)$/, "").trim())) s -= 60;
    if (v.default) s += 1;
    return s;
  }

  // The best English voice in ``voices``, or null (speechSynthesis then uses its default).
  function best(voices) {
    let pick = null;
    let top = -1;
    for (const v of voices || []) {
      const s = score(v);
      if (s > top) { pick = v; top = s; }
    }
    return pick;
  }

  return { score, best };
})();

if (typeof module !== "undefined") module.exports = VoiceCore;
