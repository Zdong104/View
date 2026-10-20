import assert from "node:assert/strict";
import { test } from "node:test";
import { buildRules, EXT_CHAR_BUDGET, extChunks, pathExtRegex, PRIORITY } from "../../src/background/rules.ts";
import { EXTS } from "../../src/shared/kinds.ts";
import { DEFAULT_PREFS, normalizePrefs } from "../../src/shared/prefs.ts";

const BASE = "chrome-extension://abc/viewer.html";
const allOn = normalizePrefs({ intercept: { pdf: true, image: true, model: true, table: true, text: true, media: true } });

test("ids are unique and positive", () => {
  const rules = buildRules(allOn, BASE);
  const ids = new Set(rules.map((r) => r.id));
  assert.equal(ids.size, rules.length);
  assert.ok(rules.every((r) => r.id >= 1));
});

test("no rules when everything is off", () => {
  const off = normalizePrefs({ intercept: { pdf: false, image: false, model: false, table: false, text: false, media: false } });
  assert.deepEqual(buildRules(off, BASE), []);
});

test("default prefs: PDF by content-type, never POST, frames included", () => {
  const rules = buildRules(DEFAULT_PREFS, BASE);
  const byType = rules.find((r) => r.priority === PRIORITY.pdf &&
    r.condition.responseHeaders?.some((h) => h.header === "content-type" && h.values?.includes("application/pdf")));
  assert.ok(byType);
  assert.deepEqual(byType.condition.excludedRequestMethods, ["post"]);
  assert.deepEqual(byType.condition.resourceTypes, ["main_frame", "sub_frame"]);
  assert.equal(byType.action.redirect?.regexSubstitution, `${BASE}?kind=pdf&url=\\0`);
  // text and media are off by default: no rule redirects to them
  assert.ok(!rules.some((r) => r.action.redirect?.regexSubstitution.includes("kind=text")));
});

test("attachments in frames are never hijacked, and outrank redirects", () => {
  const rules = buildRules(allOn, BASE);
  const sub = rules.find((r) => r.action.type === "allow" && r.condition.resourceTypes.includes("sub_frame"));
  assert.ok(sub);
  const maxRedirect = Math.max(...rules.filter((r) => r.action.type === "redirect").map((r) => r.priority));
  assert.ok(sub.priority > maxRedirect);
});

test("regexes stay small (DNR caps each compiled regex at 2 KB)", () => {
  for (const r of buildRules(allOn, BASE)) {
    if (r.condition.regexFilter) assert.ok(r.condition.regexFilter.length < 400, r.condition.regexFilter);
  }
});

test("path extension regex", () => {
  const re = new RegExp(pathExtRegex(["pdf", "stl"], "file"), "i");
  assert.ok(re.test("file:///home/u/x.pdf"));
  assert.ok(re.test("file:///home/u/X.STL"));
  assert.ok(!re.test("file:///home/u/x.pdf.txt"));
  const web = new RegExp(pathExtRegex(["csv"], "https?"));
  assert.ok(web.test("https://h/data.csv?dl=1"));
  assert.ok(!web.test("https://h/view?file=data.csv"));
});

test("extension groups respect the regex budget and keep every extension", () => {
  for (const exts of Object.values(EXTS)) {
    const groups = extChunks(exts);
    assert.deepEqual(groups.flat(), [...exts]);
    for (const g of groups) assert.ok(g.length === 1 || g.join("").length <= EXT_CHAR_BUDGET, g.join("|"));
  }
});
