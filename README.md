# svgshot

Convert a Windows-style GUI screenshot from PNG to an editable, simplified SVG.
This is an **early, conservative reconstructor**, not a general bitmap tracer. It
uses OCR for SVG `<text>`, color regions for panels and buttons, fits small
circles and outlines, groups radio controls with their captions, and embeds
unrecognized small artwork as PNG islands. It reduces gradients, shadows,
antialiasing noise, and tiny color variants. It does not yet reliably recognize
arbitrary icons, tables, complex layouts, or every Windows theme.

## Install

Requires Python 3.10+, [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
on `PATH`, and [Inkscape](https://inkscape.org/) on `PATH` only when requesting
a comparison report. Install the Python package:

```sh
python -m pip install .
svgshot screenshot.png screenshot.svg
```

On Windows, install Tesseract and add its executable directory to `PATH`.
The SVG contains editable text and shapes; it may also contain a few embedded
PNG regions for details the recognizer cannot confidently describe. Use
`--no-raster` to omit these. `--no-ocr` explicitly disables text reconstruction.

```sh
svgshot screenshot.png screenshot.svg --scene scene.json \
  --diagnostic text-boxes.svg \
  --report quality.json --strict
```

The CLI returns 0 on successful conversion, 1 on errors, and 2 with `--strict`
when the comparison report has warnings. It writes the SVG and report before
returning 2. `--report` renders the SVG through Inkscape. The optional scene
JSON exposes bounding boxes, hierarchy, and recognition confidence for tuning.
The diagnostic SVG displays input and output side by side, with magenta OCR
boxes on both and cyan text baselines on the output. Hover over a box to read
its recognized text. It does not require Inkscape.

## Tuning

Provide `--config settings.json`, for example:

```json
{
  "colors": 12,
  "min_area": 18,
  "max_raster": 128,
  "raster_fallback": true,
  "ocr": true,
  "language": "eng",
  "font_family": "auto"
}
```

`colors` controls palette quantization (2–32); a smaller value discards more
visual detail. `min_area` discards tiny components, while `max_raster` limits
the width or height of a retained unknown detail. The other options are
self-explanatory. `--lang` overrides the OCR language. Try a few settings on
your own screenshot corpus and inspect both SVG and report; there is no
universal setting for every UI.
`auto` uses Segoe UI when installed on Windows and Arial elsewhere. You can
set `--font-family "Segoe UI"` to fit and render with a specific installed font.
This controls SVG typography; it is not a Tesseract recognition hint.

## Validation

Pixel difference alone punishes useful simplification and harmless font
changes. The report keeps several checks separate:

| Field | What it measures | Interpretation |
| --- | --- | --- |
| `edge_recall_4px` | Source edges with an output edge within 4 pixels, excluding OCR text regions | Low values suggest missing or displaced controls. |
| `edge_precision_4px` | Output edges with a source edge within 4 pixels | Low values suggest invented or duplicated shapes. |
| `blurred_color_error` | Mean RGB difference after a 3-pixel blur, divided by 255 | Detects large missing surfaces while allowing small decorative changes. |
| `text.recall` | Whether independently OCRing the rendered SVG recovers the recognized input lines nearby | Detects missing or unreadable labels; low-confidence text missed by input OCR requires ground truth. |
| `ground_truth` | Known labels and control boxes in an optional manifest | Detects omissions that visual comparison and input OCR may both miss. |

These are diagnostics, not a claim that one number captures correctness.
Current warning thresholds are deliberately simple starting points. Inspect
the individual missed elements and the rendered SVG before adjusting them.
Text positions and font metrics can vary without invalidating the result.

Create two deterministic Windows-style fixtures and compare them with known
element boxes and text. The manifest comes from the fixture specification,
independently of recognition:

```sh
svgshot-fixture light.png light.json
svgshot light.png light.svg --report light-report.json --manifest light.json --strict

svgshot-fixture dark.png dark.json --theme dark
svgshot dark.png dark.svg --report dark-report.json --manifest dark.json --strict
python -m unittest discover -s tests -v
```

The fixture draws known controls without needing a live Windows installation.
For a second, more realistic corpus, a tiny Win32 test application can create
standard controls and capture them under Wine with a virtual X display in CI.
Wine implements Win32 rendering but its theme and fonts are not pixel-identical
to current Windows. Microsoft Windows containers do not provide an interactive
desktop. Keep a small set of genuine Windows screenshots with manually checked
manifests alongside generated fixtures for real-world coverage.

## Current boundaries

- Input OCR can miss small or low-contrast text. The tool makes a local OCR
  pass over likely buttons, but a known-element manifest is needed to prove
  completeness.
- Large photos and custom artwork are intentionally not reconstructed.
  Small unknown regions can stay embedded as raster PNGs.
- Color quantization and simple shape fitting can merge nearby controls or
  misclassify unusual UI. Inspect `--scene` and adjust the settings.
- The generated SVG is an approximation. Font substitutions change line width;
  the output does not reflow labels or infer invisible accessibility metadata.
