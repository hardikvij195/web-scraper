' Launches run-agent-loop.bat with no visible console window.
' Used by the "HVT Lead Finder Agent" scheduled task so the agent runs
' invisibly in the background from logon onwards.
' The task re-fires every 10 minutes (so a crashed loop comes back). This script exits
' at once after launching, so the task never counts as "running" and IgnoreNew does not
' help - 27 parallel loops were found on 2026-08-26. Only launch when none is alive.
'
' W182 (T1078, DELL 2026-10-08 19:00 -> 09:50 next day): a loop cmd.exe that is ALIVE but hung
' (git pull stalled on a half-dead network right after the agent's own watchdog os._exit(3))
' used to block every relaunch for ever - the CRM saw nothing for 14.8 h. Now a loop counts as
' healthy only while the agent keeps touching data\agent.alive (every successful CRM poll,
' normally every 5 s). If that file (or, when missing, data\agent.log) is older than 30 min
' AND agent.log is also silent for 30 min, the loop and its python children are killed and a
' fresh loop is launched. A machine that is merely offline keeps logging "cloud unreachable"
' every poll, so its log stays fresh and it is NOT restarted (same rule as agent.py's watchdog).
Option Explicit
Const STALE_MIN = 30
Dim sh, fso, root, wmi, procs, p, loopPids, aliveFile, logFile, aliveAge, logAge, killed
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set wmi = GetObject("winmgmts:\\.\root\cimv2")

loopPids = ""
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'cmd.exe'")
For Each p In procs
  If Not IsNull(p.CommandLine) Then
    If InStr(1, p.CommandLine, "run-agent-loop.bat", vbTextCompare) > 0 Then loopPids = loopPids & p.ProcessId & ","
  End If
Next

If Len(loopPids) > 0 Then
  aliveFile = root & "\data\agent.alive"
  logFile = root & "\data\agent.log"
  aliveAge = -1
  logAge = -1
  If fso.FileExists(aliveFile) Then aliveAge = DateDiff("n", fso.GetFile(aliveFile).DateLastModified, Now)
  If fso.FileExists(logFile) Then logAge = DateDiff("n", fso.GetFile(logFile).DateLastModified, Now)
  ' Fresh loop: the agent polled recently, or the loop is still writing its log (git/pip step,
  ' first install, offline machine). Never launch a second loop next to it.
  If aliveAge >= 0 And aliveAge < STALE_MIN Then WScript.Quit 0
  If logAge < 0 Or logAge < STALE_MIN Then WScript.Quit 0
  ' Stale: kill the loop(s), every python agent, then fall through and relaunch.
  killed = KillStale(loopPids)
  AppendLog logFile, "[" & Now & "] autostart: loop stale (alive " & AgeText(aliveAge) & " ago, log " & logAge & "m ago) - killed PID " & killed & " and relaunched"
End If

sh.CurrentDirectory = root
sh.Run """" & root & "\run-agent-loop.bat""", 0, False

Function AgeText(mins)
  If mins < 0 Then
    AgeText = "never"
  Else
    AgeText = mins & "m"
  End If
End Function

Function KillStale(pidList)
  ' Children first (python -m webscraper agent, plus anything else the loop spawned), then the
  ' cmd.exe loops themselves. Returns the comma-joined PIDs that were terminated.
  Dim ids, i, q, child, out
  out = ""
  ids = Split(pidList, ",")
  For i = 0 To UBound(ids)
    If Len(ids(i)) > 0 Then
      Set q = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ParentProcessId = " & ids(i))
      For Each child In q
        On Error Resume Next
        child.Terminate
        On Error GoTo 0
        out = out & child.ProcessId & ","
      Next
    End If
  Next
  Set q = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'python%'")
  For Each child In q
    If Not IsNull(child.CommandLine) Then
      If InStr(1, child.CommandLine, "webscraper agent", vbTextCompare) > 0 Then
        On Error Resume Next
        child.Terminate
        On Error GoTo 0
        out = out & child.ProcessId & ","
      End If
    End If
  Next
  For i = 0 To UBound(ids)
    If Len(ids(i)) > 0 Then
      Set q = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId = " & ids(i))
      For Each child In q
        On Error Resume Next
        child.Terminate
        On Error GoTo 0
        out = out & child.ProcessId & ","
      Next
    End If
  Next
  If Len(out) > 0 Then out = Left(out, Len(out) - 1)
  KillStale = out
End Function

Sub AppendLog(path, line)
  Dim f
  On Error Resume Next
  Set f = fso.OpenTextFile(path, 8, True)
  f.WriteLine line
  f.Close
  On Error GoTo 0
End Sub
