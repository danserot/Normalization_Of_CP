"""Upload limits shared by direct model extraction and the HTTP API."""
import os
from .errors import DocumentError

IMAGE_FORMATS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
FORMATS = {'.pdf', '.docx', '.xlsx', '.xls', '.csv', '.tsv', '.txt', '.json'} | IMAGE_FORMATS
MAX_BYTES = int(os.getenv('MAX_FILE_SIZE_MB', '25')) * 1024 * 1024
