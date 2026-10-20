// In-place editors for text boxes and notes (markup.py edit_item). A text box
// is edited in a transparent textarea laid exactly over the box; a note in a
// small popover under its icon. Ctrl+Enter or Escape (or clicking elsewhere)
// finishes. A new text box left empty is dropped; an emptied one is removed.
import type { Annotation } from "./api.ts";
import type { Markup } from "./controller.ts";
import type { SurfaceView } from "./surface.ts";
import { bbox, fontOf, LINE_HEIGHT, TEXT_PAD, textHeight } from "./geom.ts";
import { cssFamily, measure } from "./render.ts";
import { el } from "../ui/kit.ts";
import { prefetchFontsFor } from "./pdf-fonts.ts";

export interface Editor {
  readonly id: string;
  finish(): void;
  cancel(): void;
}

type TextAnn = Annotation & { type: "text" };
type NoteAnn = Annotation & { type: "note" };

function isFinishKey(e: KeyboardEvent): boolean {
  return e.key === "Escape" || (e.key === "Enter" && (e.ctrlKey || e.metaKey));
}

export function editText(ctrl: Markup, view: SurfaceView, ann: TextAnn, isNew: boolean): Editor {
  const orig = ann;
  let cur = ann;
  const ta = el("textarea.mk-text-editor", { "aria-label": "Text box", spellcheck: "true" });
  ta.value = ann.text;
  const ac = new AbortController();
  let done = false;

  const place = () => {
    const r = view.clientRect(bbox(cur));
    const s = view.scale;
    const f = fontOf(cur);
    Object.assign(ta.style, {
      left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`,
      font: `${f.italic ? "italic " : ""}${f.bold ? "bold " : ""}${f.size * s}px/${LINE_HEIGHT} ${cssFamily(f.family)}`,
      padding: `${TEXT_PAD * s}px`, color: cur.style.stroke, textAlign: f.align,
      background: cur.style.fill ?? "transparent",
    });
  };
  const show = () => {
    if (isNew) ctrl.setPreview(cur.surface, cur);
    else ctrl.setLive(cur);
    place();
  };

  const editor: Editor = {
    id: ann.id,
    finish: () => {
      if (done) return;
      done = true;
      ac.abort();
      ta.remove();
      ctrl.editing = null;
      const text = ta.value;
      const next = { ...cur, text, h: textHeight({ ...cur, text }, measure) };
      if (isNew) {
        ctrl.setPreview(cur.surface, null);
        if (text.trim()) ctrl.addFromTool(next);
        else ctrl.setTool("select");
      } else {
        ctrl.cancelLive();
        if (!text.trim()) ctrl.remove([ann.id]);
        else if (text !== orig.text || next.h !== orig.h) ctrl.update(ann.id, { text, h: next.h } as Partial<Annotation>);
        else ctrl.renderSurface(ann.surface);
      }
    },
    cancel: () => {
      if (done) return;
      done = true;
      ac.abort();
      ta.remove();
      ctrl.editing = null;
      if (isNew) ctrl.setPreview(cur.surface, null); else ctrl.cancelLive();
    },
  };

  const sig = { signal: ac.signal };
  ta.addEventListener("input", () => {
    cur = { ...cur, text: ta.value };
    cur = { ...cur, h: textHeight(cur, measure) };
    // CJK etc. will need an installed font on PDF export: ask for font access while we have a user gesture
    if (ctrl.docKind === "pdf") prefetchFontsFor(ta.value);
    show();
  }, sig);
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isFinishKey(e)) { e.preventDefault(); editor.finish(); }
  }, sig);
  ta.addEventListener("blur", () => setTimeout(() => editor.finish()), sig);
  addEventListener("scroll", place, { ...sig, capture: true, passive: true });
  addEventListener("resize", place, sig);

  ctrl.editing = editor;
  document.body.append(ta);
  show();
  ta.focus();
  if (isNew) ta.select();
  return editor;
}

export function editNote(ctrl: Markup, view: SurfaceView, ann: NoteAnn, isNew: boolean): Editor {
  const ta = el("textarea", { "aria-label": "Note text" });
  ta.value = ann.text;
  const pop = el("div.popover.mk-pop.mk-note-pop", { role: "dialog", "aria-label": "Note" },
    el("h3", { text: "Note" }), ta, el("div.mk-hint", { text: "Ctrl+Enter to finish" }));
  const ac = new AbortController();
  let done = false;

  const place = () => {
    const r = view.clientRect(bbox(ann));
    const top = r.bottom + 6 + pop.offsetHeight > innerHeight ? Math.max(8, r.top - 6 - pop.offsetHeight) : r.bottom + 6;
    pop.style.top = `${top}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - pop.offsetWidth / 2, innerWidth - pop.offsetWidth - 8))}px`;
  };

  const editor: Editor = {
    id: ann.id,
    finish: () => {
      if (done) return;
      done = true;
      ac.abort();
      pop.remove();
      ctrl.editing = null;
      const text = ta.value;
      if (isNew) {
        ctrl.setPreview(ann.surface, null);
        ctrl.addFromTool({ ...ann, text });
      } else if (text !== ann.text) {
        ctrl.update(ann.id, { text } as Partial<Annotation>);
      } else ctrl.renderSurface(ann.surface);
    },
    cancel: () => {
      if (done) return;
      done = true;
      ac.abort();
      pop.remove();
      ctrl.editing = null;
      if (isNew) ctrl.setPreview(ann.surface, null);
    },
  };

  const sig = { signal: ac.signal };
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (isFinishKey(e)) { e.preventDefault(); editor.finish(); }
  }, sig);
  // Finish when focus leaves the popover.
  pop.addEventListener("focusout", (e) => {
    if (!pop.contains(e.relatedTarget as Node | null)) setTimeout(() => editor.finish());
  }, sig);
  addEventListener("scroll", place, { ...sig, capture: true, passive: true });

  ctrl.editing = editor;
  if (isNew) ctrl.setPreview(ann.surface, ann);
  document.body.append(pop);
  place();
  ta.focus();
  return editor;
}
