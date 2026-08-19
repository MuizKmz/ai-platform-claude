"""Markdown and plain text.

No library: the format is simple enough that a parser is shorter than the code to
configure someone else's. It handles pipe tables, because Markdown tables are the
easiest place to verify the atomic-block behaviour that PDFs make hard to test.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.knowledge.parsers.base import Block, BlockType, ParsedDocument, ParseError

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


class MarkdownParser:
    """Markdown and .txt. Plain text simply yields paragraphs."""

    @property
    def suffixes(self) -> tuple[str, ...]:
        return (".md", ".markdown", ".txt")

    def parse(self, path: Path) -> ParsedDocument:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ParseError(f"could not read {path.name}") from exc

        blocks: list[Block] = []
        heading: str | None = None
        buffer: list[str] = []
        table: list[str] = []
        in_list = False

        def flush_paragraph() -> None:
            nonlocal buffer, in_list
            if buffer:
                text = "\n".join(buffer).strip()
                if text:
                    blocks.append(
                        Block(
                            type=BlockType.LIST if in_list else BlockType.PARAGRAPH,
                            text=text,
                            heading=heading,
                        )
                    )
            buffer = []
            in_list = False

        def flush_table() -> None:
            nonlocal table
            if table:
                # One block for the whole table, header row included.
                blocks.append(
                    Block(type=BlockType.TABLE, text="\n".join(table).strip(), heading=heading)
                )
            table = []

        for line in raw.splitlines():
            if _TABLE_ROW.match(line):
                flush_paragraph()
                table.append(line)
                continue
            flush_table()

            if match := _HEADING.match(line):
                flush_paragraph()
                heading = match.group(2).strip()
                blocks.append(
                    Block(
                        type=BlockType.HEADING,
                        text=heading,
                        level=len(match.group(1)),
                        heading=heading,
                    )
                )
                continue

            if not line.strip():
                flush_paragraph()
                continue

            if _LIST_ITEM.match(line):
                in_list = True
            buffer.append(line)

        flush_paragraph()
        flush_table()

        title = next((b.text for b in blocks if b.type is BlockType.HEADING), None)
        return ParsedDocument(blocks=blocks, title=title or path.stem)
