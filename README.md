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

## Semantic capture: store first or convert directly

Capture and SVG conversion are separate tools. The PNG is the reusable corpus
entry: pixels plus a compressed, versioned semantic snapshot.

```sh
python -m svgshot.grab corpus/window.png
python -m svgshot.convert corpus/window.png window.svg --html window.html
```

For convenience, capture and convert in memory without creating an intermediate
PNG or JSON file:

```sh
python -m svgshot.convert --capture window.svg --html window.html
```

Installed entry points are `svgshot-grab` and `svgshot-convert`. Both support
`--foreground --delay 5`; capture also offers platform-specific window selection.
Use `--no-ocr` on the converter to reconstruct from supplied semantics and imagery.
`--allow-raster` remains opt-in. The native capture executables do not require Python or the SVG renderer's
numpy/scipy/Tesseract dependencies on any platform.

The unified schema uses named roles/states/styles and PNG pixel coordinates,
with provider-specific metadata retained separately. See the
[version 3 schema specification](docs/capture-schema.md) and
[JSON Schema](docs/capture.schema.json). Legacy UIA v1/v2 PNGs and JSON sidecars
remain readable:

```sh
python -m svgshot.convert capture.png revised.svg --uia capture.uia.json
python -m svgshot.snapshot capture.png --json capture.capture.json
```

The original `python -m svgshot.capture output.svg` command is retained; it keeps
its old capture-and-save flow. New work should use the explicit commands above.
The pixel-only `svgshot` / `python -m svgshot.cli` converter remains available.

### Windows

The native helper uses C++20, the Windows SDK, UIA, Windows Graphics Capture, WIC
and vendored miniz. It emits schema v3 directly. Requires Windows 10 1903+ and a
non-minimized target. Install Visual Studio's Desktop development with C++ workload
and a Windows SDK, then choose ARM64, x64 or Win32:

```powershell
cmake -S capture/windows -B build/capture -A ARM64
cmake --build build/capture --config Release
.\build\capture\Release\svgshot-capture-win.exe --out capture
.\.venv\Scripts\python.exe -m svgshot.convert capture.png capture.svg
```

Native `--out PREFIX` writes `PREFIX.png`; `--json` additionally writes the unified
snapshot to `PREFIX.uia.json` (legacy filename retained). `--uia-only` writes JSON
only. `--stdout` emits a binary semantic PNG with no intermediate file. Selection
supports the existing point-at-window picker, `--hwnd`, and delayed `--foreground`.
`--helper` or `SVGSHOT_CAPTURE_HELPER` can select an explicit native build.

After native-source changes, rebuild. Python searches an explicit helper, the
environment override, PATH, and then build/capture/Release or build/capture.
Privacy defaults from v2 remain: password content is always excluded; offscreen
text, full edit values and diagnostic strings are redacted unless explicitly
permitted by `--include-hidden-content`. Names/help may contain nonpainted text.
UIA and bitmap are successive observations; moving/resizing the window fails.
Windows captures up to 5000 nodes, 64 levels, 2000 lines per element, 2048 format
runs and 256 selections, with limits/errors retained in native metadata.

### macOS

Build with Xcode command-line tools on macOS 14+:

```sh
mkdir -p build/capture
python3 scripts/build_macos_capture.py build/capture/svgshot-capture-macos
build/capture/svgshot-capture-macos --out capture.png
python -m svgshot.convert capture.png capture.svg
```

The Swift helper uses AXUIElement, ScreenCaptureKit/SCScreenshotManager, ImageIO
and system zlib. It supports a window chooser, `--list-windows`, `--window CGWindowID`,
`--foreground`, delayed capture, file output and binary PNG stdout. Accessibility
and screen-capture authorization are required. The helper refuses ambiguous
AX-window/screenshot associations, checks movement, clips text to visible ranges,
and redacts secure text fields. This first backend captures a descriptive subset;
unsupported/error results remain explicit. Hidden-content opt-in is not supported.
Native provider smoke is currently compile/help only; GUI/privacy integration
needs testing on an authorized Mac desktop.

### Linux

The native C++ helper uses libatspi, Xlib, libpng, json-c and zlib. Python, GI and
Pillow are not capture runtime dependencies. See
[Linux setup and Wayland limitations](docs/linux-capture.md).

```sh
cmake -S capture/linux -B build/capture
cmake --build build/capture -j2
build/capture/svgshot-capture-linux --list-windows
build/capture/svgshot-capture-linux --out capture.png --window 0:0
python -m svgshot.convert capture.png capture.svg
```

Automatic capture is supported on X11. Wayland currently requires explicit pairing
with a window bitmap and its bounds; the backend does not guess which portal window
matches an accessibility tree. Linux CI exercises a real GTK/AT-SPI/X11 fixture.

### Latest-source capture binaries

The [rolling capture release](https://github.com/sh1bot/svgshot/releases/tag/capture-latest)
updates from main without a version tag. Each download is a single native
executable, with no archive extraction or companion files needed. Run `--version`
to identify its exact source commit. On Linux/macOS, mark the download executable
with `chmod +x FILE`. Use `--helper FILE` to select a downloaded helper in the
Python frontend/converter. CI also retains artifacts for branch/PR builds.

| Platform | Built architectures | Runtime |
|---|---|---|
| Windows | x86, x86-64, ARM64 | Windows 10 1903+ |
| macOS | x86-64, ARM64 | macOS 14+, permissions |
| Linux glibc | x86, x86-64, ARMv7 hard-float, ARM64, RISC-V64 | AT-SPI2, X11, libpng, json-c, zlib |

Current macOS supports no 32-bit application targets. The current Windows SDK/VS
capture build has no supported ARM32 target. Linux x86/x64/ARM binaries target
Debian 12 (glibc 2.36+); RISC-V64 targets Debian 13 (glibc 2.41+). Build locally on
other libc versions. The converter remains a separate Python tool.

### Static accessibility output

SVG retains the exact unified snapshot in metadata, separates visible artwork from
its semantic outline, and describes controls as informational groups. HTML adds a
nested textual outline for readers that flatten SVG accessibility. Browser and
screen-reader support varies; static captures cannot reproduce live interaction
or announcements. Test the final documentation embedding with the intended reader.
