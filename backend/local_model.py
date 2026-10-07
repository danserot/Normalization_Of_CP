"""Compatibility imports for archived consumers; inference is OpenAI-only."""
from .openai_model import MODEL_ID, MODEL_NAME, SemanticModelService, model_available, semantic_model

__all__ = ['MODEL_ID', 'MODEL_NAME', 'SemanticModelService', 'model_available', 'semantic_model']
