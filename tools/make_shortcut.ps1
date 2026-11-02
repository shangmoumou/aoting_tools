# Create shortcuts through the windowless Windows Script Host launcher.
# Keep the Bat parameter for compatibility with create_shortcut.bat.

param(
    [Parameter(Mandatory = $true)][string]$Bat,
    [Parameter(Mandatory = $true)][string]$Ico,
    [string]$Name = 'AOTING Tools Portal.lnk'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Bat)) {
    Write-Host "[ERROR] launcher not found: $Bat"
    exit 1
}

$sh  = New-Object -ComObject WScript.Shell
$dir = [IO.Path]::GetDirectoryName($Bat)
$cmd = [IO.Path]::Combine($env:SystemRoot, 'System32', 'wscript.exe')

# '/c ""bat""' -- doubled quotes survive paths with spaces or parentheses.
$launcher = [IO.Path]::Combine($dir, 'start_aoting_tools.vbs')
if (-not (Test-Path -LiteralPath $launcher)) { throw "Launcher not found: $launcher" }
# wscript is a GUI executable, so the shortcut never opens a console.
$arg = '//nologo "' + $launcher + '"'

# GetFolderPath honors a redirected Desktop (OneDrive etc.), unlike
# $env:USERPROFILE\Desktop which is often wrong on managed machines.
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = [IO.Path]::Combine($env:USERPROFILE, 'Desktop') }

$targets = @(
    [IO.Path]::Combine($desktop, $Name),
    [IO.Path]::Combine($dir, $Name)
)

foreach ($p in $targets) {
    $s = $sh.CreateShortcut($p)
    $s.TargetPath       = $cmd   # must be an .exe, or pinning is blocked
    $s.Arguments        = $arg
    $s.WorkingDirectory = $dir   # the .bat resolves relatives paths
    $s.IconLocation     = $Ico
    $s.Description      = 'AOTING Tools Portal'
    $s.Save()
    Write-Host ("  created: " + $p)
}

Write-Host ("  target : " + $cmd + " " + $arg)
