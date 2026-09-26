# PDF translation runtime architecture

The implemented vertical path is:

`manifest -> TranslationUnit -> ContextPacket -> TranslationRequest -> Provider -> TranslationResult -> quality gates -> SQLite state/cache/ledger`

## Boundaries

- PDF inspection is local and read-only. PyMuPDF records hash, page count, dimensions, rotation and extractable character count.
- BabelDOC 0.6.4 owns parsing and write-back. `pdf-smoke` captures fragments, locates them against page geometry, reconstructs conservative semantic groups in spatial reading order, translates each group once, and moves explicit layout markers to natural target-language sentence boundaries before injection. Injection still matches exact captured content rather than callback order. `--reuse-capture` validates and reuses a frozen page set without loading the layout model until PDF write-back. The legacy general `render` command remains guarded.
- The translator receives only one explicit unit plus its explicit context. The OpenAI adapter does not use a conversation or previous response ID and sets `store=false`.
- Runtime-generated request IDs, usage and provider response IDs are not accepted from model output.
- Local cache identity includes the complete unit, context packet and model specification.

## State and recovery

Each run freezes an input hash and configuration hash. Reusing a run ID with changed inputs or configuration is rejected. Accepted, blocked and review-required units are not resubmitted on resume. SQLite uses WAL mode. Every external attempt is persisted in `provider_attempts`, `events` and the usage ledger before downstream result/quality validation can fail. Invalid structured output and critical protected/layout-marker failures may retry within `provider_max_attempts`; the persisted count prevents a resume from resetting the limit. Missing API keys fail before an attempt is charged.

## Known document boundary

Three real source PDFs are registered locally and `dm.pdf` has frozen sample sets. Physical pages 42, 51 and 577 pass the v0.5 DeepSeek capture/runtime/injection path and merge into three-page mono/dual artifacts. Three fragmented page-42 paragraphs are reconstructed before translation, and exact page-text lookup prevents the short page-51 phrase `we can` from being assigned to an unrelated block. Formula-only and code-placeholder units bypass the model exactly. Layout markers are restored and resegmented locally while preserving the complete BabelDOC marker order; decimal points and spaced ellipses are excluded from sentence-boundary detection. Exact region-level source bounding boxes remain incomplete for 41 of 74 units, BabelDOC TOC migration still warns, and formula-internal English plus mixed-script spacing remain layout limitations.

## V1 context path

`TranslationUnit -> applicable scopes -> approved Style/Glossary -> explicit semantic dependencies -> optional neighbors -> exact confirmed TM -> budget selection -> ContextPacket`

Scope precedence is `unit > section > chapter > document > domain`. Conflicting approved terminology at the same scope blocks packet construction. Required style, glossary and semantic dependencies cannot be trimmed to satisfy the context budget. Optional neighbors and exact confirmed TM are trimmed last.

For captured PDFs, spatial reading order builds previous/next links among translatable units. Links may cross only consecutive physical pages, so disjoint benchmark samples cannot leak context. Numbered headings build nested section paths, while page-level fallback scopes preserve traceability when no heading is visible. Neighbor text is capped before budget selection.

Every packet records selection reasons, source IDs, revisions, a memory snapshot hash and a conservative token estimate. Changing selected context changes the translation cache key; the state store returns the unit to `prepared` instead of silently retaining an accepted result from an obsolete context snapshot.

The rc.2 A/B path freezes two zero-budget configurations with identical model, prompt, adapter, common style and adaptive per-unit output ceilings. The context arm adds only neighboring source excerpts from a dedicated database. Preparation rejects approved glossary, semantic or confirmed translation-memory content as confounders. Paid execution remains double gated by an explicit CLI flag and an in-memory combined budget; tracked configuration files stay at zero. Results can be exported as deterministic anonymous X/Y pairs with a separate reveal key.

The rc.5 prompt encloses reference context and the source to translate in separate tags and explicitly forbids returning translated context. A paid diagnostic found that without this boundary a short copyright footer could inherit neighboring prose. Both final A/B arms use the same corrected prompt. Formula-marker reordering without a layout split is retained as a major human-review issue; with layout splits it remains a critical block. Boundary-adjacent formula markers can move only within their neighboring protected-marker interval.

The v0.7 candidate defaults to the V1 context path for general PDF translation. Neighbor links are recomputed within the same section and exclude obvious copyright/contact running matter. Capture reuse is rejected when its document hash or physical page set does not match the input. A post-render audit compares page geometry, raster-image digests and positions, vector drawing commands to a 0.001 pt numeric tolerance, and recognizable math-font glyph text/font/size/position. Page 42 fails the math-font glyph comparison (source 50, target 32) even though the image/vector checks pass, so the output is hard-blocked for formal use. Other formula/symbol glyphs, unchanged text fonts/positions and pixel-level background have not yet been proven. Source-target meaning equivalence also needs independent review; automatic quality acceptance is not such proof.

The rc.2 experimental writer copies a source page and empties only uniquely located whole `Tm ... TJ` target-text operations in the copied original content stream. It never replays the page through BabelDOC. It then inserts Chinese with a locally embedded font. Before publication, it refuses insertion slots overlapping unchanged glyphs, images or vectors; after saving, it verifies all glyphs (text/font/size/origin/bbox) outside target slots, page primitives and rendered pixels outside a 0.25 pt antialias margin around the exact target slots. A page-42 paragraph passed, with six source text operators replaced and zero changed pixels outside target slots at 2×. This is not yet an automatic full-page writer: exact segment identification, multiple streams and other PDF text operators remain unsupported and must fail closed.

Rc.3 maps eligible captured fragments to native PDF prose automatically, plans single-line Chinese insertions from a complete previously accepted semantic group, preserves stand-alone numeric labels, and checks all numeric/selected symbol glyphs independently. On page 42, four of 21 fragments passed a read-only source-removal audit. One two-fragment paragraph passed automatic write-back: six text operators replaced, all 50 recognizable math-font and 140 numeric/symbol glyphs unchanged, and zero changed pixels outside target at 2× with a 0.5 pt antialias margin. This remains a partial-page probe: 17 fragments were blocked by conservative checks, multiple streams and other PDF text operators remain unsupported, and complete-page meaning/visual QA is unproven.

Rc.4 addresses a separate content gap: BabelDOC replaces visible inline formulas and footnote marks with opaque placeholders, so the prior model input could omit facts such as `= 1` or `≠`. After capture identity validation, bounded unique native-text matching attaches readable marker-to-literal hints to each translation unit. The provider receives them in a separate read-only prompt section and still must return the unchanged marker. Hints are part of request/cache identity and conservative input-cost estimates; unresolved formula markers yield `needs_review` and are exposed in the bilingual review queue. This improves source information available to future calls, but does not prove translation equivalence or fix the full-page writer.

Rc.5 expands source-preserving layout for a verified two-line paragraph whose original PDF ends with a separate `1:` text operation. Native source prose is removed while that operation retains its exact glyph/font/size/position. The complete saved Chinese translation is wrapped after translation to end near the original digit. The copied PDF's content stream is then altered only under strict BT/TJ/font-reset and no-intervening-protected-text checks so the new Chinese precedes `1:` in text extraction. The writer verifies inserted text is visible and ordered before and after saving; original visual primitives and all outside-target glyphs/pixels match, and 2× full-page pixels are identical before versus after inlining. This remains one partial-page diagnostic and does not establish complete-page source/target meaning or formal readiness.

Output ceilings use `min(configured maximum, max(384, ceil(source characters * 0.75) + 128))`. A response explicitly truncated at `max_output_tokens` is charged and recorded before one bounded retry with an expanded ceiling; resume continues from the persisted attempted ceiling rather than starting over.

The v0.6 release candidate has structural evidence on smoke and development samples, but no paid translation-quality comparison. Frozen-test pages remain untouched until a candidate is locked and a separate blind-test decision is approved.
