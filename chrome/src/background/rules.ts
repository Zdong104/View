// declarativeNetRequest rules that make View the default viewer.
// Pure function of (prefs, viewer URL) so it can be unit-tested under node.
//
// How it works (Chrome 128+, `condition.responseHeaders`): DNR evaluates these
// rules when the response headers of a main_frame/sub_frame navigation arrive
// and redirects the navigation to viewer.html?kind=K&url=<original URL>. The
// viewer then re-fetches the bytes itself (GET, with cookies). Modelled on the
// MV3 pdf.js Chromium extension (mozilla/pdf.js extensions/chromium/pdfHandler.js).
//
// Rule priority bands (higher wins; an "allow" stops lower-priority rules):
//   100  per-tab "open the original" bypass         (session rules, see sw.ts)
//    90  sub_frame + Content-Disposition: attachment -> allow (never hijack a download in a frame)
//    80  PDF main_frame "=download" + attachment     -> allow (Google Drive & co, as pdf.js)
//    70  PDF redirects (Content-Type / octet-stream + .pdf / disposition filename)
//    60  main_frame attachment -> allow              (only if prefs.respectAttachment)
//    50  other kinds by Content-Type
//    40  other kinds by URL extension when Content-Type is generic (octet-stream, text/plain)
//    30  file:// URLs by extension (only effective with "Allow access to file URLs")
import { EXTS, KINDS, MIMES, type Kind } from "../shared/kinds.ts";
import type { Prefs } from "../shared/prefs.ts";
import { dnrSubstitution } from "../shared/viewer-url.ts";

/** Structural subset of chrome.declarativeNetRequest.Rule (avoids enum types). */
export interface DnrRule {
  id: number;
  priority: number;
  action: { type: "redirect" | "allow"; redirect?: { regexSubstitution: string } };
  condition: {
    regexFilter?: string;
    urlFilter?: string;
    isUrlFilterCaseSensitive?: boolean;
    resourceTypes: ("main_frame" | "sub_frame")[];
    excludedRequestMethods?: "post"[];
    responseHeaders?: { header: string; values?: string[] }[];
    excludedResponseHeaders?: { header: string; values?: string[]; excludedValues?: string[] }[];
  };
}

export const PRIORITY = {
  bypass: 100,
  subframeAttachment: 90,
  pdfDownloadParam: 80,
  pdf: 70,
  attachment: 60,
  byType: 50,
  byExt: 40,
  file: 30,
} as const;

/** Content-Types servers use when they don't know better. */
const GENERIC_TYPES = ["application/octet-stream", "binary/octet-stream", "application/x-download",
  "application/force-download", "application/unknown"];

/**
 * Max total characters of the extensions alternated in one regex. DNR caps each
 * compiled (case-insensitive) RE2 program at 2 KB; 12 long names like
 * "gitattributes" already overflow it, ~40 characters is comfortably below.
 * The e2e test registers every rule with all kinds on to catch regressions.
 */
export const EXT_CHAR_BUDGET = 40;

const withParams = (mimes: readonly string[]) => mimes.flatMap((m) => [m, `${m};*`]);

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Split extensions into groups whose total length stays within `budget`. */
export function extChunks(exts: readonly string[], budget = EXT_CHAR_BUDGET): string[][] {
  const out: string[][] = [];
  let cur: string[] = [];
  let len = 0;
  for (const e of exts) {
    if (cur.length && len + e.length > budget) { out.push(cur); cur = []; len = 0; }
    cur.push(e);
    len += e.length;
  }
  if (cur.length) out.push(cur);
  return out;
}

/** Matches a URL whose *path* ends in one of exts (query/fragment allowed after). */
export function pathExtRegex(exts: readonly string[], scheme = "[a-z][a-z0-9+.-]*"): string {
  return `^${scheme}:[^?#]*\\.(?:${exts.map(escapeRe).join("|")})(?:[?#].*)?$`;
}

type NoId = Omit<DnrRule, "id">;

export function buildRules(prefs: Prefs, viewerBase: string): DnrRule[] {
  const rules: NoId[] = [];
  const on = KINDS.filter((k) => prefs.intercept[k]);
  if (on.length === 0) return [];
  const redirect = (kind: Kind) => ({
    type: "redirect" as const,
    redirect: { regexSubstitution: dnrSubstitution(viewerBase, kind) },
  });
  const allow = { type: "allow" as const };

  rules.push({
    priority: PRIORITY.subframeAttachment,
    action: allow,
    condition: {
      urlFilter: "*",
      resourceTypes: ["sub_frame"],
      responseHeaders: [{ header: "content-disposition", values: ["attachment*"] }],
    },
  });

  if (prefs.intercept.pdf) {
    const frames: ("main_frame" | "sub_frame")[] = ["main_frame", "sub_frame"];
    rules.push({
      priority: PRIORITY.pdfDownloadParam,
      action: allow,
      condition: {
        urlFilter: "=download",
        resourceTypes: ["main_frame"],
        responseHeaders: [{ header: "content-disposition", values: ["attachment*"] }],
      },
    });
    rules.push({
      priority: PRIORITY.pdf,
      action: redirect("pdf"),
      condition: {
        regexFilter: "^.*$",
        resourceTypes: frames,
        excludedRequestMethods: ["post"],
        responseHeaders: [{ header: "content-type", values: withParams(MIMES.pdf) }],
      },
    });
    rules.push({
      priority: PRIORITY.pdf,
      action: redirect("pdf"),
      condition: {
        regexFilter: "^.*\\.pdf\\b.*$",
        resourceTypes: frames,
        excludedRequestMethods: ["post"],
        responseHeaders: [{ header: "content-type", values: withParams(GENERIC_TYPES) }],
      },
    });
    // Disposition says *.pdf. responseHeaders is an OR, so "Content-Type is generic
    // AND disposition names a .pdf" is spelled as: disposition matches, and
    // content-type is not anything-except-generic (double negation, as pdf.js).
    rules.push({
      priority: PRIORITY.pdf,
      action: redirect("pdf"),
      condition: {
        regexFilter: "^.*$",
        resourceTypes: frames,
        excludedRequestMethods: ["post"],
        responseHeaders: [{ header: "content-disposition", values: ["*.pdf", '*.pdf"*', "*.pdf'*"] }],
        excludedResponseHeaders: [{ header: "content-type", excludedValues: withParams(GENERIC_TYPES) }],
      },
    });
  }

  if (prefs.respectAttachment && on.some((k) => k !== "pdf")) {
    rules.push({
      priority: PRIORITY.attachment,
      action: allow,
      condition: {
        urlFilter: "*",
        resourceTypes: ["main_frame"],
        responseHeaders: [{ header: "content-disposition", values: ["attachment*"] }],
      },
    });
  }

  for (const kind of on) {
    if (kind === "pdf") continue;
    // Non-PDF kinds only take over top-level navigations; frames keep native behaviour.
    rules.push({
      priority: PRIORITY.byType,
      action: redirect(kind),
      condition: {
        regexFilter: "^.*$",
        resourceTypes: ["main_frame"],
        excludedRequestMethods: ["post"],
        responseHeaders: [{ header: "content-type", values: withParams(MIMES[kind]) }],
      },
    });
    const lying = kind === "text" ? GENERIC_TYPES : [...GENERIC_TYPES, "text/plain"];
    for (const exts of extChunks(EXTS[kind])) {
      rules.push({
        priority: PRIORITY.byExt,
        action: redirect(kind),
        condition: {
          regexFilter: pathExtRegex(exts, "https?"),
          resourceTypes: ["main_frame"],
          excludedRequestMethods: ["post"],
          responseHeaders: [{ header: "content-type", values: withParams(lying) }],
        },
      });
    }
  }

  for (const kind of on) {
    for (const exts of extChunks(EXTS[kind])) {
      rules.push({
        priority: PRIORITY.file,
        action: redirect(kind),
        condition: {
          regexFilter: pathExtRegex(exts, "file"),
          resourceTypes: kind === "pdf" ? ["main_frame", "sub_frame"] : ["main_frame"],
        },
      });
    }
  }

  return rules.map((r, i) => ({ id: i + 1, ...r }));
}
