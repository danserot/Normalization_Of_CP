"""Source-preserving document structure shared by parsers and business extraction.

The flat ``cells`` view remains the API/training contract. Hierarchical objects
reference those same cells; no second copy of the document is manufactured.
Coordinates use PDF points, with the origin at the upper-left corner.
"""
from dataclasses import asdict, dataclass, field
import os
import time


MAX_CHARS = int(os.getenv('MAX_CHARS', '200000'))
MAX_CELLS = int(os.getenv('MAX_CELLS', '30000'))
DEADLINE = min(1800, max(10, int(os.getenv('EXTRACTION_TIMEOUT_SECONDS', '300'))))


class DocumentError(ValueError):
    pass


@dataclass
class CanonicalCell:
    id: str
    file: str
    text: str
    block: str
    row: int
    cell: int
    page: int | None = None
    sheet: str | None = None
    kind: str = 'text'
    method: str = 'native'
    bbox: list[float] | None = None
    source_method: str = 'native'
    rowspan: int = 1
    colspan: int = 1
    merged_into: str | None = None
    coordinate: str | None = None
    reading_order: int = 0
    vision_agreement: bool | None = None

    @property
    def column(self):
        return self.cell


@dataclass
class CanonicalRow:
    index: int
    cells: list[CanonicalCell] = field(default_factory=list)


@dataclass
class CanonicalTable:
    id: str
    page: int | None = None
    sheet: str | None = None
    bbox: list[float] | None = None
    rows: list[CanonicalRow] = field(default_factory=list)
    header_rows: list[int] = field(default_factory=list)
    reading_order: int = 0


@dataclass
class CanonicalBlock:
    id: str
    type: str
    page: int | None = None
    sheet: str | None = None
    bbox: list[float] | None = None
    cell_ids: list[str] = field(default_factory=list)
    reading_order: int = 0


@dataclass
class CanonicalPage:
    page: int
    width: float | None = None
    height: float | None = None
    block_ids: list[str] = field(default_factory=list)
    rotation: int = 0


@dataclass
class NativeWord:
    id: str
    text: str
    page: int
    bbox: list[float]
    block: int = 0
    line: int = 0
    order: int = 0


@dataclass
class CanonicalDocument:
    file: str
    cells: list[CanonicalCell] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    chars: int = 0
    pages: int = 0
    tables: int = 0
    page_objects: list[CanonicalPage] = field(default_factory=list)
    blocks: list[CanonicalBlock] = field(default_factory=list)
    table_objects: list[CanonicalTable] = field(default_factory=list)
    native_words: list[NativeWord] = field(default_factory=list)
    routing: dict = field(default_factory=lambda: {
        'format': '', 'mandatory': False, 'requested': False, 'used': False,
        'available': None, 'status': 'native', 'required_pages': [],
        'processed_pages': [], 'reasons': [], 'issues': [],
    })
    _block_index: dict = field(default_factory=dict, repr=False)
    _table_index: dict = field(default_factory=dict, repr=False)
    _row_index: dict = field(default_factory=dict, repr=False)
    _page_index: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.cells:
            self.reindex()

    def check(self):
        if time.monotonic() - self.started > DEADLINE:
            raise DocumentError(f'Превышено время обработки ({DEADLINE} секунд)')

    def ensure_page(self, number, width=None, height=None, rotation=None):
        if number not in self._page_index:
            page = CanonicalPage(number, width, height)
            self.page_objects.append(page)
            self._page_index[number] = page
        page = self._page_index[number]
        if width is not None:
            page.width, page.height = width, height
        if rotation is not None:
            page.rotation = rotation
        self.pages = max(self.pages, number)
        return page

    def _index_cell(self, item):
        block = self._block_index.get(item.block)
        if block is None:
            block = CanonicalBlock(item.block, item.kind, item.page, item.sheet,
                                   reading_order=item.reading_order)
            self.blocks.append(block)
            self._block_index[item.block] = block
        block.cell_ids.append(item.id)
        if item.bbox:
            block.bbox = union_boxes([block.bbox, item.bbox])
        if item.page:
            page = self.ensure_page(item.page)
            if item.block not in page.block_ids:
                page.block_ids.append(item.block)
        if item.kind == 'table':
            table = self._table_index.get(item.block)
            if table is None:
                table = CanonicalTable(item.block, item.page, item.sheet,
                                       reading_order=item.reading_order)
                self.table_objects.append(table)
                self._table_index[item.block] = table
                self.tables = len(self.table_objects)
            if item.bbox:
                table.bbox = union_boxes([table.bbox, item.bbox])
            row_key = (item.block, item.row)
            if row_key not in self._row_index:
                row = CanonicalRow(item.row)
                table.rows.append(row)
                self._row_index[row_key] = row
            self._row_index[row_key].cells.append(item)

    def add(self, text, block, row, cell=1, **location):
        self.check()
        text = str(text if text is not None else '').replace('\u00ad', '-').strip()
        if self.chars + len(text) > MAX_CHARS or len(self.cells) >= MAX_CELLS:
            raise DocumentError(f'Документ превышает лимит {MAX_CHARS} символов / {MAX_CELLS} ячеек')
        item = CanonicalCell(f'c{len(self.cells)}', self.file, text, block, row, cell, **location)
        self.chars += len(text)
        self.cells.append(item)
        self._index_cell(item)
        return item

    def reindex(self):
        dimensions = {p.page: (p.width, p.height, p.rotation) for p in self.page_objects}
        headers = {t.id: t.header_rows for t in self.table_objects}
        block_types = {b.id: b.type for b in self.blocks}
        self.blocks, self.table_objects, self.page_objects = [], [], []
        self._block_index, self._table_index, self._row_index, self._page_index = {}, {}, {}, {}
        self.chars, self.tables = sum(len(c.text) for c in self.cells), 0
        for item in self.cells:
            self._index_cell(item)
        for number, (width, height, rotation) in dimensions.items():
            self.ensure_page(number, width, height, rotation)
        for table in self.table_objects:
            table.header_rows = headers.get(table.id, [])
        for block in self.blocks:
            block.type = block_types.get(block.id, block.type)

    def text(self):
        return '\n'.join(c.text for c in self.cells if c.text)

    def public(self):
        return [asdict(c) for c in self.cells]

    def structure(self):
        """A small hierarchy using cell IDs, suitable for annotation and tracing."""
        return {
            'pages': [asdict(page) for page in self.page_objects],
            'blocks': [asdict(block) for block in self.blocks],
            'tables': [
                {'id': table.id, 'page': table.page, 'sheet': table.sheet,
                 'bbox': table.bbox, 'reading_order': table.reading_order,
                 'header_rows': table.header_rows,
                 'rows': [{'row': row.index, 'cell_ids': [c.id for c in row.cells]}
                          for row in table.rows]}
                for table in self.table_objects
            ],
        }


def union_boxes(boxes):
    valid = [b for b in boxes if b and len(b) == 4]
    if not valid:
        return None
    return [min(b[0] for b in valid), min(b[1] for b in valid),
            max(b[2] for b in valid), max(b[3] for b in valid)]


# Stable imports for existing annotation/training and rules consumers.
Source = CanonicalDocument
Cell = CanonicalCell
