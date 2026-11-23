$bridge = Join-Path $env:LOCALAPPDATA 'WindowsConsole\bridge.ps1'
$session = (Get-Process -Id $PID).SessionId
Get-CimInstance Win32_Process -Filter "Name='powershell.exe' AND SessionId=$session" |
    Where-Object { $_.CommandLine -and $_.CommandLine.TrimEnd('"').EndsWith($bridge,[StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction Stop }
