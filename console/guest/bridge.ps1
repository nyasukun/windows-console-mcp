# User-session text bridge. Only the fixed operations below are accepted on COM1.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace ConsoleBridge -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern uint GetClipboardSequenceNumber();

// INPUT is a DWORD followed by an aligned union. The largest union member
// is MOUSEINPUT (32 bytes on Win64, 24 bytes on Win32).
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
private struct KeyInput {
    public ushort vk, scan;
    public uint flags, time;
    public System.UIntPtr extra;
}
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
private struct MouseInput {
    public int x, y;
    public uint data, flags, time;
    public System.UIntPtr extra;
}
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Explicit)]
private struct InputData {
    [System.Runtime.InteropServices.FieldOffset(0)] public KeyInput key;
    [System.Runtime.InteropServices.FieldOffset(0)] public MouseInput mouse;
}
private struct Input { public uint type; public InputData data; }
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true)]
private static extern uint SendInput(uint count, Input[] events, int size);

public static void TypeUnicode(string text) {
    if (text.Length == 0) return;
    Input[] events = new Input[text.Length * 2];
    for (int i = 0; i < events.Length; i++) {
        events[i].type = 1;
        events[i].data.key.scan = text[i / 2];
        events[i].data.key.flags = (uint)(4 | (i % 2 == 1 ? 2 : 0));
    }
    if (SendInput((uint)events.Length, events, System.Runtime.InteropServices.Marshal.SizeOf(typeof(Input))) != events.Length)
        throw new System.InvalidOperationException("Windows could not insert the committed text.");
}
'@
$port = New-Object System.IO.Ports.SerialPort 'COM1',115200,'None',8,'One'
$port.Handshake = 'None'
$port.WriteTimeout = 15000
try {
    $port.Open()
    while ($true) {
        $line = $port.ReadLine()
        $reply = @{ ok = $false }
        try {
            $req = $line | ConvertFrom-Json -ErrorAction Stop
            $reply.id = $req.id
            switch ($req.op) {
                'ping' { }
                'type' {
                    $bytes = [Convert]::FromBase64String($req.data)
                    if ($bytes.Length -gt 65536) { throw 'Text exceeds 64 KiB' }
                    [ConsoleBridge.Native]::TypeUnicode([Text.Encoding]::UTF8.GetString($bytes))
                }
                'paste' {
                    $bytes = [Convert]::FromBase64String($req.data)
                    if ($bytes.Length -gt 65536) { throw 'Text exceeds 64 KiB' }
                    $text = [Text.Encoding]::UTF8.GetString($bytes)
                    if ($text.Length) {
                        [Windows.Forms.Clipboard]::SetText($text.Replace("`r`n","`n").Replace("`n","`r`n"))
                        [Windows.Forms.SendKeys]::SendWait('^v')
                    }
                }
                'copy' {
                    $before = [ConsoleBridge.Native]::GetClipboardSequenceNumber()
                    [Windows.Forms.SendKeys]::SendWait('^c')
                    for ($i=0; $i -lt 30 -and [ConsoleBridge.Native]::GetClipboardSequenceNumber() -eq $before; $i++) { Start-Sleep -Milliseconds 40 }
                    if ([ConsoleBridge.Native]::GetClipboardSequenceNumber() -eq $before) { throw 'Select text in Windows first' }
                    $bytes = [Text.Encoding]::UTF8.GetBytes([Windows.Forms.Clipboard]::GetText())
                    if ($bytes.Length -gt 65536) { throw 'Text exceeds 64 KiB' }
                    $reply.data = [Convert]::ToBase64String($bytes)
                }
                default { throw 'Unsupported operation' }
            }
            $reply.ok = $true
        } catch { $reply.error = $_.Exception.Message }
        $port.WriteLine(($reply | ConvertTo-Json -Compress))
    }
} finally { $port.Dispose() }
