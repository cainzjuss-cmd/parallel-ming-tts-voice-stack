# Start the Ming TTS worker pool the way dow_spool.py expects it.
#
# Default is the SHARED-weight pool: tools/ming_weightd.py loads Ming once (~2.6 GB) and
# spawns the workers as its children, which map that one copy over CUDA IPC instead of
# each holding their own. Measured 2026-11-07 on the same 6-sentence reply (40.7 s audio);
# the audio was byte-identical to the streaming baseline in every configuration:
#   shared    3 workers   40.1 s wall | first audio +15.2 s | 16.4-16.7 GB idle | 16.9 peak
#   parked    3 workers   42.3 s      | +15.7 s             | 20.3 GB idle      | 20.5 peak
#   streaming 2 workers   56.4 s      | +17.6 s             | 12.1 GB idle      | 17.6 peak
#   shared    2 workers   55.0 s      | +22.6 s             | 16.1 GB idle      | 16.4 peak
#   parked    4 workers   23.2 GB idle - OOM-killed the Strata engine; do not scale up
# Sharing only pays at 3+ workers: shared-2 is no faster than streaming-2 and raises the
# idle floor, so switch $MODE to 'streaming' with 2 ports when you want the VRAM back.
# The workers are children of the holder - their IPC mappings die with it, so they live
# and die as a set. Keep $ports in step with DOW_COMFY in dow_spool.py.
$MODE    = 'shared'                 # 'shared' | 'streaming'
$ports   = 8188, 8189, 8190         # streaming mode wants just 8188, 8189
$root    = 'O:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable'
$tools   = 'C:\Users\Sander\AppData\Local\hermes\tools'
$scratch = 'C:\Users\Sander\AppData\Local\hermes\cache\scratch'

# NB: match the spawn bootstrap too. The IPC children are launched as
# `python -c "from multiprocessing.spawn import spawn_main; ..."` - their command line
# never contains ComfyUI/main.py, so killing only the holder leaves orphaned workers
# sitting on the ports with dead IPC mappings, and the new children cannot bind.
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -match 'ComfyUI/main\.py|ming_weightd|multiprocessing\.spawn|multiprocessing\.popen' } |
  ForEach-Object { "killing $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 4

if ($MODE -eq 'shared') {
  $env:MING_WORKER_PORTS = ($ports -join ',')
  $p = Start-Process -FilePath "$root\python_embeded\python.exe" `
    -ArgumentList '-s', "$tools\ming_weightd.py" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$scratch\weightd.log" `
    -RedirectStandardError  "$scratch\weightd_err.log"
  "weightd pid $($p.Id) serving $($ports -join ',')"
} else {
  foreach ($pt in $ports) {
    $p = Start-Process -FilePath "$root\python_embeded\python.exe" `
      -ArgumentList @('-s', 'ComfyUI/main.py', '--port', $pt) `
      -WorkingDirectory $root -WindowStyle Hidden -PassThru
    "worker $pt pid $($p.Id)"
  }
}

foreach ($pt in $ports) {
  for ($i = 0; $i -lt 150; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$pt/system_stats").StatusCode -eq 200) { "$pt ready after ${i}s"; break } } catch {}
    Start-Sleep -Seconds 1
  }
}
"gpu: $((nvidia-smi --query-gpu=memory.used --format=csv,noheader) -join '')"
