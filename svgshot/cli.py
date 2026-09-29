from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from .recognize import Options, reconstruct
from .svg import to_svg
from .validate import compare


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconstruct a GUI screenshot as editable SVG")
    parser.add_argument("input", type=Path, help="Input PNG screenshot")
    parser.add_argument("output", type=Path, help="Output SVG")
    parser.add_argument("--config", type=Path, help="JSON recognition options")
    parser.add_argument("--scene", type=Path, help="Save recognized scene graph as JSON")
    parser.add_argument("--report", type=Path, help="Render and compare SVG; write JSON report (needs Inkscape)")
    parser.add_argument("--manifest", type=Path, help="Ground-truth JSON fixture for --report")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero if the report has warnings")
    parser.add_argument("--no-ocr", action="store_true", help="Disable text detection explicitly")
    parser.add_argument("--no-raster", action="store_true", help="Discard unrecognized small details")
    parser.add_argument("--lang", help="Tesseract language (default: eng)")
    args = parser.parse_args(argv)
    try:
        if args.input.suffix.lower() != ".png":
            raise ValueError("Input must be a PNG")
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Input and output paths must differ")
        if args.manifest and not args.report:
            parser.error("--manifest requires --report")
        if args.strict and not args.report:
            parser.error("--strict requires --report")
        with Image.open(args.input) as source:
            source.load()
            options = Options.from_file(str(args.config) if args.config else None)
            if args.no_ocr:
                options.ocr = False
            if args.no_raster:
                options.raster_fallback = False
            if args.lang:
                options.language = args.lang
            scene = reconstruct(source, options)
            args.output.write_text(to_svg(scene), encoding="utf-8")
            if args.scene:
                args.scene.write_text(json.dumps(scene.to_dict(), indent=2)+"\n", encoding="utf-8")
            if args.report:
                report = compare(source, str(args.output), scene,
                                 str(args.manifest) if args.manifest else None,
                                 options.ocr, options.language)
                args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
                print(json.dumps(report, indent=2))
                if args.strict and report["warnings"]:
                    return 2
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"svgshot: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
