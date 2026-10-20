# Permission justifications (Web Store "Privacy practices" tab)

**Single purpose:** View opens documents (PDF, images, 3D models, tables, text, media) in its own viewer and lets the user view, annotate, sign and convert them.

| Permission | Justification |
|---|---|
| Host permission `<all_urls>` | View can only replace Chrome's built-in handling if it can see document navigations on any site, and the viewer has to re-download the document from that site, with the user's cookies, to display it. View reads no page content; the only exception is detecting embedded PDFs (see the content script below). |
| `declarativeNetRequestWithHostAccess` | Redirects navigations to PDFs and the other file kinds the user enabled to the View viewer page, based on the response's Content-Type, file name and Content-Disposition. Also restores the original Referer on the viewer's re-download, and allows a one-time "Open in Chrome" bypass. |
| `webRequest` | Read-only observation of the Referer header on document navigations that View takes over, so the viewer's re-download is accepted by sites with hot-link protection. Nothing is blocked, modified or stored beyond the browsing session. |
| `webNavigation` | (1) If "Allow access to file URLs" is off, detects that the user opened a local PDF so View can explain how to enable it. (2) Notices when an "Open in Chrome" navigation has finished, so the one-time bypass is removed. |
| `storage` | Saves the user's preferences: which file kinds open in View, and the theme. Session storage holds short-lived per-tab navigation state. |
| `contextMenus` | Adds "Open Link in View" and "Open in View" (for images, video and audio). |
| Content script on all sites | Finds PDFs shown in `<embed>`/`<object>` elements and in `blob:`/`data:` documents, which network rules can't see, and shows them in View. It does nothing on pages without such elements. |
| `web_accessible_resources: viewer.html` | So pages and frames can be redirected to the viewer. |

**Remote code:** none. All code, including the pdf.js worker, the WebAssembly decoders and the OCR engine and models, ships in the package.

**Data usage disclosures:**
- **Website content:** handled only locally, to display the document the user opened. It is never collected or transmitted.
- **Not collected:** personally identifiable information, health, financial, authentication, personal communications, location, web history, or user activity.
- Certify:
  - no data is sold;
  - data is not used or transferred for purposes unrelated to the single purpose;
  - data is not used for creditworthiness or lending.
