# Robust Text Highlight/Annotation Anchoring: Primary-Source Research

Scope: W3C Web Annotation Data Model, Hypothesis client (and supporting libraries), EPUB CFI spec, and Readwise's first-party docs (the only product among Kindle/Readwise/Medium/Google Docs with genuine first-party technical material; see Sources for what was checked and rejected).

## 1. What gets stored when a user highlights text

**W3C Web Annotation Data Model** (`https://www.w3.org/TR/annotation-model/#selectors`, §4.2) defines selectors as attachable to an annotation `target`, and explicitly recommends storing more than one:

> "Multiple Selectors can be given to describe the same Segment in different ways in order to maximize the chances that it will be discoverable later" (§4.2).

Three relevant selector types:
- **TextQuoteSelector** (§4.2.4): `exact` (required, exactly 1) — "A copy of the text which is being selected, after normalization." `prefix`/`suffix` (optional, SHOULD have exactly 1) — text "immediately before/after the text which is being selected."
- **TextPositionSelector** (§4.2.5): `start`/`end` (both required, non-negative integers) — start is inclusive character position 0, end exclusive.
- **RangeSelector** (§4.2.8): `startSelector`/`endSelector` (both required), "typically both of the same class" (e.g. two `XPathSelector`s), describing the inclusive start / exclusive end of a range.

Selectors compose via `refinedBy` (§4.2.9): "The relationship between a broader selector and the more specific selector that SHOULD be applied to the results of the first." Example 29 chains a `FragmentSelector` (paragraph id) refined by a `TextQuoteSelector` — coarse structural narrowing then content-based precision.

**Hypothesis** (`web.hypothes.is/blog/fuzzy-anchoring`) stores all three selector kinds per annotation target: a `RangeSelector` (XPath pair + string offsets), a `TextPositionSelector` (global offsets into the whole document string), and a `TextQuoteSelector` with `exact` plus `prefix`/`suffix` each "the (32-char long) text immediately before/after the selected text." In `src/annotator/anchoring/html.ts`, `describe()` iterates anchor types `[MediaTimeAnchor, RangeAnchor, TextPositionAnchor, TextQuoteAnchor]`, calling `fromRange(root, range)` on each and discarding failures — one highlight redundantly described by every applicable selector type.

**EPUB CFI** (`idpf.org/epub/linking/cfi/epub-cfi.html`, §3.1.1/§3.1.4) stores a pure structural path, not text: step indices through the DOM/XML tree (even indices for element children, odd for text-node "chunks") plus a trailing colon-prefixed character offset, e.g. `epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/3:10)` (§3.1.10). No quote or context text is part of the CFI itself.

**Readwise** (`readwise.io/api_deets`): a Highlight object stores `text` ("technically the only field required"), an optional `note`, and `location` interpreted per `location_type` (`page`, `location`, `none`, `order`, `offset`, `time_offset` — default `order`) — a coarser, ordering-oriented position rather than a precise DOM offset. Readwise's Reader FAQ (`docs.readwise.io/reader/docs/faqs/highlights-tags-notes`) describes cross-surface sync as matching "position of highlight made in one platform ... with sufficient confidence" against the other, implying an internal representation not detailed publicly.

## 2. Avoiding drift between "text used to search" and "text actually in the DOM"

Hypothesis's source is the most concrete here. `src/annotator/anchoring/rendered-text.ts` computes one canonical "rendered text" string by walking the DOM and concatenating `textContent`, with a single explicit normalization: "Walk `root`'s DOM and produce its rendered text (with each `<br>` replaced by a space)," since "unlike block tags (which almost always have whitespace between them in the HTML source), `<br>` elements are written inline" and would otherwise glue words together. It deliberately does **not** consult computed styles (`display`/`visibility`) or collapse whitespace further — it is explicitly not equivalent to the display-aware `Selection.toString()`. This one function is the sole source of truth for computing offsets on save and re-locating them on load, so search text and DOM text cannot diverge independently.

`src/annotator/anchoring/text-range.ts` maps offsets to DOM via `document.createNodeIterator(element, NodeFilter.SHOW_TEXT)`, walking only text nodes and accumulating `textContent` lengths until the target offset falls inside one, returning `{ node, offset }`. `TextRange`/`TextPosition` are built so that "changes in the DOM content of the range which don't affect its text content" don't break anchoring — markup-only edits (e.g. wrapping a `<span>` around text) stay valid because offsets are re-derived from character position rather than a cached node reference.

**EPUB CFI** sidesteps the problem entirely: it never derives from rendered/selection text. It is a structural, XML-tree-indexed address (§3.1.1, §3.1.4) computed against the document's own child-node ordering and UTF-16 offsets inside a specific text node — no "search text vs. rendered text" gap, but also no tolerance for structural change (see §4).

The **W3C model** prescribes no algorithm here; it only requires `exact` be "a copy of the text which is being selected, after normalization" (§4.2.4), leaving normalization to implementations — Hypothesis's `renderedTextOf()` is one concrete instance.

Readwise's public docs do not describe their DOM-text-extraction implementation at this level.

## 3. Disambiguating duplicate/repeated passages

**Hypothesis**'s `src/annotator/anchoring/match-quote.ts` runs a weighted composite scorer over all candidate matches: quote-similarity weight 50, prefix-match weight 20, suffix-match weight 20, proximity-to-hint weight 2 (normalized by the max possible score, 92); highest score wins. Absent `prefix`/`suffix` default their component scores to 1.0 (perfect), so quote-only anchoring degrades gracefully but loses disambiguation power. A `TextPositionSelector`'s `start`, when available, is passed to `matchQuote()` as a `hint` to bias toward the position-adjacent occurrence when the exact string repeats (`html.ts`'s `anchor()`).

**W3C**'s rationale for storing prefix/suffix alongside `exact` is disambiguation-by-context, but the spec states this only implicitly (§4.2.4, prefix/suffix "SHOULD" be included) without prescribing a matching algorithm — that gap is what Hypothesis's scorer fills.

**EPUB CFI** has no duplicate-quote problem by construction: it addresses a tree position via step indices rather than searching text, so identical strings elsewhere in the document get different CFIs automatically (§3.1.1).

**Readwise**'s docs disclose no disambiguation logic beyond "sufficient confidence" position matching (see §1, §4).

## 4. Tolerating small content changes (edits, re-renders, pagination)

**Hypothesis** documents four re-anchoring strategies tried in sequence (`web.hypothes.is/blog/fuzzy-anchoring`):
1. Apply the stored XPath (`RangeSelector`) and verify the text matches the saved quote.
2. Fall back to `TextPositionSelector` global offsets — "handles cases when the structure of the document has changed, but the text content has not."
3. Fuzzy-search for `prefix` near the expected start and `suffix` near the expected end, then verify the intervening text against `exact`.
4. Fuzzy-search on `exact` alone as a last resort.

`match-quote.ts`'s `matchQuote()` first tries a fast exact `indexOf()`; only if that fails does it invoke the `approx-string-match` library (`approx-string-match: ^2.0.0` in `hypothesis/client`'s `package.json`) for edit-distance search. Max tolerated errors is `Math.min(256, quote.length / 2)` — up to half the quote's length may differ, capped at 256 edits. `textMatchScore()` reports similarity as `1 - (errors / str.length)`. The library (authored by Hypothesis engineer Robert Knight) implements a bit-parallel (Myers-style/Bitap) approximate string search, expected time `O((maxErrors / 32) * text.length)`.

**W3C**: no fuzzy-matching algorithm specified — the model only supplies the selector vocabulary (`exact`, `prefix`, `suffix`, offsets) an implementation's matcher would consume.

**EPUB CFI**: the spec defines no fuzzy/approximate fallback — a CFI is a precise structural address, so any change to the numbered child-node sequence (an inserted paragraph, re-flowed pagination) invalidates the exact path. Its only tolerance mechanism is that the offset is a range *within* a text node rather than an absolute document offset, keeping resolution local — it does not itself re-match approximately.

**Readwise**'s FAQ names a confidence-based fallback: on failed matching, "the app will notify you that it failed to match and those highlights will still be visible in the Notebook tab" rather than being silently dropped or mis-placed. No edit-distance/threshold detail is published.

## 5. Ranges spanning multiple block elements

**W3C RangeSelector** (§4.2.8) is designed for exactly this: `startSelector` and `endSelector` are independent selectors (commonly `XPathSelector`s) pointing at start/end boundary nodes, "both SHOULD be of the same class." Example 28 shows a range spanning two different `<td>` cells via two `XPathSelector`s — the same pattern extends to a highlight crossing a paragraph or list-item boundary.

**Hypothesis** stores this as the `RangeSelector`'s XPath-pair-plus-offsets (`xpath.ts` builds/resolves XPath to element boundaries; `text-range.ts`'s `TextRange` holds a `TextPosition` pair, each `{element, offset}`, whose `resolve()`/`relativeTo()` walk up to a common ancestor and back down via `createNodeIterator`). This walk is agnostic to how many block elements separate the two positions, since it operates purely in the flattened rendered-text offset space from `renderedTextOf()`. Multi-paragraph selection is therefore not a special case: the flattening step (§2) already erases block boundaries into one offset space, and the two endpoints are just two positions in it.

**EPUB CFI** represents ranges via §3.4's three-part comma syntax `epubcfi(P,S,E)`: a shared **parent path** `P` (must "end at a step that is common for resolving both" endpoints) followed by **local** start (`S`) and end (`E`) subpaths relative to it. Spec example: `epubcfi(/6/4[chap01ref]!/4[body01]/10[para05],/2/1:1,/3:4)` — `para05` is the shared ancestor, and `/2/1:1`/`/3:4` descend from it to the start/end positions, which may sit in different child elements under that ancestor.

**Readwise**'s public docs do not describe multi-block range representation.

## Sources

- W3C, *Web Annotation Data Model*, §4.2 Selectors (incl. §4.2.4 TextQuoteSelector, §4.2.5 TextPositionSelector, §4.2.8 RangeSelector, §4.2.9 Refinement of Selection) — https://www.w3.org/TR/annotation-model/#selectors
- Hypothesis engineering blog, "Fuzzy Anchoring" — https://web.hypothes.is/blog/fuzzy-anchoring/
- `hypothesis/client`, `src/annotator/anchoring/html.ts` (anchor()/describe() strategy chain) — https://github.com/hypothesis/client/blob/main/src/annotator/anchoring/html.ts
- `hypothesis/client`, `src/annotator/anchoring/match-quote.ts` (fuzzy quote matching, scoring) — https://github.com/hypothesis/client/blob/main/src/annotator/anchoring/match-quote.ts
- `hypothesis/client`, `src/annotator/anchoring/text-range.ts` (TextPosition/TextRange, createNodeIterator offset mapping) — https://github.com/hypothesis/client/blob/main/src/annotator/anchoring/text-range.ts
- `hypothesis/client`, `src/annotator/anchoring/rendered-text.ts` (canonical rendered-text computation) — https://github.com/hypothesis/client/blob/main/src/annotator/anchoring/rendered-text.ts
- `hypothesis/client`, `package.json` (confirms `approx-string-match@^2.0.0` dependency) — https://github.com/hypothesis/client/blob/main/package.json
- `robertknight/approx-string-match-js`, README (bit-parallel approximate string search library used by Hypothesis) — https://github.com/robertknight/approx-string-match-js
- IDPF/W3C, *EPUB CFI (Canonical Fragment Identifier) Specification*, §3.1.1 (steps), §3.1.4 (character offsets), §3.1.10 (examples), §3.4 (ranges) — https://idpf.org/epub/linking/cfi/epub-cfi.html
- Readwise, public Highlight API field reference — https://readwise.io/api_deets
- Readwise, Reader docs FAQ on cross-platform highlight matching — https://docs.readwise.io/reader/docs/faqs/highlights-tags-notes

**Categories checked, no genuine first-party material found (excluded):** Kindle "My Clippings.txt" has no official Amazon documentation — only third-party guides describe its append-only log structure. Amazon/Kindle-adjacent patents found (e.g. US7337389B1, US20110184828A1) belong to Microsoft or cover ink/coordinate- or database-based annotation storage, not text-offset anchoring, so were not cited as anchoring evidence. No first-party Medium engineering post on their highlight implementation was found. No first-party Google Docs writeup on suggested-edit range anchoring was found.

## Comparison to reader3's implementation

reader3 sits closest to a bare-bones **TextQuoteSelector with no prefix/suffix and no fallback tier** — it's the simplest cell in the design space every source above treats as a starting point, not an endpoint.

**1. What's stored.** `HighlightBody` (`server.py:155-158`) persists only `chapter` (an index, not a DOM path) and `text` (the exact quote) — no `prefix`/`suffix`, no character offsets, no structural selector. This is a single, non-redundant selector, whereas every robust system surveyed stores **more than one**: W3C explicitly recommends multiple selectors "to maximize the chances [content] will be discoverable later" (§4.2), and Hypothesis's `describe()` (`html.ts`) attaches a `RangeSelector` + `TextPositionSelector` + `TextQuoteSelector` to every anchor, trying each in turn on relocation. reader3 has exactly one shot at finding the passage again — if that `text` string isn't found verbatim, there is no fallback selector to try. `chapter` is the one coarse position hint reader3 does keep, roughly analogous to Readwise's `location`/`order` field, but it's only used to scope *which chapter's* HTML to search, never to narrow *where within it* (no offset, no proximity hint).

**2. Avoiding search-text/DOM-text drift.** This session's actual bug (fixed in `templates/reader.html:504-522`) was exactly the failure mode Hypothesis's design pre-empts structurally: reader3 originally saved `Selection.toString()` (rendered/collapsed text) but searched raw `nodeValue` concatenation (`findRange`, `templates/reader.html:596-622`) — two different text-extraction functions that were never guaranteed to agree. Hypothesis avoids this by construction: `renderedTextOf()` (`rendered-text.ts`) is the *one* canonical text-flattening function, called identically when computing offsets to save and when re-resolving them on load, so the two sides can't independently drift. The fix applied here (`rawTextOfRange()`, reusing the same raw-`nodeValue` walk `findRange` already searches with) is a narrower version of the same idea — single shared extraction logic — but only handles the "on-save vs on-search" half of it. It's missing Hypothesis's specific patched edge case (`<br>` → space; a bare `<br>` line break in EPUB-generated HTML would still glue two words together in reader3's raw concatenation), and, being untested against `full` at save time, offers no confirmation the saved text is even findable before persisting it.

**3. Duplicate passages.** `findRange`'s `full.indexOf(text)` (`templates/reader.html:606`) returns only the *first* match in the chapter, full stop — reader3 has no way to distinguish two non-overlapping highlights of the same repeated phrase. (It does skip DOM already wrapped in `mark.hl` when building `full`, which incidentally prevents a highlight from re-matching text already claimed by an earlier highlight in the same reload pass — but that's a side effect of the skip-marked-nodes logic, not a designed disambiguation feature, and it does nothing for two identical *un-highlighted* occurrences.) This is the one dimension EPUB CFI solves "for free" that reader3 could adopt cheaply: CFI's step-index addressing means identical strings elsewhere in the document get different addresses automatically, because it never searches text at all (§3.1.1). Hypothesis instead solves it with prefix/suffix-weighted scoring plus a position hint — both approaches are unavailable to reader3 today since it stores neither structural path nor context.

**4. Tolerating small content changes.** reader3 has zero fuzzy tolerance: an exact-substring miss (`idx === -1`) silently drops the highlight with no user-visible signal (`templates/reader.html:606-608`, `624-627` — the `.forEach` just skips a `null` range). This is a real gap against Hypothesis's four-tier fallback (exact selector → position offsets → prefix/suffix fuzzy search → quote-only fuzzy search, each backed by `approx-string-match`'s edit-distance search tolerating up to `quote.length / 2` character differences) and even against Readwise's minimal guarantee that a failed match stays visible rather than vanishing. In reader3's favor: EPUB chapter HTML is static once parsed (`load_book_cached` caches the parsed `Book`), so the live-editable-webpage problem Hypothesis is built for mostly doesn't apply here — the realistic threats are the ones this session actually hit (a saved-text/search-text mismatch, or genuinely corrupted/truncated stored text), not third-party page edits. That changes the cost-benefit: full edit-distance fuzzy matching is probably overkill, but a visible "N highlights couldn't be placed" indicator (mirroring Readwise's "failed to match... still visible in the Notebook tab") would be a cheap, high-value addition — right now a silently-dropped highlight looks identical to a successfully-placed one until a reader happens to scroll past where it should be.

**5. Multi-block ranges.** This is reader3's strongest dimension. `wrapRange()` (`templates/reader.html:542-568`) independently wraps every intersecting text node in its own `<mark>`, which sidesteps block boundaries the same way Hypothesis's flattened-offset `TextRange`/`renderedTextOf()` approach does and the way W3C's `RangeSelector` (independent start/end selectors) is designed to. The gap is only on the *relocation* side: `findRange`'s `full` is a bare concatenation with no separator inserted at block boundaries at all (not even Hypothesis's single `<br>`-as-space patch), so it depends entirely on the source EPUB HTML already containing whitespace between block tags — true often enough in practice (matching Hypothesis's own observation that "block tags... almost always have whitespace between them in the HTML source"), but unverified and silently fails the same way as case 4 when it isn't.

**Bottom line:** reader3's model is "one exact quote, first-match search, no fallback" — the simplest point in the design space, workable because book content is static (unlike the live web pages Hypothesis anchors against) but with no defense against its own failure modes: a corrupted/mismatched saved string (this session's bugs), a repeated passage (§3), or a block-boundary edge case (§5) all fail the same way — silently and invisibly. The two cheapest upgrades suggested by the research, in order of leverage: (a) surface unplaced highlights in the UI instead of dropping them silently (the single biggest gap vs. every source surveyed, and the one that would have made this session's bugs self-diagnosing instead of requiring a manual report), and (b) store `chapter`-relative character offsets alongside `text` as a second selector, cheap to add since `findRange` already computes them internally — enabling a position-hint disambiguation for duplicate quotes without needing full fuzzy matching.
