"""Offline translation for View.

Two models, chosen in Preferences (``model_choice()``):

  * High quality: Tencent's Hy-MT2-1.8B on llama.cpp (preview.translate_llm),
    1.2 GB, 23 of View's languages, a paragraph at a time, on the GPU
    (Vulkan) when there is one.  It appears here as the pseudo pack
    MODEL_PACK, so plan() / missing() / download() / installed() and the
    "Language Pack Needed" flow work for it unchanged.
  * Fast: the Argos packs below, for every pair when chosen, and always for
    the pairs Hy-MT2 does not cover (Swedish, Greek, Romanian, Hungarian,
    Finnish, Danish, Norwegian).

"Automatic" (the default) is High quality with a dedicated GPU, else Fast.

Fast engine: Argos Translate's language packs (OpenNMT Transformer models,
CTranslate2 format) run directly on ``ctranslate2`` + ``sentencepiece``.  The
``argostranslate`` package itself is not used: it pulls in stanza, spaCy and
PyTorch (gigabytes); its runtime core is the same two libraries.  Packs with
a SentencePiece tokenizer are encoded with it; the OPUS-MT derived ones ship
a subword-nmt BPE model, applied here after a Moses-style tokenization.

Nothing is sent to any server: the engine is an optional, per-user install
(preview.translate_install, like OCR), and a pack (~70-300 MB) is downloaded
from the Argos package index the first time a language pair is used, into
``<user data dir>/view/translate/<from>_<to>``.  Pairs without a direct pack
pivot through English.

On Windows the engine runs in a helper process (view-translate.exe, see
preview.translate_helper) for the same reason as OCR: ctranslate2 only ships
for python.org's Python.  ``VIEW_TRANSLATE_HELPER`` names a helper explicitly.

Language detection is built in (Unicode scripts, then common words and
letters for Latin-script languages): instant, no download, and the user can
override it.

Public API:

    LANGUAGES, language_name(code)
    detect(text) -> code | None
    is_available(src=None, tgt=None) -> bool   something can translate (src -> tgt)
    engine_available() -> bool              Argos's engine importable or helper present
    target() / set_target(code) / default_target()
    model_choice() / set_model_choice(MODEL_AUTO | MODEL_FAST | MODEL_QUALITY)
    plan(src, tgt) -> [(from, to), ...]     the packs a translation uses
    missing(src, tgt) -> [(from, to), ...]  of those, the ones not downloaded
    pack_name(pair), download_message(need) for the UI
    download(pairs, on_progress, cancelled) blocking
    translate(text, src, tgt, on_partial=None, cancelled=None) -> str   blocking
    translate_batch(texts, src, tgt, on_item=None, cancelled=None) -> [str]
                                            blocking; one result per piece

CLI:  python -m preview.translate [--from xx] --to yy "text" | -  (stdin)
      VIEW_TRANSLATE_MODEL=auto|fast|quality overrides the setting,
      VIEW_TRANSLATE_CPU=1 keeps the high-quality model off the GPU.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from .i18n import N_, _, ngettext

# (code, English name for _()).  Argos codes: "zh" Simplified, "zt" Traditional.
LANGUAGES = [
    ("en", N_("English")), ("es", N_("Spanish")), ("zh", N_("Chinese (Simplified)")),
    ("zt", N_("Chinese (Traditional)")), ("fr", N_("French")), ("de", N_("German")),
    ("it", N_("Italian")), ("pt", N_("Portuguese")), ("ru", N_("Russian")),
    ("ja", N_("Japanese")), ("ko", N_("Korean")), ("ar", N_("Arabic")), ("hi", N_("Hindi")),
    ("nl", N_("Dutch")), ("pl", N_("Polish")), ("tr", N_("Turkish")), ("uk", N_("Ukrainian")),
    ("vi", N_("Vietnamese")), ("id", N_("Indonesian")), ("th", N_("Thai")),
    ("sv", N_("Swedish")), ("cs", N_("Czech")), ("el", N_("Greek")), ("he", N_("Hebrew")),
    ("fa", N_("Persian")), ("ro", N_("Romanian")), ("hu", N_("Hungarian")),
    ("fi", N_("Finnish")), ("da", N_("Danish")), ("nb", N_("Norwegian")),
]
CODES = [c for c, _n in LANGUAGES]
PIVOT = "en"
INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
INDEX_MAX_AGE = 7 * 86400
MAX_CHARS = 20_000            # longer text is cut (a document: the first pages)
_NO_SPACE = {"zh", "zt", "ja", "th"}   # sentences are joined without a space

_LOCAL_ONLY = False           # set inside the helper process itself

# the high-quality model as a "pack" (no "_" in it: never taken for an Argos pair)
MODEL_PACK = ("hy-mt2", "*")
MODEL_AUTO, MODEL_FAST, MODEL_QUALITY = "auto", "fast", "quality"


class NotInstalled(RuntimeError):
    """The engine (ctranslate2 + sentencepiece, or the helper) is missing."""


class NeedsDownload(RuntimeError):
    """A language pack has not been downloaded yet."""

    def __init__(self, pairs):
        super().__init__(", ".join(f"{a}→{b}" for a, b in pairs))
        self.pairs = pairs


class Unsupported(RuntimeError):
    pass


def language_name(code: str | None) -> str:
    for c, n in LANGUAGES:
        if c == code:
            return _(n)
    return code or "?"


def pango_language(code: str | None) -> str | None:
    """The language tag Pango takes for one of CODES.  Pango picks the fonts
    by it: Chinese text without one can come out in Japanese glyph forms, or
    in a mix of fonts."""
    return {"zh": "zh-cn", "zt": "zh-tw"}.get(code, code) if code else None


# On Windows Pango ignores the language when it falls back to other fonts: it
# goes down Segoe UI's list, so Chinese came out in MS UI Gothic (Japanese)
# with the simplified-only characters in Microsoft JhengHei, two fonts on two
# baselines.  Naming the language's own font first keeps the text in one font.
_WINDOWS_FONTS = {"zh": "Microsoft YaHei UI,Microsoft YaHei",
                  "zt": "Microsoft JhengHei UI,Microsoft JhengHei",
                  "ja": "Yu Gothic UI,Meiryo UI,Meiryo",
                  "ko": "Malgun Gothic"}


def font_family(code: str | None) -> str | None:
    """A font family (list) for proportional text in the language `code`
    where Pango does not pick one by the language (Windows), else None."""
    fonts = _WINDOWS_FONTS.get(code) if sys.platform == "win32" else None
    return fonts and fonts + ",Segoe UI"


CJK = {"zh", "zt", "ja", "ko"}


# ---------------------------------------------------------------------------
# Settings: the target language (view/settings.json, next to the UI language)
# ---------------------------------------------------------------------------
def _norm(lang: str | None) -> str | None:
    """A locale name (zh_CN.UTF-8, pt-BR, zh_Hant_TW) -> one of CODES."""
    if not lang:
        return None
    l = lang.split(".")[0].split("@")[0].replace("-", "_")
    low = l.lower()
    if low.startswith("zh"):
        return "zt" if any(t in low for t in ("_tw", "_hk", "_mo", "hant")) else "zh"
    if low in ("no", "nn") or low.startswith(("no_", "nn_")):
        return "nb"
    if low.startswith("iw"):
        return "he"
    code = low.split("_")[0]
    return code if code in CODES else None


def default_target() -> str:
    """The UI language, else the system's, else English."""
    from . import i18n
    chosen = i18n.chosen()
    if chosen != i18n.SYSTEM:
        return _norm(chosen) or "en"
    langs = []
    if sys.platform == "win32":
        langs = [l for l in os.environ.get("LANGUAGE", "").split(":") if l] \
            or [i18n._windows_ui_language() or ""]
    else:
        try:
            from gi.repository import GLib
            langs = list(GLib.get_language_names())
        except Exception:  # noqa: BLE001
            langs = [os.environ.get("LANG", "")]
    for l in langs:
        c = _norm(l)
        if c:
            return c
    return "en"


def target() -> str:
    from . import i18n
    t = i18n._read().get("translate-target")
    return t if t in CODES else default_target()


def set_target(code: str):
    from . import i18n
    data = i18n._read()
    data["translate-target"] = code
    path = i18n.settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def model_choice() -> str:
    """MODEL_AUTO (the default), MODEL_FAST or MODEL_QUALITY."""
    env = os.environ.get("VIEW_TRANSLATE_MODEL")
    if env in (MODEL_AUTO, MODEL_FAST, MODEL_QUALITY):
        return env
    from . import i18n
    m = i18n._read().get("translate-model")
    return m if m in (MODEL_FAST, MODEL_QUALITY) else MODEL_AUTO


def set_model_choice(choice: str):
    from . import i18n
    data = i18n._read()
    data["translate-model"] = choice
    path = i18n.settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def quality_model() -> bool:
    """Whether the high-quality model is the one in use (where it covers
    the pair).  "Automatic" asks for a dedicated GPU (may take a second the
    first time: llama.cpp lists the Vulkan devices)."""
    from . import translate_llm
    if _LOCAL_ONLY or not translate_llm.supported():
        return False
    choice = model_choice()
    if choice == MODEL_AUTO:
        return translate_llm.has_gpu()
    return choice == MODEL_QUALITY


def _llm_pair(src, tgt) -> bool:
    from . import translate_llm
    return (src in translate_llm.CODES and tgt in translate_llm.CODES and src != tgt
            and quality_model())


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------
_SIMPLIFIED = set("这个们来时为说国会过还后对学发经么样没关现应进动种问实头间听东车门见长开给让当书"
                  "无与业从电话线边办义条马鸟鱼语读写认识谁谢请钱银际体")
_TRADITIONAL = set("這個們來時為說國會過還後對學發經麼樣沒關現應進動種問實頭間聽東車門見長開給讓當書"
                   "無與業從電話線邊辦義條馬鳥魚語讀寫認識誰謝請錢銀際體")

# frequent short words, and letters that (nearly) only one language uses
_WORDS = {
    "en": "the and of to in is that it for you was with on are as this be have not at by from "
          "but they his her which or we an will can",
    "es": "el la de que y en los las del se por un una para con no es lo al como más pero sus "
          "le ya fue este ha porque esta son muy también",
    "fr": "le la les de des et est un une du que qui en dans pour pas sur au avec ce il elle "
          "sont nous vous ne se plus par",
    "de": "der die das und ist nicht ein eine zu den von mit sich des auf für im dem auch es "
          "an als nach wie oder aber wird sind",
    "it": "il di che la è un una per non del della in sono gli le con si da al lo ma come "
          "anche questo nel alla",
    "pt": "o a de que e do da em um uma para com não os as no na por mais dos das se ao foi é "
          "ele mas também",
    "nl": "de het een en van ik te dat die in is niet je op zijn met voor er maar ook aan als bij",
    "pl": "i w nie na się z do to jest że jak co ale o tak po od za",
    "tr": "ve bir bu da de için ile ne çok daha gibi var olarak ama en o mi ben sen",
    "id": "dan yang di ini itu dengan untuk tidak dari dalam akan pada ada saya juga ke bisa",
    "vi": "và của là có không một những được cho này người các với trong đã",
    "sv": "och att det som en är på av för med till den har inte jag de om",
    "cs": "a v se na je že to s z do jako ale i o pro",
    "ro": "și în de la cu nu pe este o că din un ce sau pentru",
    "hu": "a az és hogy nem is egy meg van ez de csak",
    "fi": "ja on ei se että oli hän mutta kun tai ovat myös",
    "da": "og i at det er en til på af for med som den ikke har",
    "nb": "og i det er en til på som av for med at ikke har jeg",
}
_WORDS = {k: set(v.split()) for k, v in _WORDS.items()}
_WORD_LANGS: dict[str, list] = {}
for _k, _ws in _WORDS.items():
    for _w in _ws:
        _WORD_LANGS.setdefault(_w, []).append(_k)
_LETTERS = {
    "es": "ñ¿¡", "pt": "ãõ", "fr": "çèêœë", "de": "ßäöü", "pl": "łąęśźżćń", "tr": "ğışİ",
    "ro": "șțşţ", "cs": "řěůčš", "hu": "őű", "sv": "å", "da": "æø", "nb": "æø",
    "vi": "ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ", "it": "ò",
}


def _script(ch: str) -> str | None:
    o = ord(ch)
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x3040 <= o <= 0x30FF:
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "ko"
    if 0x0400 <= o <= 0x04FF:
        return "cyr"
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
        return "arab"
    if 0x0590 <= o <= 0x05FF:
        return "he"
    if 0x0370 <= o <= 0x03FF:
        return "el"
    if 0x0E00 <= o <= 0x0E7F:
        return "th"
    if 0x0900 <= o <= 0x097F:
        return "hi"
    if ch.isalpha() and o < 0x0250 or 0x1E00 <= o <= 0x1EFF:
        return "latin"
    return None


def detect(text: str) -> str | None:
    """Best guess at the language of ``text`` (one of CODES), None if it has
    no letters.  Script first; Latin text by its common words and letters."""
    sample = (text or "")[:4000]
    counts: dict[str, int] = {}
    for ch in sample:
        s = _script(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return None
    if counts.get("kana", 0) and counts.get("kana", 0) * 10 >= counts.get("han", 0):
        counts["han"] = counts.get("han", 0) + counts.pop("kana")
        cjk = "ja"
    else:
        counts.pop("kana", None)
        cjk = None
    script = max(counts, key=counts.get)
    if script == "han":
        if cjk:
            return cjk
        trad = sum(ch in _TRADITIONAL for ch in sample)
        simp = sum(ch in _SIMPLIFIED for ch in sample)
        return "zt" if trad > simp else "zh"
    if script == "cyr":
        return "uk" if re.search("[іїєґІЇЄҐ]", sample) else "ru"
    if script == "arab":
        fa = sum(sample.count(c) for c in "پچژگکی")
        ar = sum(sample.count(c) for c in "يكة")
        return "fa" if fa > ar else "ar"
    if script != "latin":
        return script
    low = sample.lower()
    # (whole words only: "O1", "H1b" are not the Portuguese "o")
    words = re.findall(r"(?<!\w)[^\W\d_]+(?!\w)", low)
    score = {k: 0.0 for k in _WORDS}
    score["en"] = 0.0
    for w in words:
        langs = _WORD_LANGS.get(w)
        if langs:                       # a word several languages share counts less
            for k in langs:
                score[k] += 1 / len(langs)
    for k, letters in _LETTERS.items():
        n = sum(low.count(c) for c in letters)
        score[k] += 2 * n
    best = max(score, key=score.get)
    if score[best] <= 0 or (score[best] < 1.5 and best != "en" and len(words) >= 4):
        return "en"                     # one stray hit in longer text is no evidence
    return best


# ---------------------------------------------------------------------------
# Language packs
# ---------------------------------------------------------------------------
def packs_dir() -> str:
    from . import runtime
    return os.path.join(runtime.user_data_dir(), "translate")


def _pack_dir(pair) -> str:
    return os.path.join(packs_dir(), f"{pair[0]}_{pair[1]}")


def installed() -> list[tuple[str, str]]:
    """Downloaded packs as (from, to), MODEL_PACK last if it is there."""
    out = []
    try:
        for name in sorted(os.listdir(packs_dir())):
            d = os.path.join(packs_dir(), name)
            if "_" in name and os.path.isfile(os.path.join(d, "model", "model.bin")):
                out.append(tuple(name.split("_", 1)))
    except OSError:
        pass
    from . import translate_llm
    if translate_llm.installed():
        out.append(MODEL_PACK)
    return out


def installed_size(packs=True, model=True) -> int:
    """Bytes on disk: the Argos packs and/or the high-quality model."""
    from . import translate_llm
    total = translate_llm.size() if model else 0
    if packs:
        skip = {os.path.normcase(os.path.abspath(p)) for p in translate_llm.dirs()}
        for root, dirs, files in os.walk(packs_dir()):
            dirs[:] = [d for d in dirs
                       if os.path.normcase(os.path.abspath(os.path.join(root, d))) not in skip]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total


def remove_all():
    """Every Argos pack and the high-quality model (its server stopped)."""
    from . import translate_llm
    translate_llm.remove()
    shutil.rmtree(packs_dir(), ignore_errors=True)
    with _models_lock:
        _models.clear()


def remove_packs():
    """The Argos packs only."""
    from . import translate_llm
    keep = {os.path.normcase(os.path.abspath(p)) for p in translate_llm.dirs()}
    try:
        names = os.listdir(packs_dir())
    except OSError:
        names = []
    for name in names:
        p = os.path.join(packs_dir(), name)
        if os.path.normcase(os.path.abspath(p)) not in keep:
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    with _models_lock:
        _models.clear()


def pack_name(pair) -> str:
    """A pack as the UI names it."""
    if tuple(pair) == MODEL_PACK:
        return _("High-quality model (Hy-MT2)")
    return f"{language_name(pair[0])} → {language_name(pair[1])}"


def download_message(need) -> str:
    """What the "Language Pack Needed" prompt says about ``need`` (from
    missing()): which downloads, and how large."""
    need = [tuple(p) for p in need]
    packs = [p for p in need if p != MODEL_PACK]
    parts = []
    if MODEL_PACK in need:
        from . import translate_llm
        n = translate_llm.download_size()
        size = f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"
        parts.append(_("This needs the high-quality translation model, downloaded once "
                       "({size}).").format(size=size))
        if translate_llm.device()["kind"] != "gpu":
            parts.append(_("No GPU was found, so it runs on the processor: expect minutes "
                           "per document. The Fast model in Preferences is quicker."))
    if packs:
        names = ", ".join(pack_name(p) for p in packs)
        parts.append(ngettext(
            "This needs a language pack, downloaded once (70–300 MB): {names}.",
            "This needs {n} language packs, downloaded once (70–300 MB each): {names}.",
            len(packs)).format(n=len(packs), names=names))
    return " ".join(parts)


def _index_cache() -> str:
    from . import runtime
    return os.path.join(runtime.user_cache_dir(), "translate-index.json")


def _urlopen(url, timeout=30):
    import urllib.request
    from . import updates
    req = urllib.request.Request(url, headers={"User-Agent": "View"})
    return urllib.request.urlopen(req, timeout=timeout, context=updates.ssl_context())


def _index() -> dict:
    """{(from, to): [url, ...]} from the Argos package index (cached a week)."""
    path = _index_cache()
    data = None
    try:
        if time.time() - os.path.getmtime(path) < INDEX_MAX_AGE:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError):
        data = None
    if data is None:
        try:
            with _urlopen(INDEX_URL) as r:
                raw = r.read()
            data = json.loads(raw)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(path + ".tmp", path)
        except Exception as e:  # noqa: BLE001  offline: an old copy is still good
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                raise RuntimeError(_("Could not reach the language pack index. "
                                     "Check your internet connection and try again.")
                                   + f"\n({e})") from e
    out = {}
    for p in data if isinstance(data, list) else []:
        links = [u for u in p.get("links", []) if str(u).startswith("https://")]
        if links and p.get("from_code") and p.get("to_code"):
            out[(p["from_code"], p["to_code"])] = links
    return out


def plan(src: str, tgt: str, index: dict | None = None) -> list[tuple[str, str]]:
    """The packs that translate src -> tgt: [MODEL_PACK] where the
    high-quality model is in use and covers both, else Argos packs, direct
    or through English.  Uses only downloaded packs when they suffice (no
    network)."""
    if src == tgt:
        return []
    if _llm_pair(src, tgt):
        return [MODEL_PACK]
    have = set(installed())
    direct, pivot = [(src, tgt)], [(src, PIVOT), (PIVOT, tgt)]
    if (src, tgt) in have:
        return direct
    if PIVOT not in (src, tgt) and set(pivot) <= have:
        return pivot
    idx = index if index is not None else _index()
    known = have | set(idx)
    if (src, tgt) in known:
        return direct
    if PIVOT not in (src, tgt) and set(pivot) <= known:
        return pivot
    raise Unsupported(_("No language pack translates {src} into {tgt}.").format(
        src=language_name(src), tgt=language_name(tgt)))


def missing(src: str, tgt: str) -> list[tuple[str, str]]:
    have = set(installed())
    need = [p for p in plan(src, tgt) if p not in have]
    if MODEL_PACK in need:
        _probe_device()
    return need


def _probe_device():
    """Find the GPU now, in this (worker) thread: download_message() tells
    the user about it from the main thread, and that should not wait."""
    from . import translate_llm
    translate_llm.device()


_pair_locks: dict = {}
_pair_locks_lock = threading.Lock()


def _incoming_dir() -> str:
    """Where packs download and unpack, under unique names (two windows, or
    two View processes, may fetch the same pack at once); a finished pack is
    renamed into place.  Leftovers of an interrupted download are cleared."""
    d = os.path.join(packs_dir(), ".incoming")       # (no "_": never taken for a pack)
    os.makedirs(d, exist_ok=True)
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if time.time() - os.path.getmtime(p) > 86400:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass
    return d


def download(pairs, on_progress=None, cancelled=None):
    """Download and unpack packs.  on_progress(fraction, text) from this thread;
    cancelled() -> True stops it.  Blocking.  A pack another window is already
    downloading is waited for, not fetched twice.  MODEL_PACK fetches the
    high-quality model and llama.cpp (preview.translate_llm.download)."""
    pairs = [tuple(p) for p in pairs]
    argos = [p for p in pairs if p != MODEL_PACK]
    idx = _index() if argos else {}
    incoming = _incoming_dir() if argos else None
    for k, pair in enumerate(pairs):
        urls = idx.get(pair)
        if pair != MODEL_PACK and not urls:
            raise Unsupported(_("No language pack for {pair}.").format(pair=f"{pair[0]}→{pair[1]}"))
        label = _("Downloading {src} → {tgt}…").format(src=language_name(pair[0]),
                                                       tgt=language_name(pair[1]))
        with _pair_locks_lock:
            lock = _pair_locks.setdefault(pair, threading.Lock())
        while not lock.acquire(timeout=0.2):
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
        try:
            if pair == MODEL_PACK:
                from . import translate_llm
                if not translate_llm.installed():
                    n = len(pairs)
                    translate_llm.download(
                        on_progress and (lambda f, t, k=k: on_progress((k + f) / n, t)),
                        cancelled)
            elif pair not in installed():
                _download_pack(pair, urls[0], incoming, label, (k, len(pairs)),
                               on_progress, cancelled)
        finally:
            lock.release()
    if on_progress:
        on_progress(1.0, _("Done"))


def _download_pack(pair, url, incoming, label, step, on_progress, cancelled):
    k, n = step
    fd, part = tempfile.mkstemp(prefix=f"{pair[0]}-{pair[1]}.", suffix=".part", dir=incoming)
    try:
        try:
            with os.fdopen(fd, "wb") as f, _urlopen(url, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    if cancelled and cancelled():
                        raise RuntimeError("Cancelled")
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress:
                        frac = got / total if total else 0.0
                        on_progress((k + frac * 0.95) / n,
                                    f"{label}  {got / 1e6:.0f} / {total / 1e6:.0f} MB"
                                    if total else label)
            if on_progress:
                on_progress((k + 0.97) / n, _("Unpacking…"))
            _unpack(part, _pack_dir(pair), incoming)
        except OSError as e:
            raise RuntimeError(_("Could not download the language pack. Check your "
                                 "internet connection and try again.") + f"\n({e})") from e
    finally:
        try:
            os.remove(part)
        except OSError:
            pass


def _unpack(archive, dest, incoming):
    """An .argosmodel (a zip with one top folder) -> dest/{model/, tokenizer,
    metadata.json}, unpacked in `incoming` and then renamed into place (a
    complete pack already at dest, from another download, is kept).  Skips
    the stanza sentence splitter (not used) and any member that would land
    outside dest."""
    tmp = tempfile.mkdtemp(prefix=os.path.basename(dest).replace("_", "-") + ".",
                           suffix=".tmp", dir=incoming)
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = info.filename.replace("\\", "/").split("/")[1:]
                if not parts or not parts[-1] or parts[0] in ("stanza", "README.md") \
                        or any(p in ("", ".", "..") for p in parts) or ":" in info.filename:
                    continue
                target = os.path.join(tmp, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
        if not os.path.isfile(os.path.join(tmp, "model", "model.bin")):
            raise RuntimeError("not a translation package: model/model.bin is missing")
        done = os.path.join(dest, "model", "model.bin")
        if not os.path.isfile(done):
            shutil.rmtree(dest, ignore_errors=True)         # an incomplete one
            try:
                os.replace(tmp, dest)
            except OSError:
                if not os.path.isfile(done):                # (not just lost a race)
                    raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
try:   # a user-level engine installed by View's "Install translation" offer
    from .translate_install import activate as _activate_private_engine
    _activate_private_engine()
except Exception:  # noqa: BLE001
    pass


def helper_path() -> str | None:
    """The out-of-process engine (view-translate.exe), if this build has one."""
    if _LOCAL_ONLY:
        return None
    env = os.environ.get("VIEW_TRANSLATE_HELPER")
    if env:
        return env if os.path.isfile(env) else None
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        p = os.path.join(os.path.dirname(sys.executable), "translate", "view-translate.exe")
        if os.path.isfile(p):
            return p
    return None


def is_available(src: str | None = None, tgt: str | None = None) -> bool:
    """Whether something can translate src -> tgt (unknown: any pair): the
    high-quality model (downloaded on use, nothing to install) where it is
    in use and covers the pair, else Argos's engine."""
    if engine_available():
        return True
    from . import translate_llm
    return (translate_llm.supported() and quality_model()
            and (src is None or src in translate_llm.CODES)
            and (tgt is None or tgt in translate_llm.CODES))


def engine_available() -> bool:
    """Argos's engine: ctranslate2 + sentencepiece importable, or the helper."""
    if helper_path():
        return True
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("ctranslate2", "sentencepiece"))
    except Exception:  # noqa: BLE001
        return False


_models: dict = {}            # pack dir -> _Model, most recent last
_models_lock = threading.Lock()
_run_lock = threading.Lock()
MAX_LOADED = 2                # each takes 100-400 MB of RAM


class _Model:
    def __init__(self, d):
        import ctranslate2
        threads = max(1, min(8, (os.cpu_count() or 4) // 2))
        self.translator = ctranslate2.Translator(os.path.join(d, "model"), device="cpu",
                                                 inter_threads=1, intra_threads=threads)
        self.to_code = os.path.basename(d).split("_", 1)[1]
        self.from_code = os.path.basename(d).split("_", 1)[0]
        spm = os.path.join(d, "sentencepiece.model")
        if os.path.isfile(spm):
            import sentencepiece
            self.sp = sentencepiece.SentencePieceProcessor(model_file=spm)
            self.bpe = None
        else:
            self.sp = None
            with open(os.path.join(d, "bpe.model"), encoding="utf-8") as f:
                self.bpe = _BPE(f)
        self.prefix = None
        try:
            with open(os.path.join(d, "metadata.json"), encoding="utf-8") as f:
                self.prefix = json.load(f).get("target_prefix") or None
        except (OSError, ValueError):
            pass

    def encode(self, sentence):
        if self.sp is not None:
            return self.sp.encode(sentence, out_type=str)
        return self.bpe.segment(_moses_tokenize(sentence, self.from_code))

    def decode(self, tokens):
        if self.prefix and tokens and tokens[0] == self.prefix:
            tokens = tokens[1:]
        if self.sp is not None:
            # (as Argos does: the target pieces need not be in the source model)
            return "".join(tokens).replace("\u2581", " ").strip()
        return _moses_detokenize(" ".join(tokens).replace("@@ ", "").rstrip("@").split(),
                                 self.to_code)

    def translate(self, sentences):
        toks = [self.encode(s) for s in sentences]
        kw = {"target_prefix": [[self.prefix]] * len(toks)} if self.prefix else {}
        # no_repeat_ngram_size: the small models can loop on lists, arrows and
        # slashes ("条条条条…", "packing packing …") until max_decoding_length
        res = self.translator.translate_batch(
            toks, beam_size=2, max_batch_size=16, replace_unknowns=True, no_repeat_ngram_size=4,
            max_decoding_length=min(512, 32 + 3 * max((len(t) for t in toks), default=0)),
            **kw)
        return [self.decode(r.hypotheses[0]) for r in res]


def _model(d) -> _Model:
    with _models_lock:
        m = _models.pop(d, None)
        if m is None:
            m = _Model(d)
        _models[d] = m
        while len(_models) > MAX_LOADED:
            _models.pop(next(iter(_models)))
        return m


def translate(text, src, tgt, on_partial=None, cancelled=None) -> str:
    """Translate ``text`` from ``src`` (None: detect) into ``tgt``.  Blocking;
    on_partial(text_so_far) is called from this thread as sentences finish.
    Raises NotInstalled, NeedsDownload or Unsupported."""
    text = (text or "")[:MAX_CHARS]
    src = src or detect(text) or PIVOT
    if src == tgt or not text.strip():
        return text
    if _llm_pair(src, tgt):
        return _llm_translate(text, src, tgt, on_partial, cancelled)
    if not engine_available():
        raise NotInstalled(_("The translation engine is not installed."))
    hops = plan(src, tgt)
    need = [p for p in hops if p not in set(installed())]
    if need:
        raise NeedsDownload(need)
    dirs = [_pack_dir(p) for p in hops]
    exe = helper_path()
    if exe:
        return _helper(exe).translate(text, dirs, tgt, on_partial, cancelled)
    return translate_local(text, dirs, tgt, on_partial, cancelled)


def translate_batch(texts, src, tgt, on_item=None, cancelled=None) -> list[str]:
    """Translate each of ``texts`` (paragraph-sized pieces of one document)
    from ``src`` into ``tgt``, keeping them apart: the result has one string
    per input.  Blocking; on_item(i, translation) is called from this thread
    as each piece is done (in order), cancelled() -> True stops between
    batches (RuntimeError("Cancelled")).  No length limit: the caller shows
    progress.  Raises NotInstalled, NeedsDownload or Unsupported."""
    texts = [t or "" for t in texts]
    out = [None] * len(texts)
    todo = []
    for i, t in enumerate(texts):
        if src == tgt or not t.strip():
            out[i] = t
            if on_item:
                on_item(i, t)
        else:
            todo.append(i)
    if not todo:
        return out
    if _llm_pair(src, tgt):
        return _llm_batch(texts, todo, out, src, tgt, on_item, cancelled)
    if not engine_available():
        raise NotInstalled(_("The translation engine is not installed."))
    hops = plan(src, tgt)
    need = [p for p in hops if p not in set(installed())]
    if need:
        raise NeedsDownload(need)
    dirs = [_pack_dir(p) for p in hops]
    exe = helper_path()
    if exe:                      # one request per piece (the helper's protocol)
        h = _helper(exe)
        for i in todo:
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
            out[i] = h.translate(texts[i], dirs, tgt, None, cancelled)
            if on_item:
                on_item(i, out[i])
        return out
    units = {i: _segment(texts[i], src, tgt) for i in todo}
    sents = [(i, k) for i in todo for k, u in enumerate(units[i]) if u[0] == "s"]
    left = {i: sum(1 for u in units[i] if u[0] == "s") for i in todo}
    emitted = 0
    step = 12
    for a in range(0, len(sents), step):
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        chunk = sents[a:a + step]
        batch = [units[i][k][1] for i, k in chunk]
        with _run_lock:           # per batch: a selection translation can slip in
            for d in dirs:
                batch = _model(d).translate(batch)
        for (i, k), t in zip(chunk, batch):
            units[i][k] = ("s", t)
            left[i] -= 1
        while emitted < len(todo) and left[todo[emitted]] == 0:
            i = todo[emitted]
            out[i] = _assemble(units[i], tgt)
            if on_item:
                on_item(i, out[i])
            emitted += 1
    for i in todo:                # pieces with only separators
        if out[i] is None:
            out[i] = _assemble(units[i], tgt)
            if on_item:
                on_item(i, out[i])
    return out


def translate_local(text, dirs, tgt, on_partial=None, cancelled=None) -> str:
    """Run the packs in ``dirs`` in turn (in this process)."""
    src = os.path.basename(dirs[0]).split("_", 1)[0] if dirs else None
    units = _segment(text, src, tgt)
    todo = [i for i, u in enumerate(units) if u[0] == "s"]
    sents = [units[i][1] for i in todo]
    out = list(units)
    with _run_lock:
        for h, d in enumerate(dirs):
            m = _model(d)
            last = h == len(dirs) - 1
            done = []
            step = 8 if last and on_partial else 64
            for k in range(0, len(sents), step):
                if cancelled and cancelled():
                    raise RuntimeError("Cancelled")
                done += m.translate(sents[k:k + step])
                if last and on_partial:
                    for i, t in zip(todo, done):
                        out[i] = ("s", t)
                    on_partial(_assemble(out[:todo[len(done) - 1] + 1], tgt))
            sents = done
    for i, t in zip(todo, sents):
        out[i] = ("s", t)
    return _assemble(out, tgt)


# ---------------------------------------------------------------------------
# The high-quality model: a paragraph (a hard line) per request
# ---------------------------------------------------------------------------
def _llm_units(text, src, tgt):
    """Text -> [("s", piece for the model) | ("k", kept) | ("sep", separator)]:
    one piece per hard line (a paragraph once _reflow has joined a PDF's
    wrapped lines), so the model sees whole sentences in their context; a
    line over translate_llm.MAX_PIECE characters goes as runs of whole
    sentences.  A line with nothing to translate, or already in the target
    language, is kept (see _runs)."""
    from .translate_llm import MAX_PIECE
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    units = []
    for block in re.split(r"\n[ \t]*\n+", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if units:
            units.append(("sep", "\n\n"))
        for j, line in enumerate(_reflow(lines)):
            if j:
                units.append(("sep", "\n"))
            if all(k == "k" for k, _s in _runs(line, src, tgt)):
                units.append(("k", line))
                continue
            chunk = ""
            for s in _sentences(line) if len(line) > MAX_PIECE else [line]:
                if chunk and len(chunk) + 1 + len(s) > MAX_PIECE:
                    units += [("s", chunk), ("sep", " ")]
                    chunk = s
                else:
                    chunk = f"{chunk} {s}" if chunk else s
            if chunk:
                units.append(("s", chunk))
    return units


def _llm_join(units, tgt):
    return "".join(s for k, s in units
                   if not (k == "sep" and s == " " and tgt in _NO_SPACE)).rstrip()


def _llm_translate(text, src, tgt, on_partial, cancelled):
    from . import translate_llm
    if not translate_llm.installed():
        _probe_device()
        raise NeedsDownload([MODEL_PACK])
    units = _llm_units(text, src, tgt)
    idx = [i for i, u in enumerate(units) if u[0] == "s"]
    out = list(units)

    def piece(k, res):
        out[idx[k]] = ("s", res)
        if on_partial:
            on_partial(_llm_join(out[:idx[k] + 1], tgt))
    translate_llm.translate_pieces([units[i][1] for i in idx], src, tgt, piece, cancelled)
    return _llm_join(out, tgt)


def _llm_batch(texts, todo, out, src, tgt, on_item, cancelled):
    """translate_batch() on the high-quality model: every piece of every
    text in one queue (the server runs translate_llm.SLOTS at once), each
    text reported as soon as it and the ones before it are done."""
    from . import translate_llm
    if not translate_llm.installed():
        _probe_device()
        raise NeedsDownload([MODEL_PACK])
    units = {i: _llm_units(texts[i], src, tgt) for i in todo}
    flat = [(i, k) for i in todo for k, u in enumerate(units[i]) if u[0] == "s"]
    left = {i: sum(1 for u in units[i] if u[0] == "s") for i in todo}
    emitted = [0]

    def flush():
        while emitted[0] < len(todo) and left[todo[emitted[0]]] == 0:
            i = todo[emitted[0]]
            out[i] = _llm_join(units[i], tgt)
            if on_item:
                on_item(i, out[i])
            emitted[0] += 1

    def piece(n, res):
        i, k = flat[n]
        units[i][k] = ("s", res)
        left[i] -= 1
        flush()
    flush()
    translate_llm.translate_pieces([units[i][k][1] for i, k in flat], src, tgt, piece,
                                   cancelled)
    flush()
    return out


# ---------------------------------------------------------------------------
# Splitting text into sentences, and joining the translations
# ---------------------------------------------------------------------------
_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "sra", "srta", "st", "vs", "etc", "e.g",
           "i.e", "no", "fig", "figs", "p", "pp", "vol", "inc", "ltd", "jr", "co", "corp",
           "approx", "dept", "ca", "cf", "al", "ed", "eds", "nr", "art", "op", "z.b", "bzw"}
_CJK_END = "。！？；"
_END = ".!?…" + _CJK_END + "：:;；"
_BULLET = re.compile(r"^\s*([-*•·▪◦–]|\d+[.)]|[a-zA-Z][.)])\s")


def _wide(ch):
    return _script(ch) in ("han", "kana", "ko", "th")


def _reflow(lines):
    """Lines of one paragraph -> its hard lines: lines wrapped by a PDF, an
    OCR'd page or a text file are joined back into their sentences."""
    longest = max(len(l) for l in lines)
    out = [lines[0]]
    for nxt in lines[1:]:
        prev = out[-1]
        last = prev.rstrip("\"'”’)]）」』")[-1:] or prev[-1:]
        hard = (last in _END or _BULLET.match(nxt) or len(prev) < 0.6 * longest
                and longest > 30)
        if hard:
            out.append(nxt)
        elif prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and nxt[:1].islower():
            out[-1] = prev[:-1] + nxt
        elif _wide(prev[-1]) and _wide(nxt[0]):
            out[-1] = prev + nxt
        else:
            out[-1] = prev + " " + nxt
    return out


def _sentences(line):
    """One hard line -> sentences."""
    out, start = [], 0
    for m in re.finditer(r"[.!?…]+[\"'”’)\]]*\s+|[。！？；]+[”’」』）)]*\s*", line):
        end = m.end()
        piece = line[start:end].strip()
        if m.group(0)[0] == ".":
            word = re.findall(r"([^\s(\"'“‘]+)\.+[\"'”’)\]]*\s*$", line[start:end])
            w = word[0].lower() if word else ""
            if w in _ABBREV or (len(w) == 1 and w.isalpha()) or w.isdigit():
                continue
        if piece:
            out.append(piece)
        start = end
    rest = line[start:].strip()
    if rest:
        out.append(rest)
    # very long "sentences" (lists, tables) translate badly: cut at commas
    res = []
    for s in out:
        while len(s) > 400:
            cut = max(s.rfind("; ", 0, 300), s.rfind(", ", 0, 300), s.rfind("，", 0, 300))
            if cut < 100:
                cut = s.rfind(" ", 0, 300)
            if cut < 100:
                break
            res.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        res.append(s)
    return res


_SCRIPTS = {"zh": {"han"}, "zt": {"han"}, "ja": {"han", "kana"}, "ko": {"ko"},
            "ru": {"cyr"}, "uk": {"cyr"}, "ar": {"arab"}, "fa": {"arab"}, "he": {"he"},
            "el": {"el"}, "th": {"th"}, "hi": {"hi"}}
_SHORT_WORDS = {"han", "kana", "ko", "th"}     # scripts where one character is a word


def scripts_of(lang) -> set:
    return _SCRIPTS.get(lang, {"latin"})


def _runs(sentence, src, tgt):
    """One sentence -> [("s", piece to translate) | ("k", piece kept as is)].

    Text already in the target language is not run through the engine again
    (the models garble it: "工资." -> "工资/工资工资/"): a sentence in the
    target's script is kept; in a mixed one (English with Chinese words, into
    Chinese) only the stretches in the source's script that hold a real word
    are translated, the rest stays as written."""
    if not src or not tgt or src == tgt:
        return [("s", sentence)]
    s_scr, t_scr = scripts_of(src), scripts_of(tgt)
    word = r"[^\W\d_]" if s_scr & _SHORT_WORDS else r"[^\W\d_]{3,}"
    if not re.search(word, sentence) and not any(_script(c) in t_scr for c in sentence):
        return [("k", sentence)]        # no word to translate ("dd", "O1, H1b")
    if s_scr & t_scr:
        # same script (English -> Spanish): only a sentence that clearly is
        # in the target language already is kept
        if len(re.findall(r"[^\W\d_]+", sentence)) >= 4 and detect(sentence) == tgt:
            return [("k", sentence)]
        return [("s", sentence)]
    kinds = []
    for ch in sentence:
        sc = _script(ch)
        kinds.append("t" if sc in t_scr else "s" if sc in s_scr else "o" if sc else "n")
    if "t" not in kinds:
        return [("s", sentence)]
    out, i, n = [], 0, len(sentence)
    while i < n:
        if kinds[i] != "s":
            j = i
            while j < n and kinds[j] != "s":
                j += 1
            out.append(("k", sentence[i:j]))
            i = j
            continue
        j = i
        last = i
        while j < n and kinds[j] in ("s", "n"):
            if kinds[j] == "s":
                last = j
            j += 1
        piece = sentence[i:last + 1]
        out.append(("s" if re.search(word, piece) else "k", piece))
        i = last + 1
    merged = []                         # neighbours of one kind as one piece
    for k, t in out:
        if merged and merged[-1][0] == k:
            merged[-1] = (k, merged[-1][1] + t)
        else:
            merged.append((k, t))
    return merged


def _segment(text, src=None, tgt=None):
    """Text -> [("s", sentence) | ("k", kept as is) | ("sep", separator)]
    keeping paragraphs and hard line breaks (see _runs for "k")."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    units = []
    for block in re.split(r"\n[ \t]*\n+", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if units:
            units.append(("sep", "\n\n"))
        for j, line in enumerate(_reflow(lines)):
            if j:
                units.append(("sep", "\n"))
            for k, s in enumerate(_sentences(line)):
                if k:
                    units.append(("sep", " "))
                units += _runs(s, src, tgt)
    return units


_FULL_WIDTH = {".": "。", ",": "，", ":": "：", ";": "；", "?": "？", "!": "！"}


def _cjk_punct(s):
    """ASCII punctuation the models emit after Chinese/Japanese -> full width."""
    s = re.sub(r"(?<=[^\x00-\x7f])\s*([.,:;?!])(?!\d)\s*",
               lambda m: _FULL_WIDTH[m.group(1)], s)
    return re.sub(r"(?<=[^\x00-\x7f]) +(?=[^\x00-\x7f])", "", s)


def _assemble(units, tgt):
    out = []
    for kind, s in units:
        if kind == "sep" and s == " " and (tgt in _NO_SPACE):
            continue
        if kind == "s" and tgt in ("zh", "zt", "ja"):
            s = _cjk_punct(s)
        out.append(s)
    return "".join(out).rstrip()


# ---------------------------------------------------------------------------
# Moses-style tokenization + subword-nmt BPE (for the OPUS-MT derived packs)
# ---------------------------------------------------------------------------
_ESCAPE = [("&", "&amp;"), ("|", "&#124;"), ("<", "&lt;"), (">", "&gt;"), ("'", "&apos;"),
           ('"', "&quot;"), ("[", "&#91;"), ("]", "&#93;")]


def _moses_tokenize(s, lang):
    s = re.sub(r"\s+", " ", s.strip())
    s = re.sub(r"([^\w\s.'`,\-])", r" \1 ", s)
    s = re.sub(r"(\w)-(?=\w)", r"\1 @-@ ", s)
    s = re.sub(r"([^\d]),", r"\1 , ", s)
    s = re.sub(r",([^\d])", r" , \1", s)
    s = re.sub(r"\.\.\.+", " ... ", s)
    if lang == "en":
        s = re.sub(r"([^\w])'", r"\1 ' ", s)
        s = re.sub(r"'([^\w])", r" ' \1", s)
        s = re.sub(r"(\w)'(\w)", r"\1 '\2", s)
    elif lang in ("fr", "it"):
        s = re.sub(r"(\w)'(\w)", r"\1' \2", s)
        s = re.sub(r"(^|[^\w])'|'([^\w]|$)", r"\1 ' \2", s)
    else:
        s = s.replace("'", " ' ")
    toks = []
    for t in s.split():
        # a word-final period (not an abbreviation) is its own token
        if len(t) > 1 and t.endswith(".") and t != "..." and \
                not re.fullmatch(r"(\w\.)+|\d+\.", t) and t[:-1].lower() not in _ABBREV:
            toks += [t[:-1], "."]
        else:
            toks.append(t)
    out = []
    for t in toks:
        for a, b in _ESCAPE:
            t = t.replace(a, b)
        out.append(t)
    return out


def _moses_detokenize(tokens, lang):
    unesc = []
    for t in tokens:
        for a, b in reversed(_ESCAPE):
            t = t.replace(b, a)
        unesc.append(t)
    s = " ".join(unesc).replace(" @-@ ", "-").replace("@-@", "-")
    quote_open = True
    words = s.split(" ")
    out = ""
    for w in words:
        if not w:
            continue
        if w == '"':
            if quote_open:
                out += (" " if out else "") + w
                attach_next = True
            else:
                out += w
                attach_next = False
            quote_open = not quote_open
            if attach_next:
                out += "\0"
            continue
        if out.endswith("\0"):
            out = out[:-1] + w
        elif re.fullmatch(r"[.,!?;:%)\]}…]+|'s|'re|'ve|'ll|'d|'m|n't|'", w) and out:
            out += w
        elif out.endswith(("(", "[", "{", "¿", "¡")) or (lang in ("fr", "it") and out.endswith("'")):
            out += w
        else:
            out += (" " if out else "") + w
    out = out.replace("\0", "")
    if lang in _NO_SPACE:
        out = re.sub(r"(?<=[^\x00-\x7f]) (?=[^\x00-\x7f])", "", out)
    return out


class _BPE:
    """subword-nmt's apply_bpe (codes version 0.2), without glossaries."""

    def __init__(self, f):
        first = f.readline()
        lines = f if first.startswith("#version") else [first, *f]
        self.ranks = {}
        for i, line in enumerate(lines):
            parts = line.rstrip("\n").split(" ")
            if len(parts) == 2 and tuple(parts) not in self.ranks:
                self.ranks[tuple(parts)] = i
        self.cache = {}

    def _word(self, w):
        hit = self.cache.get(w)
        if hit is not None:
            return hit
        word = list(w[:-1]) + [w[-1] + "</w>"]
        while len(word) > 1:
            pairs = [(self.ranks.get((word[i], word[i + 1]), None), i) for i in range(len(word) - 1)]
            pairs = [p for p in pairs if p[0] is not None]
            if not pairs:
                break
            best = min(pairs)[0]
            a, b = next((word[i], word[i + 1]) for r, i in pairs if r == best)
            merged, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == a and word[i + 1] == b:
                    merged.append(a + b)
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = merged
        if word[-1] == "</w>":
            word = word[:-1]
        elif word[-1].endswith("</w>"):
            word[-1] = word[-1][:-4]
        if len(self.cache) < 100_000:
            self.cache[w] = word
        return word

    def segment(self, tokens):
        out = []
        for t in tokens:
            pieces = self._word(t)
            out += [p + "@@" for p in pieces[:-1]] + [pieces[-1]]
        return out


# ---------------------------------------------------------------------------
# Helper process client (Windows)
# ---------------------------------------------------------------------------
_helpers: dict = {}


def _helper(exe):
    h = _helpers.get(exe)
    if h is None:
        h = _helpers[exe] = _Helper(exe)
    return h


class _Helper:
    """Client for preview.translate_helper over the child's stdin/stdout.

    Request:  one JSON line {"text", "dirs", "tgt"}.
    Response: JSON lines {"partial": "..."} then {"text": "..."} or {"error": "..."}.
    The child starts on first use and stays up; it is restarted once if it dies."""

    def __init__(self, exe):
        self.exe = exe
        self.proc = None

    def _start(self):
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        hello = self._read()
        if not hello.get("ready"):
            raise RuntimeError(f"translation helper did not start: {hello.get('error', hello)}")

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("translation helper exited")
        return json.loads(line)

    def _call(self, req, on_partial):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write(json.dumps(req).encode() + b"\n")
        self.proc.stdin.flush()
        while True:
            res = self._read()
            if "partial" in res:
                if on_partial:
                    on_partial(res["partial"])
                continue
            return res

    def translate(self, text, dirs, tgt, on_partial=None, cancelled=None):
        req = {"text": text, "dirs": dirs, "tgt": tgt}
        with _run_lock:
            try:
                res = self._call(req, on_partial)
            except (OSError, ValueError, RuntimeError):
                self.close()
                res = self._call(req, on_partial)        # one restart
        if "error" in res:
            raise RuntimeError(res["error"])
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        return res["text"]

    def close(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(2)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    args = list(argv[1:])
    src = tgt = None
    if "--from" in args:
        i = args.index("--from")
        src = args[i + 1]
        del args[i:i + 2]
    if "--to" in args:
        i = args.index("--to")
        tgt = args[i + 1]
        del args[i:i + 2]
    if not args:
        print("usage: python -m preview.translate [--from xx] [--to yy] TEXT | -")
        return 2
    text = sys.stdin.read() if args == ["-"] else " ".join(args)
    tgt = tgt or target()
    src = src or detect(text)
    print(f"[{src} -> {tgt}]", file=sys.stderr)
    need = missing(src, tgt)
    if need:
        print(f"[downloading {need}]", file=sys.stderr)
        download(need, lambda f, t: print(f"\r{t}   ", end="", file=sys.stderr))
        print(file=sys.stderr)
    t0 = time.perf_counter()
    print(translate(text, src, tgt))
    engine = "Argos"
    if plan(src, tgt) == [MODEL_PACK]:
        from . import translate_llm
        on = translate_llm.running_on()
        engine = ("Hy-MT2 (not started: nothing to translate)" if on is None else
                  f"Hy-MT2 on {on} (Vulkan)" if on else "Hy-MT2 on the CPU")
    print(f"[{time.perf_counter() - t0:.2f} s, {engine}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
