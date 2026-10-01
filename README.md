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

The Windows executable also works directly: run it to pick a window and save a
PNG on the Desktop, named with the local date, time and window title. To choose a
filename, run `svgshot-capture-windows-x86_64.exe capture.png` (using your downloaded
executable's name).

Capture requires the platform accessibility service. macOS also requires
Accessibility and Screen Recording permission. Linux captures pixels directly
on X11; Wayland requires an explicitly supplied window image. See
[Linux capture notes](docs/linux-capture.md).

By default, password content is excluded. Captures can include descriptive names
that are not visible in the screenshot, so review captures before sharing them.

## Output and limits

The SVG contains editable text and shapes with a semantic description of the
captured controls. Browsers and screen readers vary in how they expose SVG
semantics; the optional HTML output provides a text outline as well. Static
captures do not preserve interaction or live announcements.

Complex layouts, small text and custom artwork may be simplified or missed.
Unrecognized image details may remain as PNG regions. Use `--no-raster` to omit
them, or `--allow-raster` to retain additional image detail in semantic output.
Inspect the SVG at its intended size before relying on it.

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
