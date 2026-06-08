#!/usr/bin/env python3
"""
Kimi Code endpoint verifier — passthrough vs agent-loop, and structured-output fidelity.

This script settles two open questions about routing Graphiti's entity extraction
through a Kimi Code (or any CLI-agent) local endpoint:

  TEST A  Passthrough vs agent-loop
          Is the exposed endpoint a transparent chat proxy (forwards your
          messages, returns a clean completion), or does it wrap your prompt in
          the CLI's own coding-agent scaffolding (system prompts, tool-use loop)?
          If it's an agent endpoint, extraction is dead on arrival.

  TEST B  Structured-output fidelity
          Even on a clean passthrough, does response_format/json_schema survive
          the hop and constrain the output? Compares strict json_schema mode
          against json_object mode (schema injected into the prompt — Graphiti's
          documented mitigation for providers that accept-but-don't-constrain).

It deliberately uses the SAME call surface Graphiti uses: the OpenAI Python SDK
against an OpenAI-compatible /v1/chat/completions endpoint. Graphiti's
OpenAIClient calls client.beta.chat.completions.parse() with a Pydantic
response_format; we exercise both the strict and the prompt-injected paths so you
can see which one your endpoint actually honours.

Usage:
  pip install openai jsonschema
  export KIMI_BASE_URL="http://127.0.0.1:<port>/v1"   # Kimi Code's local endpoint
  export KIMI_API_KEY="..."                            # whatever the endpoint expects
  export KIMI_MODEL="kimi-k2-..."                      # model id the endpoint routes to
  python test_kimi_passthrough.py

Exit code 0 = at least one structured-output mode produced schema-valid output on
a clean passthrough. Non-zero = the route is unsafe for Graphiti ingestion as-is.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency: pip install openai")

try:
    from jsonschema import Draft7Validator
except ImportError:
    sys.exit("Missing dependency: pip install jsonschema")


# --------------------------------------------------------------------------- #
# Graphiti-shaped extraction payload.
#
# This mirrors the structure of Graphiti's node-extraction prompt: a system
# message that defines the task and pins the output to a strict JSON object, and
# a user message carrying the passage plus an explicit entity-type menu. The
# schema below is a faithful stand-in for Graphiti's ExtractedEntities model
# (list of {name, entity_type_id}).  It is intentionally small but exercises
# every property the real parser depends on: required fields, an integer enum,
# and "additionalProperties": false.
# --------------------------------------------------------------------------- #

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["extracted_entities"],
    "properties": {
        "extracted_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "entity_type_id"],
                "properties": {
                    "name": {"type": "string"},
                    "entity_type_id": {"type": "integer", "enum": [0, 1, 2]},
                },
            },
        }
    },
}

ENTITY_TYPE_MENU = textwrap.dedent(
    """\
    Entity Types:
    0: Entity (default; any named entity not better described below)
    1: Person (a named human being)
    2: Organization (a company, institution, or group)
    """
)

PASSAGE = (
    "In 2017, Ilya Sutskever co-founded OpenAI alongside Sam Altman and Elon Musk. "
    "Sutskever had previously worked at Google Brain in Mountain View."
)

SYSTEM_PROMPT = (
    "You are an information-extraction engine. Extract entity nodes from the "
    "provided message. You must respond with a single JSON object and nothing "
    "else — no prose, no explanation, no markdown fences."
)

USER_PROMPT = textwrap.dedent(
    f"""\
    {ENTITY_TYPE_MENU}

    Given the MESSAGE, extract every entity that is explicitly or implicitly
    mentioned. For each, return its name and the id of the best-fitting entity
    type from the menu above.

    MESSAGE:
    {PASSAGE}
    """
)

# Schema-in-prompt variant used by the json_object path (Graphiti's
# structured_output_mode="json_object" mitigation). The schema travels in the
# prompt instead of relying on the endpoint to enforce response_format.
USER_PROMPT_WITH_SCHEMA = USER_PROMPT + textwrap.dedent(
    f"""\

    Respond ONLY with a JSON object conforming exactly to this JSON Schema:
    {json.dumps(EXTRACTION_SCHEMA, indent=2)}
    """
)

# Markers Kimi-family models emit when an agent/tool-call layer leaks through,
# and generic agent-trajectory tells. Presence of any of these in message
# content is strong evidence the endpoint is NOT a clean passthrough.
AGENT_LEAK_MARKERS = [
    r"<\|tool_call_begin\|>",
    r"<\|tool_call_end\|>",
    r"<\|tool_calls_section_begin\|>",
    r"<\|im_start\|>",
    r"functions\.",
]
AGENT_PROSE_TELLS = [
    r"\bI'll help you\b",
    r"\blet me (start|begin|first)\b",
    r"\bhere'?s? (my|the) plan\b",
    r"\bI'?ll (now |first )?(read|search|look|create|run|use the)\b",
    r"\bstep 1[:.]",
]


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    raw: dict = field(default_factory=dict)


def _client() -> tuple[OpenAI, str]:
    base_url = os.environ.get("KIMI_BASE_URL")
    api_key = os.environ.get("KIMI_API_KEY", "sk-no-key")
    model = os.environ.get("KIMI_MODEL")
    if not base_url:
        sys.exit("Set KIMI_BASE_URL to the Kimi Code endpoint, e.g. http://127.0.0.1:PORT/v1")
    if not model:
        sys.exit("Set KIMI_MODEL to the model id the endpoint should route to.")
    return OpenAI(base_url=base_url, api_key=api_key, timeout=120.0), model


def _extract_json_object(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a possibly-noisy completion."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def _schema_errors(obj: dict | None) -> list[str]:
    if obj is None:
        return ["output was not parseable JSON"]
    v = Draft7Validator(EXTRACTION_SCHEMA)
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in v.iter_errors(obj)]


def _raw_completion(client: OpenAI, model: str, messages: list[dict], **kw) -> dict:
    """Call chat.completions and return the raw dict, so we can inspect everything
    the endpoint sent back — including non-standard fields an agent layer adds."""
    resp = client.chat.completions.create(model=model, messages=messages, **kw)
    return resp.model_dump()


def test_a_passthrough(client: OpenAI, model: str) -> Result:
    """Send a bare Graphiti-style extraction request with NO tools and NO agent
    framing. A passthrough returns one chat.completion whose message.content is
    the requested JSON. An agent endpoint injects scaffolding: tool_calls, agent
    prose, leaked tool-call markers, or reasoning content bleeding into content."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]
    try:
        raw = _raw_completion(client, model, messages, temperature=0.0)
    except Exception as e:  # noqa: BLE001
        return Result("A: passthrough vs agent-loop", False, f"request failed: {e!r}")

    choices = raw.get("choices") or []
    if not choices:
        return Result("A: passthrough vs agent-loop", False, "no choices in response", raw)

    msg = choices[0].get("message", {}) or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls")
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")

    leaks = [m for m in AGENT_LEAK_MARKERS if re.search(m, content)]
    prose = [m for m in AGENT_PROSE_TELLS if re.search(m, content, re.IGNORECASE)]
    parsed = _extract_json_object(content)

    verdict_bad = []
    if tool_calls:
        verdict_bad.append(f"response carried tool_calls ({len(tool_calls)}) — agent/tool layer is active")
    if leaks:
        verdict_bad.append(f"agent/tool-call markers leaked into content: {leaks}")
    if prose:
        verdict_bad.append(f"agent-trajectory prose in content: {prose}")
    if reasoning and not content.strip():
        verdict_bad.append("only reasoning_content returned, content empty — non-standard shape")
    if parsed is None:
        verdict_bad.append("content was not a clean JSON object (agent endpoints return prose)")

    if verdict_bad:
        return Result(
            "A: passthrough vs agent-loop",
            False,
            "AGENT-LOOP suspected — " + "; ".join(verdict_bad),
            raw,
        )
    return Result(
        "A: passthrough vs agent-loop",
        True,
        "PASSTHROUGH — single completion, clean JSON content, no tool layer, no agent prose.",
        raw,
    )


def test_b_structured(client: OpenAI, model: str) -> tuple[Result, Result]:
    """B1 strict json_schema vs B2 json_object (schema in prompt)."""
    # B1: strict response_format with json_schema — what Graphiti's default
    # structured_output_mode tries first.
    schema_rf = {
        "type": "json_schema",
        "json_schema": {"name": "extracted_entities", "strict": True, "schema": EXTRACTION_SCHEMA},
    }
    messages_strict = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]
    try:
        raw = _raw_completion(client, model, messages_strict, temperature=0.0, response_format=schema_rf)
        content = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        errs = _schema_errors(_extract_json_object(content))
        b1 = Result(
            "B1: response_format=json_schema (strict)",
            not errs,
            "schema-valid output" if not errs else f"schema violations: {errs}",
            raw,
        )
    except Exception as e:  # noqa: BLE001
        # A 400 here is itself a finding: the endpoint rejects / doesn't support
        # json_schema, i.e. it would drop or choke on Graphiti's default mode.
        b1 = Result(
            "B1: response_format=json_schema (strict)",
            False,
            f"endpoint rejected/failed json_schema mode: {e!r} "
            "(suggests response_format is unsupported or dropped on this hop)",
        )

    # B2: json_object mode with the schema embedded in the prompt.
    messages_obj = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_WITH_SCHEMA},
    ]
    try:
        raw = _raw_completion(
            client, model, messages_obj, temperature=0.0, response_format={"type": "json_object"}
        )
        content = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        errs = _schema_errors(_extract_json_object(content))
        b2 = Result(
            "B2: response_format=json_object (schema in prompt)",
            not errs,
            "schema-valid output" if not errs else f"schema violations: {errs}",
            raw,
        )
    except Exception as e:  # noqa: BLE001
        b2 = Result(
            "B2: response_format=json_object (schema in prompt)",
            False,
            f"json_object mode failed: {e!r}",
        )
    return b1, b2


def _print(r: Result) -> None:
    flag = "PASS" if r.passed else "FAIL"
    print(f"[{flag}] {r.name}")
    print(textwrap.indent(r.detail, "       "))
    if os.environ.get("KIMI_TEST_DUMP_RAW") and r.raw:
        print(textwrap.indent(json.dumps(r.raw, indent=2)[:4000], "       | "))
    print()


def main() -> int:
    client, model = _client()
    print(f"Endpoint : {os.environ['KIMI_BASE_URL']}")
    print(f"Model    : {model}\n")

    a = test_a_passthrough(client, model)
    _print(a)

    if not a.passed:
        print("=> Test A failed. If this is an agent endpoint, the whole route is wrong for")
        print("   Graphiti ingestion regardless of structured-output handling. Stopping is")
        print("   reasonable, but B still runs below for completeness.\n")

    b1, b2 = test_b_structured(client, model)
    _print(b1)
    _print(b2)

    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    if not a.passed:
        print("• Endpoint behaves like an AGENT-LOOP, not a passthrough. Do not route")
        print("  Graphiti extraction through it. Use a pure passthrough proxy (e.g.")
        print("  CLIProxyAPI / kimi-proxy) or call Moonshot's API directly.")
    else:
        print("• Endpoint is a clean PASSTHROUGH.")
        if b1.passed and b2.passed:
            print("• Both structured modes work. Prefer json_object (B2) for robustness —")
            print("  it doesn't depend on the hop forwarding/enforcing response_format.")
        elif b2.passed and not b1.passed:
            print("• json_schema is dropped/unenforced on this hop, but json_object works.")
            print("  Set Graphiti structured_output_mode='json_object'. This is exactly the")
            print("  documented mitigation — premise confirmed.")
        elif b1.passed and not b2.passed:
            print("• Strict json_schema works; json_object did not validate. Unusual —")
            print("  keep json_schema mode but pin temperature=0 and re-run.")
        else:
            print("• Neither structured mode produced schema-valid output. The passthrough")
            print("  is clean but the model isn't honouring the schema — tighten the prompt")
            print("  or add a repair/retry pass before trusting it for dedup.")

    ok = a.passed and (b1.passed or b2.passed)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
