"""HTML parsing.

The main risk here is ingesting navigation, cookie banners, and footers as
content. They appear on every page of a site, so they become many near-identical
chunks that crowd real answers out of the top-k — the same failure as repeated
PDF headers, arriving by a different route.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from app.knowledge.parsers.base import Block, BlockType, ParsedDocument, ParseError

# Removed entirely: these never contain the answer to a question.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "form")

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class HtmlParser:
    @property
    def suffixes(self) -> tuple[str, ...]:
        return (".html", ".htm")

    def parse(self, path: Path) -> ParsedDocument:
        try:
            markup = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ParseError(f"could not read HTML {path.name}") from exc

        soup = BeautifulSoup(markup, "lxml")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()

        # Prefer the semantic content root when the document offers one.
        root = soup.find("main") or soup.find("article") or soup.body or soup
        if not isinstance(root, Tag):
            raise ParseError(f"{path.name} has no usable content")

        blocks: list[Block] = []
        heading: str | None = None

        for element in root.find_all([*_HEADINGS, "p", "table", "ul", "ol", "pre"]):
            if not isinstance(element, Tag):
                continue
            # Skip nested elements already consumed by an ancestor block.
            if element.find_parent(["table", "ul", "ol"]) is not None:
                continue

            name = element.name
            if name in _HEADINGS:
                text = element.get_text(" ", strip=True)
                if text:
                    heading = text
                    blocks.append(
                        Block(
                            type=BlockType.HEADING,
                            text=text,
                            level=_HEADINGS[name],
                            heading=heading,
                        )
                    )
            elif name == "table":
                text = _table_text(element)
                if text:
                    blocks.append(Block(type=BlockType.TABLE, text=text, heading=heading))
            elif name in ("ul", "ol"):
                items = [
                    li.get_text(" ", strip=True)
                    for li in element.find_all("li", recursive=False)
                    if isinstance(li, Tag)
                ]
                text = "\n".join(f"- {item}" for item in items if item)
                if text:
                    blocks.append(Block(type=BlockType.LIST, text=text, heading=heading))
            else:
                text = element.get_text(" ", strip=True)
                if text:
                    blocks.append(Block(type=BlockType.PARAGRAPH, text=text, heading=heading))

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if isinstance(title_tag, Tag) else None
        return ParsedDocument(blocks=blocks, title=title or path.stem)


def _table_text(table: Tag) -> str:
    """Pipe-delimited rows, matching the DOCX rendering so chunks look alike."""
    rows = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"])
            if isinstance(cell, Tag)
        ]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)
