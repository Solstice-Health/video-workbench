"""Bounded memory drafts. Validation preserves every supplied text character."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def nonblank(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        raise ValueError("Text must be nonblank and contain no unsupported control characters")
    return value


Key = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(nonblank)]
Tag = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(nonblank)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class MemoryEntry(Record):
    id: Identifier
    kind: Literal["fact", "rule", "safety", "asset", "example", "guidance"]
    text: Text
    sources: list[Identifier] = Field(min_length=1, max_length=128)
    tags: list[Tag] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("Entry source IDs must be unique")
        return self


class MemoryDraft(Record):
    key: Key
    title: Annotated[str, Field(min_length=1, max_length=200), AfterValidator(nonblank)]
    summary: Text
    entries: list[MemoryEntry] = Field(max_length=100)
    gaps: list[Text] = Field(default_factory=list, max_length=32)
    conflicts: list[Text] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_entries(self):
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("Memory entry IDs must be unique")
        return self


class Omission(Record):
    path: Annotated[str, Field(min_length=1, max_length=8192)]
    reason: Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(nonblank)]


class Coverage(Record):
    """Host-generated accounting, separate from the model's claimed gaps."""
    selected_folders: list[str] = Field(default_factory=list, max_length=100)
    inventoried_files: int = Field(default=0, ge=0, le=512)
    included_files: list[str] = Field(default_factory=list, max_length=512)
    excluded: list[Omission] = Field(default_factory=list, max_length=512)
    skipped: list[Omission] = Field(default_factory=list, max_length=10000)
