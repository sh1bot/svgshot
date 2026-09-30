# svgshot

Convert a GUI screenshot from PNG to a simplified SVG.
This is an **early, conservative reconstructor**, not a general bitmap tracer. It
uses OCR for SVG `<text>`, color regions for panels and buttons, fits small
circles and outlines, groups radio controls with their captions, and embeds
unrecognized artwork as PNG islands. It looks for title bars and close controls
at either end of a window, including windows inside a larger desktop image.
It reduces gradients, shadows, antialiasing noise, and tiny color variants.
Complex layouts, watermarks, and custom artwork may need substantial raster
fallback; their SVGs are less editable.

## Install

Requires Python 3.10+, [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
on `PATH`. [Inkscape](https://inkscape.org/) on `PATH` enables automatic visual
fidelity repair and is required for a comparison report. Install the Python package:

```sh
python -m pip install .
svgshot screenshot.png screenshot.svg
```

On Windows, install Tesseract and add its executable directory to `PATH`.
The SVG contains editable text and shapes. It can also contain embedded PNG
regions for details the recognizer cannot confidently describe. With Inkscape
available, the converter renders its first attempt and selectively retains
source regions where color or edges differ substantially. Check
`scene.fidelity_raster_coverage` in the report to see the fraction of the image
covered by these regions. Use `--no-fidelity` for the unrepaired vector attempt,
or `--no-raster` to omit all embedded PNG regions. `--no-ocr` disables text
reconstruction.

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
  "fidelity_fallback": true,
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
| `edge_recall_4px` | Source edges with an output edge within 4 pixels, excluding OCR text regions | Low values suggest missing or displaced controls; `geometry.regions` locates weak areas. |
| `edge_precision_4px` | Output edges with a source edge within 4 pixels | Low values suggest invented or duplicated shapes. |
| `blurred_color_error` | Mean RGB difference after a 3-pixel blur, divided by 255 | Detects large missing surfaces while allowing small decorative changes. |
| `text.recall` | Whether OCR of the rendered SVG recovers text in the reconstructed scene | Detects drawing errors, but can pass when the source OCR was wrong. |
| `source_text.recall` | Whether rendered OCR recovers a separate, high-confidence OCR pass over the input | Detects transcription errors and omitted labels without trusting the scene's text; check `missing` for details. |
| `ground_truth` | Known labels and control boxes in an optional manifest | Detects omissions that visual comparison and input OCR may both miss. |
| `before_fidelity` | Initial color error and edge recall, plus selected repair coverage and region count | Shows how well the editable reconstruction worked before source crops masked mistakes. |
| `scene.fidelity_raster_coverage` | Area covered by source PNG regions added by the fidelity pass | A high value means a visually faithful result with limited editability; over 25% adds a warning. |

These are diagnostics, not a claim that one number captures correctness.
Current warning thresholds are deliberately simple starting points. Inspect
the individual missed elements and the rendered SVG before adjusting them.
Text positions and font metrics can vary without invalidating the result.
The separate OCR pass still cannot prove that both readers did not make the
same mistake; a manually checked manifest is stronger evidence.

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
- Large photos and custom artwork are not reconstructed. Unknown regions can
  stay embedded as raster PNGs; highly detailed screenshots can have large
  fallback coverage. Use `--no-fidelity` and the initial quality measurements
  to examine reconstruction failures separately.
- Close-control detection works at both ends of title bars, but its macOS
  coverage currently comes from synthetic tests; real macOS screenshots still
  need corpus validation.
- Color quantization and simple shape fitting can merge nearby controls or
  misclassify unusual UI. Inspect `--scene` and adjust the settings.
- The generated SVG is an approximation. Font substitutions change line width;
  the output does not reflow labels or infer invisible accessibility metadata.
