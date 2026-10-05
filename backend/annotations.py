"""Persistent original files, versioned human annotations and grouped SFT export."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import fitz
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps

from .annotation_data import Annotation, SaveAnnotation, group_split, training_examples, validate_annotation
from .extraction import FORMATS, MAX_BYTES, DocumentError


def initialize_annotations(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS annotations (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
        original BLOB NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, annotation TEXT NOT NULL
    )''')


def create_annotation_router(connect_db, require_auth, pipeline):
    router = APIRouter(prefix='/api/annotations', dependencies=[Depends(require_auth)])

    def get_row(identifier, original=False):
        with connect_db() as connection:
            columns = '*' if original else 'id,fingerprint,filename,created_at,updated_at,revision,status,payload,annotation'
            row = connection.execute(f'SELECT {columns} FROM annotations WHERE id = ?', (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Документ разметки не найден')
        return row

    def public_row(row, detail=False):
        result = {'id': row['id'], 'filename': row['filename'], 'status': row['status'], 'revision': row['revision'],
                  'createdAt': row['created_at'], 'updatedAt': row['updated_at']}
        if detail:
            result.update(json.loads(row['payload']))
            result['preview'].pop('html', None)  # HTML is served separately in a sandboxed frame.
            result['annotation'] = json.loads(row['annotation'])
        return result

    @router.get('')
    def list_documents():
        with connect_db() as connection:
            rows = connection.execute('SELECT id,filename,status,revision,created_at,updated_at FROM annotations ORDER BY updated_at DESC').fetchall()
        return {'documents': [public_row(row) for row in rows]}

    @router.post('')
    async def upload_document(file: UploadFile = File(...)):
        filename = Path((file.filename or 'document').replace('\\', '/')).name
        suffix = Path(filename).suffix.lower()
        try:
            if suffix not in FORMATS:
                raise HTTPException(415, 'Неподдерживаемый формат файла')
            content = await file.read(MAX_BYTES + 1)
            if not content or len(content) > MAX_BYTES:
                raise HTTPException(413, 'Файл должен быть непустым и не больше 25 МБ')
            fingerprint = hashlib.sha256(suffix.encode() + b'\0' + content).hexdigest()
            with connect_db() as connection:
                row = connection.execute('SELECT id,fingerprint,filename,created_at,updated_at,revision,status,payload,annotation FROM annotations WHERE fingerprint = ?', (fingerprint,)).fetchone()
            if row:
                return {**public_row(row, True), 'duplicate': True}
            if pipeline.busy:
                raise HTTPException(429, 'Дождитесь завершения текущего чтения документа', headers={'Retry-After': '5'})
            pipeline.busy = True
            try:
                payload = await pipeline.run(content, filename, mode='annotation')
            except DocumentError as error:
                raise HTTPException(422, str(error)) from error
            finally:
                pipeline.busy = False
            annotation = payload.pop('annotation')
            identifier, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
            with connect_db() as connection:
                connection.execute('INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (identifier, fingerprint, filename, content, now, now, 1, 'draft',
                     json.dumps(payload, ensure_ascii=False), json.dumps(annotation, ensure_ascii=False)))
            return public_row(get_row(identifier), True)
        finally:
            await file.close()

    @router.get('/export')
    def export_dataset():
        with connect_db() as connection:
            # One consistent snapshot; never load hundreds of original BLOBs into RAM.
            connection.execute('BEGIN')
            if not connection.execute("SELECT 1 FROM annotations WHERE status='reviewed' LIMIT 1").fetchone():
                raise HTTPException(422, 'Сначала сохраните хотя бы один проверенный документ')
            counts = {'train': 0, 'val': 0, 'test': 0}
            manifest, buffer = [], BytesIO()
            info = {f'kp_{split}': {'file_name': f'kp_{split}.json', 'columns':
                {'prompt': 'instruction', 'query': 'input', 'response': 'output', 'system': 'system'}} for split in counts}
            with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
                for split in counts:
                    with archive.open(f'kp_{split}.json', 'w') as output:
                        output.write(b'[')
                        for row in connection.execute("SELECT id,fingerprint,filename,revision,annotation,payload FROM annotations WHERE status='reviewed' ORDER BY id"):
                            annotation = Annotation.model_validate_json(row['annotation'])
                            if group_split(annotation.group) != split:
                                continue
                            payload = json.loads(row['payload'])
                            validate_annotation(annotation, payload, row['filename'], True)
                            samples = training_examples(annotation, payload, row['filename'])
                            manifest.append({'id': row['id'], 'filename': row['filename'], 'fingerprint': row['fingerprint'],
                                'revision': row['revision'], 'group': annotation.group, 'split': split,
                                'exampleStart': counts[split], 'exampleCount': len(samples)})
                            for sample in samples:
                                output.write(((',' if counts[split] else '') + '\n' + json.dumps(sample, ensure_ascii=False)).encode())
                                counts[split] += 1
                        output.write(b'\n]')
                archive.writestr('dataset_info.json', json.dumps(info, ensure_ascii=False, indent=2))
                archive.writestr('manifest.json', json.dumps({'formatVersion': 1, 'task': 'Layout + Requisites',
                    'counts': counts, 'documents': manifest}, ensure_ascii=False, indent=2))
                archive.writestr('README.txt', 'Only human-reviewed annotations. Groups stay in one split. Small collections may have empty splits. Inspect counts before training. Inputs use parsed source cells; OCR is not corrected by this dataset. Longer examples must be checked against training cutoff_len. Human notes/issues are audit data, not trained answers. Originals remain in the application database.')
        return Response(buffer.getvalue(), media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="kp-dataset.zip"', 'Cache-Control': 'no-store'})

    @router.get('/{identifier}')
    def get_document(identifier: str):
        return public_row(get_row(identifier), True)

    @router.post('/{identifier}')
    def save_document(identifier: str, submission: SaveAnnotation):
        row = get_row(identifier)
        try:
            validate_annotation(submission.annotation, json.loads(row['payload']), row['filename'], submission.status == 'reviewed')
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        now = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
            changed = connection.execute('UPDATE annotations SET annotation=?, status=?, updated_at=?, revision=revision+1 WHERE id=? AND revision=?',
                (submission.annotation.model_dump_json(), submission.status, now, identifier, submission.revision)).rowcount
            if changed != 1:
                raise HTTPException(409, 'Документ изменён в другой вкладке. Откройте его заново перед сохранением')
        return public_row(get_row(identifier), True)

    @router.get('/{identifier}/original')
    def original(identifier: str):
        row = get_row(identifier, original=True)
        return Response(bytes(row['original']), media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['filename']),
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

    @router.get('/{identifier}/preview')
    def word_document(identifier: str):
        preview = json.loads(get_row(identifier)['payload'])['preview']
        if preview['kind'] != 'word':
            raise HTTPException(404, 'Этот документ не содержит HTML-превью')
        return Response(preview['html'], media_type='text/html', headers={
            'Content-Security-Policy': "default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'self'",
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

    def render_image(row, page):
        preview = json.loads(row['payload'])['preview']
        if preview['kind'] == 'pdf':
            if not 1 <= page <= preview['pages']:
                raise HTTPException(404, 'Страница не найдена')
            with fitz.open(stream=bytes(row['original']), filetype='pdf') as pdf:
                p = pdf[page - 1]
                scale = min(2, 1800 / max(p.rect.width, p.rect.height, 1))
                pixmap = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                return pixmap.tobytes('png'), 'image/png'
        if preview['kind'] == 'image' and page == 1:
            with Image.open(BytesIO(row['original'])) as original_image:
                image = ImageOps.exif_transpose(original_image)
                image.thumbnail((2400, 2400))
                output = BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=90)
            return output.getvalue(), 'image/jpeg'
        raise HTTPException(404, 'Визуальная страница не найдена')

    @router.get('/{identifier}/pages/{page}')
    async def page_image(identifier: str, page: int):
        content, mime = await asyncio.to_thread(render_image, get_row(identifier, original=True), page)
        return Response(content, media_type=mime, headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})

    return router
