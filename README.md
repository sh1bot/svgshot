# svgshot

`svgshot` turns GUI screenshots into simplified, editable SVG. Text and common
controls become SVG elements; complex imagery may remain embedded in the SVG as
small PNG regions. The result is an approximation, so check it before publishing.

## Install

Use Python 3.10 or later for conversion. From a checkout:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

Screenshot conversion uses Tesseract OCR. Install Tesseract for your operating
system and make it available on `PATH`. Inkscape is optional; it is needed for
rendered comparison reports.

## Convert a screenshot

```sh
svgshot screenshot.png screenshot.svg
```

To save a recognition report, add `--report report.json` (requires Inkscape).
Use `--no-ocr` to omit reconstructed text. Conversion can also produce an
accessible HTML outline:

```sh
svgshot-convert screenshot.png screenshot.svg --html screenshot.html
```

## Capture and convert

A capture PNG stores the screenshot and its semantic accessibility data together.
Keep the PNG as a reusable source, then convert it when needed:

```sh
svgshot-grab corpus/window.png
svgshot-convert corpus/window.png window.svg --html window.html
```

Or capture and convert directly without saving an intermediate PNG:

```sh
svgshot-convert --capture window.svg --html window.html
```

The capture executable is native on Windows, macOS and Linux. Download one from
the [latest capture release](https://github.com/sh1bot/svgshot/releases/tag/capture-latest).
Each download is a single executable; `--version` prints its source commit. On
Linux and macOS, run `chmod +x FILE` after downloading. To use a downloaded helper
with the Python commands, pass `--helper FILE` or set `SVGSHOT_CAPTURE_HELPER`.

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
those details in the pixel-only `svgshot` command. The semantic converter keeps
extra raster detail off by default; `--allow-raster` opts in. Inspect the output
at its intended size before relying on it.

For a pixel-only SVG without captured accessibility data, `svgshot` also accepts
any PNG screenshot directly. The PNG is not modified.

## Capture data

Capture metadata is a version 3, compressed JSON record embedded in the PNG's
`seMA` chunk.
The [schema reference](docs/capture-schema.md) describes its structure and the
[JSON Schema](docs/capture.schema.json) defines its fields. Extract metadata with:

```sh
python -m svgshot.snapshot capture.png --json capture.json
```

## Supported capture platforms

| Platform | Architectures | Notes |
|---|---|---|
| Windows | x86, x64, ARM64 | Windows 10 1903 or later |
| macOS | x64, ARM64 | macOS 14 or later; accessibility and screen permissions |
| Linux | x86, x64, ARM32, ARM64, RISC-V64 | AT-SPI2, X11, libpng, json-c and zlib system libraries |

The Windows SDK capture build has no ARM32 target; current macOS releases have no
32-bit target. Linux x86/x64/ARM binaries target Debian 12 or later; RISC-V64
requires Debian 13 or later. Other Linux distributions may need a local build.

To build a capture helper locally, see the platform CMake/Swift sources under
`capture/`. Linux-specific setup and Wayland behavior are described in the
[Linux capture notes](docs/linux-capture.md).
