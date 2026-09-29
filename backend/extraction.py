"""Bounded, offline document readers. Every cell retains its physical location."""
import csv
import json
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass, field
from io import BytesIO, StringIO
from pathlib import Path

import fitz
import pytesseract
from docx import Document
from PIL import Image, ImageOps

FORMATS = {'.pdf', '.docx', '.xlsx', '.xls', '.csv', '.tsv', '.txt', '.json', '.png', '.jpg', '.jpeg', '.webp'}
MAX_BYTES = 25 * 1024**2
MAX_PAGES = 50
MAX_CHARS = 200_000
MAX_CELLS = 30_000
MAX_SIDE = 2400
MAX_PIXELS = 20_000_000
DEADLINE = 180
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class DocumentError(ValueError):
    pass


@dataclass
class Cell:
    id: str
    file: str
    text: str
    block: str
    row: int
    cell: int
    page: int | None = None
    sheet: str | None = None
    method: str = 'text'
    bbox: list[float] | None = None


@dataclass
class Source:
    file: str
    cells: list[Cell] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    chars: int = 0

    def check(self):
        if time.monotonic() - self.started > DEADLINE:
            raise DocumentError('Превышено время обработки (180 секунд)')

    def add(self, text, block, row, cell=1, **location):
        self.check()
        text = str(text if text is not None else '').strip()
        self.chars += len(text)
        if self.chars > MAX_CHARS or len(self.cells) >= MAX_CELLS:
            raise DocumentError('Документ превышает лимит 200 000 символов / 30 000 ячеек')
        # Empty cells matter: retain the column index without shifting columns.
        self.cells.append(Cell(f'c{len(self.cells)}', self.file, text, block, row, cell, **location))

    def text(self):
        return '\n'.join(c.text for c in self.cells if c.text)

    def public(self):
        return [asdict(c) for c in self.cells]


def decode(content):
    if content.startswith((b'\xff\xfe', b'\xfe\xff')):
        return content.decode('utf-16')
    for encoding in ('utf-8-sig', 'cp1251'):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise DocumentError('Не удалось определить кодировку: используйте UTF-8, UTF-16 или Windows-1251')


def check_zip(content):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(e.file_size for e in entries) > 80 * 1024**2:
            raise DocumentError('Слишком большой распакованный документ (лимит 80 МБ)')


def add_lines(source, text, block, **location):
    for row, line in enumerate(text.splitlines(), 1):
        # Do not split ordinary single spaces in names or localized numbers.
        for col, value in enumerate(re.split(r'\t|\s{2,}|\|', line), 1):
            source.add(value, block, row, col, **location)


def ocr(source, original, page):
    if original.width * original.height > MAX_PIXELS:
        raise DocumentError('Изображение превышает 20 миллионов пикселей')
    image = ImageOps.exif_transpose(original).convert('RGB')
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    if max(image.size) < 1200:
        scale = min(2, 1600 / max(image.size))
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    image = ImageOps.autocontrast(ImageOps.grayscale(image))
    lang = os.getenv('TESSERACT_LANG', 'rus+eng')
    available = pytesseract.get_languages(config='')
    if any(part not in available for part in lang.split('+')):
        raise DocumentError(f'Не установлены языки Tesseract: {lang}')
    try:
        orientation = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT, timeout=8)
        if orientation.get('orientation_conf', 0) >= 5 and orientation.get('rotate'):
            image = image.rotate(-orientation['rotate'], expand=True)
    except (pytesseract.TesseractError, RuntimeError):
        pass
    # Automatic layout for full pages; uniform-block mode for small crops.
    psm = 3 if image.height > image.width * 0.7 else 6
    data = pytesseract.image_to_data(image, lang=lang, config=f'--oem 1 --psm {psm}',
                                     output_type=pytesseract.Output.DICT, timeout=35)
    # Tesseract may return each table column as a separate block. Reassemble
    # physical rows by their baseline before assigning cell numbers.
    indices = [i for i, text in enumerate(data['text']) if text.strip()]
    groups = []
    for i in sorted(indices, key=lambda n: data['top'][n] + data['height'][n] / 2):
        center = data['top'][i] + data['height'][i] / 2
        if not groups or abs(groups[-1][0] - center) > max(8, data['height'][i] * .6):
            groups.append((center, []))
        groups[-1][1].append(i)
    for row, (_, indices) in enumerate(groups, 1):
        # Gaps retain approximate table cell boundaries; uncertain layouts go to model/manual review.
        chunks, last_right = [], None
        for i in sorted(indices, key=lambda n: data['left'][n]):
            left, height = data['left'][i], data['height'][i]
            if last_right is None or left - last_right > max(18, height * 1.2):
                chunks.append([])
            chunks[-1].append(i)
            last_right = left + data['width'][i]
        for col, chunk in enumerate(chunks, 1):
            x = min(data['left'][i] for i in chunk)
            y = min(data['top'][i] for i in chunk)
            right = max(data['left'][i] + data['width'][i] for i in chunk)
            bottom = max(data['top'][i] + data['height'][i] for i in chunk)
            source.add(' '.join(data['text'][i] for i in chunk), f'page-{page}', row, col,
                       page=page, method='ocr', bbox=[x, y, right, bottom])
    source.ocr_pages.append(page)
    source.warnings.append(f'Страница {page}: OCR, сверьте значения и таблицы с оригиналом')
    image.close()


def read_document(content: bytes, filename: str) -> Source:
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise DocumentError('Неподдерживаемый формат файла')
    if not content or len(content) > MAX_BYTES:
        raise DocumentError('Файл пуст или превышает 25 МБ')
    source = Source(filename)
    if suffix == '.pdf':
        if not content.startswith(b'%PDF'):
            raise DocumentError('Содержимое файла не соответствует PDF')
        with fitz.open(stream=content, filetype='pdf') as pdf:
            if pdf.needs_pass or len(pdf) > MAX_PAGES:
                raise DocumentError('PDF защищён паролем или превышает 50 страниц')
            for page_no, page in enumerate(pdf, 1):
                source.check()
                text = page.get_text('text').strip()
                if len(re.findall(r'\w', text)) < 15 or text.count('\ufffd') > len(text) * .1:
                    scale = min(3, MAX_SIDE / max(page.rect.width, page.rect.height, 1))
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    with Image.frombytes('RGB', [pix.width, pix.height], pix.samples) as image:
                        ocr(source, image, page_no)
                    del pix
                    continue
                tables = page.find_tables().tables
                boxes = []
                for index, table in enumerate(tables):
                    boxes.append(fitz.Rect(table.bbox))
                    for row, values in enumerate(table.extract(), 1):
                        for col, value in enumerate(values, 1):
                            source.add(value, f'page-{page_no}-table-{index}', row, col,
                                       page=page_no, bbox=list(table.bbox))
                # Group positioned words into rows (works for borderless aligned tables).
                rows = []
                for word in sorted(page.get_text('words'), key=lambda w: (round(w[1] / 3), w[0])):
                    if any(box.contains(fitz.Rect(word[:4])) for box in boxes):
                        continue
                    if not rows or abs(rows[-1][0][1] - word[1]) > 3:
                        rows.append([])
                    rows[-1].append(word)
                for row_no, words in enumerate(rows, 1):
                    chunks = []
                    for word in sorted(words, key=lambda w: w[0]):
                        if not chunks or word[0] - chunks[-1][-1][2] > 12:
                            chunks.append([])
                        chunks[-1].append(word)
                    for col, chunk in enumerate(chunks, 1):
                        source.add(' '.join(w[4] for w in chunk), f'page-{page_no}', row_no, col,
                                   page=page_no, bbox=[chunk[0][0], chunk[0][1], chunk[-1][2], chunk[-1][3]])
    elif suffix == '.docx':
        check_zip(content)
        doc = Document(BytesIO(content))
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        for index, element in enumerate(doc.element.body):
            if element.tag.endswith('}p'):
                add_lines(source, Paragraph(element, doc).text, f'paragraph-{index}')
            elif element.tag.endswith('}tbl'):
                for row_no, row in enumerate(Table(element, doc).rows, 1):
                    for col, cell in enumerate(row.cells, 1):
                        source.add(cell.text, f'table-{index}', row_no, col)
        for index, section in enumerate(doc.sections):
            for kind in ('header', 'footer'):
                part = getattr(section, kind)
                for row_no, paragraph in enumerate(part.paragraphs, 1):
                    source.add(paragraph.text, f'{kind}-{index}', row_no)
    elif suffix in {'.xlsx', '.xls'}:
        if suffix == '.xlsx':
            import openpyxl
            check_zip(content)
            workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
            try:
                if len(workbook.worksheets) > 30:
                    raise DocumentError('Превышен лимит 30 листов')
                for sheet in workbook:
                    if sheet.max_row > 10000 or sheet.max_column > 100:
                        raise DocumentError('Лист превышает 10 000 строк / 100 колонок')
                    for row_no, values in enumerate(sheet.iter_rows(values_only=True), 1):
                        for col, value in enumerate(values, 1):
                            source.add(value, f'sheet-{sheet.title}', row_no, col, sheet=sheet.title)
                source.warnings.append('Excel: формулы читаются из сохранённого результата; пустой кэш остаётся пустым')
            finally:
                workbook.close()
        else:
            import xlrd
            workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
            try:
                if workbook.nsheets > 30:
                    raise DocumentError('Превышен лимит 30 листов')
                for sheet in workbook.sheets():
                    if sheet.nrows > 10000 or sheet.ncols > 100:
                        raise DocumentError('Лист превышает 10 000 строк / 100 колонок')
                    for row in range(sheet.nrows):
                        for col in range(sheet.ncols):
                            cell = sheet.cell(row, col)
                            value = xlrd.xldate.xldate_as_datetime(cell.value, workbook.datemode).date().isoformat() if cell.ctype == 3 else cell.value
                            source.add(value, f'sheet-{sheet.name}', row + 1, col + 1, sheet=sheet.name)
            finally:
                workbook.release_resources()
    elif suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        with Image.open(BytesIO(content)) as image:
            ocr(source, image, 1)
    else:
        text = decode(content)
        if len(text) > MAX_CHARS:
            raise DocumentError('Превышен лимит 200 000 символов')
        if suffix == '.json':
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get('proposal'), dict):
                value = value['proposal']
            if isinstance(value, list):
                value = {'items': value}
            if not isinstance(value, dict):
                raise DocumentError('JSON должен содержать объект предложения или массив позиций')
            for row, (key, val) in enumerate(value.items(), 1):
                if isinstance(val, list) and all(isinstance(v, dict) for v in val):
                    keys = list(dict.fromkeys(k for item in val for k in item))
                    for col, k in enumerate(keys, 1):
                        source.add(k, f'json-{key}', 1, col)
                    for index, item in enumerate(val, 2):
                        for col, k in enumerate(keys, 1):
                            source.add(item.get(k), f'json-{key}', index, col)
                elif not isinstance(val, (dict, list)):
                    source.add(f'{key}: {val if val is not None else ""}', 'json-fields', row)
                else:
                    source.warnings.append(f'JSON: неподдерживаемая вложенная структура {key}')
        elif suffix in {'.csv', '.tsv'}:
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=';\t,|')
            except csv.Error:
                dialect = csv.excel_tab if suffix == '.tsv' else csv.excel
            for row, values in enumerate(csv.reader(StringIO(text), dialect), 1):
                for col, value in enumerate(values, 1):
                    source.add(value, 'text-table', row, col)
        else:
            add_lines(source, text, 'text')
    if not source.text().strip():
        raise DocumentError('В документе не найден пригодный текст')
    return source
