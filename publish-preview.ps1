param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory, ParameterSetName='Publish')][string]$Bundle,
    [Parameter(Mandatory, ParameterSetName='Publish')][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [Parameter(Mandatory, ParameterSetName='Rollback')][string]$RestoreShortcut
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$primary = Join-Path $root 'SEED.lnk'
$shell = New-Object -ComObject WScript.Shell
$original = $shell.CreateShortcut($primary)
if (!(Test-Path -LiteralPath $original.TargetPath -PathType Leaf)) { throw 'Existing SEED shortcut target is unavailable.' }

function Get-Workspace($link) {
    $arg = [regex]::Match($link.Arguments, '(?i)(?:^|\s)--data-root(?:=|\s+)(?:"([^"]+)"|([^\s"]+))')
    if ($arg.Success) { return [IO.Path]::GetFullPath(($arg.Groups[1].Value + $arg.Groups[2].Value)) }
    if ($link.Arguments -match '--data-root') { throw 'Cannot resolve the previous data-root argument.' }
    return Join-Path (Split-Path -Parent $link.TargetPath) 'Data'
}

function Close-Seed([string]$executable) {
    foreach ($instance in @(Get-Process -Name SEED -ErrorAction SilentlyContinue | Where-Object Path -eq $executable)) {
        if (!$instance.CloseMainWindow() -or !$instance.WaitForExit(12000)) { throw 'Close the running SEED window and save any pending work before retrying.' }
    }
}

function Wait-Seed([string]$executable, [string]$workspace, [datetime]$since) {
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $window = Get-Process -Name SEED -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $executable -and $_.MainWindowTitle -eq 'THESEED' -and $_.Responding } |
            Select-Object -First 1
        if ($window) {
            if (!$workspace) { return $window }
            $proof = Join-Path $workspace '.theseed-launch.json'
            if ((Test-Path -LiteralPath $proof) -and (Get-Item -LiteralPath $proof).LastWriteTimeUtc -ge $since) {
                $info = Get-Content -LiteralPath $proof -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($info.pid -eq $window.Id -and $info.executable -eq $executable -and $info.workspace -eq $workspace -and
                    $info.commit -ceq $Commit -and $info.channel -eq 'candidate' -and $info.mode -eq 'isolated') { return $info }
                throw 'The running executable reported a mismatched commit, workspace or process.'
            }
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The expected SEED window and launch metadata were not available.'
}

function Set-Primary([string]$from) {
    $stage = Join-Path $root ('.seed-switch-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        [IO.File]::Copy($from, $stage)
        [IO.File]::Replace($stage, $primary, [NullString]::Value)
        if ((Get-FileHash -LiteralPath $primary).Hash -ne (Get-FileHash -LiteralPath $from).Hash) { throw 'Shortcut replacement did not verify.' }
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage }
    }
}

if ($RestoreShortcut) {
    $previous = (Resolve-Path -LiteralPath $RestoreShortcut).Path
    $restore = $shell.CreateShortcut($previous)
    if (!(Test-Path -LiteralPath $restore.TargetPath -PathType Leaf) -or
        !(Test-Path -LiteralPath (Join-Path (Get-Workspace $restore) 'seed.sqlite3') -PathType Leaf)) {
        throw 'The rollback executable or its original database is unavailable.'
    }
    Close-Seed $original.TargetPath
    Set-Primary $previous
    Start-Process -FilePath $primary
    Wait-Seed $restore.TargetPath '' ([DateTime]::UtcNow) | Select-Object Id,Path,MainWindowTitle
    return
}

$source = (Resolve-Path -LiteralPath $Bundle).Path
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The build Python is required for the local workspace backup.' }
$metadata = Get-Content -LiteralPath (Join-Path $source '_internal\seed\build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($metadata.commit -cne $Commit -or $metadata.channel -ne 'candidate') { throw 'Packaged build metadata does not match the requested GitHub commit.' }
foreach ($path in @('SEED.exe','speech\whisper.cpp\whisper-cli.exe','speech\whisper.cpp\ggml-base-q5_1.bin')) {
    if (!(Test-Path -LiteralPath (Join-Path $source $path) -PathType Leaf)) { throw "Candidate component missing: $path" }
}
if (Test-Path -LiteralPath (Join-Path $source 'Data')) { throw 'Candidate bundle must not contain workspace data.' }

$release = Join-Path $root ('dist\theseed-preview-' + $Commit.Substring(0, 12))
if (Test-Path -LiteralPath $release) { throw 'Candidate release directory already exists. Its files have been preserved.' }
New-Item -Path $release -ItemType Directory | Out-Null
Copy-Item -LiteralPath $source -Destination (Join-Path $release 'SEED') -Recurse
$installed = Join-Path $release 'SEED'
$executable = Join-Path $installed 'SEED.exe'
foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
    $copy = Join-Path $installed $file.FullName.Substring($source.Length + 1)
    if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $copy).Hash) { throw 'Candidate copy failed verification.' }
}

$rollback = New-Item -Path (Join-Path $release 'rollback') -ItemType Directory
$previousLink = Join-Path $rollback.FullName 'SEED-Previous.lnk'
[IO.File]::Copy($primary, $previousLink)
$preview = New-Item -Path (Join-Path $release 'Data') -ItemType Directory
Set-Content -LiteralPath (Join-Path $preview.FullName '.theseed-preview') -Value 'theseed-preview/1' -Encoding ASCII

$staged = Join-Path $release '.seed-candidate.lnk'
$candidate = $shell.CreateShortcut($staged)
$candidate.TargetPath = $executable
$candidate.WorkingDirectory = $installed
$candidate.IconLocation = "$executable,0"
$candidate.Arguments = "--isolated-workspace --data-root `"$($preview.FullName)`""
$candidate.Save()

try {
    $smokeStart = [DateTime]::UtcNow
    Start-Process -FilePath $executable -WorkingDirectory $installed -WindowStyle Hidden `
        -ArgumentList "--isolated-workspace --data-root `"$($preview.FullName)`""
    try { $null = Wait-Seed $executable $preview.FullName $smokeStart } finally { Close-Seed $executable }

    Close-Seed $original.TargetPath
    $switched = $false
    try {
        $backup = Join-Path $rollback.FullName 'workspace-before-preview.zip'
        Push-Location -LiteralPath $PSScriptRoot
        try {
            @'
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from seed.backup import backup_workspace, open_backup

class ReadOnlyWorkspace:
    def __init__(self, root):
        self.root = root.resolve()
        self.path = self.root / "seed.sqlite3"

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            yield db

backup = Path(sys.argv[2])
backup_workspace(ReadOnlyWorkspace(Path(sys.argv[1])), backup)
with open_backup(backup):
    pass
'@ | & $python - (Get-Workspace $original) $backup
            if ($LASTEXITCODE -ne 0) { throw 'Local workspace backup verification failed.' }
        } finally { Pop-Location }
        if ((Get-FileHash -LiteralPath $primary).Hash -ne (Get-FileHash -LiteralPath $previousLink).Hash) {
            throw 'The primary shortcut changed during candidate verification.'
        }
        $switched = $true
        Set-Primary $staged
        $start = [DateTime]::UtcNow
        Start-Process -FilePath $primary
        Wait-Seed $executable $preview.FullName $start
    } catch {
        if ($switched) { Set-Primary $previousLink }
        if ($switched) { Close-Seed $executable }
        Start-Process -FilePath $primary
        throw
    }
} finally {
    if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged }
}
