"""Code-aware text segmenter: splits text into PROSE / CODE / COMMENT spans.

Entry point: ``split_segments(text) -> list[Segment]``.

Cheap approach: detect fenced triple-backtick blocks via regex, then classify
``//``, ``#``, and ``/* */`` spans inside each block as COMMENT; everything
else in the block is CODE; text outside fences is PROSE. The fence delimiter
lines themselves (opening ```` ```<lang> ```` + closing ```` ``` ````) are emitted
as PROSE so the ``<lang>`` position — which matches arbitrary ``[^\n`]*`` and can
smuggle PII — is scanned by the local detectors (F5). Offsets are absolute into
the original text; ``split_segments`` tiles ``[0, len(text))`` with no gap.

Seam to tree-sitter: if embedded string literals containing comment markers
or malformed fences cause false classifications, replace ``_split_code_block``
with a tree-sitter tokenization pass while keeping this module's interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SegmentKind(StrEnum):
    PROSE = "PROSE"
    CODE = "CODE"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class Segment:
    text: str
    kind: SegmentKind
    start: int
    end: int


# Matches a fenced code block: ```lang\n ... ```.  Group 1 = code content.
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)

# Block comments (/* ... */) and line comments (// or #).
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?://|#)[^\n]*")


def _split_code_block(code: str, abs_offset: int) -> list[Segment]:
    """Return CODE / COMMENT segments for the content of a fenced block."""
    block_spans = [(m.start(), m.end()) for m in _BLOCK_COMMENT_RE.finditer(code)]

    line_spans: list[tuple[int, int]] = []
    for m in _LINE_COMMENT_RE.finditer(code):
        pos = m.start()
        if not any(s <= pos < e for s, e in block_spans):
            line_spans.append((m.start(), m.end()))

    all_comments = sorted(block_spans + line_spans)

    segments: list[Segment] = []
    pos = 0
    for s, e in all_comments:
        # Clip a comment that overlaps an earlier one (e.g. a block comment
        # nested in a line comment, `// x /* y */`) so spans never overlap —
        # the no-gap-no-overlap tiling is what makes the F5 coverage check sound.
        s = max(s, pos)
        e = max(e, pos)
        if e <= pos:
            continue
        if s > pos:
            seg_text = code[pos:s]
            segments.append(Segment(seg_text, SegmentKind.CODE, abs_offset + pos, abs_offset + s))
        segments.append(Segment(code[s:e], SegmentKind.COMMENT, abs_offset + s, abs_offset + e))
        pos = e

    if pos < len(code):
        seg_text = code[pos:]
        segments.append(
            Segment(seg_text, SegmentKind.CODE, abs_offset + pos, abs_offset + len(code))
        )

    return segments


def split_segments(text: str) -> list[Segment]:
    """Split text into PROSE, CODE, and COMMENT segments.

    Fenced ```lang ... ``` blocks are broken into CODE / COMMENT sub-segments;
    the fence delimiter lines and everything outside fences become PROSE.  All
    Segment.start / .end values are absolute into *text*, and the returned spans
    tile ``[0, len(text))`` with no gap: ``text[seg.start:seg.end] == seg.text``
    always, and every byte of *text* belongs to exactly one segment.
    """
    segments: list[Segment] = []
    pos = 0

    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            segments.append(Segment(text[pos : m.start()], SegmentKind.PROSE, pos, m.start()))

        # Opening delimiter + language tag (```<lang>\n): PROSE, so the <lang>
        # position (arbitrary [^\n`]*) is scanned by the full detector set (F5).
        code_start = m.start(1)
        if code_start > m.start():
            segments.append(
                Segment(text[m.start() : code_start], SegmentKind.PROSE, m.start(), code_start)
            )

        code_end = m.end(1)
        segments.extend(_split_code_block(m.group(1), code_start))

        # Closing delimiter (```): PROSE, keeps the tiling gap-free.
        if m.end() > code_end:
            segments.append(Segment(text[code_end : m.end()], SegmentKind.PROSE, code_end, m.end()))

        pos = m.end()

    if pos < len(text):
        segments.append(Segment(text[pos:], SegmentKind.PROSE, pos, len(text)))

    return segments
