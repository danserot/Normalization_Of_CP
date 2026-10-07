"""Offline regression defaults; API contract tests opt in with MockTransport."""
import os

# Do not spend money or send fixtures to external services during pytest.
if not os.getenv('TEST_VISION'):
    os.environ['OPENAI_API_KEY'] = ''
os.environ['VISION_PDF_MODE'] = 'adaptive'
