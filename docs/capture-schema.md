# Unified semantic capture schema, version 3

A capture is a static observation of a selected window and its pixels. It is not
an executable interface. This schema uses platform-neutral concepts and preserves
provider-specific records separately; no platform API is the canonical model.

The normative structural definition is [capture.schema.json](capture.schema.json).
Additional invariants below apply even where JSON Schema cannot express them.

## Container and versions

A PNG carries one `suIA` private ancillary, unsafe-to-copy chunk, immediately before
IEND. Its existing 16-byte header is unchanged: `SVGSHOT\0` (8 bytes), container
version 1, encoding 1 (UTF-8 JSON), compression 1 (zlib-wrapped DEFLATE), reserved
zero, and a big-endian uint32 uncompressed length. JSON schema version and container
version are independent. Limits: 64 MiB uncompressed JSON, 16 MiB chunk payload.
The image's IDAT streams are independent of the snapshot stream. Unsafe-to-copy
means image editors should discard this chunk when changing pixels.

Version 3 identifies itself with `format: "svgshot.capture"`. Versions 1 and 2 are
legacy Windows UIA snapshots; importers normalize them, retaining their original
privacy-filtered payload under `native.snapshot`. Existing PNGs and JSON sidecars
remain readable. Capturing never retroactively changes a stored corpus entry.

## Top-level fields

| Field | Meaning |
|---|---|
| `format`, `version` | Format identifier and integer schema version |
| `source` | `platform` and `provider`; providers currently `windows-uia`, `macos-ax`, `linux-atspi` |
| `image` | Pixel dimensions, canonical coordinate space, and optional source-coordinate mapping |
| `root` | Selected window's semantic tree |
| `native` | Provider-tagged, privacy-filtered native metadata |
| `capture_policy` | Visibility/privacy choices and implemented collection limits |
| `warnings` | Human-readable limitations and partial-capture notices |

All common `bounds` and text `rectangles` are `[x, y, width, height]` in **PNG image
pixels**, top-left origin, x rightward, y downward. Fractional coordinates are
allowed. Zero-area rectangles represent unavailable geometry only when the
corresponding field status explains why. Bounds may extend beyond the image;
renderers clip them. They do not imply every enclosed pixel is unobscured.

`image.source_bounds` records the rectangle represented by the bitmap in provider
coordinates. Optional `source_to_image: [a,b,c,d,e,f]` maps `x,y` to
`a*x+c*y+e, b*x+d*y+f`. Native coordinates retain their provider's meaning; common
coordinates never require OS-specific interpretation. Width/height must be
positive integers, and must exactly match PNG IHDR.

## Nodes

Each node has a capture-local unique string `id`, named string `role`, `bounds`,
`states`, `relationships`, `children`, `text`, and `value`. Optional descriptive
fields are `label`, `description`, `help`, `role_description`, and `shortcuts`.
These have different meanings and must not be combined during capture.

Role vocabulary includes button, checkbox, radiobutton, combobox, edit, text,
image, hyperlink, list/listitem, tree/treeitem, table/datagrid/dataitem,
header/headeritem, menu/menubar/menuitem, tab/tabitem, window, pane, group,
document, toolbar, statusbar, scrollbar, slider, spinner, progressbar, separator,
and custom. Extensions may add named roles. Unknown roles must retain their native
role and degrade to a descriptive group, never be silently discarded.

States are optional facts: `enabled`, `focused`, `focusable`, `selected`,
`protected`, `offscreen`, `read_only`, and `editable` booleans; `checked` is
`unchecked`, `checked`, or `mixed`; `expansion` is `collapsed`, `expanded`,
`partial`, or `leaf`. Missing facts mean unknown/not collected, not false.

`relationships` maps named relationships (e.g. `labelled_by`, `described_by`,
`controls`, `flows_to`, `member_of`) to arrays of capture-local IDs. All common
references must resolve inside the capture. External references remain native
metadata; collectors must not fetch external names/contents to complete a link.
`children` gives structural order, which may differ from reading order. Additional
reading-order relationships can be retained without changing this structure.

`hints` contains optional provider-derived visual hints, currently `toolkit`,
`class`, and `text_layout`. They are advisory: absence must not change semantic
meaning. A hint supplied by only one platform can still be useful. Native HWNDs,
process IDs, automation IDs and similar diagnostics are not common fields and
are omitted by default where they might reveal unintended data.

## Observations and missing data

`value` is an observation: `{"status":"value","value":...}` or a non-value status.
Text has `status` plus `lines`. Supported statuses are `value`, `not_supported`,
`not_captured`, `error`, `redacted`, `truncated`, `mixed`, and `unresolved`.
Native records may additionally retain provider-specific statuses and error codes.

Optional `field_status` maps paths such as `states.enabled` or `bounds` to
observation records. A failed getter must not turn into false, empty or zero.
Zero-area geometry must have a non-value `field_status.bounds` entry. A genuine
empty label or empty list is distinct from an unsupported attribute.

`truncated` means some usable content exists but collection stopped; preserve the
partial value/lines and explain the limit. `mixed` is a valid aggregate result,
not an error; finer text runs may still supply uniform values. `redacted` is a
privacy decision, not provider absence.

## Text

`text.lines` contains records with `content`, `rectangles`, `style`, and optional
`runs`. A line is a captured layout segment, not necessarily a linguistic line.
Multiple rectangles are permitted. `content` preserves whitespace and Unicode.
Nested formatting `runs` retain contiguous substrings and optional geometry.
Providers may split equal styles into several runs; consumers can coalesce them.

Style fields include `foreground` and `background` as `#rrggbb` in sRGB,
`font_family`, `font_size` with an explicit `font_size_unit` (`pt` or `px`), `font_weight` (numeric), `italic`,
`underline`, `strikethrough`, `language`, `read_only`, and `hidden`. Unsupported
styles are absent, never invented by OCR. Alpha, colour-space source details,
and provider-specific decorations remain native metadata until normalized.

Optional `text.selections` is an observation whose value is an array of text
records. Only visible intersections may be included by default. Providers without
reliable selection-range geometry must report a non-value status. No capture
backend changes focus, selection, scroll position or values to obtain content.

## Native preservation and privacy

Windows retains the existing broad v2 snapshot under `native.snapshot`, including
typed UIA properties, patterns, text attributes, error statuses and capture limits.
macOS and Linux currently retain node records under `native.nodes`, keyed by the
same node IDs. `native_ref` identifies a node's native record. Native records are
not unrestricted dumps: password content is never captured, and default policies
exclude offscreen content and full editable values. Descriptive names/help can
still include text not painted in the bitmap; this limitation remains explicit.

The macOS/Linux backends initially collect a smaller descriptive subset than
Windows. Omitted information must be marked not captured where relevant. Native
metadata permits future normalization without depending on the renderer's current
needs, but does not claim to capture every attribute or reproduce every screen
reader behaviour. Dynamic announcements and interactive behaviour are out of scope.

## Extensibility

Consumers ignore unrecognized optional fields, retain native metadata on a
lossless import/export, and reject unsupported schema versions. New optional
fields need not be implemented by every provider. A required-field or meaning
change increments the schema version. Renderers consume common semantics and may
use optional hints; reading native provider details is a deliberate extension.

## Capture and conversion flows

Store captures independently of rendering:

```sh
python -m svgshot.grab corpus/window.png
python -m svgshot.convert corpus/window.png window.svg --html window.html
python -m svgshot.snapshot corpus/window.png --json window.capture.json
```

Capture and convert directly, without an intermediate PNG file:

```sh
python -m svgshot.convert --capture window.svg --html window.html
```

Installed commands are `svgshot-grab` and `svgshot-convert`. Direct conversion
calls the same capture backend, using an in-memory PNG stream, so semantics and
privacy defaults match the stored-capture flow. It does not add a corpus entry.
Native Windows/macOS helpers also support PNG stdout and standalone file output.
The old `python -m svgshot.capture output.svg ...` command remains compatible and
keeps its original capture-and-save behaviour.

Build and distribution instructions are in the repository README and
[linux-capture.md](linux-capture.md). Rolling `capture-latest` release assets are
built from main, include SOURCE.txt, and update without requiring a version tag.
