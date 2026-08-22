' Launches run-agent-loop.bat with no visible console window.
' Used by the "HVT Lead Finder Agent" scheduled task so the agent runs
' invisibly in the background from logon onwards.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
sh.Run """" & root & "\run-agent-loop.bat""", 0, False
