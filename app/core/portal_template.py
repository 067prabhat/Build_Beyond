"""
Portal Template Contract - the single source of truth for what a tender
synopsis should look like for a given country/portal.

This file defines the DATA MODEL only. Where the template comes from
(live API, cache, JSON file, or default) is decided by template_loader.py.

Consumed by:
  - Skill 1: to know the portal name, date format, terminology
  - Skill 3: to enforce section superset + fixed labels in the prompt
  - AI Validator: to check the synopsis against declared rules
  - docx_exporter: to render sections in the correct order
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal

TemplateSource = Literal["live", "cache", "file", "default"]


@dataclass(frozen=True)
class Section:
    """One section in the portal's tender notice structure."""
    id: str
    title: str
    order: int
    required: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        return cls(
            id=d["id"],
            title=d["title"],
            order=int(d.get("order", 999)),
            required=bool(d.get("required", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FieldSpec:
    """One field the portal expects, mapped to a SAP source hint."""
    portal_field_id: str
    label: str
    section_id: str
    data_type: str = "text"        # text | date | datetime | currency | code | enum
    sap_source_hint: str = ""      # e.g. "BidSubmissionDeadline"
    required: bool = False
    important: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "FieldSpec":
        return cls(
            portal_field_id=d["portal_field_id"],
            label=d["label"],
            section_id=d["section_id"],
            data_type=d.get("data_type", "text"),
            sap_source_hint=d.get("sap_source_hint", ""),
            required=bool(d.get("required", False)),
            important=bool(d.get("important", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PortalTemplate:
    """
    The complete contract for one portal's tender notice format.
    Every layer in template_loader returns exactly this shape.
    """
    portal_country_code: str
    portal_name: str
    version: str
    source: TemplateSource
    section_superset: tuple[Section, ...]
    fields: tuple[FieldSpec, ...]
    date_format: str = "DD MMM YYYY"
    currency_hint: str = ""
    default_language: str = "English"
    standard: str = ""
    terminology: dict = field(default_factory=dict)
    template_hash: str = ""
    fetched_at: str = ""            # ISO timestamp
    validation_weights_override: dict = field(default_factory=dict)

    # ── Construction helpers ──────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict, source: TemplateSource = "file") -> "PortalTemplate":
        sections = tuple(
            sorted(
                (Section.from_dict(s) for s in d.get("section_superset", [])),
                key=lambda s: s.order,
            )
        )
        fields_ = tuple(FieldSpec.from_dict(f) for f in d.get("fields", []))
        tmpl = cls(
            portal_country_code=d["portal_country_code"],
            portal_name=d["portal_name"],
            version=d.get("version", "unknown"),
            source=source,
            section_superset=sections,
            fields=fields_,
            date_format=d.get("date_format", "DD MMM YYYY"),
            currency_hint=d.get("currency_hint", ""),
            default_language=d.get("default_language", "English"),
            standard=d.get("standard", ""),
            terminology=dict(d.get("terminology", {})),
            template_hash=d.get("template_hash", ""),
            fetched_at=d.get("fetched_at", ""),
            validation_weights_override=dict(d.get("validation_weights_override", {})),
        )
        if not tmpl.template_hash:
            object.__setattr__(tmpl, "template_hash", tmpl.compute_hash())
        return tmpl

    def to_dict(self) -> dict:
        return {
            "portal_country_code": self.portal_country_code,
            "portal_name":         self.portal_name,
            "version":             self.version,
            "source":              self.source,
            "section_superset":    [s.to_dict() for s in self.section_superset],
            "fields":              [f.to_dict() for f in self.fields],
            "date_format":         self.date_format,
            "currency_hint":       self.currency_hint,
            "default_language":    self.default_language,
            "standard":            self.standard,
            "terminology":         dict(self.terminology),
            "template_hash":       self.template_hash,
            "fetched_at":          self.fetched_at,
            "validation_weights_override": dict(self.validation_weights_override),
        }

    def compute_hash(self) -> str:
        """Stable hash used by drift detection and response cache."""
        canonical = {
            "sections": [(s.id, s.title, s.order, s.required) for s in self.section_superset],
            "fields":   [(f.portal_field_id, f.label, f.section_id, f.data_type,
                          f.required, f.important) for f in self.fields],
            "date":     self.date_format,
            "terms":    sorted(self.terminology.items()),
        }
        payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # ── Convenience accessors ─────────────────────────────────────────

    def required_sections(self) -> list[Section]:
        return [s for s in self.section_superset if s.required]

    def required_field_labels(self) -> list[str]:
        return [f.label for f in self.fields if f.required]

    def fields_for_section(self, section_id: str) -> list[FieldSpec]:
        return [f for f in self.fields if f.section_id == section_id]

    def get_section_by_id(self, sid: str) -> Section | None:
        for s in self.section_superset:
            if s.id == sid:
                return s
        return None


def now_iso() -> str:
    """UTC ISO timestamp for template fetched_at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")