' Tatami Room's Restart: holds WSL up for 45 seconds while the app is away (it holds WSL itself again once
' it's back), with no window. The app starts this through wscript, which outlives it. A wsl.exe the app
' started itself would end with the app, or, started detached, would have no console, so Windows would
' open a terminal window on screen for it.
If WScript.Arguments.Count <> 1 Then WScript.Quit
distro = WScript.Arguments(0)
Set re = New RegExp
re.Pattern = "^[A-Za-z0-9._-]{1,64}$"   ' a distro's name, as WSL allows them (wsl.exe takes it unquoted)
If Not re.Test(distro) Then WScript.Quit
CreateObject("WScript.Shell").Run "wsl.exe -d " & distro & " -- sleep 45", 0, False
