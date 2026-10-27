"""Validation of reviewer output before anything is posted.

The four steps from DESIGN.md run in order and the first failure stops
the review:

1. structured_output present and valid against schema.json
2. no string in structured_output (keys or values) contains the real Claude
   token
3. no string in structured_output contains a GitHub token the orchestrator
   is using
4. total posted body length under 60000 characters, measured on the payload
   that would be posted (review body plus inline comment bodies)

Steps 2 and 3 look only for the secrets the orchestrator holds, as exact
substrings. The reviewer container is given none of them; these steps catch
a failure of that isolation, not a model that disguises a token it has.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

MAX_TOTAL_BODY = 60000


@dataclass
class Secrets:
    real_token: str
    github_tokens: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    step: int | None = None  # 1..4 for a refusal
    code: str = ""
    reason: str = ""
    structured: dict | None = None
    payload: dict | None = None

    def __str__(self) -> str:
        if self.ok:
            return "ok"
        return f"refused at step {self.step} ({self.code}): {self.reason}"


def _refuse(step: int, code: str, reason: str, structured=None) -> ValidationResult:
    return ValidationResult(False, step, code, reason, structured)


# ---------------------------------------------------------------- schema

def load_schema() -> dict:
    return json.loads(resources.files("reviewbot").joinpath("schema.json").read_text("utf-8"))


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_SUPPORTED = {"type", "required", "properties", "items", "enum", "maxLength",
              "minLength", "maxItems", "minItems", "minimum", "maximum",
              "additionalProperties", "description", "title"}


def schema_errors(value: Any, schema: dict, where: str = "$") -> list[str]:
    """Validate against the JSON Schema subset used by schema.json.

    Unknown keywords raise ValueError rather than being ignored, so a schema
    change that this validator cannot enforce is noticed.
    """
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise ValueError(f"schema keyword(s) not supported: {sorted(unknown)}")
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        if t not in _TYPES:
            raise ValueError(f"schema type not supported: {t}")
        if not _TYPES[t](value):
            return [f"{where}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        # The value is model output; do not echo it into logs.
        errors.append(f"{where}: value not in {schema['enum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: length {len(value)} > {schema['maxLength']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: length {len(value)} < {schema['minLength']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: {value} > {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: {len(value)} items > {schema['maxItems']}")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: {len(value)} items < {schema['minItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(schema_errors(item, schema["items"], f"{where}[{i}]"))
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required field {name!r}")
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                errors.extend(schema_errors(value[name], sub, f"{where}.{name}"))
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(f"{where}: unexpected field {name!r}")
    return errors


# ---------------------------------------------------------------- secrets

def read_real_token() -> str | None:
    """The real Claude token from CLAUDE_CODE_OAUTH_TOKEN, which the
    orchestrator passes to each review's proxy and looks for in the review
    output. Returns None when it is unset or empty."""
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip() or None


def _strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(_strings(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_strings(v))
    return out


def _contains(value: Any, secret: str) -> bool:
    return any(secret in s for s in _strings(value))


# ---------------------------------------------------------------- driver

def extract_structured(stdout: str) -> tuple[dict | None, str]:
    """Pull structured_output out of `claude -p --output-format json` stdout."""
    try:
        doc = json.loads(stdout)
    except ValueError as exc:
        return None, f"stdout is not JSON ({exc.msg})"
    if isinstance(doc, list):  # some versions print an array of messages
        results = [m for m in doc if isinstance(m, dict) and m.get("type") == "result"]
        if not results:
            return None, "no result message in stdout"
        doc = results[-1]
    if not isinstance(doc, dict):
        return None, "stdout JSON is not an object"
    if doc.get("is_error"):
        return None, f"claude reported an error (subtype {doc.get('subtype')!r})"
    so = doc.get("structured_output")
    if so is None:
        return None, "structured_output missing"
    if not isinstance(so, dict):
        return None, "structured_output is not an object"
    return so, ""


def total_body_length(payload: dict) -> int:
    return len(payload.get("body", "")) + sum(
        len(c.get("body", "")) for c in payload.get("comments", []))


def validate_review(stdout: str, secrets: Secrets,
                    build_payload: Callable[[dict], dict] | None = None,
                    schema: dict | None = None) -> ValidationResult:
    # 1. present and schema-valid
    structured, why = extract_structured(stdout)
    if structured is None:
        return _refuse(1, "no_structured_output", why)
    errors = schema_errors(structured, schema or load_schema())
    if errors:
        return _refuse(1, "schema", "; ".join(errors[:5]), structured)

    # 2. the real Claude token
    if _contains(structured, secrets.real_token):
        return _refuse(2, "claude_token", "contains the real Claude token", structured)

    # 3. a GitHub token in use
    if any(tok and _contains(structured, tok) for tok in secrets.github_tokens):
        return _refuse(3, "github_token", "contains a GitHub token in use", structured)

    # 4. size of what would be posted
    if build_payload is not None:
        payload = build_payload(structured)
    else:
        payload = {"body": structured["summary"],
                   "comments": [{"body": c["body"]} for c in structured["comments"]]}
    size = total_body_length(payload)
    if size >= MAX_TOTAL_BODY:
        return _refuse(4, "too_long", f"total body length {size} >= {MAX_TOTAL_BODY}", structured)

    return ValidationResult(True, structured=structured, payload=payload)
