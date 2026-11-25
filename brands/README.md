# Halumar brand assets

Brand icon and logo for the **Halumar** Home Assistant custom integration
(domain: `halumar`). The icon is the mark (a small "h" that is a house with a
lit window), the logo is the wordmark "halumar" alone: the h of the wordmark is
the mark, so mark and wordmark never appear together.

## Files

Rendered from the vector sources in `docs/images/brand/` (mark:
`halumar-mark-color.svg`, `halumar-mark-on-dark.svg`; wordmark:
`halumar-wordmark-color.svg`, `halumar-wordmark-on-dark.svg`).
Filenames and sizes follow the
[home-assistant/brands](https://github.com/home-assistant/brands) specification.

| File | Size (px) | Purpose |
|------|-----------|---------|
| `custom_integrations/halumar/icon.png` | 256 x 256 | Mark, normal DPI, for light backgrounds |
| `custom_integrations/halumar/icon@2x.png` | 512 x 512 | Mark, hDPI, for light backgrounds |
| `custom_integrations/halumar/dark_icon.png` | 256 x 256 | Mark, normal DPI, for dark backgrounds |
| `custom_integrations/halumar/dark_icon@2x.png` | 512 x 512 | Mark, hDPI, for dark backgrounds |
| `custom_integrations/halumar/logo.png` | 1154 x 256 | Wordmark, normal DPI (shortest side 256), for light backgrounds |
| `custom_integrations/halumar/logo@2x.png` | 2308 x 512 | Wordmark, hDPI (shortest side 512), for light backgrounds |
| `custom_integrations/halumar/dark_logo.png` | 1154 x 256 | Wordmark, normal DPI, for dark backgrounds |
| `custom_integrations/halumar/dark_logo@2x.png` | 2308 x 512 | Wordmark, hDPI, for dark backgrounds |

Spec compliance:

- **Icon** is 1:1 and trimmed: the mark fills the full height, centered
  horizontally on a transparent square. Sizes are exactly 256 and 512.
- **Logo** is the wordmark in the kit's own tight frame. Its shortest side (height)
  is 256 / 512, inside the required "128 to 256 normal, 256 to 512 hDPI" range.
- **Dark variants**: on dark backgrounds the house and the wordmark are cream
  (`#FBF5EA`), the window stays amber. Light versions: terracotta house
  (`#C2553A`), amber window (`#EFA347`), wordmark in earth (`#3B2A25`).
- All files are PNG with transparency (transparency is preferred by the spec).

The identical eight files are also shipped **inside the integration** at
`custom_components/halumar/brand/` (see "Where Home Assistant takes the icon from" below).

The geometry of the logo is fixed: the files are rendered from the sources
unchanged; only the `viewBox` is set to the trimmed, square (icon) or tight
(logo) frame.

## Where Home Assistant takes the icon from

Two mechanisms deliver the icon. The integration supports both.

### 1. Local brand images (Home Assistant 2026.3+): no PR required

Since Home Assistant **2026.3**, a custom integration can bundle its own brand
images in a `brand/` directory inside the integration folder, and local brand
images take precedence over images from the brands repository. The frontend
requests the same filenames used by the brands repo (`icon.png`, `icon@2x.png`,
`logo.png`, `logo@2x.png`, optional `dark_*`), served locally at
`/api/brands/integration/halumar/<file>`.

The integration ships them here:

```
custom_components/halumar/brand/
├── icon.png             256 x 256
├── icon@2x.png          512 x 512
├── dark_icon.png        256 x 256
├── dark_icon@2x.png     512 x 512
├── logo.png             1154 x 256
├── logo@2x.png          2308 x 512
├── dark_logo.png        1154 x 256
└── dark_logo@2x.png     2308 x 512
```

On Home Assistant 2026.3 or newer, the Halumar icon and logo appear in the UI, the
Marketplace and HACS without a brands pull request. This is the primary delivery path.

> Caveat: the integration's minimum supported version is Home Assistant 2025.11.0. On
> installations running **Home Assistant 2025.11 to 2026.2**, the local `brand/` directory is
> ignored and the icon only appears once the brands PR (below) is merged.

### 2. home-assistant/brands pull request: for Home Assistant < 2026.3

The classic path. Brand images live in the public
[home-assistant/brands](https://github.com/home-assistant/brands) repo and are
served from `brands.home-assistant.io`. Custom-integration domains are served
under the `_/` prefix, e.g. `https://brands.home-assistant.io/_/halumar/icon.png`.
The brands repository marks its `custom_integrations` folder as legacy,
because custom integrations ship their images in their own `brand/` folder
since 2026.3; check its README before opening a PR.

Submission steps:

1. Fork `home-assistant/brands`.
2. Copy this folder's contents into the fork at `custom_integrations/halumar/`.
   (The layout under `brands/custom_integrations/halumar/` in this repo mirrors
   the target exactly, so it is a straight copy.)
3. Open a PR. Automated checks validate sizes / square / trim / transparency.
4. Once merged, the icon is served from `brands.home-assistant.io` and shows for
   all Home Assistant versions, including those below 2026.3.

If Halumar is ever contributed to Home Assistant Core, remove the local
`custom_components/halumar/brand/` directory and add the images to the brands
repository instead (per the Home Assistant developer docs).

## Regenerating the assets

Requires `rsvg-convert` (`brew install librsvg`). Run from the repository root.

```bash
SRC=docs/images/brand
OUT=brands/custom_integrations/halumar
TMP=$(mktemp -d)

for v in color on-dark; do
  pre=$([ "$v" = on-dark ] && echo dark_ || true)
  # Icon: the mark on a square, trimmed frame (content 16..80 x 6..90 of the 96 grid)
  sed 's/viewBox="13 3 70 90"/viewBox="6 6 84 84"/' \
    "$SRC/halumar-mark-$v.svg" > "$TMP/icon.svg"
  rsvg-convert -w 256 -h 256 "$TMP/icon.svg" -o "$OUT/${pre}icon.png"
  rsvg-convert -w 512 -h 512 "$TMP/icon.svg" -o "$OUT/${pre}icon@2x.png"
  # Logo: the wordmark alone, in its own frame
  rsvg-convert -h 256 "$SRC/halumar-wordmark-$v.svg" -o "$OUT/${pre}logo.png"
  rsvg-convert -h 512 "$SRC/halumar-wordmark-$v.svg" -o "$OUT/${pre}logo@2x.png"
done

# Then copy the eight files into custom_components/halumar/brand/ as well.
cp "$OUT"/*.png custom_components/halumar/brand/
```

Verify the sizes with `file <file>` (it prints width x height for PNG files), or on macOS with
`sips -g pixelWidth -g pixelHeight <file>`.

## Source

- Vector sources: `docs/images/brand/` (from the Halumar brand kit, unchanged).
- Spec references: home-assistant/brands `README.md` and the Home Assistant developer docs
  page "Brand images" (`developers.home-assistant.io/docs/core/integration/brand_images`).
