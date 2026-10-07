"""Merge visual structure with exact native text without making it business data."""
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
import re

from .canonical import CanonicalDocument, DocumentError, union_boxes, MAX_CELLS, MAX_CHARS


@dataclass
class VisualCell:
    text: str
    row: int
    column: int
    rowspan: int = 1
    colspan: int = 1
    header: bool = False


class _TableHTML(HTMLParser):
    """Decode only table structure; scripts/attributes never become executable."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
        if self.depth > 1:
            return
        if tag == 'tr':
            self.current_row = []
            self.rows.append(self.current_row)
        elif tag in {'td', 'th'}:
            if self.current_row is None:
                self.current_row = []
                self.rows.append(self.current_row)
            attributes = dict(attrs)
            def span(name):
                try:
                    return min(100, max(1, int(attributes.get(name, '1'))))
                except (TypeError, ValueError):
                    return 1
            self.current_cell = {'parts': [], 'rowspan': span('rowspan'),
                                 'colspan': span('colspan'), 'header': tag == 'th'}
            self.current_row.append(self.current_cell)
        elif tag == 'br' and self.current_cell is not None:
            self.current_cell['parts'].append('\n')

    def handle_endtag(self, tag):
        if tag == 'table':
            self.depth = max(0, self.depth - 1)
        if self.depth > 1:
            return
        if tag in {'td', 'th'}:
            self.current_cell = None
        elif tag == 'tr':
            self.current_row = None

    def handle_data(self, data):
        if self.current_cell is not None and self.depth <= 1:
            self.current_cell['parts'].append(data)


def parse_table_html(html):
    parser = _TableHTML()
    parser.feed(html)
    occupied, cells = set(), []
    for row, values in enumerate(parser.rows, 1):
        column = 1
        for value in values:
            while (row, column) in occupied:
                column += 1
            item = VisualCell(''.join(value['parts']).strip(), row, column,
                              value['rowspan'], value['colspan'], value['header'])
            if column + item.colspan > 102 or len(cells) >= MAX_CELLS:
                raise ValueError('Visual table exceeds cell/column limits')
            cells.append(item)
            for y in range(row, row + item.rowspan):
                for x in range(column, column + item.colspan):
                    if (y, x) in occupied:
                        raise ValueError('Overlapping visual table spans')
                    occupied.add((y, x))
            column += item.colspan
    return cells


def _contains(box, other, tolerance=.5):
    if not box or not other:
        return False
    x, y = (other[0] + other[2]) / 2, (other[1] + other[3]) / 2
    return box[0] - tolerance <= x <= box[2] + tolerance and box[1] - tolerance <= y <= box[3] + tolerance


def _overlap(a, b):
    if not a or not b:
        return 0.
    intersection = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    area = min(max(0., a[2] - a[0]) * max(0., a[3] - a[1]),
               max(0., b[2] - b[0]) * max(0., b[3] - b[1]))
    return intersection / area if area else 0.


def _normalized(text):
    return re.sub(r'\s+', '', str(text)).casefold().replace('\u00ad', '')


def quality_native_text(text):
    text = str(text).strip()
    if not text or '\ufffd' in text or '\x00' in text:
        return False
    meaningful = sum(c.isalnum() for c in text)
    return meaningful > 0 and sum(c.isprintable() or c.isspace() for c in text) / len(text) > .95


def _words_text(words):
    ordered = sorted(words, key=lambda w: (w.block, w.line, w.order))
    lines = []
    for word in ordered:
        key = (word.block, word.line)
        if not lines or lines[-1][0] != key:
            lines.append((key, []))
        lines[-1][1].append(word.text)
    return '\n'.join(' '.join(parts) for _, parts in lines)


def _agreement(native, visual):
    if not visual.strip():
        return None
    a, b = _normalized(native), _normalized(visual)
    # Numeric discrepancies matter even if a long paragraph is almost equal.
    if re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= .9


class DocumentFusion:
    def _native_for_cell(self, cell, bbox, native_cells, words, region):
        if bbox:
            # A whole table box is not an individual cell location.
            candidates = [w for w in words if _contains(bbox, w.bbox)]
            text = _words_text(candidates)
            if quality_native_text(text):
                return text, bbox, candidates
        # A native grid is exact only when the visual and native grids agree.
        # The caller supplies matching cells, never guesses by column position.
        native = native_cells.get((cell.row, cell.column))
        if native and quality_native_text(native.text):
            candidates = [w for w in words if _contains(native.bbox, w.bbox)] if native.bbox else []
            return native.text, native.bbox, candidates
        # When official VL returns HTML without cell boxes, exact text matches
        # can still locate a source phrase. Conflicting numbers are not guessed.
        normalized = _normalized(cell.text)
        matches = [c for c in region if quality_native_text(c.text) and _normalized(c.text) == normalized]
        if len(matches) == 1:
            native = matches[0]
            candidates = [w for w in words if _contains(native.bbox, w.bbox)] if native.bbox else []
            return native.text, native.bbox, candidates
        return '', bbox, []

    def _table(self, target, block, page, original, words, claimed, *, provider='paddleocr-vl'):
        visual_cells = parse_table_html(block.html)
        if not visual_cells:
            raise ValueError('Visual table has no cells')
        region = [c for c in original.cells if c.page == page and _contains(block.bbox, c.bbox)]
        native_tables = [t for t in original.table_objects if t.page == page and _overlap(t.bbox, block.bbox) >= .85]
        native_cells = {}
        visual_height = max(c.row + c.rowspan - 1 for c in visual_cells)
        visual_width = max(c.column + c.colspan - 1 for c in visual_cells)
        if len(native_tables) == 1:
            native_table = native_tables[0]
            native_height = len(native_table.rows)
            native_width = max((c.cell for r in native_table.rows for c in r.cells), default=0)
            if native_height == visual_height and native_width == visual_width:
                native_cells = {(row, c.cell): c
                                for row, native_row in enumerate(sorted(native_table.rows, key=lambda r: r.index), 1)
                                for c in native_row.cells if not c.merged_into}
        # Official optional cell boxes must cover every origin cell. An incomplete
        # box list cannot safely be zipped onto a different HTML cell order.
        boxes = block.cell_boxes if len(block.cell_boxes) == len(visual_cells) else []
        occupied = {}
        native_hits = 0
        for index, visual in enumerate(visual_cells):
            box = boxes[index] if boxes else None
            native, location, used_words = self._native_for_cell(visual, box, native_cells, words, region)
            agreement = _agreement(native, visual.text) if native else None
            method = 'native+vision' if native else provider
            source_method = 'pdf-native+vision' if native else provider
            item = target.add(native or visual.text, block.id, visual.row, visual.column,
                              page=page, kind='table', bbox=location, method=method,
                              source_method=source_method, rowspan=visual.rowspan,
                              colspan=visual.colspan, reading_order=block.reading_order,
                              vision_agreement=agreement)
            if native:
                native_hits += 1
                claimed.update(w.id for w in used_words)
            if agreement is False:
                target.warnings.append(f'Страница {page}, таблица {block.id}, строка {visual.row}, колонка {visual.column}: native текст расходится с Vision; сохранён native текст')
                target.routing['issues'].append('native_vision_conflict')
            for y in range(visual.row, visual.row + visual.rowspan):
                for x in range(visual.column, visual.column + visual.colspan):
                    occupied[(y, x)] = item
            if visual.header:
                table = target._table_index[block.id]
                if visual.row not in table.header_rows:
                    table.header_rows.append(visual.row)
        # Empty placeholders retain the logical columns of merged headers.
        for row in range(1, visual_height + 1):
            for column in range(1, visual_width + 1):
                origin = occupied.get((row, column))
                if origin and (origin.row, origin.cell) == (row, column):
                    continue
                target.add('', block.id, row, column, page=page, kind='table',
                           method=provider, source_method=provider,
                           merged_into=origin.id if origin else None,
                           reading_order=block.reading_order)
        if words and not boxes and native_hits < sum(bool(c.text) for c in visual_cells):
            target.warnings.append(f'Страница {page}, таблица {block.id}: Vision не вернул координаты всех ячеек; часть значений требует сверки с native текстом')
            target.routing['issues'].append('visual_table_cells_without_native_alignment')

    def merge(self, source, result):
        original = source
        target = CanonicalDocument(source.file, started=source.started)
        target.warnings = list(source.warnings)
        target.routing = source.routing
        target.pages = source.pages
        target.native_words = source.native_words
        processed, replaced, claimed = [], [], set()
        transcribed_pages = set()
        for page_result in result.pages:
            number = page_result.page
            if not any(p.page == number for p in source.page_objects):
                target.warnings.append(f'Vision вернул неизвестную страницу {number}; страница проигнорирована')
                continue
            page = next(p for p in source.page_objects if p.page == number)
            target.ensure_page(number, page.width, page.height, page.rotation)
            if getattr(page_result, 'source_method', '') == 'openai-vision':
                # These blocks describe the entire rendered page, with no
                # trustworthy geometry. Replace that page's native canonical
                # layer once so repeated native/Vision tables cannot duplicate
                # commercial items. Native words remain available for review.
                transcribed_pages.add(number)
                page_valid = True
                for block in sorted(page_result.blocks, key=lambda b: b.reading_order):
                    if block.type == 'table':
                        start = len(target.cells)
                        try:
                            self._table(target, block, number, original, [], claimed,
                                        provider='openai-vision')
                        except ValueError:
                            target.cells = target.cells[:start]
                            target.reindex()
                            page_valid = False
                            target.warnings.append(f'Страница {number}: некорректная структура таблицы OpenAI; требуется проверка оригинала')
                            target.routing['issues'].append('invalid_visual_table')
                    elif block.text.strip():
                        target.add(block.text, block.id, 1, page=number, bbox=None,
                                   method='openai-vision', source_method='openai-vision',
                                   reading_order=block.reading_order)
                        target._block_index[block.id].type = block.type
                if page_valid and getattr(page_result, 'complete', True):
                    # A reviewed blank page is complete even without blocks.
                    processed.append(number)
                else:
                    target.routing['issues'].append('incomplete_openai_page')
                continue
            words = [w for w in source.native_words if w.page == number]
            accepted = []
            for block in sorted(page_result.blocks, key=lambda b: b.reading_order):
                if not block.bbox or len(block.bbox) != 4:
                    target.warnings.append(f'Страница {number}: визуальный блок без координат пропущен')
                    continue
                if block.type == 'table':
                    start = len(target.cells)
                    prior_claimed = set(claimed)
                    try:
                        self._table(target, block, number, original, words, claimed)
                    except ValueError:
                        # Failed blocks cannot delete the usable native source.
                        target.cells = target.cells[:start]
                        claimed = prior_claimed
                        target.reindex()
                        target.warnings.append(f'Страница {number}: некорректная структура визуальной таблицы; сохранён native блок')
                        target.routing['issues'].append('invalid_visual_table')
                        continue
                else:
                    candidates = [w for w in words if _contains(block.bbox, w.bbox)]
                    native = _words_text(candidates)
                    is_native = quality_native_text(native)
                    text = native if is_native else block.text
                    if not text.strip():
                        continue
                    agreement = _agreement(native, block.text) if is_native else None
                    target.add(text, block.id, 1, page=number, bbox=block.bbox,
                               method='native+vision' if is_native else 'paddleocr-vl',
                               source_method='pdf-native+vision' if is_native else 'paddleocr-vl',
                               reading_order=block.reading_order, vision_agreement=agreement)
                    target._block_index[block.id].type = block.type
                    if is_native:
                        claimed.update(w.id for w in candidates)
                    if agreement is False:
                        target.warnings.append(f'Страница {number}, блок {block.id}: native текст расходится с Vision; сохранён native текст')
                        target.routing['issues'].append('native_vision_conflict')
                accepted.append(block.bbox)
            if accepted:
                processed.append(number)
                replaced.extend((number, bbox) for bbox in accepted)
        for item in target.cells:
            item.id = 'visual-' + item.id
            if item.merged_into:
                item.merged_into = 'visual-' + item.merged_into
        # Preserve native blocks outside accepted visual regions. Native text in
        # uncovered regions remains available even when VL omits a section.
        for item in source.cells:
            if item.page in transcribed_pages:
                continue
            location = item.bbox or source._block_index[item.block].bbox
            if any(number == item.page and _contains(box, location) for number, box in replaced):
                continue
            target.cells.append(item)
        # Keep unassigned words once as source text, never as duplicate item rows.
        unassigned = {}
        for word in source.native_words:
            inside = any(page == word.page and _contains(box, word.bbox) for page, box in replaced)
            if inside and word.id not in claimed:
                unassigned.setdefault((word.page, word.block, word.line), []).append(word)
        for (page, native_block, line), words in unassigned.items():
            item = target.add(_words_text(words), f'page-{page}-unassigned-{native_block}', line + 1,
                       page=page, bbox=union_boxes([w.bbox for w in words]),
                       source_method='pdf-native', reading_order=10000 + native_block)
            item.id = 'unassigned-' + item.id
        if unassigned:
            target.warnings.append('Часть native текста не сопоставлена с визуальными ячейками и сохранена отдельно; требуется проверка покрытия')
            target.routing['issues'].append('unassigned_native_text')
        if len(target.cells) > MAX_CELLS or sum(len(c.text) for c in target.cells) > MAX_CHARS:
            raise DocumentError(f'Визуальная структура превышает лимит {MAX_CHARS} символов / {MAX_CELLS} ячеек')
        target.cells.sort(key=lambda c: (c.page or 0, c.reading_order, c.row, c.cell))
        # Final IDs are deterministic in canonical reading order and unique across
        # both layers, including the merged-header anchors.
        identities = {id(c): f'c{index}' for index, c in enumerate(target.cells)}
        old_to_new = {c.id: identities[id(c)] for c in target.cells}
        for item in target.cells:
            if item.merged_into:
                item.merged_into = old_to_new.get(item.merged_into, item.merged_into)
            item.id = identities[id(item)]
        target.reindex()
        for page in source.page_objects:
            target.ensure_page(page.page, page.width, page.height, page.rotation)
        used = bool(processed) or bool(transcribed_pages)
        target.routing.update(used=used, processed_pages=processed,
                              status='vision' if used else 'fallback')
        source.cells, source.warnings = target.cells, target.warnings
        source.blocks, source.table_objects, source.page_objects = target.blocks, target.table_objects, target.page_objects
        source._block_index, source._table_index = target._block_index, target._table_index
        source._row_index, source._page_index = target._row_index, target._page_index
        source.chars, source.tables = target.chars, target.tables
        return source
