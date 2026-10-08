"""Original-file packaging and image encoding; no document parsing or OCR."""
import base64
from io import BytesIO
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from .errors import DocumentError
from .document_limits import FORMATS, IMAGE_FORMATS, MAX_BYTES
from .openai_client import setting_int


MIME = {
    '.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel', '.csv': 'text/csv', '.tsv': 'text/tsv',
    '.txt': 'text/plain', '.json': 'application/json',
}
SPREADSHEETS = {'.xlsx', '.xls', '.csv', '.tsv'}

def input_parts(content, filename):
    """Package original bytes; only unsupported image encodings are converted."""
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise DocumentError('Неподдерживаемый формат файла')
    if not content or len(content) > MAX_BYTES:
        raise DocumentError('Файл пуст или превышает допустимый размер')
    if suffix not in IMAGE_FORMATS:
        return [{'type': 'input_file', 'filename': Path(filename).name,
                 'file_data': 'data:' + MIME[suffix] + ';base64,' + base64.b64encode(content).decode('ascii')}]
    try:
        with Image.open(BytesIO(content)) as image:
            frames = getattr(image, 'n_frames', 1)
            if frames > setting_int('VISION_MAX_PAGES', 50, 1, 500):
                raise DocumentError('Изображение превышает лимит кадров')
            parts = []
            for index in range(frames):
                image.seek(index)
                if image.width * image.height > 40_000_000:
                    raise DocumentError('Изображение превышает лимит 40 миллионов пикселей')
                output = BytesIO()
                image.convert('RGB').save(output, format='PNG')
                parts.append({'type': 'input_image', 'image_url': 'data:image/png;base64,' +
                              base64.b64encode(output.getvalue()).decode('ascii'), 'detail': 'high'})
            return parts
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        if isinstance(error, DocumentError):
            raise
        raise DocumentError('Не удалось открыть изображение') from None

