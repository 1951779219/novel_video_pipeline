param(
    [string]$Version,
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $Version) {
    $versionLine = Get-Content (Join-Path $repoRoot "app\version.py") | Select-String 'VERSION\s*=\s*[''"]([^''"]+)[''"]' | Select-Object -First 1
    if (-not $versionLine) {
        throw "VERSION was not found in app/version.py"
    }
    $Version = $versionLine.Matches[0].Groups[1].Value
}

if (-not $OutputDir) {
    $OutputDir = Join-Path $repoRoot "dist"
}

$packageName = "novel_video_pipeline_v${Version}_full"
$stageDir = Join-Path $OutputDir $packageName
$zipPath = Join-Path $OutputDir "${packageName}.zip"
$hashPath = Join-Path $OutputDir "${packageName}.sha256.txt"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
if (Test-Path $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

function Copy-Tree {
    param(
        [string]$SourceRelative,
        [string[]]$ExcludeDirs = @(),
        [string[]]$ExcludeFiles = @()
    )

    $sourcePath = Join-Path $repoRoot $SourceRelative
    if (-not (Test-Path $sourcePath)) {
        return
    }

    $targetPath = Join-Path $stageDir $SourceRelative
    New-Item -ItemType Directory -Force -Path $targetPath | Out-Null

    $roboArgs = @(
        $sourcePath,
        $targetPath,
        "/E",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    )

    if ($ExcludeDirs.Count -gt 0) {
        $roboArgs += "/XD"
        $roboArgs += $ExcludeDirs
    }
    if ($ExcludeFiles.Count -gt 0) {
        $roboArgs += "/XF"
        $roboArgs += $ExcludeFiles
    }

    & robocopy @roboArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed for $SourceRelative with exit code $LASTEXITCODE"
    }
}

Copy-Tree -SourceRelative "app" -ExcludeDirs @("__pycache__")
Copy-Tree -SourceRelative "runtime" -ExcludeDirs @("__pycache__")
Copy-Tree -SourceRelative "docs" -ExcludeDirs @("__pycache__")

foreach ($file in @(
    "README.md",
    "requirements.txt",
    "update.bat",
    "启动.bat",
    "桌面GUI.bat"
)) {
    $source = Join-Path $repoRoot $file
    if (Test-Path $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $stageDir $file) -Force
    }
}

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force

$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
@(
    "file=$(Split-Path -Leaf $zipPath)"
    "sha256=$hash"
) | Set-Content -LiteralPath $hashPath -Encoding ascii

Write-Output "Full package created:"
Write-Output $zipPath
Write-Output "SHA256:"
Write-Output $hash
