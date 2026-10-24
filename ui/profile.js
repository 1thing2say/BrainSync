// Profile page: edit who the user is and how they talk (app/config.py Profile), then save it to
// the server, which uses it for every reply Claude writes from then on. No state lives here.
"use strict";

const $ = (id) => document.getElementById(id);
const LISTS = ["likes", "dislikes", "routines", "avoid_topics"];
const STYLE_LISTS = ["phrases", "avoid_phrases"];
const CHOICES = ["formality", "length", "humour", "warmth", "directness"];

const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);

function fill(p) {
  $("name").value = p.name;
  $("about").value = p.about;
  for (const k of LISTS) $(k).value = p[k].join("\n");
  $("people").value = p.people.map((x) => (x.relation ? `${x.name}, ${x.relation}` : x.name)).join("\n");
  for (const k of STYLE_LISTS) $(k).value = p.personality[k].join("\n");
  $("notes").value = p.personality.notes;
  for (const k of CHOICES) {
    const input = document.querySelector(`input[name="${k}"][value="${p.personality[k]}"]`);
    if (input) input.checked = true;
  }
}

function read() {
  const personality = { notes: $("notes").value.trim() };
  for (const k of STYLE_LISTS) personality[k] = lines($(k).value);
  for (const k of CHOICES) {
    const checked = document.querySelector(`input[name="${k}"]:checked`);
    if (checked) personality[k] = checked.value;
  }
  const profile = { name: $("name").value.trim(), about: $("about").value.trim(), personality };
  for (const k of LISTS) profile[k] = lines($(k).value);
  profile.people = lines($("people").value).map((line) => {
    const [name, ...rest] = line.split(",");
    return { name: name.trim(), relation: rest.join(",").trim() };
  });
  return profile;
}

function show(state) {
  fill(state.profile);
  const where = { user: "Using the saved profile.", demo: "Using the fictional demo persona (Sam).",
                  given: "Using a profile set by the server." }[state.source] || "";
  $("source").textContent = where + (state.editable ? "" : " Saving is off on this server.");
  $("save").disabled = $("reset").disabled = !state.editable;
  $("preview").disabled = !state.llm;
  $("preview").title = state.llm ? "" : `Claude is off: ${state.llm_off_reason}`;
}

function say(text, ok = false) {
  $("message").textContent = text;
  $("message").style.color = ok ? "var(--say)" : "";
}

async function call(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(d && d.problems ? d.problems.join("; ") : d || `HTTP ${r.status}`);
  }
  return data;
}

$("profile-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    show(await call("PUT", "/api/profile", read()));
    say("Saved. The next page of replies uses it.", true);
  } catch (e) {
    say(`Not saved: ${e.message}`);
  }
});

$("reset").addEventListener("click", async () => {
  if (!confirm("Delete the saved profile and go back to the demo persona?")) return;
  try {
    show(await call("DELETE", "/api/profile"));
    say("Back to the demo persona.", true);
  } catch (e) {
    say(e.message);
  }
});

$("preview-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const out = $("preview-out");
  out.replaceChildren();
  $("preview").disabled = true;
  $("preview").textContent = "Asking Claude…";
  try {
    const r = await call("POST", "/api/profile/preview",
                         { profile: read(), partner_text: $("preview-text").value });
    if (!r.options) {
      say("Claude gave no usable page this time (see the log); try again.");
    } else {
      out.replaceChildren(...r.options.map((o) => {
        const li = document.createElement("li");
        li.textContent = `${o.utterance}  [${o.label}]`;
        return li;
      }));
      say("");
    }
  } catch (e) {
    say(`Preview failed: ${e.message}`);
  } finally {
    $("preview").disabled = false;
    $("preview").textContent = "Preview replies";
  }
});

call("GET", "/api/profile").then(show).catch((e) => say(`Could not load the profile: ${e.message}`));
