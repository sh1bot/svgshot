# Linux capture

Install distribution packages providing Python 3.10+, PyGObject, AT-SPI2 and Pillow.
On Debian/Ubuntu:

```sh
sudo apt install python3-gi gir1.2-atspi-2.0 python3-pil at-spi2-core
```

Use the distribution Python so it can import distribution-installed GI bindings.
Enable desktop accessibility if providers have not initialized their AT-SPI trees.
The capture API itself is toolkit-independent; GTK/Qt coverage depends on apps.

```sh
/usr/bin/python3 -m svgshot.grab --list-windows
/usr/bin/python3 -m svgshot.grab window.png --window 0:0
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
/usr/bin/python3 -m svgshot.grab window.png --window 0:0 \
  --bitmap selected-window.png --window-bounds 0 0 800 600
```

Bounds describe the bitmap in AT-SPI WINDOW coordinates on Wayland (SCREEN
coordinates on X11), including any decorations. The tool maps those coordinates
to image pixels, permitting a Retina-like scale. Do not include shadows/margins
unless the supplied bounds describe them. This mode records a warning that the
user supplied the association. The tool never guesses or silently attaches another
window's semantics. Store the original supplied image separately for corpus QA.

The binary distribution consists of an architecture-specific ELF launcher and
`svgshot-grab.pyz`. Keep them in the same directory. The launcher uses
`/usr/bin/python3` by default; `SVGSHOT_PYTHON` can specify another absolute path.
Dependencies are not bundled. Five glibc builds are provided: x86, x86-64,
ARMv7 hard-float, ARM64 and RISC-V64. They are built on Ubuntu 24.04/cross toolchains;
older glibc or musl systems should build the launcher locally or run the zipapp:

```sh
python3 svgshot-grab.pyz --help
cc -O2 capture/linux/launcher.c -o svgshot-capture-linux
```

These packages are capture tools. The SVG converter additionally needs the
svgshot Python package, numpy/scipy and optional Tesseract/Inkscape, as before.
