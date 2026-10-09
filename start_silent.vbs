Option Explicit
Dim shell, files, directory
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
directory = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = directory
shell.Run "pythonw.exe """ & directory & "\run.py""", 0, False
