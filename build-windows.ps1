param(
    [string]$OutputDirectory = 'dist\theseed-r8',
    [switch]$SkipArchive,
    [string]$WhisperArchive,
    [string]$WhisperModel
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$originalPath = $env:Path
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv and install requirements-build.txt first.'
}

Push-Location -LiteralPath $projectRoot
$metadataFolder = $null
try {
    $commit = $null
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue
    if ($git -and (Test-Path -LiteralPath (Join-Path $projectRoot '.git'))) {
        $commit = & $git.Source rev-parse --verify HEAD
        if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Cannot resolve build commit.' }
        $changes = & $git.Source status --porcelain --untracked-files=normal
        if ($LASTEXITCODE -ne 0 -or $changes) { throw 'Commit the build inputs before packaging a Git candidate.' }
    }
    if ([bool]$WhisperArchive -ne [bool]$WhisperModel) { throw 'Supply both pinned speech assets.' }
    if ($WhisperArchive) {
        $WhisperArchive = (Resolve-Path -LiteralPath $WhisperArchive).Path
        $WhisperModel = (Resolve-Path -LiteralPath $WhisperModel).Path
        if ((Get-FileHash -LiteralPath $WhisperArchive).Hash -ne 'B07EA0B1B4115A38E1A7B07DEBF581F0B77D999925F8ACB8F39D322B0BA0A822' -or
            (Get-FileHash -LiteralPath $WhisperModel).Hash -ne '422F1AE452ADE6F30A004D7E5C6A43195E4433BC370BF23FAC9CC591F01A8898') {
            throw 'Speech assets do not match the pinned Windows whisper.cpp v1.8.6 and ggml-base-q5_1 hashes.'
        }
    }
    # Restrict native dependency discovery to the build environment and Windows.
    $env:Path = "$(Split-Path -Parent $python);$env:SystemRoot\System32;$env:SystemRoot"
    $target = [IO.Path]::GetFullPath($OutputDirectory, $projectRoot)
    $distRoot = [IO.Path]::GetFullPath('dist', $projectRoot) + [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($distRoot, 'OrdinalIgnoreCase') -or
        $target -in @((Join-Path $projectRoot 'dist\workspace'), (Join-Path $projectRoot 'dist\archive'), (Join-Path $projectRoot 'dist\personality-r1'), (Join-Path $projectRoot 'dist\chat-r2'), (Join-Path $projectRoot 'dist\experience-r3'), (Join-Path $projectRoot 'dist\integrated-r4'), (Join-Path $projectRoot 'dist\r4-audit-candidate'), (Join-Path $projectRoot 'dist\theseed-r5'), (Join-Path $projectRoot 'dist\theseed-r6'), (Join-Path $projectRoot 'dist\theseed-r7'))) {
        throw 'Choose a dedicated output directory underneath dist.'
    }
    $bundle = Join-Path $target 'SEED'
    if (Test-Path -LiteralPath (Join-Path $bundle 'Data')) {
        throw 'Move the existing workspace build Data folder to a safe location before rebuilding.'
    }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent.' }

    & $python -c 'import struct, sys; assert sys.platform == "win32" and struct.calcsize("P") == 8'
    if ($LASTEXITCODE -ne 0) { throw 'The Windows bundle requires x64 Python.' }
    $version = & $python -c 'from seed import __version__, BUILD_REVISION; print(f"{__version__}-{BUILD_REVISION}")'
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+\+M\d+-r\d+$') { throw 'Unrecognized version.' }

    $metadataFolder = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ("theseed-build-" + [guid]::NewGuid()))
    $metadata = Join-Path $metadataFolder.FullName 'build-info.json'
    @{version=$version; commit=$commit; channel=$(if ($commit) { 'candidate' } else { 'unverified' })} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $metadata -Encoding UTF8
    $resource = (Join-Path $projectRoot 'seed\resources') + ':seed/resources'
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir --name SEED `
        --workpath 'build\pyinstaller-personality' --specpath 'build' --distpath $target `
        --add-data $resource --add-data ($metadata + ':seed') run_seed.py
    if ($LASTEXITCODE -ne 0) { throw 'SEED packaging failed.' }

    if ($WhisperArchive) {
        $speech = New-Item -ItemType Directory -Path (Join-Path $bundle 'speech\whisper.cpp') -Force
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $package = [IO.Compression.ZipFile]::OpenRead($WhisperArchive)
        try {
            foreach ($name in @('whisper-cli.exe','whisper.dll','ggml.dll','ggml-base.dll','ggml-cpu.dll')) {
                $entry = $package.GetEntry("Release/$name")
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $speech.FullName $name))
            }
        } finally {
            $package.Dispose()
        }
        Copy-Item -LiteralPath $WhisperModel -Destination (Join-Path $speech.FullName 'ggml-base-q5_1.bin')
    }

    # SEED uses the Windows Qt Multimedia backend. Keep the optional FFmpeg backend out of the bundle.
    $unneededMedia = Get-ChildItem -LiteralPath (Join-Path $bundle '_internal') -Recurse -File |
        Where-Object { $_.Name -match '^(ffmpegmediaplugin|avcodec-\d+|avdevice-\d+|avfilter-\d+|avformat-\d+|avutil-\d+|swresample-\d+|swscale-\d+|postproc-\d+)\.dll$' }
    foreach ($file in $unneededMedia) {
        Remove-Item -LiteralPath $file.FullName
    }

    $licenses = New-Item -Path $bundle -Name 'licenses' -ItemType Directory -Force
    Get-ChildItem -LiteralPath 'licenses' -Filter '*.txt' -File | Copy-Item -Destination $licenses.FullName
    foreach ($wheel in @('pyside6_essentials-6.12.0', 'pyside6_addons-6.12.0', 'shiboken6-6.12.0', 'pyinstaller-6.20.0',
            'tiktoken-0.14.0', 'regex-2026.11.15', 'requests-2.34.2', 'charset_normalizer-3.5.2', 'idna-3.20',
            'urllib3-2.8.0', 'certifi-2026.10.23')) {
        $source = ".venv\Lib\site-packages\$wheel.dist-info\licenses"
        Copy-Item -LiteralPath $source -Destination (Join-Path $licenses.FullName $wheel) -Recurse
    }
    $pythonLicense = & $python -c 'from pathlib import Path; import sys; print(Path(sys.base_prefix) / "LICENSE.txt")'
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenses.FullName 'CPython-LICENSE.txt')
    Copy-Item -LiteralPath '.venv\Lib\site-packages\pywin32_ctypes-0.2.3.dist-info\LICENSE.txt' `
        -Destination (Join-Path $licenses.FullName 'pywin32-ctypes-LICENSE.txt')
    Copy-Item -LiteralPath 'README.md','THIRD_PARTY_NOTICES.md','THESEED.md' -Destination $bundle

    if (-not $SkipArchive) {
        $archive = Join-Path $projectRoot "dist\SEED-v$version-win-x64.zip"
        $archiveStem = [System.IO.Path]::ChangeExtension($archive, $null).TrimEnd('.')
        & $python -c 'import shutil, sys; print(shutil.make_archive(sys.argv[1], "zip", sys.argv[2], "SEED"))' `
            $archiveStem $target
        if ($LASTEXITCODE -ne 0) { throw 'SEED archive creation failed.' }
        Get-FileHash -LiteralPath $archive -Algorithm SHA256
    }
} finally {
    if ($metadataFolder) {
        if (Test-Path -LiteralPath $metadata) { Remove-Item -LiteralPath $metadata }
        [IO.Directory]::Delete($metadataFolder.FullName)
    }
    $env:Path = $originalPath
    Pop-Location
}
