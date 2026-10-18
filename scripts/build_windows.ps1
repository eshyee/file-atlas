param([switch]$SkipFrontend, [switch]$SkipFaceModels)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot

function Get-VerifiedFile {
    param([string]$Uri, [string]$Destination, [string]$Sha256)
    if ((Test-Path -LiteralPath $Destination) -and
        ((Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash -eq $Sha256)) { return }
    $partial = "$Destination.download"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash
    if ($actual -ne $Sha256) { throw "Downloaded file checksum mismatch: $Destination" }
    Move-Item -Force -LiteralPath $partial -Destination $Destination
}

Push-Location -LiteralPath $taskRoot
try {
    if (-not $SkipFrontend) {
        Push-Location -LiteralPath (Join-Path $taskRoot 'frontend')
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
            npm run build
            if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        } finally { Pop-Location }
    }
    $modelArguments = @()
    if (-not $SkipFaceModels) {
        python -c "import cv2, numpy"
        if ($LASTEXITCODE -ne 0) { throw 'Install the .[faces] optional dependencies for the full Windows build.' }
        $modelRoot = Join-Path $taskRoot 'build/face-models'
        New-Item -ItemType Directory -Force -Path $modelRoot | Out-Null
        $commit = '47534e27c9851bb1128ccc0102f1145e27f23f98'
        Get-VerifiedFile "https://github.com/opencv/opencv_zoo/raw/$commit/models/face_detection_yunet/face_detection_yunet_2023mar.onnx?download=" (Join-Path $modelRoot 'face_detection_yunet_2023mar.onnx') '8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4'
        Get-VerifiedFile "https://github.com/opencv/opencv_zoo/raw/$commit/models/face_recognition_sface/face_recognition_sface_2021dec.onnx?download=" (Join-Path $modelRoot 'face_recognition_sface_2021dec.onnx') '0BA9FBFA01B5270C96627C4EF784DA859931E02F04419C829E83484087C34E79'
        Get-VerifiedFile "https://raw.githubusercontent.com/opencv/opencv_zoo/$commit/models/face_detection_yunet/LICENSE" (Join-Path $modelRoot 'YuNet-LICENSE.txt') 'C83B8120C50CCBD4C4F96EDF53141BDD566EBB8F8E9227E415326AA1B1ABA958'
        Get-VerifiedFile "https://raw.githubusercontent.com/opencv/opencv_zoo/$commit/models/face_recognition_sface/LICENSE" (Join-Path $modelRoot 'SFace-LICENSE.txt') 'CFC7749B96F63BD31C3C42B5C471BF756814053E847C10F3EB003417BC523D30'
        # Add only the four verified files. A stale partial download or any
        # unrelated file in the build cache must never enter the executable.
        foreach ($name in @('face_detection_yunet_2023mar.onnx',
                            'face_recognition_sface_2021dec.onnx',
                            'YuNet-LICENSE.txt', 'SFace-LICENSE.txt')) {
            $modelArguments += @('--add-data', "$(Join-Path $modelRoot $name);models")
        }
    }
    # Normal startup stays asInvoker; elevation is a separate explicit UAC action.
    $pyinstallerArguments = @('--noconfirm', '--clean', '--onefile', '--name', 'FileAtlas', '--manifest', 'scripts/fileatlas.manifest', '--add-data', 'frontend/dist;frontend/dist', '--collect-all', 'PIL', '--exclude-module', 'pillow_heif', '--exclude-module', '_pillow_heif') + $modelArguments + @('run.py')
    python -m PyInstaller @pyinstallerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Windows executable build failed.' }
    $releaseArguments = @('scripts/prepare_windows_release.py')
    if ($SkipFaceModels) { $releaseArguments += '--without-face-models' }
    python @releaseArguments
    if ($LASTEXITCODE -ne 0) { throw 'Windows release validation or packaging failed.' }
    Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $taskRoot 'dist/FileAtlas.exe')
} finally { Pop-Location }
