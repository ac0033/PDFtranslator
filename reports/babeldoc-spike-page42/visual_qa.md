# BabelDOC page 42 spike: visual QA

Status: **conditional pass for the document adapter only**.

- The output contains exactly one requested physical page in both mono and dual PDFs.
- The dual page shows the original on the left and the synthetic write-back on the right without visible clipping or overlap.
- Display equations (2.5), (2.6), and (2.7), the probability chart, marginal notes, and footnotes remain present and visibly anchored.
- The adapter intercepted 21 distinct source segments. Fourteen segments contained 27 BabelDOC placeholders, which were preserved by the deterministic recorder.
- The synthetic `译：` prefix proves interception and write-back; the retained English is intentional and is not translation-quality evidence.

Open issues:

- Several extracted strings lose spaces around inline components (for example, words adjacent to formulas). This must be tested with a real translator before V0 language acceptance.
- BabelDOC emitted `Failed to migrate TOC ... 'int' object is not iterable`. The one-page output is readable, but bookmark migration is not accepted as working.
- The stable local unit IDs currently identify document hash, page, sequence, and source hash; exact source bounding boxes are not yet exported.
