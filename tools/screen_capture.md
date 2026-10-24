# Screen capture: the evaluation set for button detection

This note says how the pages in `data/screens/` were captured. `tools/screen_eval.py` runs the
Haiku detector on them and scores its boxes against the page's own clickable elements.

The folder is gitignored (`data/*` in `.gitignore`), so each machine captures its own copy. The
pages change over time, so numbers from two captures are not strictly comparable.

## What each page gives

- `data/screens/<slug>.jpg`: a JPEG of the visible viewport only (not the full page), scaled
  down so that its long edge is at most 1280 px. This matches what the extension sends in a
  `scan` (see `PROTOCOL-v3.md`).
- `data/screens/<slug>.json`:

  ```json
  {"url": "https://example.com/", "title": "Example Domain",
   "viewport": {"w": 1280, "h": 800, "dpr": 2},
   "image": {"w": 1280, "h": 800},
   "hints": [{"id": 0, "x": 520, "y": 380, "w": 80, "h": 18, "role": "link",
              "text": "Learn more", "tag": "a"}]}
  ```

  `hints` are in viewport CSS pixels, like the `hints` of a `scan`. To draw them on the image,
  multiply by `image.w / viewport.w` and `image.h / viewport.h`.

## The hint rules

`tools/screen_capture.js` is the function that was evaluated in each page. It follows the same
rules as `clients/chrome/hints.js`:

- It looks at `a[href]`, `button`, `input:not([type=hidden])`, `select`, `textarea`,
  `[role=button|link|tab|menuitem|checkbox]`, `[onclick]`, and `[tabindex]` elements whose
  cursor is `pointer`.
- An element counts as visible when its rectangle has a non-zero size and overlaps the
  viewport, it is not `visibility: hidden` or `display: none`, its opacity is above 0, and
  `elementFromPoint` at the center of its clipped rectangle returns the element or one of its
  descendants. That last check drops elements hidden under banners, menus and overlays.
- The box is the rectangle clipped to the viewport and rounded. At most 200 hints are kept, in
  document order, with ids 0, 1, 2 and so on.
- `role` is one of the detector's roles: `button`, `link`, `input`, `checkbox`, `tab`,
  `menu_item` or `other`. It comes from the ARIA role when there is one and from the tag
  otherwise (submit inputs are buttons, radio and checkbox inputs are checkboxes).
- `text` is the first non-empty of aria-label, innerText, the value (or placeholder) of a form
  field, title, and the alt of an image inside, with whitespace collapsed and cut to 120
  characters. It is always empty for password inputs.

The hints are the page's own idea of what is clickable, so they are a ground truth with gaps.
Custom controls with no clickable markup (a `div` with a JS listener and no `cursor: pointer`)
are missed, and styled radio buttons whose real input has opacity 0 are dropped. Pages built
from closed shadow roots can give no hints at all.

## Procedure

For each page:

1. Open the page in Chrome with a 1280 x 800 viewport and wait for the load event, then about
   2.5 seconds more for late content.
2. If a cookie or consent banner is showing, click its reject (or accept) button when that is a
   single plain button. Pages with a consent wall that cannot be dismissed this way are skipped.
3. Evaluate `tools/screen_capture.js` in the page. It returns
   `{viewport, url, title, hints}`.
4. Take a JPEG screenshot (quality 80) of the viewport to `data/screens/<slug>.jpg`.
5. Run `sips -Z 1280 data/screens/<slug>.jpg` when the long edge is over 1280 px (at a device
   pixel ratio of 2 the raw shot is 2560 x 1600), then read the size with
   `sips -g pixelWidth -g pixelHeight data/screens/<slug>.jpg`.
6. Write `data/screens/<slug>.json` with the url and title the page reported, the viewport, the
   image size and the hints.

The intended route is the chrome-devtools MCP tools (`new_page` or `navigate_page`,
`resize_page`, `evaluate_script` with the contents of `screen_capture.js`, `take_screenshot`
with `format: "jpeg"`, `close_page`). On 2026-10-24 that browser was held by another session, so
the set was captured with the same steps over the Chrome DevTools Protocol instead: a private
headless Chrome (`--headless=new`, its own `--user-data-dir`, `--hide-scrollbars`), the user
agent of a normal desktop Chrome, `Emulation.setDeviceMetricsOverride` at 1280 x 800 with a
device scale factor of 2, `Page.navigate`, `Runtime.evaluate` of `(<screen_capture.js>)()`, and
`Page.captureScreenshot` with `format: "jpeg", quality: 80`. Both routes give the same files.

Nothing was typed into any page, so no form holds personal data, and every page is public and
needs no login.

## Pages (capture of 2026-10-24)

36 pages, all 1280 x 800 images from a 1280 x 800 viewport at a device pixel ratio of 2.

| slug | URL | hints | kind |
| --- | --- | --- | --- |
| apple | https://www.apple.com/ | 18 | product landing page |
| arxiv-listing | https://arxiv.org/list/cs.HC/recent | 70 | dense listing |
| bbc-news | https://www.bbc.com/news | 30 | news front page |
| bing-results | https://www.bing.com/search?q=brain+computer+interface | 26 | search results with ads |
| books-shop | https://books.toscrape.com/ | 42 | shop grid with "Add to basket" |
| cdc-gov | https://www.cdc.gov/ | 15 | government, hero carousel |
| craigslist-search | https://sfbay.craigslist.org/search/bia | 63 | search results with filters |
| craigslist-sfbay | https://sfbay.craigslist.org/ | 185 | very dense link grid |
| ddg-results | https://html.duckduckgo.com/html/?q=steady+state+visual+evoked+potential | 24 | plain search results |
| duckduckgo-home | https://duckduckgo.com/ | 8 | search home page |
| example | https://example.com/ | 1 | one link |
| github-login | https://github.com/login | 14 | login form (empty) |
| gnu | https://www.gnu.org/ | 24 | old-style site with video |
| gutenberg | https://www.gutenberg.org/ | 19 | library home with covers |
| hn | https://news.ycombinator.com/ | 164 | tiny text links |
| httpbin-form | https://httpbin.org/forms/post | 13 | bare HTML form |
| irs-gov | https://www.irs.gov/ | 19 | government, marketing layout |
| lite-cnn | https://lite.cnn.com/ | 26 | text-only news list |
| mdn-array | https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array | 40 | reference docs, three columns |
| nasa | https://www.nasa.gov/ | 12 | dark hero image |
| nodejs | https://nodejs.org/en | 49 | landing page with code tabs |
| npm-package | https://www.npmjs.com/package/express | 33 | package page with tabs |
| npr-text | https://text.npr.org/ | 7 | text-only news list |
| nps-gov | https://www.nps.gov/index.htm | 10 | photo hero with dropdowns |
| openstreetmap | https://www.openstreetmap.org/ | 37 | map with icon buttons |
| pypi-project | https://pypi.org/project/requests/ | 22 | package page with tabs |
| python-docs | https://docs.python.org/3/ | 52 | documentation index |
| python-org | https://www.python.org/ | 21 | project home page |
| rust-lang | https://www.rust-lang.org/ | 12 | landing page, big call to action |
| usa-gov | https://www.usa.gov/ | 19 | government portal |
| w3c | https://www.w3.org/ | 17 | organization home page |
| weather-gov | https://www.weather.gov/ | 30 | old-style portal with a map |
| whatwg-html | https://html.spec.whatwg.org/multipage/ | 28 | specification table of contents |
| wikipedia-article | https://en.wikipedia.org/wiki/Steady_state_visually_evoked_potential | 42 | article with sidebars |
| wikipedia-main | https://en.wikipedia.org/wiki/Main_Page | 33 | portal with a dismissable notice |
| wikipedia-search | https://en.wikipedia.org/w/index.php?search=evoked+potential&fulltext=1&ns0=1 | 20 | search results |

Pages that were tried and left out:

- `https://stackoverflow.com/questions/231767/...`: a Cloudflare "Just a moment..." check page
  instead of the question.
- `https://archive.org/`: rendered fine but gave 0 hints, because its controls live in shadow
  roots that `querySelectorAll` does not reach.
- `https://github.com/python/cpython`: the page did not finish loading within 30 seconds in
  headless Chrome, twice.
