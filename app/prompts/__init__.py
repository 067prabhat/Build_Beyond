"""
Centralised Prompt Registry.

All Claude prompts live here as versioned files. Code loads them via
`load_prompt(name, **context)` which returns a dict of everything Claude
needs: system, user, version, max_tokens, temperature, seed, weights.

Files:
  manifest.yaml               metadata + versions for every prompt
  system/*.md                 static system prompts
  user/*.jinja                templated user prompts
  snippets/*.md               reusable partials included by other templates
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent

# Lazy imports so this module still imports cleanly even before deps are installed
_jinja_env = None
_yaml_mod  = None


def _ensure_deps():
    global _jinja_env, _yaml_mod
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(
            loader=FileSystemLoader(_PROMPTS_DIR),
            autoescape=select_autoescape(disabled_extensions=("jinja", "md")),
            keep_trailing_newline=True,
            trim_blocks=False,
            lstrip_blocks=False,
        )
    if _yaml_mod is None:
        import yaml
        _yaml_mod = yaml
    return _jinja_env, _yaml_mod


@lru_cache(maxsize=1)
def _manifest() -> dict:
    _, yaml = _ensure_deps()
    text = (_PROMPTS_DIR / "manifest.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def load_prompt(name: str, **context) -> dict:
    """
    Load and render a prompt by name.

    Returns a dict with:
      system       (str)   : system prompt content
      user         (str)   : rendered user prompt
      version      (str)   : semver-ish, from manifest
      max_tokens   (int)
      temperature  (float)
      seed         (int|None)
      weights      (dict)  : only present for validator, else {}

    Raises KeyError if `name` is not declared in manifest.yaml.
    """
    env, _ = _ensure_deps()
    m = _manifest().get("prompts", {}).get(name)
    if not m:
        raise KeyError(f"Prompt '{name}' not declared in manifest.yaml")

    system_path = m.get("system")
    user_path   = m.get("user")

    system = ""
    if system_path:
        system = (_PROMPTS_DIR / system_path).read_text(encoding="utf-8")

    user = ""
    if user_path:
        tmpl = env.get_template(user_path)
        user = tmpl.render(**context)

    logger.info(f"[Prompts] Loaded '{name}' v{m.get('version', '?')}")
    return {
        "system":      system,
        "user":        user,
        "version":     m.get("version", "unknown"),
        "max_tokens":  int(m.get("max_tokens", 2000)),
        "temperature": float(m.get("temperature", 0.2)),
        "seed":        m.get("seed"),
        "weights":     dict(m.get("weights", {})),
    }


def list_prompts() -> list[str]:
    return list(_manifest().get("prompts", {}).keys())


def prompt_version(name: str) -> str:
    return _manifest().get("prompts", {}).get(name, {}).get("version", "unknown")