param(
    [Parameter(Mandatory)][string]$Ddon,
    [Parameter(Mandatory)][string]$Dd2,
    [string]$Skilltool = "work\skilltool\skilltool.exe",
    [string]$Revil = ".local\revil-2.10.201\revil_toolset.exe",
    [string]$Python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source,
    [string]$NativePython
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $root
try {
    $output = Join-Path $root 'work\desktop'
    $build = Join-Path $root 'work\desktop-build'
    $project = Join-Path $PSScriptRoot 'DDONReborn.csproj'
    $settings = [ordered]@{
        Root = $root
        Python = (Resolve-Path -LiteralPath $Python).Path
        Ddon = (Resolve-Path -LiteralPath $Ddon).Path
        Dd2 = (Resolve-Path -LiteralPath $Dd2).Path
        Skilltool = (Resolve-Path -LiteralPath $Skilltool).Path
        Revil = (Resolve-Path -LiteralPath $Revil).Path
    }
    if ($NativePython) {
        $settings.NativePython = (Resolve-Path -LiteralPath $NativePython).Path
    }
    $cache = Join-Path $root '.local\nuget'
    $restore = @()
    if (Test-Path -LiteralPath $cache) {
        $restore += "-p:RestoreSources=$cache"
    }
    dotnet publish $project -c Release -r win-x64 --self-contained false -o $output "-p:BaseIntermediateOutputPath=$build/" "-p:BaseOutputPath=$build/bin/" @restore
    if ($LASTEXITCODE -ne 0) { throw "Desktop app build failed ($LASTEXITCODE)." }
    $settings | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'viewer-settings.json') -Encoding utf8
    Write-Output (Join-Path $output 'DDON Reborn.exe')
}
finally {
    Pop-Location
}
