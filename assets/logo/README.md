# Vezir logo assets

Hand-coded SVGs. Modern dev-tool aesthetic: monochrome chess-vizier mark
with a single coral accent, lowercase wordmark in Fira Mono Bold
(outlined to paths so there is no font dependency at render time).

## Files

| File | Use it for | Notes |
|---|---|---|
| `vezir-mark.svg` | Mark only, on light backgrounds. | 256×256 viewBox, transparent, ink `#111111`. |
| `vezir-mark-light.svg` | Mark only, on dark backgrounds. | Same geometry, white `#FFFFFF` ink. |
| `vezir-mark-app.svg` | App icon source — light variant. iOS / macOS / Android / Windows. | 1024×1024 white rounded-square with ink mark. |
| `vezir-mark-app-dark.svg` | App icon source — dark variant. | 1024×1024 ink rounded-square with white mark. |
| `vezir-logo.svg` | Horizontal lockup, on light backgrounds. README hero, GitHub social card, slide title pages. | ~632×200, ink mark + ink wordmark + coral dot. |
| `vezir-logo-light.svg` | Horizontal lockup, on dark backgrounds. | Same geometry, white ink. Pair with `vezir-logo.svg` via `<picture>` for theme-aware GitHub READMEs. |
| `vezir-logo-stacked.svg` | Vertical lockup for square contexts: avatars, hero blocks. | 400×420, ink. |
| `favicon.svg` | Browser tab favicon. | Optimized for 16/32px; uses a simplified 3-peak coronet. |

### Theme-aware embedding (GitHub README)

GitHub READMEs render on a light or dark surface depending on the
viewer's preference. Use a `<picture>` element to serve the right
variant:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo/vezir-logo-light.svg">
  <img src="assets/logo/vezir-logo.svg" alt="vezir" width="320">
</picture>
```

The same pattern works for the mark-only files (`vezir-mark.svg` /
`vezir-mark-light.svg`).

## Brand tokens

| Token | Hex | Use |
|---|---|---|
| Ink | `#111111` | Mark, wordmark, body text |
| Surface | `#FFFFFF` | Background |
| Coral | `#FF6B35` | Accent — the audio dot above the mark, link colors, hover states |

Three colors. No gradients, no shadows, no tints. The coral is used
sparingly — only the audio dot in the mark. Don't introduce a fourth
color casually.

### Why this palette

The earlier gold-on-navy palette read as heritage / luxury / Ottoman
revival — wrong for self-hosted dev infrastructure. The new system is
strict: black on white, with a single warm accent. This vocabulary is
shared with Linear, Vercel, Resend, Plausible, and similar dev-tool
brands. The coral specifically (warmer than red, more distinctive than
blue) gives vezir something to be recognized by.

## Wordmark

Lowercase `vezir` in **Fira Mono Bold**. Outlined to paths inside the
lockup SVGs, so renders identically anywhere — no font file required at
view time.

For other places the wordmark appears (slide decks, web UI headings,
documentation), use Fira Mono or any modern monospace bold. If neither
is available, JetBrains Mono Bold or IBM Plex Mono Bold are acceptable
substitutes — they share the same geometric character.

Treatment rules:
- Always lowercase. `vezir`, never `VEZIR`, `Vezir`, or `VEZIR`.
- Tracking: −0.02em (slight negative tracking).
- Single weight (Bold / 700).
- Don't decorate. No drop caps, no underlines, no italic.

## Mark

A stylized chess-vizier piece — five-point queen coronet, tapered stem,
flat plinth — with a single coral dot floating just above the crown.

The chess reference is intentional: in the historical game, the
vizier piece evolved into the queen, the most powerful piece on the
board. The coral dot above the crown reads as audio coming in / an
attention signal — vezir's listening function.

The mark is constructed on a 16-unit grid:
- Crown: 8 units tall (5 peaks, 4 valleys)
- Stem: 6.25 units tall, tapered from 4.5 to 6 units wide
- Plinth: 1.75 units tall, 10 units wide, 0.4 unit corner radius
- Audio dot: 0.75 unit radius, centered 1.75 units above crown

Reproducible. Editable. No hand-tuned curves.

## Don't

- Don't recolor outside `#111111` / `#FFFFFF` / `#FF6B35`.
- Don't apply effects (shadows, glows, bevels, gradients).
- Don't stretch or skew. Scale uniformly.
- Don't rotate.
- Don't capitalize the wordmark. Ever.
- Don't recreate the wordmark in a serif. The lockup is monospace by design.
- Don't separate the dot from the mark — they read together.

## Clear space

Minimum padding around any logo file equals the diameter of the audio
dot (≈12 units in the mark's grid, ≈18px when the mark is rendered at
256px). Don't crowd it.

## Generating raster assets

The SVGs are the source of truth. Generate PNGs at any size:

```bash
# cairosvg (Python)
python -c "import cairosvg; cairosvg.svg2png(url='vezir-mark.svg', \
  write_to='vezir-mark-512.png', output_width=512)"

# rsvg-convert
rsvg-convert -w 512 vezir-mark.svg -o vezir-mark-512.png

# Inkscape
inkscape vezir-mark.svg --export-type=png \
  --export-filename=vezir-mark-512.png --export-width=512
```

For iOS/Android app stores, render `vezir-mark-app.svg` (light) or
`vezir-mark-app-dark.svg` at the platform's required sizes.

For Windows `.ico`, render `favicon.svg` at 16/32/48 and combine:

```bash
for s in 16 32 48; do
  rsvg-convert -w $s favicon.svg -o /tmp/favicon-$s.png
done
convert /tmp/favicon-{16,32,48}.png favicon.ico
```

## History

The first internal prototype used a gold-on-navy Ottoman-arch design
with a serif "VEZIR" wordmark. It was retired because the heritage
visual language read as old-fashioned for a self-hosted developer tool.
The current system keeps the conceptual reference (vizier = trusted
advisor; chess queen = most powerful piece) but expresses it in
contemporary dev-tool vocabulary: monochrome geometric mark, monospace
lowercase wordmark, single accent.
