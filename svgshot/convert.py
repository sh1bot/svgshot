"""Convert a stored semantic PNG, or capture and convert directly in memory."""
import argparse
import io
import json
from pathlib import Path
import sys
import subprocess
from .grab import add_options, capture_bytes, options
from .snapshot import read_snapshot
from .schema import from_uia


def main(argv=None):
    parser = argparse.ArgumentParser(description='Convert a semantic PNG to SVG, optionally capturing first')
    parser.add_argument('paths', type=Path, nargs='+', metavar='PATH')
    parser.add_argument('--capture', action='store_true', help='Capture directly; provide only output.svg')
    parser.add_argument('--html', type=Path)
    parser.add_argument('--scene', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--no-ocr', action='store_true')
    parser.add_argument('--allow-raster', action='store_true')
    parser.add_argument('--uia', type=Path, help='Legacy UIA JSON sidecar')
    add_options(parser)
    args = parser.parse_args(argv)
    try:
        if len(args.paths) != (1 if args.capture else 2):
            raise ValueError('Use input.png output.svg, or --capture output.svg')
        output = args.paths[-1]
        if output.suffix.lower() != '.svg':
            raise ValueError('Output must have an .svg extension')
        outputs = [p for p in (output, args.html, args.scene) if p]
        if len({p.resolve() for p in outputs}) != len(outputs):
            raise ValueError('Output paths must differ')
        if args.capture:
            if args.uia:
                raise ValueError('--uia is only for stored legacy captures')
            data = capture_bytes(**options(args))
            snapshot = read_snapshot(io.BytesIO(data))
        else:
            if any((args.helper,args.hwnd,args.window,args.foreground,args.delay,args.include_hidden_content,args.bitmap,args.window_bounds)):
                raise ValueError('Live capture options require --capture')
            inputs = [args.paths[0]] + ([args.uia] if args.uia else [])
            if any(p.resolve() in {i.resolve() for i in inputs} for p in outputs):
                raise ValueError('Outputs must not overwrite capture inputs')
            data = args.paths[0].read_bytes()
            snapshot = json.loads(args.uia.read_text(encoding='utf-8')) if args.uia else read_snapshot(io.BytesIO(data))
        if snapshot.get('version') in (1,2):
            snapshot = from_uia(snapshot)
        from PIL import Image
        from .capture import merge_uia, semantic_svg, accessible_html, validate_snapshot
        from .recognize import Options, reconstruct
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            validate_snapshot(snapshot, image)
            config = Options.from_file(str(args.config) if args.config else None)
            config.raster_fallback = args.allow_raster
            config.fidelity_fallback = False
            if args.no_ocr:
                config.ocr = False
            scene = merge_uia(reconstruct(image, config), image, snapshot)
            svg = semantic_svg(scene, snapshot, config.font_family)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(svg, encoding='utf-8')
        if args.html:
            args.html.write_text(accessible_html(svg,snapshot), encoding='utf-8')
        if args.scene:
            args.scene.write_text(json.dumps(scene.to_dict(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for warning in snapshot.get('warnings',[]):
            print('svgshot convert: '+warning,file=sys.stderr)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'svgshot convert: {error}',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
