"""
Document Language Identifier — core engine.

Pipeline:
  1. Load document (PDF or DOCX), get per-page text or renderable images.
  2. Decide which pages to actually process (sampling strategy for long docs).
  3. For each sampled page:
       - If it has extractable text -> use it directly.
       - If it's image-only (scanned) -> run Tesseract OSD to get dominant
         script, then segment into blocks, OCR each block with the right
         language pack, and run langdetect on the recovered text.
  4. Adaptive densification: if two adjacent sampled pages disagree on
     language, go back and sample every page in that gap.
  5. Aggregate into a language profile: % of pages/text per language,
     confidence, and which page ranges each language appears in.

This is a prototype — clear, inspectable, and meant to be extended
(e.g. swap Tesseract OCR for a cloud OCR provider) rather than a
polished production service.
"""

from __future__ import annotations

import io
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from langdetect import detect_langs, LangDetectException

try:
    import docx  # python-docx
except ImportError:
    docx = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Scripts that map to exactly one language (no further disambiguation
# needed once OSD tells us the script).
# Scripts that map to exactly one plausible language, so once OSD names
# the script the language is settled (langdetect still refines the few
# cases where one script serves several languages).
#
# CRITICAL: these keys must be the labels Tesseract's OSD *actually emits
# at runtime*, which are NOT always the Unicode script name you'd expect.
# Verified empirically against generated samples for every major script
# (see verify_osd_scripts.py, which reproduces the check):
#   - Korean text -> OSD emits "Korean", NOT "Hangul"
#   - Japanese    -> OSD emits "Japanese" (handled separately below)
#   - Chinese     -> OSD emits "Han"
# Both spellings are kept where builds may differ. Getting this wrong is
# silent and nasty: an unhandled label falls through to the Latin branch,
# gets OCR'd with a Latin-alphabet model, and yields confident-looking
# garbage in some random European language rather than an honest failure.
SINGLE_LANG_SCRIPTS = {
    "Korean": "kor",
    "Hangul": "kor",          # alias; kept in case a build emits this instead
    "Cyrillic": "rus",        # also ua/bg/sr - langdetect refines from the text
    "Devanagari": "hin",      # also mr/ne - langdetect refines from the text
    "Greek": "ell",
    "Hebrew": "heb",
    "Thai": "tha",
    "Bengali": "ben",         # also Assamese
    "Tamil": "tam",
    "Telugu": "tel",
    "Kannada": "kan",
    "Malayalam": "mal",
}

# Latin script is ambiguous across many languages, but Tesseract's
# character *recognition* (as opposed to dictionary correction) is stable
# across Latin languages, so we OCR once with a combined model and let
# langdetect - which is actually good at distinguishing Latin languages
# on real text - decide. This avoids the instability of OCR'ing once per
# candidate language and comparing langdetect confidence across noisy
# variants (which in testing produced false positives, e.g. English
# misread as German).
LATIN_COMBINED_TESS_LANG = "eng+fra+deu+spa"

# Unicode ranges for Japanese-only kana (not used in Chinese at all).
# Presence of these characters in Han-script OCR output is a reliable,
# deterministic signal that the text is Japanese rather than Chinese -
# far more reliable than running langdetect on bare Han text, which
# frequently mis-calls Chinese as Japanese with no kana present at all.
_HIRAGANA_RANGE = (0x3040, 0x309F)
_KATAKANA_RANGE = (0x30A0, 0x30FF)

# A small set of characters that only appear in Traditional Chinese
# (not exhaustive - good enough as a lightweight simplified/traditional
# heuristic; a proper implementation would use a full char-set diff).
_TRADITIONAL_ONLY_CHARS = set("繁體國學語識識識鑑臺灣關聲")

# Minimum characters of extracted/OCR'd text before we trust a langdetect
# result. Kept low for CJK/Devanagari/etc since each character carries far
# more information than a Latin character; kept higher for Latin scripts
# where langdetect needs more context to be reliable.
MIN_TEXT_LEN_FOR_DETECTION = 20
MIN_TEXT_LEN_CJK = 6

# Below 20 pages, just process everything — sampling isn't worth it.
SMALL_DOC_PAGE_THRESHOLD = 20

# Baseline sampling interval for large docs.
SAMPLE_STRIDE = 10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    page_num: int  # 1-indexed
    source: str  # "native_text" | "ocr"
    script: Optional[str] = None  # whole-page dominant script if OCR'd (may not match every language on a mixed page - see language_scripts)
    languages: list[tuple[str, float]] = field(default_factory=list)  # [(lang, prob), ...]
    language_scripts: dict = field(default_factory=dict)  # lang -> script actually used to OCR the block(s) that produced it. More precise than `script` on a mixed-script page.
    text_sample: str = ""
    note: str = ""


@dataclass
class DocumentReport:
    file_path: str
    total_pages: int
    pages_processed: list[int]
    page_results: list[PageResult]
    language_summary: dict  # lang -> {"pages": int, "pct_pages": float, "avg_confidence": float}
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction: PDF
# ---------------------------------------------------------------------------

def open_pdf(file_path: str) -> fitz.Document:
    return fitz.open(file_path)


def page_has_extractable_text(page: fitz.Page, min_chars: int = 15) -> bool:
    """A page counts as 'native text' if it yields a meaningful amount of
    text directly (not from OCR). Otherwise treat it as scanned/image-only."""
    text = page.get_text("text").strip()
    return len(text) >= min_chars


def get_native_text(page: fitz.Page) -> str:
    return page.get_text("text")


def render_page_to_image(page: fitz.Page, zoom: float = 2.0) -> Image.Image:
    """Rasterize a PDF page to a PIL image for OCR. zoom=2.0 ~= 144dpi,
    bump to 3.0 for small/faint scans if accuracy is poor."""
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return Image.open(io.BytesIO(pix.tobytes("png")))


# ---------------------------------------------------------------------------
# Extraction: DOCX
# ---------------------------------------------------------------------------

def get_docx_text(file_path: str) -> str:
    if docx is None:
        raise RuntimeError("python-docx not installed")
    d = docx.Document(file_path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Script detection (Pass 1) — free, via Tesseract OSD
# ---------------------------------------------------------------------------

def detect_script(image: Image.Image) -> Optional[str]:
    """Run Tesseract OSD (--psm 0) to get the dominant script of an image.
    Returns None if OSD can't confidently determine a script - this
    reliably happens on small crops (a single-line heading, a short
    caption) because OSD needs enough characters to judge orientation
    and script with confidence. Callers should use resolve_block_script
    to get a fallback for that case rather than assuming a full page."""
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        return osd.get("script")
    except pytesseract.TesseractError:
        return None


def _script_char_ratio(text: str, script: str) -> float:
    """What fraction of characters in text plausibly belong to `script`.
    Used to classify short blocks where OSD refuses to run."""
    if not text:
        return 0.0
    ranges = {
        # Japanese is Han (kanji) plus kana - count both so a kana-heavy
        # sample isn't undercounted.
        "Han": [(0x4E00, 0x9FFF), (0x3040, 0x30FF)],
        "Japanese": [(0x4E00, 0x9FFF), (0x3040, 0x30FF)],
        "Korean": [(0xAC00, 0xD7A3), (0x1100, 0x11FF)],
        "Hangul": [(0xAC00, 0xD7A3), (0x1100, 0x11FF)],
        "Arabic": [(0x0600, 0x06FF), (0x0750, 0x077F)],
        "Cyrillic": [(0x0400, 0x04FF)],
        "Devanagari": [(0x0900, 0x097F)],
        "Greek": [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
        "Hebrew": [(0x0590, 0x05FF)],
        "Thai": [(0x0E00, 0x0E7F)],
        "Bengali": [(0x0980, 0x09FF)],
        "Tamil": [(0x0B80, 0x0BFF)],
        "Telugu": [(0x0C00, 0x0C7F)],
        "Kannada": [(0x0C80, 0x0CFF)],
        "Malayalam": [(0x0D00, 0x0D7F)],
    }
    if script in ranges:
        spans = ranges[script]
        match = sum(1 for c in text if any(lo <= ord(c) <= hi for lo, hi in spans))
    else:  # Latin / default
        match = sum(1 for c in text if c.isalpha() and ord(c) < 0x250)
    return match / max(len(text), 1)


def resolve_block_script(block_image: Image.Image, page_script: Optional[str]) -> Optional[str]:
    """Get a block's script, falling back to a cheap Unicode-composition
    probe when OSD can't decide (common on short/single-line blocks like
    headings). This is what lets a short heading in a different script
    from the page body be detected, rather than silently inheriting the
    body's script.

    The probe is deliberately biased TOWARD the page's own script. The
    reason: running the English model over non-Latin text produces
    Latin-looking noise that scores well on the "is this Latin?" ratio
    test, so a naive highest-ratio-wins comparison hands almost every
    block to Latin and wrecks whole-page detection for Tamil, Thai,
    Greek, etc. The page-level OSD call had the whole page's worth of
    characters to work with and is much stronger evidence than a probe
    on one small crop, so Latin has to clearly beat it - not merely tie -
    before we override.
    """
    script = detect_script(block_image)
    if script:
        return script

    if not page_script or page_script == "Latin":
        # Nothing better to compare against; just check whether this
        # block looks Latin at all.
        text = _safe_ocr(block_image, "eng")
        return "Latin" if _script_char_ratio(text, "Latin") >= 0.5 else page_script

    page_probe_lang = {"Han": "chi_sim", "Japanese": "jpn"}.get(page_script) or SINGLE_LANG_SCRIPTS.get(page_script)
    if not page_probe_lang:
        return page_script

    page_text = _safe_ocr(block_image, page_probe_lang)
    page_ratio = _script_char_ratio(page_text, page_script)

    # If the block reads convincingly as the page's script, we're done -
    # don't even consider Latin.
    if page_ratio >= 0.35:
        return page_script

    # Only now consider Latin, and require it to be both strong in
    # absolute terms and clearly better than the page-script reading.
    latin_text = _safe_ocr(block_image, "eng")
    latin_ratio = _script_char_ratio(latin_text, "Latin")
    if latin_ratio >= 0.6 and latin_ratio > page_ratio + 0.25:
        return "Latin"

    return page_script


# ---------------------------------------------------------------------------
# Block segmentation (for mixed-script / mixed-language pages)
# ---------------------------------------------------------------------------

def segment_blocks(image: Image.Image) -> list[dict]:
    """Use Tesseract's layout analysis to split a page image into text
    blocks (paragraph-level), each with a bounding box. This lets us run
    script/language detection per block instead of per page, so a page
    with an English heading over a Chinese body isn't treated as one
    monolithic script."""
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config="--psm 1")
    n = len(data["level"])
    boxes = []
    for i in range(n):
        if data["level"][i] != 4:  # level 4 = individual text region within Tesseract's hierarchy
            continue
        # NOTE: do not dedupe by block_num alone - a single Tesseract
        # "block" can contain multiple level-4 regions (e.g. a heading
        # immediately followed by a paragraph get grouped under the same
        # block_num but are visually/semantically distinct regions that
        # may be in different languages). Keep every level-4 region.
        boxes.append({
            "left": data["left"][i],
            "top": data["top"][i],
            "width": data["width"][i],
            "height": data["height"][i],
        })
    crops = []
    for box in boxes:
        if box["width"] < 20 or box["height"] < 10:
            continue
        crop = image.crop((box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]))
        crops.append({"bbox": box, "image": crop})
    # Fallback: if layout analysis found nothing usable, treat whole page as one block.
    if not crops:
        crops = [{"bbox": {"left": 0, "top": 0, "width": image.width, "height": image.height}, "image": image}]
    return crops


# ---------------------------------------------------------------------------
# Pass 2/3: OCR a block and resolve it to a specific language, routed by
# the script OSD already gave us. Deterministic where possible (Han's
# kana check, single-language scripts); langdetect only where the script
# is genuinely ambiguous (Latin).
# ---------------------------------------------------------------------------

def _contains_kana(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if _HIRAGANA_RANGE[0] <= cp <= _HIRAGANA_RANGE[1] or _KATAKANA_RANGE[0] <= cp <= _KATAKANA_RANGE[1]:
            return True
    return False


def _looks_traditional(text: str) -> bool:
    return any(ch in _TRADITIONAL_ONLY_CHARS for ch in text)


def ocr_and_resolve_language(image: Image.Image, script: str) -> tuple[str, str, float]:
    """OCR a single block/page image and resolve it to an ISO-ish language
    code, using the cheapest reliable method for that script. Returns
    (lang_code, ocr_text, confidence)."""

    if script == "Japanese":
        # OSD already told us directly it's Japanese (a separate, more
        # specific label than "Han" - Tesseract uses this when the
        # glyph shapes look like Japanese typeface/kanji conventions,
        # sometimes even without kana present). Trust it and OCR
        # directly with the Japanese model rather than routing through
        # the Han/kana-check path.
        text = _safe_ocr(image, "jpn")
        conf = _langdetect_confidence(text) if len(text.strip()) >= MIN_TEXT_LEN_CJK else (0.9 if text.strip() else 0.0)
        return "ja", text, conf

    if script == "Han":
        # Pass A: OCR with the Chinese model - it reads Han glyphs
        # accurately regardless of whether the text is actually Chinese
        # or Japanese kanji, so this is a safe first pass.
        raw = _safe_ocr(image, "chi_sim")
        if _contains_kana(raw):
            # Deterministic signal it's Japanese - re-OCR with the
            # Japanese model for a cleaner read (it also handles kana).
            text = _safe_ocr(image, "jpn")
            lang = "ja"
        else:
            text = raw
            lang = "zh-tw" if _looks_traditional(text) else "zh-cn"
        conf = _langdetect_confidence(text) if len(text.strip()) >= MIN_TEXT_LEN_CJK else (0.9 if text.strip() else 0.0)
        return lang, text, conf

    if script in SINGLE_LANG_SCRIPTS:
        tess_lang = SINGLE_LANG_SCRIPTS[script]
        text = _safe_ocr(image, tess_lang)
        lang = _tess_lang_to_iso(tess_lang) or tess_lang
        conf = _langdetect_confidence(text) if len(text.strip()) >= MIN_TEXT_LEN_CJK else (0.9 if text.strip() else 0.0)
        return lang, text, conf

    if script == "Arabic":
        text = _safe_ocr(image, "ara")
        conf = _langdetect_confidence(text) if len(text.strip()) >= MIN_TEXT_LEN_CJK else (0.9 if text.strip() else 0.0)
        # langdetect can split Arabic-script text into ar/fa/ur; trust it
        # if confident, otherwise default to "ar" since that's what OSD
        # + the ara model target.
        try:
            results = detect_langs(text) if len(text.strip()) >= MIN_TEXT_LEN_CJK else []
            if results and results[0].lang in ("ar", "fa", "ur"):
                return results[0].lang, text, results[0].prob
        except LangDetectException:
            pass
        return ("ar", text, conf) if text.strip() else ("", text, 0.0)

    # Latin (or unrecognized script, fall back to Latin-style handling):
    # one combined OCR pass, then let langdetect pick the actual language.
    text = _safe_ocr(image, LATIN_COMBINED_TESS_LANG)
    text = text.strip()
    if len(text) < MIN_TEXT_LEN_FOR_DETECTION:
        return ("", text, 0.0)
    try:
        results = detect_langs(text)
        if results:
            return results[0].lang, text, results[0].prob
    except LangDetectException:
        pass
    return ("", text, 0.0)


def _safe_ocr(image: Image.Image, tess_lang: str) -> str:
    try:
        return pytesseract.image_to_string(image, lang=tess_lang).strip()
    except pytesseract.TesseractError:
        return ""


def _langdetect_confidence(text: str, expected_hint: Optional[str] = None) -> float:
    """Run langdetect and return the top probability."""
    try:
        results = detect_langs(text)
        if results:
            return results[0].prob
    except LangDetectException:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Per-page processing
# ---------------------------------------------------------------------------

def process_native_text_page(page_num: int, text: str) -> PageResult:
    text = text.strip()
    if len(text) < MIN_TEXT_LEN_FOR_DETECTION:
        return PageResult(page_num=page_num, source="native_text", note="too little text to detect")
    try:
        results = detect_langs(text)
        langs = [(r.lang, r.prob) for r in results]
    except LangDetectException:
        langs = []
    return PageResult(
        page_num=page_num,
        source="native_text",
        languages=langs,
        text_sample=text[:200],
    )


def process_scanned_page(page_num: int, image: Image.Image) -> PageResult:
    """Segment the page into blocks, get each block's OWN dominant script
    via OSD (not just one page-level script call) so a page with e.g. an
    English heading over a Chinese body is handled correctly, then resolve
    each block's language and aggregate weighted by recovered text length
    (a long block should count for more than a 3-word heading)."""
    blocks = segment_blocks(image)

    # Page-level script (used for reporting / fallback if per-block OSD fails).
    page_script = detect_script(image)

    block_lang_results: list[tuple[str, float, int, str]] = []  # (lang, conf, char_count, script)
    combined_text_parts = []
    any_block_had_text = False

    for block in blocks:
        block_script = resolve_block_script(block["image"], page_script)
        if block_script is None:
            continue
        lang, text, conf = ocr_and_resolve_language(block["image"], block_script)
        if text:
            any_block_had_text = True
            combined_text_parts.append(text)
        if lang:
            block_lang_results.append((lang, conf, len(text), block_script))

    if page_script is None and not block_lang_results:
        return PageResult(page_num=page_num, source="ocr", note="OSD could not determine script (poor scan quality?)")

    if not block_lang_results:
        note = "script detected but OCR yielded no usable text" if not any_block_had_text \
            else "text recovered but too short/ambiguous to confidently assign a language"
        return PageResult(page_num=page_num, source="ocr", script=page_script, note=note)

    # Aggregate block-level language votes into a page-level distribution,
    # weighted by how much text each language accounted for (so a 2-word
    # heading doesn't outvote a 40-word body paragraph). Also track which
    # script actually produced each language - on a mixed page (e.g. a
    # Latin heading over a Han body), the page's overall OSD script alone
    # would misleadingly attribute the WRONG script to one of the
    # languages, so this is tracked per-language, not just per-page.
    lang_weighted: dict[str, list[tuple[float, int]]] = {}
    lang_to_scripts: dict[str, set] = {}
    for lang, conf, char_count, block_script in block_lang_results:
        lang_weighted.setdefault(lang, []).append((conf, char_count))
        lang_to_scripts.setdefault(lang, set()).add(block_script)

    aggregated = []
    for lang, entries in lang_weighted.items():
        total_chars = sum(c for _, c in entries) or 1
        weighted_conf = sum(conf * c for conf, c in entries) / total_chars
        aggregated.append((lang, weighted_conf, total_chars))
    aggregated.sort(key=lambda x: -x[2])  # order by amount of text, most first
    languages = [(lang, conf) for lang, conf, _ in aggregated]
    language_scripts = {lang: sorted(scripts)[0] for lang, scripts in lang_to_scripts.items()}

    return PageResult(
        page_num=page_num,
        source="ocr",
        script=page_script,
        languages=languages,
        language_scripts=language_scripts,
        text_sample=" ".join(combined_text_parts)[:200],
    )


_TESS_TO_ISO = {
    "eng": "en", "fra": "fr", "deu": "de", "spa": "es",
    "chi_sim": "zh-cn", "chi_tra": "zh-tw", "jpn": "ja", "kor": "ko",
    "ara": "ar", "rus": "ru", "hin": "hi",
    "ell": "el", "heb": "he", "tha": "th", "ben": "bn",
    "tam": "ta", "tel": "te", "kan": "kn", "mal": "ml",
}


def _tess_lang_to_iso(tess_lang: str) -> Optional[str]:
    return _TESS_TO_ISO.get(tess_lang)


# ---------------------------------------------------------------------------
# Sampling strategy
# ---------------------------------------------------------------------------

def choose_sample_pages(total_pages: int) -> list[int]:
    """1-indexed page numbers to process initially, before adaptive
    densification. Small docs: everything. Large docs: first, middle,
    last, + every Nth page for baseline coverage."""
    if total_pages <= SMALL_DOC_PAGE_THRESHOLD:
        return list(range(1, total_pages + 1))

    pages = set()
    pages.add(1)
    pages.add(total_pages)
    pages.add((total_pages // 2) + 1)
    for p in range(1, total_pages + 1, SAMPLE_STRIDE):
        pages.add(p)
    return sorted(pages)


def dominant_lang(result: PageResult) -> Optional[str]:
    if not result.languages:
        return None
    return result.languages[0][0]


def densify_gaps(sampled_results: list[PageResult], total_pages: int) -> list[int]:
    """Look at consecutive processed pages; if their dominant language
    disagrees, return the in-between page numbers so we can go back and
    process every page in that gap to find the real transition point."""
    extra_pages: set[int] = set()
    ordered = sorted(sampled_results, key=lambda r: r.page_num)
    for a, b in zip(ordered, ordered[1:]):
        gap = b.page_num - a.page_num
        if gap <= 1:
            continue
        lang_a, lang_b = dominant_lang(a), dominant_lang(b)
        if lang_a is not None and lang_b is not None and lang_a != lang_b:
            for p in range(a.page_num + 1, b.page_num):
                extra_pages.add(p)
    return sorted(extra_pages)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def analyze_pdf(file_path: str, on_total_known=None, on_page_done=None) -> DocumentReport:
    """on_total_known(total_pages) fires once, as soon as the page count
    is known (before any processing). on_page_done(PageResult) fires once
    per page as it finishes - including the extra pages processed during
    adaptive densification. Both are optional; used by the web app to
    stream live progress to the frontend instead of leaving the person
    watching a static spinner for however long OCR takes."""
    doc = open_pdf(file_path)
    total_pages = doc.page_count
    if on_total_known:
        on_total_known(total_pages)
    warnings = []

    sample_pages = choose_sample_pages(total_pages)
    results: dict[int, PageResult] = {}

    def process(page_num: int):
        page = doc[page_num - 1]
        if page_has_extractable_text(page):
            results[page_num] = process_native_text_page(page_num, get_native_text(page))
        else:
            image = render_page_to_image(page)
            results[page_num] = process_scanned_page(page_num, image)
        if on_page_done:
            on_page_done(results[page_num])

    for p in sample_pages:
        process(p)

    # Adaptive densification pass
    extra = densify_gaps(list(results.values()), total_pages)
    if extra:
        warnings.append(
            f"Detected a language change between sampled pages — densified {len(extra)} "
            f"additional page(s) to locate the transition: {extra}"
        )
        for p in extra:
            process(p)

    all_results = [results[p] for p in sorted(results.keys())]
    summary = summarize_languages(all_results, total_pages)

    return DocumentReport(
        file_path=file_path,
        total_pages=total_pages,
        pages_processed=sorted(results.keys()),
        page_results=all_results,
        language_summary=summary,
        warnings=warnings,
    )


def analyze_docx(file_path: str, on_total_known=None, on_page_done=None) -> DocumentReport:
    """DOCX has no 'scanned' concept — text is always native. We chunk by
    paragraph-ish blocks (split on blank lines) and detect per chunk so a
    bilingual document (e.g. an English cover memo + Chinese appendix
    pasted in as text) still gets both languages surfaced.

    See analyze_pdf for what on_total_known/on_page_done are for."""
    text = get_docx_text(file_path)
    raw_chunks = [c.strip() for c in re.split(r"\n{1,}", text) if c.strip()]
    # total is known immediately here, unlike a PDF where sampling means
    # we don't necessarily touch every page - report the count of chunks
    # actually worth analyzing (skips ones below the length threshold).
    chunks = [c for c in raw_chunks if len(c) >= MIN_TEXT_LEN_FOR_DETECTION]
    if on_total_known:
        on_total_known(len(chunks) or 1)

    results = []
    for i, chunk in enumerate(chunks, start=1):
        try:
            langs = [(r.lang, r.prob) for r in detect_langs(chunk)]
        except LangDetectException:
            langs = []
        pr = PageResult(page_num=i, source="native_text", languages=langs, text_sample=chunk[:200])
        results.append(pr)
        if on_page_done:
            on_page_done(pr)

    summary = summarize_languages(results, total_units=len(results) or 1)
    return DocumentReport(
        file_path=file_path,
        total_pages=len(results),
        pages_processed=[r.page_num for r in results],
        page_results=results,
        language_summary=summary,
    )


def summarize_languages(results: list[PageResult], total_units: int) -> dict:
    """Aggregate per-page/chunk language calls into a document-level
    summary: for each language, how many units it appeared as dominant in,
    the % of processed units, average confidence, and which script(s)
    (Latin, Han, Japanese, ...) it was recognized from. Uses each page's
    language_scripts mapping (accurate per-language, even on a mixed
    page) rather than the page's single overall `script` field, which
    would misattribute the wrong script to one language on a mixed page.
    Native-text pages (no OCR involved) have no script at all, so those
    units just don't contribute to the scripts list."""
    lang_hits: dict[str, list[float]] = {}
    lang_scripts: dict[str, set] = {}
    for r in results:
        if not r.languages:
            continue
        top_lang, top_prob = r.languages[0]
        lang_hits.setdefault(top_lang, []).append(top_prob)
        script_for_this_lang = r.language_scripts.get(top_lang) or r.script
        if script_for_this_lang:
            lang_scripts.setdefault(top_lang, set()).add(script_for_this_lang)

    processed = len(results) or 1
    summary = {}
    for lang, confs in lang_hits.items():
        summary[lang] = {
            "units_dominant": len(confs),
            "pct_of_processed_units": round(100 * len(confs) / processed, 1),
            "avg_confidence": round(statistics.mean(confs), 3),
            "scripts": sorted(lang_scripts.get(lang, set())),
        }
    return dict(sorted(summary.items(), key=lambda x: -x[1]["units_dominant"]))


def analyze_document(file_path: str, on_total_known=None, on_page_done=None) -> DocumentReport:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return analyze_pdf(file_path, on_total_known, on_page_done)
    elif ext in (".docx",):
        return analyze_docx(file_path, on_total_known, on_page_done)
    else:
        raise ValueError(f"Unsupported file type: {ext} (expected .pdf or .docx)")
