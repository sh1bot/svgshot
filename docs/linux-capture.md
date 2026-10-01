# Linux capture

The capture executable is native C++ and needs no Python, PyGObject or Pillow.
Install system AT-SPI2, X11, libpng, json-c and zlib libraries. On Debian/Ubuntu,
install the development packages to build locally (their runtime dependencies
are installed automatically):

```sh
sudo apt install cmake g++ pkg-config libatspi2.0-dev libx11-dev libpng-dev libjson-c-dev zlib1g-dev at-spi2-core
cmake -S capture/linux -B build/capture
cmake --build build/capture -j2
```

Enable desktop accessibility if providers have not initialized their AT-SPI trees.
The capture API itself is toolkit-independent; GTK/Qt coverage depends on apps.

```sh
build/capture/svgshot-capture-linux --list-windows
build/capture/svgshot-capture-linux --out window.png --window 0:0
```

Window IDs are current desktop enumeration positions; refresh the list when apps
or windows change. `--foreground` requires exactly one AT-SPI window marked active.
With no target and an interactive terminal, the tool offers a text window chooser.
It reads only the selected tree and does not activate its controls.

On X11, Pillow/XCB captures the window's screen rectangle. Occlusion is retained:
move overlapping windows away before capture. This is a screen observation, not
a hidden backing-store screenshot. Offscreen and protected content are redacted.

On Wayland, automatic window association is deliberately not implemented yet.
The portal's selected resource must be proven to match the selected AT-SPI tree;
matching dimensions alone is insufficient. Until a reliable compositor/portal
association is implemented, provide a bitmap explicitly representing that window:

```sh
build/capture/svgshot-capture-linux --out window.png --window 0:0 \
  --bitmap selected-window.png --window-bounds 0 0 800 600
```

Bounds describe the bitmap in AT-SPI WINDOW coordinates on Wayland (SCREEN
coordinates on X11), including any decorations. The tool maps those coordinates
to image pixels, permitting a Retina-like scale. Do not include shadows/margins
unless the supplied bounds describe them. This mode records a warning that the
user supplied the association. The tool never guesses or silently attaches another
window's semantics. Store the original supplied image separately for corpus QA.

Each binary download is a single ELF executable. No companion files or Python
runtime are needed. Mark it executable with `chmod +x FILE`; run `FILE --version`
to see its source commit. Five glibc builds are provided: x86, x86-64, ARMv7
hard-float, ARM64 and RISC-V64. x86/x64/ARM builds target Debian 12 (glibc 2.36+);
RISC-V64 targets Debian 13 (glibc 2.41+). System libraries remain dependencies;
older glibc or musl systems should build locally.

The Python frontend/converter invokes the same native helper. Use `--helper FILE`
or `SVGSHOT_CAPTURE_HELPER` to select a downloaded executable. The SVG converter
still needs the svgshot Python package, numpy/scipy and optional
Tesseract/Inkscape, as before. Python/GI/GTK/Pillow are used by the CI fixture,
not by the capture executable.
