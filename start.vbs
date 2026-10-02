Option Explicit
Dim shell, fso, folder, launcher, command, result, runtime, argument, checkMode, probeStarted, probeCount
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(folder, "launcher.pyw")
shell.CurrentDirectory = folder
checkMode = False
probeStarted = Timer
probeCount = 0
For Each argument In WScript.Arguments
    If argument = "--check" Then checkMode = True
Next

runtime = ""
If Not RuntimeWorks(fso.BuildPath(folder, "runtime\python.exe"), runtime) Then
    runtime = FindOnPath("python.exe")
    If runtime = "" Then runtime = FindOnPath("python3.exe")
    If runtime = "" Then runtime = FindFromLauncher()
End If

If runtime = "" Then
    If Not checkMode Then MsgBox "Python 3.9 or later could not be found. Please use debug.bat to inspect the environment.", 16, ChrW(&H66DC) & ChrW(&H6838)
    WScript.Quit 1
End If
If checkMode Then
    WScript.Echo runtime
    WScript.Quit 0
End If

command = QuoteArg(runtime) & " " & QuoteArg(launcher)
For Each argument In WScript.Arguments
    If argument = "--no-browser" Or argument = "--no-dialog" Then
        command = command & " " & argument
    End If
Next
result = shell.Run(command, 0, True)
WScript.Quit result

Function FindOnPath(executableName)
    Dim entries, entry, directory, candidate, resolved
    FindOnPath = ""
    entries = Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
    For Each entry In entries
        directory = Trim(CStr(entry))
        If Len(directory) >= 2 Then
            If Left(directory, 1) = Chr(34) And Right(directory, 1) = Chr(34) Then directory = Mid(directory, 2, Len(directory) - 2)
        End If
        If directory <> "" And Not IsWindowsStoreAlias(directory) Then
            candidate = fso.BuildPath(directory, executableName)
            If fso.FileExists(candidate) Then
                resolved = ""
                If RuntimeWorks(candidate, resolved) Then
                    FindOnPath = resolved
                    Exit Function
                End If
            End If
        End If
    Next
End Function

Function FindFromLauncher()
    Dim entries, entry, directory, candidate, paths, executablePath, resolved
    FindFromLauncher = ""
    entries = Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
    For Each entry In entries
        directory = Trim(CStr(entry))
        If Len(directory) >= 2 Then
            If Left(directory, 1) = Chr(34) And Right(directory, 1) = Chr(34) Then directory = Mid(directory, 2, Len(directory) - 2)
        End If
        If directory <> "" And Not IsWindowsStoreAlias(directory) Then
            candidate = fso.BuildPath(directory, "py.exe")
            If fso.FileExists(candidate) Then
                paths = LauncherPythonPaths(candidate)
                For Each executablePath In paths
                    If RuntimeWorks(executablePath, resolved) Then
                        FindFromLauncher = resolved
                        Exit Function
                    End If
                Next
            End If
        End If
    Next
End Function

Function LauncherPythonPaths(pyLauncher)
    Dim process, attempt, waitAttempts, output, lines, line, position, found, paths(), count, path
    count = 0
    ReDim paths(0)
    waitAttempts = ProbeWaitAttempts()
    If waitAttempts = 0 Or probeCount >= 8 Then
        LauncherPythonPaths = paths
        Exit Function
    End If
    probeCount = probeCount + 1
    On Error Resume Next
    Err.Clear
    Set process = shell.Exec(QuoteArg(pyLauncher) & " -0p")
    If Err.Number <> 0 Then
        LauncherPythonPaths = paths
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0
    For attempt = 1 To waitAttempts
        If process.Status <> 0 Then Exit For
        WScript.Sleep 50
    Next
    If process.Status = 0 Then
        On Error Resume Next
        process.Terminate
        On Error GoTo 0
        LauncherPythonPaths = paths
        Exit Function
    End If
    output = process.StdOut.ReadAll
    If Len(output) > 65536 Then
        LauncherPythonPaths = paths
        Exit Function
    End If
    lines = Split(output, vbCrLf)
    For Each line In lines
        found = False
        For position = 1 To Len(line) - 2
            If Mid(line, position + 1, 1) = ":" And Mid(line, position + 2, 1) = "\" Then
                If LCase(Mid(line, position, 1)) >= "a" And LCase(Mid(line, position, 1)) <= "z" Then
                    path = Trim(Mid(line, position))
                    If Right(LCase(path), 4) = ".exe" And Not IsWindowsStoreAlias(path) And fso.FileExists(path) Then
                        If count = 0 Then
                            paths(0) = path
                        Else
                            ReDim Preserve paths(count)
                            paths(count) = path
                        End If
                        count = count + 1
                    End If
                    found = True
                    Exit For
                End If
            End If
        Next
    Next
    LauncherPythonPaths = paths
End Function

Function RuntimeWorks(executablePath, resolvedExecutable)
    Dim process, attempt, waitAttempts, output, lines, versionParts, resolvedPath, major, minor, code
    RuntimeWorks = False
    resolvedExecutable = ""
    waitAttempts = ProbeWaitAttempts()
    If waitAttempts = 0 Or probeCount >= 8 Then Exit Function
    If IsWindowsStoreAlias(executablePath) Or Not fso.FileExists(executablePath) Then Exit Function
    probeCount = probeCount + 1
    code = "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(sys.executable)"
    On Error Resume Next
    Err.Clear
    Set process = shell.Exec(QuoteArg(executablePath) & " -c " & QuoteArg(code))
    If Err.Number <> 0 Then
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0
    For attempt = 1 To waitAttempts
        If process.Status <> 0 Then Exit For
        WScript.Sleep 50
    Next
    If process.Status = 0 Then
        On Error Resume Next
        process.Terminate
        On Error GoTo 0
        Exit Function
    End If
    If process.ExitCode <> 0 Then Exit Function
    output = process.StdOut.ReadAll
    If Len(output) > 4096 Then Exit Function
    lines = Split(Replace(output, vbCr, ""), vbLf)
    If UBound(lines) < 1 Then Exit Function
    versionParts = Split(Trim(lines(0)), ".")
    If UBound(versionParts) < 1 Then Exit Function
    If Not IsNumeric(versionParts(0)) Or Not IsNumeric(versionParts(1)) Then Exit Function
    major = CInt(versionParts(0))
    minor = CInt(versionParts(1))
    resolvedPath = Trim(lines(1))
    If major <> 3 Or minor < 9 Or resolvedPath = "" Then Exit Function
    If IsWindowsStoreAlias(resolvedPath) Or Not fso.FileExists(resolvedPath) Then Exit Function
    resolvedExecutable = resolvedPath
    RuntimeWorks = True
End Function

Function ProbeWaitAttempts()
    Dim elapsed, remaining
    elapsed = Timer - probeStarted
    If elapsed < 0 Then elapsed = elapsed + 86400
    remaining = 10000 - (elapsed * 1000)
    If remaining <= 0 Or probeCount >= 8 Then
        ProbeWaitAttempts = 0
    ElseIf remaining / 50 > 50 Then
        ProbeWaitAttempts = 50
    Else
        ProbeWaitAttempts = Int(remaining / 50)
        If ProbeWaitAttempts < 1 Then ProbeWaitAttempts = 1
    End If
End Function

Function IsWindowsStoreAlias(path)
    IsWindowsStoreAlias = (InStr(1, LCase(path), "\windowsapps\", vbTextCompare) > 0)
End Function

Function QuoteArg(value)
    QuoteArg = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
