"""
AI Validator - Weighted Rubric + Critical Gates (E5).

The validator asks Claude to score each rule 0-100. This module then:
  1. Applies the manifest weights (with per-portal overrides).
  2. Computes the weighted average.
  3. Enforces critical-gate rules: any of {date_format, data_accuracy, completeness}
     below 50 forces regeneration even if the overall score is high.

Kept in app/core/ so agent.py can import it as a plain function - no
LangGraph node here on purpose.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.portal_template import PortalTemplate

logger = logging.getLogger(__name__)


# Rules that MUST all score >= 50 regardless of overall weighted score.
CRITICAL_RULES: set[str] = {"date_format", "data_accuracy", "completeness"}

PASS_THRESHOLD:  int = int(os.environ.get("VALIDATOR_PASS_THRESHOLD", "70"))
CRITICAL_FLOOR:  int = int(os.environ.get("VALIDATOR_CRITICAL_FLOOR", "50"))
MAX_ATTEMPTS:    int = int(os.environ.get("VALIDATOR_MAX_ATTEMPTS", "2"))


def effective_weights(base_weights: dict, template: PortalTemplate) -> dict:
    """
    Merge manifest weights with the template's per-portal override.
    The override wins wherever it defines a key.
    """
    weights = dict(base_weights)
    for k, v in (template.validation_weights_override or {}).items():
        if k in weights:
            weights[k] = v
    return weights


def compute_weighted_score(rule_scores: dict, weights: dict) -> float:
    total_weight = sum(weights.values()) or 1
    return sum(int(rule_scores.get(k, 0)) * int(weights[k]) for k in weights) / total_weight


def critical_failures(rule_scores: dict) -> list[str]:
    """Any critical rule under CRITICAL_FLOOR blocks the pass."""
    return [r for r in CRITICAL_RULES if int(rule_scores.get(r, 100)) < CRITICAL_FLOOR]


def grade(validator_json: dict, weights: dict) -> dict:
    """
    Post-process the validator LLM response.

    Input `validator_json` shape (from user/validator.jinja):
      {
        "rule_scores":       {rule -> 0..100},
        "critical_failures": ["rule", ...],       # LLM's own list; we recompute
        "issues":            [{"rule","detail"}],
        "fixed_synopsis":    <dict|null>
      }

    Returns a normalised object:
      {
        "rule_scores":         {...},
        "weights":             {...},
        "weighted_score":      float 0..100,
        "critical_failures":   [str],
        "passed":              bool,
        "issues":              [ ... ],
        "fixed_synopsis":      dict | None,
        "should_regenerate":   bool,
        "verdict":             "pass" | "amber" | "regenerate"
      }
    """
    rule_scores = dict(validator_json.get("rule_scores", {}))
    weighted    = compute_weighted_score(rule_scores, weights)
    crit_fails  = critical_failures(rule_scores)
    fixed       = validator_json.get("fixed_synopsis")
    issues      = list(validator_json.get("issues", []))

    passed = weighted >= PASS_THRESHOLD and not crit_fails

    # Verdict
    if passed:
        verdict = "pass"
        should_regenerate = False
    elif fixed:
        # LLM gave us a fixable version - accept it and go to HITL with warning
        verdict = "amber"
        should_regenerate = False
    else:
        verdict = "regenerate"
        should_regenerate = True

    return {
        "rule_scores":       rule_scores,
        "weights":           dict(weights),
        "weighted_score":    round(weighted, 1),
        "critical_failures": crit_fails,
        "passed":            passed,
        "issues":            issues,
        "fixed_synopsis":    fixed,
        "should_regenerate": should_regenerate,
        "verdict":           verdict,
    }


def parse_llm_json(raw: str) -> dict:
    """Handle markdown fences and stray whitespace around the validator LLM output."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    return json.loads(raw)