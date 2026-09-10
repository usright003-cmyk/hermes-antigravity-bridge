' Silent background launcher for Hermes-Antigravity Bridge
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = ScriptDir
Set WshProcessEnv = WshShell.Environment("Process")
WshProcessEnv("PYTHONPATH") = ScriptDir & "\src"
UserProfile = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
ConfigFile = UserProfile & "\.config\hermes-antigravity-bridge\config.toml"
PythonCmd = "python -m hermes_antigravity_bridge.cli --config " & Chr(34) & ConfigFile & Chr(34) & " serve"
WshShell.Run PythonCmd, 0, False
