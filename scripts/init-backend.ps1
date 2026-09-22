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
# compose mounts this as a secret and will not start while the file is missing.
# An empty file means "no push" to the server; replace it with the real key later.
$firebasePath = Join-Path $secretDir 'firebase-admin.json'
if (-not (Test-Path -LiteralPath $firebasePath)) {
    [System.IO.File]::WriteAllText($firebasePath, '', (New-Object System.Text.UTF8Encoding $false))
    Write-Output 'Created empty firebase-admin.json placeholder: push is off until a real key replaces it.'
}
$envPath = Join-Path $backendRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $backendRoot '.env.example') -Destination $envPath
}
Write-Output 'Local configuration ready. No server was started; no secret is printed.'
