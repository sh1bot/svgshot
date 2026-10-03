# svgshot

`svgshot` turns GUI screenshots into simplified, editable SVG. Text and common
controls become SVG elements; complex imagery may remain embedded in the SVG as
small PNG regions. The result is an approximation, so check it before publishing.

## Install and run

Use Python 3.10 or later. From the checkout, install the Python dependencies:

```sh
python -m pip install Pillow numpy scipy
```

Run the script directly from the checkout:

```sh
python -m svgshot.cli screenshot.png screenshot.svg
```

To install the `svgshot` command in a virtual environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/svgshot screenshot.png screenshot.svg
```

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\svgshot.exe`.

Screenshot conversion uses Tesseract OCR. Install Tesseract for your operating
system and make it available on `PATH`. Inkscape is optional; it is needed for
rendered comparison reports.

## Convert a screenshot

```sh
svgshot screenshot.png screenshot.svg
```

`svgshot` checks for accessibility data embedded in the PNG. When it finds the
data, it uses that to create semantic SVG and can also write an accessible HTML
outline:

```sh
svgshot screenshot.png screenshot.svg --html screenshot.html
```

PNG screenshots without embedded data produce a warning and are converted using
image analysis and OCR. Use `--no-ocr` to omit reconstructed text. To save a
recognition report, add `--report report.json` (requires Inkscape).

## Capture

A capture PNG stores the screenshot and its semantic accessibility data together.
Keep the PNG as a reusable source, then convert it when needed:

```sh
svgshot-grab corpus/window.png
svgshot corpus/window.png window.svg --html window.html
```

Or capture and convert directly without saving an intermediate PNG:

```sh
svgshot --capture window.svg --html window.html
```

The capture executable is native on Windows, macOS and Linux. Download one from
the [latest capture release](https://github.com/sh1bot/svgshot/releases/tag/capture-latest).
Each download is a single executable; `--version` prints its source commit. On
Linux and macOS, run `chmod +x FILE` after downloading. To use a downloaded helper
with the Python commands, pass `--helper FILE` or set `SVGSHOT_CAPTURE_HELPER`.

Capture commands accept a complete `.png` filename, either as an argument or
with `--out`. Omit the filename to save on the Desktop using the local date, time
and window title. This applies to `svgshot-grab` and the native executables on all
three platforms. Use `--stdout` to stream the PNG instead.

Capture requires the platform accessibility service. macOS also requires
Accessibility and Screen Recording permission. Linux captures pixels directly
on X11; Wayland requires an explicitly supplied window image. See
[Linux capture notes](docs/linux-capture.md).

Password content is excluded by default. `--unredacted` bypasses those
redactions, including password and offscreen content, and prints a warning. Use
it deliberately; the resulting PNG and SVG may contain sensitive data.
On Windows, `--accessibility-api auto|uia|msaa` selects the accessibility source.
The default probes MSAA when UIA is sparse or slow, and uses the first sufficiently
detailed result.
`--capture-dpi-context window` tries matching the Windows capture thread to the
target window's DPI awareness; `application` uses its process's default context.
The default is `per-monitor`. For comparison with
the native helper's redraw path, use `--print-window --capture-dpi-context window`.

## Output and limits

The SVG contains editable text and shapes with a semantic description of the
captured controls. Browsers and screen readers vary in how they expose SVG
semantics; the optional HTML output provides a text outline as well. Static
captures do not preserve interaction or live announcements.

Complex layouts, small text and custom artwork may be simplified or missed.
Unrecognized image details may remain as PNG regions. Use `--no-raster` to omit
them. `--allow-raster` enables them when disabled by a configuration file.
Inspect the SVG at its intended size before relying on it.

Both icon tracers use foreground-only median-cut palettes, automatically trying
4, 8 and 12 colours (one for monochrome icons). Use `--icon-palette-size N` to
set a fixed palette size for either tracer.
Colours are drawn in overlapping layers to avoid seams between neighbouring
fills while preserving transparent holes.

For experimental icon smoothing, use `--smooth-icons`. It blurs by 0.5 source
pixels, enlarges 4× with bicubic interpolation, quantises without dithering,
then fits straight segments and Bézier curves. Tune blur with `--icon-blur RADIUS`.
Icons that lose too much detail retain the usual bitmap fallback.

Traced icons are saved in a local cache so repeated captures can reuse their
artwork. The cache holds each observed icon crop as a PNG and editable,
standalone SVGs for each tracing algorithm. Edit an SVG in the cache and the
next conversion will use your changes. `--icon-cache-dir DIR` selects its
location; `--no-icon-cache` disables it. The default is the system user cache
directory (`svgshot/traces`), or `SVGSHOT_ICON_CACHE` if set. Source crops may
contain visible screen content and accessible names, so inspect the cache
before sharing it. A matching HTML review page beside each SVG compares all
matched bitmap crops at 16× nearest-neighbour scale with every algorithm's SVG.
It also shows the foreground target, palette image, visible islands, ordered
contour masks, and SVG beside the accessible names, offsets, and quality metrics.
The images for older traces are restored when their regenerated SVG matches the
original; traces whose original settings cannot be recovered show that the
intermediate images are unavailable.

For direct use in Python, `svgshot.tracing.trace(image, background,
cache_dir=...)` accepts a cropped Pillow image and RGB colour (`"#ffffff"` or
an RGB triple). The result contains `.svg` (the standalone file contents),
`.svg_path`, `.review_path`, `.node` (artwork ready for a scene), `.group_id`,
`.source_hash`, `.offset`, and `.match`. The optional `name` argument records an
accessible label for the source bitmap. `algorithm="smooth-palette"` selects the alternate
tracer. To trace one crop from a shell, run
`svgshot-trace icon.png '#ffffff' --cache-dir ./icon-cache`; it prints the SVG
file to edit. Each canonical group has its observed PNGs in `bitmaps/` and
separate SVGs in `algorithms/`.

## Capture data

Capture metadata is a version 3, compressed JSON record embedded in the PNG's
`seMA` chunk.
The [schema reference](docs/capture-schema.md) describes its structure and the
[JSON Schema](docs/capture.schema.json) defines its fields. Extract metadata with:

```sh
python -m svgshot.snapshot capture.png --json capture.json
```

## Capture platforms

| Platform | Requirements |
|---|---|
| Windows | Windows 10 1903 or later |
| macOS | macOS 14 or later; Accessibility and Screen Recording permissions |
| Linux | AT-SPI2, X11, libpng, json-c, zlib and libstdc++ system libraries |

To build a capture helper locally, see the platform CMake/Swift sources under
`capture/`. Linux-specific setup and Wayland behavior are described in the
[Linux capture notes](docs/linux-capture.md).
