"""Strict contracts for model-produced data and transcription."""
from pydantic import BaseModel, ConfigDict, Field
from .proposal_models import ProposalData


class ModelCell(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(max_length=5000)
    block: str = Field(min_length=1, max_length=200)
    row: int = Field(ge=1)
    cell: int = Field(ge=1, le=1000)
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, max_length=200)
    kind: str = Field(pattern='^(text|table)$')


class ModelEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str = Field(min_length=1, max_length=200)
    sourceId: str = Field(min_length=1, max_length=200)
    excerpt: str = Field(min_length=1, max_length=5000)


class DocumentAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proposal: ProposalData
    cells: list[ModelCell] = Field(max_length=30000)
    evidence: list[ModelEvidence] = Field(max_length=30000)
    complete: bool
    warnings: list[str] = Field(max_length=100)

