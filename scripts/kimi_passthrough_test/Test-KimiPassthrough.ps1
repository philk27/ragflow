<#
.SYNOPSIS
    Verify whether a Kimi Code local endpoint is a passthrough proxy or an
    agent-loop, and whether structured output survives the hop.

.DESCRIPTION
    Loads KIMI_BASE_URL / KIMI_API_KEY / KIMI_MODEL from a .env file (PowerShell,
    as requested), then either:
      * runs the rigorous Python harness (recommended; needs `pip install openai
        jsonschema`), or
      * with -RawOnly, fires the two discriminating calls with Invoke-RestMethod
        and dumps the raw JSON so you can eyeball passthrough-vs-agent yourself.

.PARAMETER EnvFile
    Path to the .env holding the Kimi Code credentials. Default: .\.env

.PARAMETER RawOnly
    Skip Python; just hit the endpoint with Invoke-RestMethod and show raw output.

.EXAMPLE
    pwsh ./Test-KimiPassthrough.ps1 -EnvFile C:\path\to\.env

.EXAMPLE
    pwsh ./Test-KimiPassthrough.ps1 -EnvFile .\.env -RawOnly
#>
param(
    [string]$EnvFile = ".env",
    [switch]$RawOnly
)

$ErrorActionPreference = "Stop"

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { throw "Env file not found: $Path" }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $name = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$name" -Value $val
    }
}

Write-Host "Loading $EnvFile ..." -ForegroundColor Cyan
Import-DotEnv -Path $EnvFile

# Accept common aliases people put in .env files.
if (-not $env:KIMI_BASE_URL -and $env:OPENAI_BASE_URL) { $env:KIMI_BASE_URL = $env:OPENAI_BASE_URL }
if (-not $env:KIMI_API_KEY  -and $env:OPENAI_API_KEY)  { $env:KIMI_API_KEY  = $env:OPENAI_API_KEY }
if (-not $env:KIMI_MODEL    -and $env:OPENAI_MODEL)    { $env:KIMI_MODEL    = $env:OPENAI_MODEL }

if (-not $env:KIMI_BASE_URL) { throw "KIMI_BASE_URL not set (the Kimi Code endpoint, e.g. http://127.0.0.1:PORT/v1)" }
if (-not $env:KIMI_MODEL)    { throw "KIMI_MODEL not set (model id the endpoint routes to)" }
if (-not $env:KIMI_API_KEY)  { $env:KIMI_API_KEY = "sk-no-key" }

Write-Host ("Endpoint : {0}" -f $env:KIMI_BASE_URL) -ForegroundColor Green
Write-Host ("Model    : {0}" -f $env:KIMI_MODEL)    -ForegroundColor Green
Write-Host ""

if (-not $RawOnly) {
    $py = (Get-Command python -ErrorAction SilentlyContinue) ?? (Get-Command python3 -ErrorAction SilentlyContinue)
    if (-not $py) { throw "Python not found. Re-run with -RawOnly, or install Python + `pip install openai jsonschema`." }
    Write-Host "Running rigorous harness (test_kimi_passthrough.py)..." -ForegroundColor Cyan
    & $py.Source (Join-Path $PSScriptRoot "test_kimi_passthrough.py")
    exit $LASTEXITCODE
}

# ----------------------------- RawOnly path ------------------------------- #
$headers = @{ "Authorization" = "Bearer $($env:KIMI_API_KEY)"; "Content-Type" = "application/json" }
$url = ($env:KIMI_BASE_URL.TrimEnd('/')) + "/chat/completions"

$system = "You are an information-extraction engine. Extract entity nodes from the provided message. You must respond with a single JSON object and nothing else."
$user = @"
Entity Types:
0: Entity
1: Person
2: Organization

Given the MESSAGE, extract every entity mentioned. Return {"extracted_entities":[{"name":...,"entity_type_id":...}]}.

MESSAGE:
In 2017, Ilya Sutskever co-founded OpenAI alongside Sam Altman and Elon Musk. Sutskever had previously worked at Google Brain in Mountain View.
"@

function Invoke-Kimi {
    param([hashtable]$Body, [string]$Label)
    Write-Host ("--- {0} ---" -f $Label) -ForegroundColor Yellow
    try {
        $json = $Body | ConvertTo-Json -Depth 20
        $resp = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $json
        $resp | ConvertTo-Json -Depth 20 | Write-Output
        $content = $resp.choices[0].message.content
        Write-Host "`n>>> message.content was:" -ForegroundColor Cyan
        Write-Host $content
        if ($resp.choices[0].message.tool_calls) {
            Write-Host "`n!!! tool_calls present -> AGENT/TOOL layer active (NOT clean passthrough)" -ForegroundColor Red
        }
        foreach ($m in @("<|tool_call_begin|>", "<|tool_calls_section_begin|>", "<|im_start|>")) {
            if ($content -like "*$m*") { Write-Host "!!! leaked marker '$m' -> agent endpoint" -ForegroundColor Red }
        }
    } catch {
        Write-Host ("request failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
    Write-Host ""
}

# Test A: bare extraction, no agent framing.
Invoke-Kimi -Label "A: passthrough vs agent-loop (no response_format)" -Body @{
    model = $env:KIMI_MODEL
    temperature = 0.0
    messages = @(
        @{ role = "system"; content = $system },
        @{ role = "user";   content = $user }
    )
}

# Test B1: strict json_schema.
Invoke-Kimi -Label "B1: response_format=json_schema (strict)" -Body @{
    model = $env:KIMI_MODEL
    temperature = 0.0
    messages = @(
        @{ role = "system"; content = $system },
        @{ role = "user";   content = $user }
    )
    response_format = @{
        type = "json_schema"
        json_schema = @{
            name = "extracted_entities"; strict = $true
            schema = @{
                type = "object"; additionalProperties = $false
                required = @("extracted_entities")
                properties = @{ extracted_entities = @{ type = "array" } }
            }
        }
    }
}

# Test B2: json_object (schema lives in the prompt).
Invoke-Kimi -Label "B2: response_format=json_object (schema in prompt)" -Body @{
    model = $env:KIMI_MODEL
    temperature = 0.0
    response_format = @{ type = "json_object" }
    messages = @(
        @{ role = "system"; content = $system },
        @{ role = "user";   content = $user + "`nRespond ONLY with the JSON object described." }
    )
}

Write-Host "Raw dump complete. Read the >>> content blocks: clean JSON object = passthrough;" -ForegroundColor Cyan
Write-Host "agent prose / tool_calls / leaked markers = agent-loop." -ForegroundColor Cyan
