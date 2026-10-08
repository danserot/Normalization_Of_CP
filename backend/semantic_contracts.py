"""Stable archived semantic-plan schemas; no inference or parser dependencies."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Quote(Strict):
    field: str
    cell: str
    value: str


class Component(Strict):
    label: str
    labelCell: str
    priceColumn: int = Field(ge=0, le=100)
    totalColumn: int = Field(ge=0, le=100)


class ExtraColumn(Strict):
    label: str
    labelCell: str
    column: int = Field(ge=1, le=100)


class Table(Strict):
    block: str
    firstRow: int = Field(ge=1)
    lastRow: int = Field(ge=1)
    nameColumn: int = Field(ge=1, le=100)
    quantityColumn: int = Field(ge=0, le=100)
    unitColumn: int = Field(ge=0, le=100)
    components: list[Component] = Field(max_length=10)
    extras: list[ExtraColumn] = Field(max_length=30)
    # Explicit aggregate columns take precedence over calculated components.
    # Legacy Layout remains unchanged for reviewed training/annotation files.
    unitPriceColumn: int = Field(default=0, ge=0, le=100)
    lineTotalColumn: int = Field(default=0, ge=0, le=100)
    componentMode: Literal['additive', 'alternative'] = 'additive'


class Layout(Strict):
    isItems: bool
    firstRow: int = Field(ge=1)
    lastRow: int = Field(ge=1)
    nameColumn: int = Field(ge=0, le=100)
    quantityColumn: int = Field(ge=0, le=100)
    unitColumn: int = Field(ge=0, le=100)
    components: list[Component] = Field(max_length=10)
    extras: list[ExtraColumn] = Field(max_length=30)


class Requisites(Strict):
    fields: list[Quote] = Field(max_length=30)


class Plan(Strict):
    fields: list[Quote] = Field(max_length=100)
    tables: list[Table] = Field(max_length=100)
    issues: list[str] = Field(max_length=30)
