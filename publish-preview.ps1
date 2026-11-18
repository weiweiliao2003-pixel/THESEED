param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory, ParameterSetName='Publish')]
    [Parameter(Mandatory, ParameterSetName='Verify')][string]$Bundle,
    [Parameter(Mandatory, ParameterSetName='Publish')]
    [Parameter(Mandatory, ParameterSetName='Verify')][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [Parameter(ParameterSetName='Publish')][string]$DailyWorkspace,
    [Parameter(ParameterSetName='Publish')][switch]$AdoptPreparedCopy,
    [Parameter(Mandatory, ParameterSetName='Verify')][switch]$VerifyOnly,
    [Parameter(Mandatory, ParameterSetName='Prepare')][string]$PrepareWorkspaceFrom,
    [Parameter(Mandatory, ParameterSetName='Rollback')][string]$RestoreShortcut
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$primary = Join-Path $root 'SEED.lnk'
$shell = New-Object -ComObject WScript.Shell
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$private = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'THESEED\workspace-recovery'

function Get-Workspace($link) {
    $arg = [regex]::Match($link.Arguments, '(?i)(?:^|\s)--data-root(?:=|\s+)(?:"([^"]+)"|([^\s"]+))')
    if ($arg.Success) { return [IO.Path]::GetFullPath(($arg.Groups[1].Value + $arg.Groups[2].Value)) }
    if ($link.Arguments -match '--data-root') { throw 'Cannot resolve the shortcut data-root argument.' }
    return Join-Path (Split-Path -Parent $link.TargetPath) 'Data'
}

function Get-Mode($link) {
    if ($link.Arguments -match '(?:^|\s)--continuous-workspace(?:\s|$)') { return 'continuous' }
    if ($link.Arguments -match '(?:^|\s)--isolated-workspace(?:\s|$)') { return 'isolated' }
    throw 'Shortcut has no verifiable workspace mode.'
}

function Get-Build($exe) {
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'SEED executable is unavailable.' }
    $info = Join-Path (Split-Path -Parent $exe) '_internal\seed\build-info.json'
    $value = Get-Content -LiteralPath $info -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($value.commit -cnotmatch '^[0-9a-f]{40}$' -or $value.channel -notin @('candidate','accepted')) {
        throw 'Packaged build metadata is unavailable.'
    }
    return $value
}

function Close-Seed([string]$executable) {
    foreach ($instance in @(Get-Process -Name SEED -ErrorAction SilentlyContinue | Where-Object Path -eq $executable)) {
        if (!$instance.CloseMainWindow() -or !$instance.WaitForExit(12000)) { throw 'Save your work and close the SEED window before retrying.' }
    }
}

function Wait-Seed($link, $build, [datetime]$since) {
    $workspace = Get-Workspace $link
    $mode = Get-Mode $link
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        $window = Get-Process -Name SEED -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $link.TargetPath -and $_.MainWindowTitle -eq 'THESEED' -and $_.Responding } |
            Select-Object -First 1
        $proof = Join-Path $workspace '.theseed-launch.json'
        if ($window -and (Test-Path -LiteralPath $proof) -and (Get-Item -LiteralPath $proof).LastWriteTimeUtc -ge $since) {
            $info = Get-Content -LiteralPath $proof -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($info.pid -eq $window.Id -and $info.executable -eq $link.TargetPath -and $info.workspace -eq $workspace -and
                $info.commit -ceq $build.commit -and $info.channel -eq $build.channel -and $info.mode -eq $mode) {
                return $info
            }
            throw 'Running executable reports a mismatched commit, workspace or process.'
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Expected SEED window and launch metadata were not available.'
}

function New-Link($path, $executable, $workspace, $mode) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $executable
    $link.WorkingDirectory = Split-Path -Parent $executable
    $link.IconLocation = "$executable,0"
    $link.Arguments = "--$mode-workspace --data-root `"$workspace`""
    $link.Save()
    return $link
}

function Set-Primary([string]$from) {
    $stage = Join-Path $root ('.seed-switch-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        [IO.File]::Copy($from, $stage)
        [IO.File]::Replace($stage, $primary, [NullString]::Value)
        if ((Get-FileHash -LiteralPath $primary).Hash -ne (Get-FileHash -LiteralPath $from).Hash) {
            throw 'Shortcut replacement did not verify.'
        }
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage }
    }
}

function Invoke-Continuity([string[]]$operation) {
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $python -m seed.continuity @operation
        if ($LASTEXITCODE -ne 0) { throw 'Workspace continuity validation failed.' }
    } finally { Pop-Location }
}

function Get-PrivateRun {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if (!(Test-Path -LiteralPath $private -PathType Container)) {
        New-Item -Path $private -ItemType Directory -Force | Out-Null
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetOwner($sid)
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
        Set-Acl -LiteralPath $private -AclObject $acl
    }
    Assert-Private $private
    return (New-Item -Path (Join-Path $private ([guid]::NewGuid().ToString('N'))) -ItemType Directory).FullName
}

function Assert-Private([string]$directory) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $item = Get-Item -LiteralPath $directory -Force
    $acl = Get-Acl -LiteralPath $directory
    $allowed = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) |
        Where-Object AccessControlType -eq 'Allow' | Select-Object -ExpandProperty IdentityReference)
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint -or
        $acl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $sid -or !$allowed.Count -or
        @($allowed | Where-Object { $_ -ne $sid }).Count) { throw 'Recovery directory must be accessible only to this Windows account.' }
}

function Invoke-Switch($candidate, $build, $previous) {
    if ((Get-FileHash -LiteralPath $primary).Hash -ne (Get-FileHash -LiteralPath $previous).Hash) {
        throw 'Daily shortcut changed during validation.'
    }
    $start = [DateTime]::UtcNow
    try {
        Set-Primary $candidate.FullName
        Start-Process -FilePath $primary
        Wait-Seed $candidate $build $start
    } catch {
        Set-Primary $previous
        Close-Seed $candidate.TargetPath
        Start-Process -FilePath $primary
        throw
    }
}

if ($PrepareWorkspaceFrom) {
    $sourceData = (Resolve-Path -LiteralPath $PrepareWorkspaceFrom).Path
    $recovery = Get-PrivateRun
    $captured = Join-Path $recovery 'Recovery'
    $prepared = Join-Path $recovery 'Prepared'
    Invoke-Continuity @('snapshot', $sourceData, $captured) | Out-Null
    $verified = Invoke-Continuity @('prepare', $captured, $prepared) | ConvertFrom-Json
    return [pscustomobject]@{Recovery=$captured;Prepared=$prepared;Schema=$verified.schema;Credentials=$verified.credentials}
}

$hasDailyShortcut = Test-Path -LiteralPath $primary -PathType Leaf
if (!$VerifyOnly) {
    if (!$hasDailyShortcut) { throw 'Existing daily shortcut is required. Use isolated verification for a fresh install.' }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Build Python is required for local recovery.' }
    $initialShortcut = (Get-FileHash -LiteralPath $primary).Hash
    $original = $shell.CreateShortcut($primary)
    $originalWorkspace = Get-Workspace $original
    $originalMode = Get-Mode $original
    $null = Get-Build $original.TargetPath
    if (!(Test-Path -LiteralPath (Join-Path $originalWorkspace 'seed.sqlite3') -PathType Leaf)) {
        throw 'Previous database is missing. The shortcut was preserved.'
    }
}

if ($RestoreShortcut) {
    $previous = (Resolve-Path -LiteralPath $RestoreShortcut).Path
    $restore = $shell.CreateShortcut($previous)
    $restoreBuild = Get-Build $restore.TargetPath
    $restoreWorkspace = Get-Workspace $restore
    $restoreMode = Get-Mode $restore
    if (!(Test-Path -LiteralPath (Join-Path $restoreWorkspace 'seed.sqlite3') -PathType Leaf)) { throw 'Rollback database is missing.' }
    if ($restoreMode -eq 'continuous') { Invoke-Continuity @('check', $restoreWorkspace) }
    $recovery = Get-PrivateRun
    $saved = Join-Path $recovery 'SEED-Before-Rollback.lnk'
    [IO.File]::Copy($primary, $saved)
    Close-Seed $original.TargetPath
    try {
        Invoke-Continuity @('snapshot', $originalWorkspace, (Join-Path $recovery 'Data')) | Out-Null
        Invoke-Switch $restore $restoreBuild $saved
    } catch {
        if ((Get-FileHash -LiteralPath $primary).Hash -eq (Get-FileHash -LiteralPath $saved).Hash -and
            !(Get-Process -Name SEED -ErrorAction SilentlyContinue | Where-Object Path -eq $original.TargetPath)) {
            Start-Process -FilePath $primary
        }
        throw
    }
    return
}

if (!$VerifyOnly) {
    if ($originalMode -eq 'continuous') {
        if ($AdoptPreparedCopy -or ($DailyWorkspace -and [IO.Path]::GetFullPath($DailyWorkspace) -ne $originalWorkspace)) {
            throw 'Updates must reuse the current daily workspace.'
        }
        $DailyWorkspace = $originalWorkspace
        Invoke-Continuity @('check', $DailyWorkspace)
    } else {
        if (!$DailyWorkspace -or !$AdoptPreparedCopy) { throw 'First update requires explicit adoption of a verified private copy.' }
        $DailyWorkspace = (Resolve-Path -LiteralPath $DailyWorkspace).Path
        Assert-Private $private
        if (!$DailyWorkspace.StartsWith($private + '\', [StringComparison]::OrdinalIgnoreCase) -or $DailyWorkspace -eq $originalWorkspace) {
            throw 'Adoption requires a detached copy in the private recovery directory.'
        }
        Assert-Private $DailyWorkspace
    }
}

$source = (Resolve-Path -LiteralPath $Bundle).Path
$build = Get-Build (Join-Path $source 'SEED.exe')
if ($build.commit -cne $Commit -or $build.channel -ne 'candidate') { throw 'Packaged build does not match the candidate SHA.' }
foreach ($path in @('speech\whisper.cpp\whisper-cli.exe','speech\whisper.cpp\ggml-base-q5_1.bin')) {
    if (!(Test-Path -LiteralPath (Join-Path $source $path) -PathType Leaf)) { throw "Candidate component missing: $path" }
}
if (Test-Path -LiteralPath (Join-Path $source 'Data')) { throw 'Candidate bundle must not contain workspace data.' }

$release = Join-Path $root ('dist\theseed-preview-' + $Commit.Substring(0, 12))
if ($VerifyOnly) {
    $release = Join-Path $root ('dist\theseed-smoke-' + $Commit.Substring(0, 12) + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
}
if (Test-Path -LiteralPath $release) {
    $release += '-retry-' + [guid]::NewGuid().ToString('N').Substring(0,8)
}
New-Item -Path $release -ItemType Directory -Force | Out-Null
$installed = Join-Path $release 'SEED'
Copy-Item -LiteralPath $source -Destination $installed -Recurse
$executable = Join-Path $installed 'SEED.exe'
foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
    $copy = Join-Path $installed $file.FullName.Substring($source.Length + 1)
    if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $copy).Hash) { throw 'Candidate copy failed verification.' }
}

$smoke = (New-Item -Path (Join-Path $release 'Smoke') -ItemType Directory).FullName
Set-Content -LiteralPath (Join-Path $smoke '.theseed-preview') -Value 'theseed-preview/1' -Encoding ASCII
$smokeLink = New-Link (Join-Path $release 'Verify-Isolated.lnk') $executable $smoke 'isolated'
$smokeStart = [DateTime]::UtcNow
Start-Process -FilePath $executable -WorkingDirectory $installed -WindowStyle Hidden -ArgumentList $smokeLink.Arguments
try { $proof = Wait-Seed $smokeLink $build $smokeStart } finally { Close-Seed $executable }
if ($VerifyOnly) { return $proof }

$rollback = (New-Item -Path (Join-Path $release 'rollback') -ItemType Directory).FullName
$previousLink = Join-Path $rollback 'SEED-Previous.lnk'
[IO.File]::Copy($primary, $previousLink)
if ((Get-FileHash -LiteralPath $previousLink).Hash -ne $initialShortcut) { throw 'Daily shortcut changed during validation.' }
$recovery = Get-PrivateRun
Close-Seed $original.TargetPath
try {
    Invoke-Continuity @('snapshot', $originalWorkspace, (Join-Path $recovery 'previous')) | Out-Null
    if ($DailyWorkspace -ne $originalWorkspace) {
        Invoke-Continuity @('snapshot', $DailyWorkspace, (Join-Path $recovery 'daily-before')) | Out-Null
    }
    if ($AdoptPreparedCopy) {
        Invoke-Continuity @('adopt', $DailyWorkspace)
    }
    Invoke-Continuity @('check', $DailyWorkspace)
    $candidate = New-Link (Join-Path $release 'SEED-Daily.lnk') $executable $DailyWorkspace 'continuous'
    Invoke-Switch $candidate $build $previousLink
} catch {
    if ((Get-FileHash -LiteralPath $primary).Hash -eq (Get-FileHash -LiteralPath $previousLink).Hash -and
        !(Get-Process -Name SEED -ErrorAction SilentlyContinue | Where-Object Path -eq $original.TargetPath)) {
        Start-Process -FilePath $primary
    }
    throw
}
