param(
    [string]$OutputDirectory = 'dist\theseed-r8',
    [switch]$SkipArchive,
    [string]$WhisperArchive,
    [string]$WhisperModel,
    [string]$SarasaDirectory,
    [string]$TranslationModel,
    [string]$TranslationRuntime
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$originalPath = $env:Path
$originalPythonPath = $env:PYTHONPATH
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
    $extra = @()
    if ($SarasaDirectory) {
        $sarasa = (Resolve-Path -LiteralPath $SarasaDirectory).Path
        $fontHashes = @{
            'SarasaUiTC-Regular.ttf' = 'C6B87D43E3E7F0BAA07089EC7D0926D50A5D2649F1BA6624CE99C37AA2239D33'
            'SarasaUiTC-SemiBold.ttf' = '5F297DA6D5EC91BAD92CB6F6C0A471155969ECD244A9462E28B382A6DB90A70D'
        }
        foreach ($name in $fontHashes.Keys) {
            $font = Join-Path $sarasa $name
            if ((Get-FileHash -LiteralPath $font).Hash -ne $fontHashes[$name]) { throw "Unexpected Sarasa UI TC v1.0.43 font: $name" }
            $extra += @('--add-data', ($font + ':seed/resources/fonts'))
        }
    }
    if ([bool]$TranslationModel -ne [bool]$TranslationRuntime) { throw 'Supply both the local translation model and its runtime.' }
    if ($TranslationModel) {
        $TranslationModel = (Resolve-Path -LiteralPath $TranslationModel).Path
        $runtime = (Resolve-Path -LiteralPath $TranslationRuntime).Path
        if ((Get-FileHash -LiteralPath $TranslationModel).Hash -ne '433E7C4F034D87FBE2353161E05F18646D7999452F801A4E1F0378522B9850AB') {
            throw 'The translation model must be the pinned public Argos en-zh 1.9 archive.'
        }
        foreach ($wheel in @('ctranslate2-4.9.0','sentencepiece-0.2.3','numpy-2.5.5','pyyaml-6.0.3')) {
            if (!(Test-Path -LiteralPath (Join-Path $runtime "$wheel.dist-info") -PathType Container)) { throw "Missing translation runtime $wheel" }
        }
        $env:PYTHONPATH = $runtime
        $extra += @('--paths', $runtime, '--collect-binaries', 'ctranslate2', '--collect-binaries', 'sentencepiece')
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
        --add-data $resource --add-data ($metadata + ':seed') @extra run_seed.py
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
    if ($TranslationModel) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $modelRoot = Join-Path $bundle '_internal\seed\resources\catalog-en-zh'
        $package = [IO.Compression.ZipFile]::OpenRead($TranslationModel)
        try {
            foreach ($relative in @('sentencepiece.model','model/config.json','model/model.bin','model/shared_vocabulary.json')) {
                $local = Join-Path $modelRoot $relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $local) -Force | Out-Null
                $entry = $package.GetEntry("translate-en_zh-1_9/$relative")
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $local)
            }
        } finally {
            $package.Dispose()
        }
        # Translation runs on the CPU. The CTranslate2 wheel also ships a GPU forwarding DLL.
        Get-ChildItem -LiteralPath (Join-Path $bundle '_internal') -Recurse -File -Filter 'cudnn64_9.dll' |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName }
    }

    # SEED uses the Windows Qt Multimedia backend. Keep the optional FFmpeg backend out of the bundle.
    $unneededMedia = Get-ChildItem -LiteralPath (Join-Path $bundle '_internal') -Recurse -File |
        Where-Object { $_.Name -match '^(ffmpegmediaplugin|avcodec-\d+|avdevice-\d+|avfilter-\d+|avformat-\d+|avutil-\d+|swresample-\d+|swscale-\d+|postproc-\d+)\.dll$' }
    foreach ($file in $unneededMedia) {
        Remove-Item -LiteralPath $file.FullName
    }

    $licenses = New-Item -Path $bundle -Name 'licenses' -ItemType Directory -Force
    Get-ChildItem -LiteralPath 'licenses' -Filter '*.txt' -File | Copy-Item -Destination $licenses.FullName
    if ($TranslationModel) {
        foreach ($wheel in @('numpy-2.5.5','pyyaml-6.0.3')) {
            Copy-Item -LiteralPath (Join-Path $runtime "$wheel.dist-info\licenses") -Destination (Join-Path $licenses.FullName $wheel) -Recurse
        }
    }
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
    $env:PYTHONPATH = $originalPythonPath
    Pop-Location
}
