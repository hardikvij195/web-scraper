' Launches run-agent-loop.bat with no visible console window.
' Used by the "HVT Lead Finder Agent" scheduled task so the agent runs
' invisibly in the background from logon onwards.
' The task re-fires every 10 minutes (so a crashed loop comes back). This script exits
' at once after launching, so the task never counts as "running" and IgnoreNew does not
' help - 27 parallel loops were found on 2026-08-26. Only launch when none is alive.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name = 'cmd.exe'")
For Each p In procs
  If Not IsNull(p.CommandLine) Then
    If InStr(1, p.CommandLine, "run-agent-loop.bat", vbTextCompare) > 0 Then WScript.Quit 0
  End If
Next
sh.CurrentDirectory = root
sh.Run """" & root & "\run-agent-loop.bat""", 0, False
