"""OpenAI semantic planning returns source references, never invented items."""
from .openai_client import configured_model, openai_client, openai_configured


MODEL_ID = configured_model()
MODEL_NAME = MODEL_ID


class SemanticModelService:
    def __init__(self, url=None, model_id=None, *, client=None):
        self.model_id = model_id
        self.client = client or openai_client

    def available(self):
        """Configuration readiness only; no paid inference during health checks."""
        return openai_configured()

    def generate(self, messages, schema, *, max_tokens, remaining):
        return self.client.generate(messages, schema, max_tokens=max_tokens,
                                    remaining=remaining, model=self.model_id)


semantic_model = SemanticModelService()


def model_available():
    return semantic_model.available()
