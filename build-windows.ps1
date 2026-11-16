param([string]$OutputDirectory = 'dist\theseed-r8', [switch]$SkipArchive)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$originalPath = $env:Path
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv and install requirements-build.txt first.'
}

Push-Location -LiteralPath $projectRoot
try {
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

    $resource = (Join-Path $projectRoot 'seed\resources') + ':seed/resources'
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir --name SEED `
        --workpath 'build\pyinstaller-personality' --specpath 'build' --distpath $target `
        --add-data $resource run_seed.py
    if ($LASTEXITCODE -ne 0) { throw 'SEED packaging failed.' }

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
    $env:Path = $originalPath
    Pop-Location
}
