"""Shared validated proposal DTO; contains no document extraction logic."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AdditionalField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=200)
    value: str = Field(max_length=2000)


class CostComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=500)
    unitPrice: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lineTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=500)
    quantity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: str = Field(max_length=50)
    unitPrice: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lineTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    components: list[CostComponent] = Field(default_factory=list, max_length=10)
    componentMode: Literal['additive', 'alternative'] | None = None
    additionalFields: list[AdditionalField] = Field(default_factory=list, max_length=30)


class ProposalData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=500)
    client: str = Field(max_length=500)
    clientContact: str = Field(max_length=500)
    validUntil: str = Field(max_length=200)
    supplier: str = Field(max_length=500)
    currency: str = Field(max_length=30)
    vat: str = Field(max_length=200)
    discount: str = Field(max_length=200)
    delivery: str = Field(max_length=500)
    paymentTerms: str = Field(max_length=1000)
    deliveryTerms: str = Field(max_length=1000)
    warranty: str = Field(max_length=500)
    documentNumber: str = Field(max_length=200)
    documentDate: str = Field(max_length=200)
    documentTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    notes: str = Field(max_length=500_000)
    items: list[ProposalItem] = Field(max_length=2000)
    additionalFields: list[AdditionalField] = Field(default_factory=list, max_length=100)


