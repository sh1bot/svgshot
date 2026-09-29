"""Deterministic Windows-style control fixture with independent ground truth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size):
    for path in ("C:/Windows/Fonts/segoeui.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def generate(png: Path, manifest: Path, theme: str = "light"):
    dark = theme == "dark"
    background = "#252525" if dark else "#f7f7f7"
    surface = "#414141" if dark else "#dedede"
    ink = "#f5f5f5" if dark else "#222222"
    border = "#d5d5d5" if dark else "#505050"
    button = "#3c5c80" if dark else "#d7e8fb"
    image = Image.new("RGB", (640, 400), background)
    draw = ImageDraw.Draw(image)
    elements = []

    def label(text, x, y, size=16):
        font = _font(size)
        draw.text((x, y), text, fill=ink, font=font)
        bounds = draw.textbbox((x,y), text, font=font)
        elements.append({"kind":"text", "text":text,
                         "box":[bounds[0],bounds[1],bounds[2]-bounds[0],bounds[3]-bounds[1]]})

    draw.rectangle((0, 0, 639, 49), fill=surface)
    label("Display settings", 22, 13, 18)
    label("Choose a theme", 32, 75)
    for y, title, selected in ((115, "Light", False), (160, "Dark", True)):
        draw.ellipse((33,y,51,y+18), outline=border, width=2)
        if selected:
            draw.ellipse((39,y+6,45,y+12), fill=border)
        elements.append({"kind":"radio-selected" if selected else "radio", "box":[33,y,19,19]})
        label(title, 64, y-2)
    draw.rectangle((33, 216, 51, 234), outline=border, width=2)
    elements.append({"kind":"checkbox", "box":[33,216,19,19]})
    label("Show advanced options", 64, 214)
    draw.rounded_rectangle((440, 324, 590, 363), radius=4, fill=button, outline=border, width=2)
    elements.append({"kind":"button", "box":[440,324,151,40]})
    label("Apply", 488, 334)
    image.save(png)
    manifest.write_text(json.dumps({"size":[640,400], "elements":elements}, indent=2)+"\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Create a known GUI screenshot and semantic ground truth")
    parser.add_argument("png", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    generate(args.png, args.manifest, args.theme)


if __name__ == "__main__":
    main()
