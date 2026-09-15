# OpenMuse Markdown response formatting QA

- Source visual truth: `/var/folders/s0/9dym5l1x20b498rmjt2w092w0000gn/T/codex-clipboard-4deee224-32e7-4fba-8d4a-35b756e2dec9.png`
- Implementation: `open-muse/client/MarkdownMessage.tsx`, rendered inside the production transcript shell
- Implementation screenshot: inline full-page Codex in-app Browser capture of the comparison page; this browser surface did not expose a filesystem path
- Viewport: 597 × 818 CSS px at device pixel ratio 2.2
- Source pixels: 1448 × 1802
- Implementation comparison pixels: approximately 1313 × 2750 (597 × 1250 CSS px at device pixel ratio 2.2)
- State: a completed assistant response containing headings, unordered and ordered lists, emphasis, inline code, a blockquote, a table, and a link
- Density normalization: both source and implementation were displayed at the same 597 CSS-pixel comparison width; the source crop was scaled proportionally from its original pixels

## Full-view comparison evidence

The original transcript shows literal Markdown punctuation and has no visual grouping between tool names and descriptions. The rendered implementation converts the same response patterns into semantic hierarchy while preserving the existing OpenMuse header, avatar, typography, background, composer, and narrow transcript width. No horizontal page overflow or hidden persistent controls were visible.

## Focused-region comparison evidence

The assistant-response region was reviewed at readable size because formatting details were the fidelity target. Bullets and numbering align cleanly with the response text; headings establish hierarchy without overpowering the role label; inline code is distinguishable without becoming a large badge; the quote and table use the existing neutral palette; and the link treatment is visible without introducing a new accent color.

## Required fidelity surfaces

- Fonts and typography: Existing DM Sans and DM Mono choices, base size, line height, and role hierarchy are preserved. Markdown headings and code use those same families and appropriate optical weights.
- Spacing and layout rhythm: List indentation, item spacing, paragraph gaps, and heading margins fit the existing compact transcript rhythm. Wide code and tables scroll within the message instead of widening the chat pane.
- Colors and visual tokens: New surfaces reuse the existing neutral grays, dark green, border colors, and background balance.
- Image quality and asset fidelity: No new image assets are needed; all existing marks and interface assets remain unchanged.
- Copy and content: Model text is preserved verbatim. Only its semantic presentation changes, and user messages remain plain text.

## Findings

No actionable P0, P1, or P2 visual differences remain for the requested formatting change.

## Open questions

None.

## Comparison history

Initial implementation comparison passed. No P0/P1/P2 fixes or recapture loop was required.

## Implementation checklist

- Render assistant messages as GitHub-flavored Markdown.
- Keep user-authored messages as literal plain text.
- Ignore raw HTML and open rendered links in a separate tab.
- Cover structured rendering, raw-HTML safety, and unsafe link protocols with focused tests.

## Follow-up polish

No P3 items identified.

final result: passed
