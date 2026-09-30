# svgshot

Convert a GUI screenshot from PNG to a simplified SVG.
This is an **early, conservative reconstructor**, not a general bitmap tracer. It
uses OCR for SVG `<text>`, color regions for panels and buttons, fits small
circles and outlines, groups radio controls with their captions, and embeds
unrecognized artwork as PNG islands. It looks for title bars and close controls
at either end of a window, including windows inside a larger desktop image.
For some modern dialogs it also identifies an inset white card and its outer
window title, a compact four-color mark, and a smooth illustrated backdrop.
It recognizes selected navigation rows, focused input fields with selected
text, bounded list panes, disabled buttons, and footer surfaces. Those regions
become named SVG shapes and editable text. Compact window and document icons
in the title or body can become simple vector approximations.
It reduces shadows, antialiasing noise, and tiny color variants. A recognized
blue title gradient uses one narrow PNG strip stretched across the whole bar,
with editable caption and close control above it. Faint watermark detail may
be discarded. Complex layouts and custom artwork remain approximate.

## Run from a checkout

Requires Python 3.10+, [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html)
on `PATH`, and the Python packages Pillow, NumPy, and SciPy. Install
[Inkscape](https://inkscape.org/release/) on `PATH` if you want `--report` or
the optional `--source-overlays` mode. For example:

| Platform | External dependencies |
| --- | --- |
| Debian/Ubuntu | `sudo apt install tesseract-ocr inkscape python3-venv` |
| macOS with Homebrew | `brew install tesseract && brew install --cask inkscape` |
| Windows | Install [Tesseract for Windows](https://github.com/UB-Mannheim/tesseract/wiki) and [Inkscape](https://inkscape.org/release/), adding their executable folders to `PATH`; reopen the terminal. |

From the repository root, set up a Python environment **once**:

```sh
python -m venv .venv
.venv/bin/python -m pip install Pillow numpy scipy
```

On Windows PowerShell, use `.\.venv\Scripts\python.exe` in place of
`.venv/bin/python`. Then run the current source directly from the repository
root; after `git pull`, repeat only this command:

```sh
.venv/bin/python -m svgshot.cli screenshot.png screenshot.svg
```

The SVG contains editable text and shapes, plus small embedded PNG details
when needed. Broad source-image overlays are **off by default**.
`--source-overlays` renders the initial SVG, compares blurred color in
96-pixel tiles, then pastes source PNG crops over tiles with large differences.
It can hide recognition mistakes or cut through controls, so inspect the
underlying reconstruction first. The report records overlay area in
`scene.fidelity_raster_coverage` (a legacy field name). `--no-raster` omits
all PNG details; `--no-ocr` disables text reconstruction.

```sh
.venv/bin/python -m svgshot.cli screenshot.png screenshot.svg --scene scene.json \
  --diagnostic text-boxes.svg \
  --report quality.json
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
  "max_raster": 64,
  "raster_fallback": true,
  "fidelity_fallback": false,
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
| `scorecard.layout_regions` | Independently detected panels, title, field rules and selections, navigation rows, list dividers, disabled buttons, footer surfaces, and blue link ink | Checks explicit vector boundaries, text, selection state, and color. Each result has a box and score. |
| `scorecard.regions` | Independently detected compact multicolor marks and decorative backdrops | Checks that they remain vector objects and compares their visible colors and broad shape. |
| `scorecard.local_text` | OCR of small source and output crops around each confident text label | Catches a lost first letter that whole-image OCR recall can overlook. Nearby controls can still confuse OCR. |
| `scorecard.findings` | Actionable faults with `kind`, `box`, and message | Inspect these first. `passes` is false when any semantic finding remains; `score` combines geometry, text, layout, and imagery for ranking iterations. |
| `ground_truth` | Known labels and control boxes in an optional manifest | Detects omissions that visual comparison and input OCR may both miss. |
| `before_fidelity` | Initial color error and edge recall, plus selected overlay coverage and region count | Legacy field name; shows the initial reconstruction before source crops mask mistakes. Present only with `--source-overlays`. |
| `scene.fidelity_raster_coverage` | Area covered by source PNG overlays | Legacy field name; a high value means limited editability. Over 25% adds a warning. |

Run the converter with `--report quality.json`, then compare `scorecard.score`,
`scorecard.passes`, and its localized `findings` across iterations. The overall
score includes blurred color error and edge precision as well as region checks.
`passes` is false when any report warning remains. Treat a failing semantic region as a
fault even when global color error is small. These are diagnostics, not a claim
that one number captures correctness.
Current warning thresholds are deliberately simple starting points. Inspect
the individual missed elements and the rendered SVG before adjusting them.
Text positions and font metrics can vary without invalidating the result.
The separate OCR pass still cannot prove that both readers did not make the
same mistake; a manually checked manifest is stronger evidence.
The region proposals and the reconstruction share shape heuristics: a novel
icon or panel they both miss cannot be discovered by this scorecard alone.
Add independently specified boxes and labels to a manifest for that case.

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
- Large photos and custom artwork are not reconstructed. Small unknown regions
  may stay embedded as raster PNGs. The optional `--source-overlays` pass can cover
  large areas; its report measures that coverage separately.
- Close-control detection works at both ends of title bars, but its macOS
  coverage currently comes from synthetic tests; real macOS screenshots still
  need corpus validation.
- Color quantization and simple shape fitting can merge nearby controls or
  misclassify unusual UI. Inspect `--scene` and adjust the settings.
- The current vector backdrop and multicolor-mark detectors cover a limited
  family of clean, axis-aligned illustrations. Other decorative art may become
  simplified shapes or a small embedded detail; source-image overlays remain
  opt-in.
- The generated SVG is an approximation. Font substitutions change line width;
  the output does not reflow labels or infer invisible accessibility metadata.
