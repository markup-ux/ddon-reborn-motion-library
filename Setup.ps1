param(
    [Parameter(Mandatory)][string]$Ddon,
    [Parameter(Mandatory)][string]$Dd2,
    [string]$Python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source,
    [string]$NativePython,
    [switch]$BrowserOnly
)

$ErrorActionPreference = 'Stop'
$Ddon = (Resolve-Path -LiteralPath $Ddon).Path
$Dd2 = (Resolve-Path -LiteralPath $Dd2).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($NativePython) { $NativePython = (Resolve-Path -LiteralPath $NativePython).Path }
if (!(Test-Path -LiteralPath (Join-Path $Ddon 'nativePC\rom') -PathType Container)) {
    throw "Select the DDON client folder containing nativePC\rom."
}
if (!(Test-Path -LiteralPath (Join-Path $Dd2 'DD2.exe') -PathType Leaf)) {
    throw 'Select the DD2 folder containing DD2.exe.'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE." }
}

function Install-SourceZip {
    param([string]$Uri, [string]$Archive, [string]$Extracted, [string]$Destination)
    if (Test-Path -LiteralPath $Destination -PathType Container) { return }
    Invoke-WebRequest -Uri $Uri -OutFile $Archive
    Expand-Archive -LiteralPath $Archive -DestinationPath '.local\downloads' -Force
    Move-Item -LiteralPath $Extracted -Destination $Destination
}

Push-Location -LiteralPath $PSScriptRoot
try {
    New-Item -ItemType Directory -Path '.local\downloads' -Force | Out-Null
    $serverRevision = '99e6aa391d3eddc24b592358a5133d3e923b39ee'
    $serverZip = "https://codeload.github.com/markup-ux/casual-dogma/zip/$serverRevision"
    $serverFolder = ".local\downloads\casual-dogma-$serverRevision\Server"
    Install-SourceZip $serverZip '.local\downloads\arrowgene.zip' $serverFolder '.local\arrowgene'
    Install-SourceZip 'https://codeload.github.com/seifhassine/REasy/zip/refs/tags/v0.8.0' '.local\downloads\reasy.zip' '.local\downloads\REasy-0.8.0' '.local\reasy-v0.8.0'

    Invoke-Checked $Python @('-m', 'venv', '.venv')
    $viewerPython = (Resolve-Path -LiteralPath '.venv\Scripts\python.exe').Path
    Invoke-Checked $viewerPython @('-m', 'pip', 'install', '-r', 'requirements.txt')
    $revil = '.local\revil-2.10.201\revil_toolset.exe'
    if (!(Test-Path -LiteralPath $revil -PathType Leaf)) {
        $archive = '.local\downloads\RevilToolset-v2.10.201-win64.7z'
        Invoke-WebRequest -Uri 'https://github.com/PredatorCZ/RevilLib/releases/download/nightly/RevilToolset-v2.10.201-win64.7z' -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '8c7382441110a281582def478b2c156a7122d0a9aa2a12c7d8a1a46820a6aaf6') {
            throw 'The Revil Toolset archive did not match its published SHA-256.'
        }
        Invoke-Checked $viewerPython @('-m', 'pip', 'install', 'py7zr')
        Invoke-Checked $viewerPython @('-m', 'py7zr', 'x', $archive, '.local\revil-2.10.201')
        if (!(Test-Path -LiteralPath $revil -PathType Leaf)) { throw 'Revil extraction did not produce revil_toolset.exe.' }
    }
    if ($NativePython) {
        Invoke-Checked $NativePython @('-c', 'import sys; assert sys.version_info >= (3, 15), "Use Python 3.15 for the DD2 codec"')
        Invoke-Checked $NativePython @('-m', 'venv', '.local\native-python')
        $NativePython = (Resolve-Path -LiteralPath '.local\native-python\Scripts\python.exe').Path
        Invoke-Checked $NativePython @('-m', 'pip', 'install', 'numpy', 'mmh3')
    }
    Invoke-Checked 'dotnet' @('publish', 'tools/skilltool/skilltool.csproj', '-c', 'Release', '-o', 'work/skilltool')
    if (!$BrowserOnly) {
        & '.\desktop\Build.ps1' -Ddon $Ddon -Dd2 $Dd2 -Python $viewerPython -NativePython $NativePython
    }
}
finally {
    Pop-Location
}
