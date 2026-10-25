// hints.js on plain-object pages: what counts as clickable and visible, the text rules, the
// password masks, the 200 cap and the reading order. No DOM, no browser.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { el, page } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const H = require("../hints.js");

const R = (x, y, w, h) => ({ x, y, w, h });

test("collects the clickable kinds with role, text, tag and viewport box", () => {
  const els = [
    el("a", { attrs: { href: "/home" }, rect: R(10, 10, 80, 20), text: "Home" }),
    el("button", { rect: R(100, 10, 60, 30), text: "  Sign\n in  " }),
    el("input", { type: "submit", attrs: { value: "Go" }, rect: R(200, 10, 40, 30) }),
    el("input", { type: "checkbox", attrs: { "aria-label": "Remember me" }, rect: R(260, 10, 16, 16) }),
    el("input", { type: "email", attrs: { placeholder: "Email" }, rect: R(300, 10, 200, 30) }),
    el("select", { attrs: { title: "Country" }, rect: R(520, 10, 100, 30) }),
    el("textarea", { attrs: { placeholder: "Message" }, rect: R(640, 10, 100, 60) }),
    el("div", { attrs: { role: "tab" }, rect: R(10, 100, 60, 30), text: "Tab A" }),
    el("li", { attrs: { role: "menuitem" }, rect: R(80, 100, 60, 30), text: "Copy" }),
    el("span", { attrs: { role: "switch", onclick: "x()" }, rect: R(150, 100, 30, 20), text: "Dark" }),
    el("div", { attrs: { onclick: "go()" }, rect: R(200, 100, 50, 30), text: "Card" }),
    el("div", { attrs: { tabindex: "0" }, rect: R(260, 100, 50, 30), text: "Tile", style: { cursor: "pointer" } }),
  ];
  const { doc, win } = page(els);
  const got = H.collect(doc, win);
  assert.deepEqual(got.viewport, { w: 1000, h: 800, dpr: 2 });
  assert.deepEqual(got.masks, []);
  const by = Object.fromEntries(got.hints.map((h) => [h.text, h]));
  assert.deepEqual(by.Home, { id: 0, x: 10, y: 10, w: 80, h: 20, role: "link", text: "Home", tag: "a" });
  assert.equal(by["Sign in"].role, "button");
  assert.equal(by.Go.role, "button");
  assert.equal(by["Remember me"].role, "checkbox");
  assert.equal(by.Email.role, "input");
  assert.equal(by.Country.role, "input");
  assert.equal(by.Message.role, "input");
  assert.equal(by["Tab A"].role, "tab");
  assert.equal(by.Copy.role, "menu_item");
  assert.equal(by.Dark.role, "checkbox");
  assert.equal(by.Card.role, "other");
  assert.equal(by.Tile.role, "other");
  assert.equal(got.hints.length, 12);
  assert.deepEqual(got.hints.map((h) => h.id), [...Array(12).keys()]);
});

test("not clickable: plain divs, a without href, hidden inputs, tabindex without a pointer cursor", () => {
  const { doc, win } = page([
    el("div", { rect: R(0, 0, 50, 50), text: "plain" }),
    el("a", { rect: R(60, 0, 50, 50), text: "anchor" }),
    el("input", { type: "hidden", rect: R(120, 0, 50, 50) }),
    el("div", { attrs: { tabindex: "-1" }, rect: R(180, 0, 50, 50), text: "focus only" }),
    el("a", { attrs: { href: "#", role: "presentation" }, rect: R(240, 0, 50, 50), text: "kept" }),
  ]);
  assert.deepEqual(H.collect(doc, win).hints.map((h) => h.text), ["kept"]);
});

test("password fields give no text, are masked, and their value never leaks", () => {
  const SECRET = "hunter2-very-secret";
  const label = el("label", { attrs: { id: "pwl" }, rect: R(0, 0, 0, 0), text: "Password" });
  label.textContent = "Password";
  const pw = el("input", { type: "password", value: SECRET, attrs: { value: SECRET, "aria-label": "Password",
                                                                        "aria-labelledby": "pwl" },
                          rect: R(100, 100, 200, 30), text: SECRET });
  const pwByAttr = el("input", { attrs: { type: "PASSWORD", value: SECRET }, rect: R(100, 150, 200, 30) });
  const card = el("input", { type: "text", value: "4111111111111111",
                            attrs: { autocomplete: "cc-number", placeholder: "Card number" }, rect: R(100, 200, 200, 30) });
  const otp = el("input", { type: "text", value: "123456", attrs: { autocomplete: "one-time-code" },
                           rect: R(100, 250, 200, 30) });
  const typed = el("input", { type: "text", value: "my typed words", rect: R(100, 300, 200, 30) });
  const area = el("textarea", { value: "private note", text: "private note", rect: R(100, 350, 200, 60) });
  // aria-labelledby pointing at a password input must not read it either
  const pointer = el("button", { attrs: { "aria-labelledby": "pwid" }, rect: R(400, 500, 60, 30), text: "" });
  pw.attrs.id = "pwid";
  pw.textContent = SECRET;
  const { doc, win } = page([label, pw, pwByAttr, card, otp, typed, area, pointer]);
  const got = H.collect(doc, win);
  const json = JSON.stringify(got);
  for (const s of [SECRET, "4111111111111111", "123456", "my typed words", "private note"]) {
    assert.ok(!json.includes(s), "leaked " + s);
  }
  const byY = Object.fromEntries(got.hints.map((h) => [h.y, h]));
  assert.equal(byY[100].tag, "input");
  assert.equal(byY[100].text, "");
  assert.equal(byY[150].text, "");
  assert.equal(byY[200].text, "", "secret autocomplete fields give no text, not even a placeholder");
  assert.equal(byY[300].text, "");
  assert.equal(byY[350].text, "");
  assert.equal(byY[500].text, "", "aria-labelledby never reads a password field");
  // masks: the two password inputs, the card number and the one-time code
  assert.deepEqual(got.masks, [R(100, 100, 200, 30), R(100, 150, 200, 30), R(100, 200, 200, 30), R(100, 250, 200, 30)]);
  assert.ok(H.isSecret(pw) && H.isSecret(pwByAttr) && H.isSecret(card) && H.isSecret(otp));
  assert.ok(!H.isSecret(typed) && !H.isSecret(area));
});

test("password masks cover covered and partly visible fields, but not display:none ones", () => {
  const pwCovered = el("input", { type: "password", rect: R(10, 10, 100, 20) });
  const cover = el("div", { rect: R(0, 0, 500, 500) });
  const pwGone = el("input", { type: "password", rect: R(10, 50, 100, 20), style: { display: "none" } });
  const pwEdge = el("input", { type: "password", rect: R(-50, 780, 100, 40) });
  const pwOff = el("input", { type: "password", rect: R(2000, 10, 100, 20) });
  const { doc, win } = page([pwCovered, pwGone, pwEdge, pwOff, cover]);
  const got = H.collect(doc, win);
  assert.deepEqual(got.hints.map((h) => h.y), [780], "the covered field is no hint");
  assert.deepEqual(got.masks, [R(10, 10, 100, 20), R(0, 780, 50, 20)]);
});

test("visibility: hidden styles, zero size, off screen, covered and checkVisibility are filtered", () => {
  const els = [
    el("button", { rect: R(0, 0, 50, 20), text: "none", style: { display: "none" } }),
    el("button", { rect: R(0, 30, 50, 20), text: "hidden", style: { visibility: "hidden" } }),
    el("button", { rect: R(0, 60, 50, 20), text: "collapse", style: { visibility: "collapse" } }),
    el("button", { rect: R(0, 90, 50, 20), text: "transparent", style: { opacity: "0" } }),
    el("button", { rect: R(0, 120, 0, 20), text: "zero" }),
    el("button", { rect: R(0, 900, 50, 20), text: "below" }),
    el("button", { rect: R(-80, 150, 50, 20), text: "left" }),
    el("button", { rect: R(0, 180, 50, 20), text: "ancestor hidden", checkVisibility: () => false }),
    el("button", { rect: R(0, 210, 50, 20), text: "covered" }),
    el("div", { rect: R(0, 205, 200, 40) }),               // a sticky header over it
    el("button", { rect: R(0, 300, 50, 20), text: "half opaque", style: { opacity: "0.5" } }),
    el("button", { rect: R(-20, 330, 50, 20), text: "clipped" }),
    el("button", { rect: R(980, 790, 50, 30), text: "corner" }),
  ];
  const { doc, win } = page(els);
  const hints = H.collect(doc, win).hints;
  assert.deepEqual(hints.map((h) => h.text), ["half opaque", "clipped", "corner"]);
  assert.deepEqual(hints[1], { id: 1, x: 0, y: 330, w: 30, h: 20, role: "button", text: "clipped", tag: "button" });
  assert.deepEqual([hints[2].x, hints[2].y, hints[2].w, hints[2].h], [980, 790, 20, 10]);
});

test("a child that covers its clickable parent does not hide it, and occlusion can be turned off", () => {
  const a = el("a", { attrs: { href: "/x" }, rect: R(10, 10, 100, 40), text: "Parent link" });
  el("span", { rect: R(10, 10, 100, 40), parent: a });
  const span = a.children[0];
  const covered = el("button", { rect: R(10, 100, 40, 40), text: "under" });
  const over = el("div", { rect: R(0, 90, 300, 300) });
  const { doc, win } = page([a, span, covered, over]);
  assert.deepEqual(H.collect(doc, win).hints.map((h) => h.text), ["Parent link"]);
  assert.deepEqual(H.collect(doc, win, { occlusion: false }).hints.map((h) => h.text), ["Parent link", "under"]);
});

test("at most 200 hints, in reading order with ids 0..199", () => {
  const els = [];
  for (let i = 249; i >= 0; i--) {
    const row = Math.floor(i / 10), col = i % 10;
    els.push(el("button", { rect: R(col * 90, row * 30, 80, 25), text: "b" + i }));
  }
  const { doc, win } = page(els, { w: 1000, h: 8000 });
  const hints = H.collect(doc, win).hints;
  assert.equal(hints.length, H.MAX_HINTS);
  assert.equal(H.MAX_HINTS, 200);
  assert.deepEqual(hints.map((h) => h.id), [...Array(200).keys()]);
  assert.deepEqual(hints.slice(0, 3).map((h) => h.text), ["b0", "b1", "b2"]);
  assert.equal(hints[199].text, "b199");
  assert.equal(H.collect(doc, win, { max: 5 }).hints.length, 5);
  assert.equal(H.collect(doc, win, { max: 999 }).hints.length, 200);
});

test("the overlay's own elements are never hints", () => {
  const host = el("div", { attrs: { id: "brainpoint-overlay" }, rect: R(0, 0, 0, 0) });
  const inner = el("button", { rect: R(10, 10, 80, 30), text: "Resume", parent: host });
  const real = el("button", { rect: R(10, 100, 80, 30), text: "Real" });
  const { doc, win } = page([host, inner, real]);
  assert.deepEqual(H.collect(doc, win, { exclude: host }).hints.map((h) => h.text), ["Real"]);
});

test("the same box twice keeps only the first (outer) element", () => {
  const a = el("a", { attrs: { href: "/" }, rect: R(5, 5, 50, 20), text: "Outer" });
  const b = el("span", { attrs: { role: "button" }, rect: R(5, 5, 50, 20), text: "Inner", parent: a });
  const { doc, win } = page([a, b]);
  assert.deepEqual(H.collect(doc, win).hints.map((h) => h.text), ["Outer"]);
});

test("text: aria-label first, labelledby, label, image alt, title; collapsed and cut to 120", () => {
  const ref = el("span", { attrs: { id: "lbl" }, rect: R(0, 0, 0, 0) });
  ref.textContent = "  Labelled   by  ";
  const lab = { innerText: "Full name" };
  const img = el("img", { attrs: { alt: "Settings" }, rect: R(0, 0, 0, 0) });
  const iconBtn = el("button", { rect: R(0, 400, 30, 30), text: "" });
  iconBtn.children.push(img);
  const els = [
    el("button", { attrs: { "aria-label": "Close dialog" }, rect: R(0, 0, 30, 30), text: "x" }),
    el("button", { attrs: { "aria-labelledby": "lbl" }, rect: R(0, 50, 30, 30), text: "" }),
    el("input", { type: "text", labels: [lab], rect: R(0, 100, 30, 30) }),
    el("a", { attrs: { href: "/", title: "Go home" }, rect: R(0, 150, 30, 30), text: " " }),
    el("button", { rect: R(0, 200, 30, 30), text: "word ".repeat(60) }),
    el("input", { type: "image", attrs: { alt: "Search" }, rect: R(0, 250, 30, 30) }),
    el("button", { rect: R(0, 300, 30, 30), text: "" }),
    iconBtn, ref,
  ];
  const { doc, win } = page(els);
  const hints = H.collect(doc, win).hints;
  const t = hints.map((h) => h.text);
  assert.deepEqual(t.slice(0, 4), ["Close dialog", "Labelled by", "Full name", "Go home"]);
  assert.equal(t[4].length, 120);
  assert.ok(!/\s\s/.test(t[4]));
  assert.equal(t[5], "Search");
  assert.equal(hints[6].text, "");
  assert.equal(hints[6].role, "icon_button", "a button without text is an icon button");
  assert.equal(hints[7].text, "Settings");
});

test("helpers: clipBox, styleHidden, isClickable", () => {
  assert.deepEqual(H.clipBox({ left: -5, top: 10, right: 20, bottom: 30 }, 100, 100), { x0: 0, y0: 10, x1: 20, y1: 30 });
  assert.equal(H.clipBox({ left: 0, top: 0, right: 0.5, bottom: 30 }, 100, 100), null);
  assert.equal(H.clipBox({ left: NaN, top: 0, right: 5, bottom: 5 }, 100, 100), null);
  assert.equal(H.clipBox(null, 100, 100), null);
  assert.deepEqual(H.clipBox({ x: 1, y: 2, width: 3, height: 4 }, 100, 100), { x0: 1, y0: 2, x1: 4, y1: 6 });
  assert.equal(H.styleHidden(null), false);
  assert.equal(H.styleHidden({ opacity: "" }), false);
  assert.equal(H.styleHidden({ opacity: "0.0" }), true);
  assert.equal(H.isClickable(el("div", { attrs: { tabindex: "0" } }), null), false);
  assert.ok(H.SELECTOR.includes("[role=menuitem]") && H.SELECTOR.includes("input:not([type=hidden])"));
});

test("a page whose getComputedStyle or getBoundingClientRect throws is skipped, not fatal", () => {
  const bad = el("button", { rect: R(0, 0, 10, 10), text: "bad" });
  bad.getBoundingClientRect = () => { throw new Error("detached"); };
  const good = el("button", { rect: R(0, 50, 10, 10), text: "good" });
  const { doc, win } = page([bad, good]);
  win.getComputedStyle = (e) => { if (e === good) throw new Error("no style"); return {}; };
  assert.deepEqual(H.collect(doc, win).hints.map((h) => h.text), ["good"]);
});

test("submit controls carry submit: true, and elements[id] is each hint's element", () => {
  const inForm = el("button", { rect: R(10, 10, 80, 30), text: "Continue" });
  inForm.form = { tagName: "FORM" };
  const plainBtn = el("button", { rect: R(100, 10, 80, 30), text: "Menu" });   // no form
  const typed = el("button", { attrs: { type: "button" }, rect: R(200, 10, 80, 30), text: "Toggle" });
  typed.form = { tagName: "FORM" };
  const sub = el("input", { type: "submit", attrs: { value: "Go" }, rect: R(300, 10, 40, 30) });
  const img = el("input", { type: "image", attrs: { alt: "Search" }, rect: R(400, 10, 40, 30) });
  const { doc, win } = page([inForm, plainBtn, typed, sub, img]);
  const got = H.collect(doc, win);
  const by = Object.fromEntries(got.hints.map((h) => [h.text, h]));
  assert.equal(by.Continue.submit, true);
  assert.equal(by.Go.submit, true);
  assert.equal(by.Search.submit, true);
  assert.equal("submit" in by.Menu, false);
  assert.equal("submit" in by.Toggle, false);
  assert.equal(got.elements.length, got.hints.length);
  assert.equal(got.elements[by.Continue.id], inForm);
  assert.equal(got.elements[by.Menu.id], plainBtn);
  assert.ok(!JSON.stringify(got.hints).includes("tagName"), "elements never go into the hints");
});
