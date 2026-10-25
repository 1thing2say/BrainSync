// What the session page shows, as pure functions of the server's messages: the state line, the
// one-line health summary, the mic card and the selection progress. Pure, so node can test it
// (tests/js/view_core.test.js); app.js only puts the results on the page. Loaded before app.js.
"use strict";

const ViewCore = (() => {
  // The state line: what the user is doing now, and its tone (a colour in style.css).
  const BANNER = {
    IDLE: ["Waiting to start", "idle"],
    BASELINE: ["Baseline: keep still", "idle"],
    PREPARING: ["Finding replies…", "idle"],
    MENU_CUE: ["Read the options", "cue"],
    STIMULATING: ["Look at a reply, or More options", "look"],
    CONFIRMING: ["Say it, or Back?", "look"],
    SELECTED: ["Selected", "picked"],
    SPEAKING: ["Speaking", "say"],
    REST: ["Rest", "idle"],
    NOT_CAUGHT: ["Didn't catch that: look again", "warn"],
    PAUSED: ["Paused", "stop"],
  };

  // ``mode`` is the shown page's mode: list mode has SAY / NEXT instead of a reply per target.
  function banner(name, detail, mode) {
    // c-VEP: the BASELINE step trains ("Training: look at the left square, cycle 12/30").
    if (name === "BASELINE" && /^Training/.test(detail || "")) return { text: detail, tone: "look" };
    let [text, tone] = BANNER[name] || [name, "idle"];
    if (name === "STIMULATING" && mode === "list") text = "Look at SAY or NEXT";
    // The spoken sentence has its own place on the page; other details follow the state.
    if (detail && name !== "SPEAKING") text += ` — ${detail}`;
    return { text, tone };
  }

  // On the page the state line sits in the still centre of the diamond and says something only
  // when the user is not picking: while they read, look, confirm or hear the reply it is empty,
  // so nothing new appears between the targets.
  const QUIET = new Set(["MENU_CUE", "STIMULATING", "CONFIRMING", "SELECTED", "SPEAKING", "REST"]);

  function status(name, detail, mode) {
    const b = banner(name, detail, mode);
    return QUIET.has(name) ? { text: "", tone: b.tone } : b;
  }

  const MIC_PROBLEMS = new Set(["denied", "nomic", "offline", "unsupported", "error", "needs_tap", "hidden"]);
  // The mic card: a short title, the detail line and a tone. ``state`` comes from the listener
  // (ui/listen_core.js), ``text`` is what it heard so far (shown while the partner talks).
  const MIC = {
    listening: ["Listening", "Speak normally: lines are sent when you pause."],
    sent: ["Heard", ""],
    stopped: ["Not listening", ""],
    idle: ["Mic off", "The mic turns on once the session starts."],
    reconnect: ["Not listening", ""],
    speaking: ["Mic off", "Off while the reply plays, so it never hears itself."],
    paused: ["Mic off (paused)", "Tap Listen to speak once."],
    muted: ["Muted", "Nothing is heard or sent. Tap Unmute to listen."],
    disconnected: ["Mic off", "The server is not connected."],
    needs_tap: ["Tap to start the mic", "Chrome wants a tap first: tap anywhere."],
    hidden: ["Keep this tab visible", "Listening may pause while the tab is hidden."],
    denied: ["Microphone blocked", "Allow it in Chrome's address bar, then tap Restart mic."],
    nomic: ["No microphone", "Plug the lapel mic in."],
    offline: ["Speech recognition offline", "Retrying."],
    unsupported: ["No speech recognition", "Use Chrome."],
    error: ["Speech recognition failed", "Tap Restart mic."],
  };

  function micCard(state, text, muted) {
    if (muted) state = "muted";
    const [title, detail] = MIC[state] || MIC.stopped;
    let tone = "off";
    if (state === "listening" || state === "sent") tone = "on";
    else if (state === "muted") tone = "muted";
    else if (MIC_PROBLEMS.has(state)) tone = "warn";
    // While the partner talks, their words so far replace the instructions.
    const heard = !muted && (state === "listening" || state === "sent") && text ? text : "";
    return { title, detail: heard ? "" : detail, heard, tone };
  }

  // One line for the operator: "Ready", or what is wrong. Keyboard mode needs no decoder or
  // display; a fake decoder or keyboard mode is a flag, shown separately and always.
  function health(s) {
    const problems = [];
    const warnings = [];
    if (!s.keyboard_mode) {
      if (!s.decoder_connected) problems.push("No decoder");
      else if (!s.decoder_ok) problems.push("Decoder not OK");
      if (s.decoder_connected && !s.display_ok) problems.push("Display off");
      if (s.decoder_connected && !s.board_ok) warnings.push("Board?");
    }
    if (MIC_PROBLEMS.has(s.mic) && s.mic !== "hidden") problems.push(`Mic: ${MIC[s.mic][0].toLowerCase()}`);
    // Before Claude's first page there is nothing to warn about; an older server sends no "llm".
    const llm = s.llm || (s.llm_ok ? "ok" : "off");
    if (llm === "off") warnings.push("Fixed replies only (no Claude)");
    else if (llm === "failed") warnings.push("Claude's last page failed: fixed list shown");
    if (s.mic_muted) warnings.push("Mic muted");
    const flags = [];
    if (s.fake_decoder) flags.push("FAKE DECODER");
    if (s.keyboard_mode) flags.push("KEYBOARD");
    const level = problems.length ? "bad" : warnings.length ? "warn" : "ok";
    const text = problems.length ? problems.join(" · ") : warnings.length ? `Ready · ${warnings.join(" · ")}` : "Ready";
    return { level, text, problems, warnings, flags };
  }

  // The selection timer: how full the bar is and its label, ``elapsed`` seconds into a trial that
  // the server ends after ``max`` seconds.
  function progress(elapsed, max) {
    const t = Math.max(0, elapsed);
    const fraction = max > 0 ? Math.min(1, t / max) : 0;
    return { fraction, label: `${Math.floor(t)} s` };
  }

  // c-VEP: the bar under each square from a ``confidence`` message: the correlation clipped to
  // 0..1 (a steady fill; scores are not probabilities, so no percent), the signed score, and
  // whether the slot is this decision's best and the committed one.
  function confidence(msg) {
    const out = {};
    for (const [slot, raw] of Object.entries((msg && msg.scores) || {})) {
      const v = Number.isFinite(raw) ? raw : 0;
      out[slot] = {
        fraction: Math.min(1, Math.max(0, v)),
        text: `${v < 0 ? "-" : "+"}${Math.abs(v).toFixed(2)}`,
        best: slot === msg.raw,
        committed: slot === msg.committed,
      };
    }
    return out;
  }

  // c-VEP: the training card from a ``training`` message.
  function training(msg) {
    const of = msg.of > 0 ? msg.of : 1;
    const fraction = Math.min(1, Math.max(0, (msg.cycle || 0) / of));
    if (msg.accuracy !== null && msg.accuracy !== undefined) {
      const chance = msg.chance !== null && msg.chance !== undefined
        ? ` (chance ${Math.round(msg.chance * 100)} %)` : "";
      return { title: "Template ready", detail: `${Math.round(msg.accuracy * 100)} % on held-out cycles${chance}`,
               fraction: 1, tone: "ok", done: true };
    }
    if (msg.message) return { title: "Training failed", detail: msg.message, fraction, tone: "bad", done: true };
    const where = msg.label ? `the ${msg.label} square` : "the first square";
    return { title: `Training: look at ${where}`, detail: `cycle ${msg.cycle || 0} of ${of}`, fraction,
             tone: "look", done: false };
  }

  return { banner, status, micCard, health, progress, confidence, training, MIC_PROBLEMS };
})();

if (typeof module !== "undefined") module.exports = ViewCore;
