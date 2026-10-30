"""Explicit, non-executable context packs. Approval metadata is a source assertion."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
FACT_KEYS = {"audience", "channel", "market", "locale", "product", "asset_policy"}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def conditions(value):
    if set(value) - FACT_KEYS or len(value) > len(FACT_KEYS):
        raise ValueError(f"Conditions use only {sorted(FACT_KEYS)}")
    for options in value.values():
        if not options or len(options) > 20 or any(not s.strip() or len(s) > 100 for s in options):
            raise ValueError("Each condition needs 1–20 nonempty values of at most 100 characters")
    return value


class Source(Record):
    id: Identifier
    path: str = Field(min_length=1, max_length=1024)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["safety_information", "rules", "guidelines", "brochure", "example", "asset", "other"]
    tags: list[str] = Field(default_factory=list, max_length=32)
    scope: dict[str, list[str]] = Field(default_factory=dict)
    status: Literal["approved", "draft", "reference", "retired"] = "reference"
    approval_id: str | None = Field(default=None, max_length=200)
    approval_evidence: list[Identifier] = Field(default_factory=list, max_length=16)
    valid_from: date | None = None
    valid_until: date | None = None
    # Anchors are selectors only for plain text/HTML. PDF page/time anchors are
    # evidence navigation hints, never an assertion that OCR or transcription is exact.
    lines: tuple[int, int] | None = None
    html_id: str | None = Field(default=None, min_length=1, max_length=200)
    pages: list[int] = Field(default_factory=list, max_length=20)
    time_seconds: tuple[float, float] | None = None
    origin: str | None = Field(default=None, max_length=1000)

    _scope = field_validator("scope")(conditions)

    @model_validator(mode="after")
    def validate_source(self):
        if any(len(tag) > 100 or not tag.strip() for tag in self.tags):
            raise ValueError("Tags must be nonempty and at most 100 characters")
        if self.lines and (self.lines[0] < 1 or self.lines[1] < self.lines[0]):
            raise ValueError("lines is an inclusive, one-based range")
        if self.lines and self.html_id:
            raise ValueError("Choose a line range or HTML id, not both")
        if any(p < 1 for p in self.pages):
            raise ValueError("PDF page anchors are one-based")
        if self.time_seconds and not 0 <= self.time_seconds[0] < self.time_seconds[1]:
            raise ValueError("Time anchor must have 0 <= start < end")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            raise ValueError("Source validity dates are reversed")
        return self


class Rule(Record):
    id: Identifier
    kind: Literal["required_copy", "asset_allowlist", "required_asset", "fonts", "palette", "min_font_size", "guidance"]
    level: Literal["required", "advisory"] = "advisory"
    description: str = Field(min_length=1, max_length=2000)
    when: dict[str, list[str]] = Field(default_factory=dict)
    sources: list[Identifier] = Field(default_factory=list, max_length=128)
    values: list[str] = Field(default_factory=list, max_length=128)
    minimum: float | None = Field(default=None, gt=0, le=4096, allow_inf_nan=False)
    min_frames: int = Field(default=1, ge=1, le=10_000_000, strict=True)
    roles: list[Literal["content", "background", "brand", "decor", "text"]] = Field(default_factory=list, max_length=5)

    _when = field_validator("when")(conditions)

    @model_validator(mode="after")
    def validate_rule(self):
        if any(not value.strip() or len(value) > 200 for value in self.values):
            raise ValueError("Rule values must be nonempty and at most 200 characters")
        if self.values and self.kind not in {"fonts", "palette"}:
            raise ValueError("values applies only to fonts and palette rules")
        if self.kind in {"required_copy", "required_asset"} and len(self.sources) != 1:
            raise ValueError(f"{self.kind} needs exactly one source")
        if self.kind == "asset_allowlist" and not self.sources:
            raise ValueError("asset_allowlist needs at least one source")
        if self.kind in {"fonts", "palette"} and (not self.values or any(not s or len(s) > 200 for s in self.values)):
            raise ValueError(f"{self.kind} needs bounded, nonempty values")
        if self.kind == "min_font_size" and self.minimum is None:
            raise ValueError("min_font_size needs minimum")
        if self.kind == "palette":
            import re
            if any(not re.fullmatch(r"#[a-fA-F0-9]{6}([a-fA-F0-9]{2})?", value) for value in self.values):
                raise ValueError("Palette values must be explicit #RRGGBB or #RRGGBBAA colors")
        if self.kind == "guidance" and self.level == "required":
            raise ValueError("Narrative guidance is advisory; use an executable rule for a required check")
        return self


class Pack(Record):
    schema_version: Literal["vwb.design-context/1"] = "vwb.design-context/1"
    brand: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=100)
    sources: list[Source] = Field(min_length=1, max_length=128)
    rules: list[Rule] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def references(self):
        sources = {s.id: s for s in self.sources}
        if len(sources) != len(self.sources) or len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("Source and rule IDs must be unique")
        for source in self.sources:
            if set(source.approval_evidence) - sources.keys():
                raise ValueError("Approval evidence must reference declared source IDs")
        for rule in self.rules:
            if set(rule.sources) - sources.keys():
                raise ValueError(f"Unknown sources in rule {rule.id}")
            if rule.kind in {"asset_allowlist", "required_asset"} and any(sources[s].kind != "asset" for s in rule.sources):
                raise ValueError("Asset rules must reference asset sources")
            if rule.kind == "required_copy" and sources[rule.sources[0]].kind != "safety_information":
                raise ValueError("required_copy must reference explicit safety_information text")
        return self


def facts(value):
    if not isinstance(value, dict) or set(value) - FACT_KEYS:
        raise ValueError(f"Brief facts use only {sorted(FACT_KEYS)}")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in value.values()):
        raise ValueError("Brief fact values must be nonempty strings up to 100 characters")
    if "asset_policy" in value and value["asset_policy"] not in {"strict", "preferred", "open"}:
        raise ValueError("asset_policy must be strict, preferred or open")
    return dict(value)


def applies(when, brief):
    """Three-valued logic: known contradiction is false; missing facts are unknown."""
    if any(key in brief and brief[key] not in values for key, values in when.items()):
        return False
    return None if any(key not in brief for key in when) else True
