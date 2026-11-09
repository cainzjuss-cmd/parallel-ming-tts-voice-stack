# Install the shared-weight Ming TTS stack on Windows.
# Run from inside the tts-stack-pack directory:  powershell -NoProfile -ExecutionPolicy Bypass -File install_windows.ps1
# Idempotent: re-running copies fresh files over old ones and re-checks the hook.
param(
    [string]$ComfyRoot  = $(if ($env:COMFY_ROOT) { $env:COMFY_ROOT } else { "C:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable" }),
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$Py         = ""
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "== root: $ComfyRoot"
Write-Host "== hermes home: $HermesHome"

if (-not $Py) {
    $cand = @("$ComfyRoot\python_embeded\python.exe", "$ComfyRoot\ComfyUI\venv\Scripts\python.exe")
    $Py = ($cand | Where-Object { Test-Path $_ } | Select-Object -First 1)
}
if (-not $Py) { Write-Error "no python found; pass -Py <path> (ComfyUI's interpreter, not a system one: the node uses its torch/comfy-kitchen)"; }
Write-Host "== python: $Py"

$comfy = Join-Path $ComfyRoot "ComfyUI"
if (-not (Test-Path $comfy)) { Write-Error "ComfyUI not found at $comfy - clone it first (see download_manifest.json, item 'comfyui')" }

# tools + chip
New-Item -ItemType Directory -Force -Path "$HermesHome\tools", "$HermesHome\tts-voices", "$HermesHome\tts-ab", "$HermesHome\cache\scratch" | Out-Null
Copy-Item "$here\files\*.py" "$HermesHome\tools\" -Force
New-Item -ItemType Directory -Force -Path "$HermesHome\desktop-plugins\dow-voice-chime" | Out-Null
Copy-Item "$here\files\plugin.js" "$HermesHome\desktop-plugins\dow-voice-chime\plugin.js" -Force
Write-Host "tools -> $HermesHome\tools ; chip -> desktop-plugins\dow-voice-chime (needs Reload desktop plugins)"

# model
& $Py "$here\dl.py" --dest $ComfyRoot
if ($LASTEXITCODE -ne 0) { Write-Error "download/verify failed - do not continue, a half-written checkpoint boots and then dies at load time" }

# node hook
$node = Join-Path $comfy "custom_nodes\ComfyUI-Ming-3-tts"
if (-not (Test-Path $node)) { Write-Error "Ming node missing at $node - clone it (download_manifest.json, 'ming_node'), WITH its assets folder, then pip install -r its requirements.txt" }
& $Py "$here\files\apply_share_hook.py" --node $node
if ($LASTEXITCODE -ne 0) { Write-Error "the loader hook could not be applied safely; see the message above and docs/upstream-proposal.md" }

# config the tools read
@{
    hermes_home  = $HermesHome
    comfy_root   = $ComfyRoot
    scratch      = "$HermesHome\cache\scratch"
    share_log    = "$HermesHome\cache\scratch\share_path.log"
} | ConvertTo-Json | Set-Content -Encoding UTF8 "$HermesHome\tools\tts_pack.json"

# smoke test: pool up, render one sentence, report what came out
$env:HERMES_HOME  = $HermesHome
$env:COMFY_ROOT   = $ComfyRoot
$env:MING_SHARE_LOG = "$HermesHome\cache\scratch\share_path.log"
Write-Host "`n== booting the pool (cold start ~60 s, it warms the clone path so the first reply is fast)"
& $Py "$HermesHome\tools\dow_tts.py" pool up
$max = 0
foreach ($p in 8188..8194) { try { Invoke-RestMethod "http://127.0.0.1:$p/system_stats" -TimeoutSec 2 | Out-Null; $max++ } catch {} }
Write-Host "workers answering: $max"
Write-Host "`nNext: add a voice. A voice dir needs ref.wav (one CONTINUOUS clip) + ref_text.txt (verbatim"
Write-Host "transcript of that same audio) + voice.json. Nothing ships pre-made - see README 'Voices'."
Write-Host "Then:  python $HermesHome\tools\dow_tts.py voices     and     pool status"
