# Linux capture

The Linux capture helper is a native executable. It reads accessibility data from
AT-SPI2 and captures pixels from X11. Python is not required by the helper.

On Debian, install the runtime libraries with:

```sh
sudo apt install libatspi2.0-0 libx11-6 libpng16-16 libjson-c5 zlib1g
```

On Ubuntu 24.04, the AT-SPI2 and PNG packages are named
`libatspi2.0-0t64` and `libpng16-16t64`. Package names can vary by distribution.
The [latest capture release](https://github.com/sh1bot/svgshot/releases/tag/capture-latest)
provides single-file executables for x86, x86-64, ARMv7, ARM64 and RISC-V64. Make
the downloaded file executable with `chmod +x FILE`; `FILE --version` reports its
source commit.

List available windows and capture one by its current ID:

```sh
./svgshot-capture-linux --list-windows
./svgshot-capture-linux --out window.png --window 0:0
```

You can use `--foreground` to select the active window. Without a target option,
the helper offers a text-based window chooser when run in a terminal. It reads
only the selected window's accessibility tree and does not activate controls.

On X11, the image represents the window's screen area; other windows can cover it.
Move overlapping windows away before capture. The target must fit on screen.
Password content is redacted, and offscreen content is excluded by default.

Automatic window association is not available on Wayland. To avoid pairing one
window's accessibility data with another window's pixels, supply the image and
its bounds explicitly:

```sh
./svgshot-capture-linux --out window.png --window 0:0 \
  --bitmap selected-window.png --window-bounds 0 0 800 600
```

Bounds are in AT-SPI window coordinates on Wayland and screen coordinates on X11.
The bitmap must show the selected window and match the bounds provided.

To build from source, install `cmake`, `g++`, `pkg-config`, `libatspi2.0-dev`,
`libx11-dev`, `libpng-dev`, `libjson-c-dev` and `zlib1g-dev`, then run:

```sh
cmake -S capture/linux -B build/capture
cmake --build build/capture -j2
```
