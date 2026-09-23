"""Benchmark / Suite / Dataset definitions (PRD §11–§21)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SuiteDef(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)  # case tag selection
    case_ids: list[str] = Field(default_factory=list)  # explicit selection


class BenchmarkDef(BaseModel):
    name: str
    description: str = ""
    owner: str | None = None
    dataset: str  # "<dataset_id>@<version>" or "<dataset_id>@latest"
    suites: list[str] = Field(default_factory=list)
    default_profile: str = "default"


class DatasetInfo(BaseModel):
    """Dataset identity without cases (cases are versioned file content, PRD §13)."""

    id: str
    version: str
    description: str = ""
    hash: str = ""  # sha256 over dataset.yaml + case files
