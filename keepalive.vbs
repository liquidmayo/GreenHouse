' Hidden launcher for keepalive.bat (no console window flash).
Dim shell, here
Set shell = CreateObject("Wscript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run """" & here & "keepalive.bat""", 0, False
