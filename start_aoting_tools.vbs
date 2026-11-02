Option Explicit
' Run the existing bootstrap hidden; keep diagnostics outside the console.
Dim shell, fso, root, logDir, logFile, command, rc, processEnv, quote
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
' Reuse a running portal instead of killing the server owned by an earlier launch.
If PortalRunning() Then
    shell.Run "http://127.0.0.1:8899", 1, False
    WScript.Quit 0
End If
logDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\aoting_tools"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
' Each launch owns its log: a running server keeps its redirected file open.
' GetTempName supplies an independent name, avoiding collisions on repeated clicks.
logFile = logDir & "\launcher-" & fso.GetTempName & ".log"
Set processEnv = shell.Environment("PROCESS")
processEnv("AOTING_HIDDEN_START") = "1"
' Run waits without displaying a console; stdin is closed to avoid hidden prompts.
quote = Chr(34)
command = "cmd.exe /d /s /c " & quote & quote & root & _
    "\start_aoting_tools.bat" & quote & " > " & quote & logFile & quote & _
    " 2>&1 < nul" & quote
rc = shell.Run(command, 0, True)
If rc <> 0 Then
    ' Two simultaneous launches can race to bind the port; reuse the winner.
    If PortalRunning() Then
        shell.Run "http://127.0.0.1:8899", 1, False
        WScript.Quit 0
    End If
    shell.Popup "Toolbox exited with code " & rc & ". See: " & logFile & vbCrLf & _
        "Run start_aoting_tools.bat for interactive setup or diagnostics.", _
        0, "AOTING Tools", 16
End If

Function PortalRunning()
    ' Probe the portal-specific endpoint, not just an arbitrary open TCP port.
    Dim request
    PortalRunning = False
    On Error Resume Next
    Set request = CreateObject("WinHttp.WinHttpRequest.5.1")
    request.SetTimeouts 1000, 1000, 1000, 1000
    request.Open "GET", "http://127.0.0.1:8899/__portal_ping", False
    request.Send
    If Err.Number = 0 Then
        PortalRunning = (request.Status = 200 And InStr(request.ResponseText, "ok") > 0)
    End If
    On Error GoTo 0
End Function
