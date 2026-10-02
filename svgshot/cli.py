from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from .capture import accessible_html, merge_uia, semantic_svg, validate_snapshot
from .diagnostic import make_diagnostic
from .grab import add_options, capture_bytes, options as capture_options
from .recognize import Options, reconstruct
from .snapshot import MissingSnapshotError, read_snapshot
from .svg import to_svg
from .validate import add_fidelity_regions, compare


def _parser():
    parser = argparse.ArgumentParser(
        description="Convert a screenshot PNG to SVG, using embedded accessibility data when available"
    )
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH",
                        help="input.png output.svg, or just output.svg with --capture")
    parser.add_argument("--capture", action="store_true",
                        help="capture a window directly instead of reading an input PNG")
    parser.add_argument("--html", type=Path, help="Write an accessible HTML outline (semantic PNGs)")
    parser.add_argument("--allow-raster", action="store_true",
                        help="Keep extra raster details in semantic output")
    parser.add_argument("--config", type=Path, help="JSON recognition options")
    parser.add_argument("--scene", type=Path, help="Save the recognized scene graph as JSON")
    parser.add_argument("--diagnostic", type=Path,
                        help="Save side-by-side input/output SVG with OCR boxes and baselines")
    parser.add_argument("--report", type=Path,
                        help="Render and compare SVG; write JSON report (needs Inkscape)")
    parser.add_argument("--manifest", type=Path, help="Ground-truth JSON fixture for --report")
    parser.add_argument("--strict", action="store_true",
                        help="Exit nonzero if the report has warnings")
    parser.add_argument("--no-ocr", action="store_true", help="Disable text detection explicitly")
    parser.add_argument("--no-raster", action="store_true", help="Discard unrecognized small details")
    parser.add_argument("--source-overlays", "--fidelity", dest="source_overlays", action="store_true",
                        help="Opt in to source PNG overlays where the SVG differs visually")
    parser.add_argument("--no-fidelity", action="store_true",
                        help="Disable source PNG overlays (including config-enabled overlays)")
    parser.add_argument("--lang", help="Tesseract language (default: eng)")
    parser.add_argument("--font-family", help="SVG font family used for measured text fitting")
    add_options(parser)
    return parser


def _has_capture_options(args):
    return any((args.helper, args.hwnd, args.window, args.foreground, args.delay,
                args.include_hidden_content, args.debug_unredacted,
                args.accessibility_api != "auto", args.bitmap, args.window_bounds))


def _raw_convert(data, input_path, output, args):
    if args.html:
        raise ValueError("--html requires a PNG with embedded semantic data")
    options = Options.from_file(str(args.config) if args.config else None)
    if args.no_ocr:
        options.ocr = False
    if args.no_raster:
        options.raster_fallback = False
    if args.source_overlays:
        options.fidelity_fallback = True
    if args.no_fidelity:
        options.fidelity_fallback = False
    if args.lang:
        options.language = args.lang
    if args.font_family:
        options.font_family = args.font_family

    with Image.open(io.BytesIO(data)) as source:
        source.load()
        scene = reconstruct(source, options)
        svg = to_svg(scene, options.font_family)
        fidelity = None
        if options.raster_fallback and options.fidelity_fallback:
            fidelity = add_fidelity_regions(source, scene, svg, options.font_family)
            if fidelity and fidelity["regions"]:
                svg = to_svg(scene, options.font_family)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(svg, encoding="utf-8")
        if args.diagnostic:
            args.diagnostic.write_text(make_diagnostic(source, svg, scene, options.font_family),
                                       encoding="utf-8")
        if args.scene:
            args.scene.write_text(json.dumps(scene.to_dict(), indent=2) + "\n", encoding="utf-8")
        if args.report:
            report = compare(source, str(output), scene,
                             str(args.manifest) if args.manifest else None,
                             options.ocr, options.language)
            if fidelity is not None:
                report["before_fidelity"] = fidelity
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
            if args.strict and report["warnings"]:
                return 2
    return 0


def _semantic_convert(data, snapshot, output, args):
    if args.report or args.manifest or args.strict or args.diagnostic or args.source_overlays or args.no_fidelity:
        raise ValueError("Pixel-analysis options cannot be used with a semantic PNG")
    from .schema import validate
    validate(snapshot)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        validate_snapshot(snapshot, image)
        config = Options.from_file(str(args.config) if args.config else None)
        config.raster_fallback = args.allow_raster
        config.fidelity_fallback = False
        if args.no_raster:
            config.raster_fallback = False
        if args.no_ocr:
            config.ocr = False
        if args.font_family:
            config.font_family = args.font_family
        if args.lang:
            config.language = args.lang
        scene = merge_uia(reconstruct(image, config), image, snapshot, config.language)
        svg = semantic_svg(scene, snapshot, config.font_family)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(accessible_html(svg, snapshot), encoding="utf-8")
    if args.scene:
        args.scene.parent.mkdir(parents=True, exist_ok=True)
        args.scene.write_text(json.dumps(scene.to_dict(), ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    for warning in snapshot.get("warnings", []):
        print("svgshot: " + warning, file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if len(args.paths) != (1 if args.capture else 2):
            raise ValueError("Use input.png output.svg, or --capture output.svg")
        output = args.paths[-1]
        if output.suffix.lower() != ".svg":
            raise ValueError("Output must have an .svg extension")
        if args.capture:
            if args.paths[0].suffix.lower() != ".svg":
                raise ValueError("With --capture, provide only output.svg")
            data = capture_bytes(**capture_options(args))
            input_path = None
        else:
            if args.paths[0].suffix.lower() != ".png":
                raise ValueError("Input must be a PNG")
            if args.debug_unredacted:
                raise ValueError("--debug-unredacted requires --capture; redacted values cannot be recovered from an existing PNG")
            if args.accessibility_api != "auto":
                raise ValueError("--accessibility-api requires --capture")
            if _has_capture_options(args):
                raise ValueError("Live capture options require --capture")
            input_path = args.paths[0]
            data = input_path.read_bytes()
            if input_path.resolve() == output.resolve():
                raise ValueError("Input and output paths must differ")

        side_outputs = [p for p in (output, args.html, args.scene, args.diagnostic, args.report) if p]
        if len({p.resolve() for p in side_outputs}) != len(side_outputs):
            raise ValueError("Output paths must differ")
        if input_path and any(input_path.resolve() == p.resolve() for p in side_outputs):
            raise ValueError("Outputs must not overwrite the input PNG")
        if args.manifest and not args.report:
            raise ValueError("--manifest requires --report")
        if args.strict and not args.report:
            raise ValueError("--strict requires --report")

        try:
            snapshot = read_snapshot(io.BytesIO(data))
        except MissingSnapshotError:
            if args.capture:
                raise ValueError("Live capture returned a PNG without embedded semantic data")
            print("svgshot: PNG has no embedded semantic data; falling back to raw screenshot analysis.",
                  file=sys.stderr)
            return _raw_convert(data, input_path, output, args)
        return _semantic_convert(data, snapshot, output, args)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            subprocess.SubprocessError) as error:
        print(f"svgshot: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
