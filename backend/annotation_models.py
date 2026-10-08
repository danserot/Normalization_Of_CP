"""Versioned human-review DTOs, shared by routes and persisted annotations."""
from typing import Literal
from pydantic import Field
from .semantic_contracts import Layout, Strict


class MarkedField(Strict):
    field: str = Field(max_length=100)
    state: str = Field(pattern='^(pending|found|missing)$')
    cell: str = Field(default='', max_length=200)
    value: str = Field(default='', max_length=5000)


class MarkedTable(Layout):
    block: str = Field(max_length=200)
    reviewed: bool = False
    componentMode: Literal['additive', 'alternative'] = 'additive'
    unitPriceColumn: int = Field(default=0, ge=0, le=100)
    lineTotalColumn: int = Field(default=0, ge=0, le=100)


class Annotation(Strict):
    fields: list[MarkedField] = Field(max_length=30)
    tables: list[MarkedTable] = Field(max_length=100)
    issues: list[str] = Field(default_factory=list, max_length=30)
    group: str = Field(default='', max_length=200)
    notes: str = Field(default='', max_length=5000)


class SaveAnnotation(Strict):
    revision: int = Field(ge=1)
    status: str = Field(pattern='^(draft|reviewed)$')
    annotation: Annotation
