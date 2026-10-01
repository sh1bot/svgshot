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

## Semantic window capture (Windows)

A **separate capture tool** combines a native Windows SDK helper with the existing
Python reconstructor. Point at a window and click to capture it; Esc cancels.
The helper reads UI Automation (UIA) and saves the window through Windows
Graphics Capture. Python renders UIA text and control states first, using OCR
and the existing simplified vector artwork for details UIA does not expose.
Small raster fallback is **off by default** in this tool; `--allow-raster` opts in.
The ordinary PNG conversion command retains its existing behavior.

The helper is C++20 using the Windows SDK and a bundled miniz compression encoder.
Building requires no extra library installation or download. Live capture requires
Windows 10 version 1903 or later, a desktop supporting Windows Graphics Capture,
and a non-minimized target window. Install Visual Studio's **Desktop development
with C++** workload, a Windows 10/11 SDK, and CMake. From the repository root,
build for your machine (use `ARM64` on Windows on ARM, `x64` on Intel/AMD):

```powershell
cmake -S capture/windows -B build/capture -A ARM64
cmake --build build/capture --config Release
```

The Python dependencies and Tesseract are the same as for ordinary conversion.
Run directly from the checkout, with no package reinstall after source updates:

```powershell
.\.venv\Scripts\python.exe -m svgshot.capture capture.svg --html capture.html
```

After updates to the native source, repeat `cmake --build build/capture --config
Release`. The Python command finds `build/capture/Release/svgshot-capture-win.exe`
automatically. `--helper path\to\svgshot-capture-win.exe`, the environment variable
`SVGSHOT_CAPTURE_HELPER`, or an executable on `PATH` can specify another build.
Installing the Python package also provides the `svgshot-capture` entry point;
the native helper is named `svgshot-capture-win.exe` to avoid a command-name collision.

For keyboard-based capture, switch to the intended window during a delay:

```powershell
.\.venv\Scripts\python.exe -m svgshot.capture capture.svg --foreground --delay 5
```

`--hwnd 0x123456` targets a known window handle. The picker freezes the desktop
visually while selecting, and its click does not press a control in the target.
The captured bitmap is a fresh window frame after selection, not that frozen
selection preview. UIA extraction has a 20-second provider timeout and limits
of 5000 elements / 64 levels; any truncation is recorded in the snapshot.

The native capture and SVG conversion tools can run independently:

```powershell
.\build\capture\Release\svgshot-capture-win.exe --out capture
.\.venv\Scripts\python.exe -m svgshot.capture revised.svg --image capture.png --html revised.html
```

Each live capture saves **one self-contained `capture.png`**, with a compressed
UTF-8 JSON UIA snapshot in a private PNG `suIA` chunk. The native `--json` option
also exports `capture.uia.json`; `--uia-only` writes only that JSON, without a PNG.
Extract the embedded snapshot on any platform with:

```sh
python -m svgshot.snapshot capture.png --json capture.uia.json
```

The collector retains the Raw View tree, including offscreen nodes. Schema v2
adds typed UIA property values and statuses, registered property/pattern names,
pattern availability, relationship IDs, text selections, line attributes, and
formatting runs. Built-in property IDs 30000–30199 are queried when registered
by the installed UIA runtime; built-in patterns 10000–10034 and text attributes
40000–40043 are covered. Pattern state properties preserve table/grid structure,
headers, range limits, scroll positions, accessibility relationships, and more.
No Invoke, SetValue, Scroll, Realize, focus-setting, or other control actions run.
Properties distinguish values, unsupported values, read failures, mixed text
attributes, redactions, and values that cannot be serialized. The existing
convenience fields remain for renderer compatibility; the typed property records
are authoritative when those convenience fields are empty/defaulted on failure.
Password contents and descendants remain redacted.

This is a bounded descriptive snapshot, not an exhaustive UIA object dump.
Custom property/pattern registrations are not discovered, unknown COM objects
are marked unserialized, and virtualized elements are not realized. Limits and
redaction policy are stored in `capture_policy`; tree/text truncation produces
warnings. Text has up to 2000 lines, 2048 formatting runs, and 256 selections per
element, with a 65536 UTF-16-character limit per text range. UIA references retain
runtime IDs even when their target lies outside the captured tree.

The privacy review found that full edit values and selections can include text
scrolled out of view. By default, offscreen/out-of-window nodes retain structural
records but their content is redacted. Full Value/Legacy value strings, automation
IDs, process/window-handle properties, and non-descriptive string properties are
also redacted. Text comes from UIA visible ranges; selections are intersected with
those ranges before their text is read. Ranges marked hidden (or mixed hidden/visible)
by the provider are redacted in the default mode. `--include-hidden-content` is an explicit
native/live-capture opt-in to the broader content, and its use is recorded in the
snapshot. Password redaction remains active in both modes.

Accessible names, help text, and descriptions of visible controls are retained:
they are necessary to interpret icon buttons and other controls. They can contain
information **not painted in the bitmap**, including full names behind clipped
labels. This is accessibility metadata, not a guarantee that every embedded string
is visible or free of PII. Review the extracted JSON before sharing sensitive
captures. The collector does not inspect files, process command lines, clipboard,
other windows, or application storage, and it does not invoke controls. Metadata
remains embedded in SVG/HTML output as well as PNG input.

The `suIA` payload has a 16-byte big-endian header: 8-byte `SVGSHOT\0` magic,
container version 1, encoding 1 (JSON), compression 1 (zlib/DEFLATE), reserved zero,
and a 4-byte uncompressed length. A separate zlib stream follows. PNG framing
supplies the chunk length and CRC. Writers put the chunk before `IEND`; readers
scan for it. Limits are 64 MiB decompressed JSON and 16 MiB chunk payload. The
chunk is ancillary, private, and unsafe to copy after image edits: an editor that
does not understand it should discard it when modifying the image data. Ordinary
viewers display the PNG; they do not automatically expose UIA semantics.

Existing version-1 PNG/JSON pairs still work without recapture:

```sh
python -m svgshot.capture revised.svg --image capture.png --uia capture.uia.json \
  --scene revised.scene.json --html revised.html
```

`--no-ocr` works without Tesseract and uses UIA captions/text plus vector geometry.
Windows Terminal text uses its UIA character grid with a monospace font, uniform
line height, preserved spaces, and colours sampled from the PNG. Clipped XAML
labels use a visual ellipsis while retaining their full accessible names.
UIA strings take precedence over overlapping OCR; OCR helps fit text into its
visible ink bounds and supplies labels absent from UIA. UIA image and container
names remain semantic descriptions rather than invented visible captions.
`--config` accepts the existing recognition options; this capture command always
disables source-image overlays, and only `--allow-raster` enables small raster
fallback. The retained SVG renderer may still use its deliberate full-width
raster title gradient.

The SVG has a navigable descriptive hierarchy, source roles in `data-uia-role`,
state descriptions, exact captured UIA metadata, and actual SVG text. It is
explicitly a **static capture**: descriptions such as “Add…, button” convey the
original control's meaning without offering fake clickable controls. Offscreen
or out-of-crop nodes stay in the source snapshot but are omitted from the reader
presentation. Visual geometry is separate from reading order to avoid duplicate
announcements. Decorative vector shapes are hidden from the accessibility tree.

Use inline SVG in documentation to expose its descendants. An `<img src="...">`
usually presents an atomic image, losing the navigable structure. `--html`
creates an inline SVG preview plus a standard HTML outline for readers whose SVG
support is limited. Test the final embedding with your target browser and screen
reader; full UIA-to-SVG behavior parity is not claimed. The original application
can provide custom accessibility interfaces or semantics UIA does not expose.

UIA and the bitmap are successive observations, not an atomic application
snapshot. Use a stable window for documentation. The helper rejects a moved or
resized window; it cannot detect every animation or content change. Coordinate
mapping across a capture-size mismatch emits a warning for inspection. Provider
text ranges may omit covered text even though window capture obtains the window
image; OCR can fill those gaps. Unknown artwork is simplified by the existing
recognizer, with its existing limitations.

The Windows workflow compiles native x64 and ARM64 builds and exercises actual
Win32 UIA controls, Windows Graphics Capture, and PNG encoding on x64. Cross-platform replay tests cover text correction,
states, coordinate mapping, XML escaping, and the separate CLI. Live capture and
screen-reader behavior still require testing on a real Windows desktop.
