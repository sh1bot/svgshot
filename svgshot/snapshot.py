"""Versioned, compressed semantic snapshot carried by a private PNG ancillary chunk.

seMA is unsafe to copy when image data changes: coordinates belong to these pixels.
The payload is a 16-byte big-endian header, then a separate zlib JSON stream.
"""
from __future__ import annotations

import argparse
import io
from contextlib import nullcontext
import json
import struct
import zlib
from pathlib import Path
from .schema import validate

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHUNK = b"seMA"
HEADER = struct.Struct(">8sBBBBI")
MAGIC = b"SVGSHOT\0"
MAX_JSON = 64 * 1024 * 1024
MAX_CHUNK = 16 * 1024 * 1024


def encode_snapshot(snapshot):
    validate(snapshot)
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_JSON:
        raise ValueError("Semantic snapshot exceeds 64 MiB")
    data = HEADER.pack(MAGIC, 1, 1, 1, 0, len(raw)) + zlib.compress(raw, 9)
    if len(data) > MAX_CHUNK:
        raise ValueError("Compressed semantic snapshot exceeds 16 MiB")
    return data


def decode_snapshot(data):
    if len(data) < HEADER.size or len(data) > MAX_CHUNK:
        raise ValueError("Invalid seMA chunk length")
    magic, version, encoding, compression, reserved, length = HEADER.unpack_from(data)
    if (magic, version, encoding, compression, reserved) != (MAGIC, 1, 1, 1, 0):
        raise ValueError("Unsupported seMA chunk format")
    if length > MAX_JSON:
        raise ValueError("Semantic snapshot exceeds 64 MiB")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(data[HEADER.size:], length+1)
        if len(raw) != length or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError("Invalid seMA decompressed length or stream")
        snapshot = json.loads(raw.decode("utf-8"))
    except (zlib.error, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid compressed UIA JSON") from error
    if not isinstance(snapshot, dict):
        raise ValueError("Semantic snapshot must be a JSON object")
    validate(snapshot)
    return snapshot


def png_chunks(path):
    """Scan framing without loading image chunks into memory."""
    with (nullcontext(path) if hasattr(path, "read") else Path(path).open("rb")) as stream:
        size = stream.seek(0, 2)
        stream.seek(0)
        if stream.read(8) != SIGNATURE:
            raise ValueError("Capture is not a PNG")
        while True:
            head = stream.read(8)
            if len(head) != 8:
                raise ValueError("Truncated PNG")
            length, name = struct.unpack(">I4s", head)
            if stream.tell()+length+4 > size:
                raise ValueError("Truncated PNG chunk")
            if name == CHUNK:
                if length > MAX_CHUNK:
                    raise ValueError("Compressed semantic snapshot exceeds 16 MiB")
                data = stream.read(length)
                crc = struct.unpack(">I", stream.read(4))[0]
                if zlib.crc32(name+data) & 0xffffffff != crc:
                    raise ValueError("Invalid seMA CRC")
                yield data
            else:
                stream.seek(length+4, 1)
            if name == b"IEND":
                if length != 0 or stream.tell() != size:
                    raise ValueError("Invalid PNG end")
                return


def read_snapshot(path):
    data = None
    for chunk in png_chunks(path):
        if data is not None:
            raise ValueError("PNG has multiple seMA snapshots")
        data = chunk
    if data is None:
        raise ValueError("PNG has no embedded seMA semantic snapshot")
    return decode_snapshot(data)


def embed_snapshot(png, snapshot):
    """Return a PNG with one authoritative snapshot, without decoding its pixels."""
    list(png_chunks(io.BytesIO(png)))  # Validate framing and existing metadata CRC.
    payload = encode_snapshot(snapshot)
    encoded = struct.pack('>I', len(payload)) + CHUNK + payload
    encoded += struct.pack('>I', zlib.crc32(CHUNK+payload) & 0xffffffff)
    out = bytearray(SIGNATURE)
    offset = 8
    while offset < len(png):
        length, name = struct.unpack('>I4s', png[offset:offset+8])
        end = offset + length + 12
        if name == b'IEND':
            out.extend(encoded)
        if name != CHUNK:
            out.extend(png[offset:end])
        offset = end
    return bytes(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract a PNG's embedded semantic snapshot as JSON")
    parser.add_argument("image", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.image.resolve() == args.json.resolve():
            raise ValueError("JSON output must not overwrite the PNG")
        snapshot = read_snapshot(args.image)
        args.json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f"svgshot snapshot: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
