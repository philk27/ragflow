# Kimi Code endpoint verifier

Tests whether routing **Graphiti's entity extraction** through a **Kimi Code**
local endpoint is safe. It answers the two unverified questions from the premise:

1. **Passthrough vs agent-loop** — does Kimi Code's exposed endpoint forward your
   `messages` and return a clean completion, or does it run your prompt through
   the CLI's coding-agent scaffolding (system prompts, tool-use loop)?
2. **Structured-output fidelity** — even on a clean passthrough, does
   `response_format`/`json_schema` survive the hop and constrain the output?

> **Why this isn't run for you in CI / the cloud session:** the test requires
> Kimi Code's *local* endpoint and *your* credentials, which live on your machine
> (the `.env` you load in PowerShell). A cloud container has no Kimi key, no
> running Kimi Code proxy, and blocked egress to Moonshot. Run it locally.

## Run it

`.env` (next to the script, or pass `-EnvFile`):

```dotenv
KIMI_BASE_URL=http://127.0.0.1:PORT/v1   # Kimi Code's OpenAI-compatible endpoint
KIMI_API_KEY=...                          # whatever the endpoint expects
KIMI_MODEL=kimi-k2-...                     # model id it routes to
```

PowerShell (loads the `.env`, then runs the rigorous Python harness):

```powershell
pip install openai jsonschema
pwsh ./Test-KimiPassthrough.ps1 -EnvFile .\.env
```

No Python? Eyeball the raw responses instead:

```powershell
pwsh ./Test-KimiPassthrough.ps1 -EnvFile .\.env -RawOnly
```

Set `KIMI_TEST_DUMP_RAW=1` to print the raw JSON of every call from the Python harness.

## How to read the result

### Test A — passthrough vs agent-loop

Sends a bare extraction request (system + user, **no tools, no agent framing**)
and inspects the response:

| Observation in the response | Verdict |
|---|---|
| One `chat.completion`, `message.content` is a clean JSON object, no `tool_calls` | **PASSTHROUGH** ✅ — safe to proceed to Test B |
| `tool_calls` present, or agent prose ("I'll help you… let me first…"), or leaked markers `<\|tool_call_begin\|>` / `<\|im_start\|>`, or only `reasoning_content` with empty `content` | **AGENT-LOOP** ❌ — route is wrong for ingestion, stop here |

If it's an agent endpoint, the elegance of any fallback is moot: Graphiti's parser
will get agent prose / tool-call-shaped output, and extraction fails or silently
degrades. Use a pure passthrough proxy (CLIProxyAPI, kimi-proxy) or call
Moonshot's API directly.

### Test B — structured-output fidelity

Runs only meaningfully if A passed. Two modes, same passage:

- **B1 `json_schema` (strict)** — Graphiti's default. A `400`/rejection here means
  the hop doesn't support or silently drops `response_format` — a real finding.
- **B2 `json_object` (schema in the prompt)** — Graphiti's
  `structured_output_mode="json_object"` mitigation: the schema travels in the
  prompt, so it's robust to a proxy that drops the field or accepts-but-doesn't-constrain.

| B1 | B2 | What to do |
|----|----|------------|
| ✅ | ✅ | Prefer **B2 (`json_object`)** anyway — doesn't depend on the hop honouring `response_format`. |
| ❌ | ✅ | **Confirms the premise.** Set Graphiti `structured_output_mode="json_object"`. |
| ✅ | ❌ | Keep `json_schema`, pin `temperature=0`, re-run. |
| ❌ | ❌ | Passthrough is clean but the model isn't honouring the schema — tighten the prompt, add a JSON-repair/retry pass before trusting dedup. |

## Assessment of the premise's technical claims

Both claims are **technically sound and worth verifying exactly as written** — the
script exists to turn them from "plausible" into "measured for *your* endpoint":

- **Claim 1 (passthrough vs agent-loop) is the real risk and is endpoint-specific.**
  The existence of kimi-proxy — which normalizes `kimi-k2-thinking`'s non-standard
  `<|tool_call_begin|>` tool-call/thinking output back to standard format — is direct
  evidence the agent-leak failure mode is real for the Kimi family. Test A reproduces
  precisely that signal. A CLI's "serve" endpoint can be *either* kind; only firing the
  call settles it.
- **Claim 2 (structured-output fidelity) and the recommended fix are correct.**
  Proxies do routinely drop fields they don't recognise, and Moonshot's API is
  OpenAI-*compatible* with documented divergences (temperature/`n` special cases).
  Moving the schema into the prompt (`json_object`) is the standard, robust mitigation
  for accept-but-don't-constrain providers. B1-vs-B2 measures whether you actually
  need it here.

One caveat to the premise: Graphiti calls `client.beta.chat.completions.parse()`.
If the endpoint rejects the `beta.parse` surface or its `response_format` shape, you
may see failures that originate in the SDK path, not the model. This harness uses the
plain `chat.completions.create` surface so a failure is unambiguously the endpoint's —
run it first, then switch Graphiti to `json_object` if B1 fails.
