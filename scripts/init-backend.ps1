$ErrorActionPreference = 'Stop'
$backendRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$secretDir = Join-Path $backendRoot '.secrets'
$secretPath = Join-Path $secretDir 'mobile.key'
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
if (-not (Test-Path -LiteralPath $secretPath)) {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [System.IO.File]::WriteAllText($secretPath, [Convert]::ToBase64String($bytes), (New-Object System.Text.UTF8Encoding $false))
    Write-Output 'Created local mobile key. Back it up securely; do not rotate while stored credentials exist.'
} else {
    Write-Output 'Existing mobile key preserved.'
}
$envPath = Join-Path $backendRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $backendRoot '.env.example') -Destination $envPath
}
Write-Output 'Local configuration ready. No server was started; no secret is printed.'
