[CmdletBinding()]
param(
    [switch]$SkipWebBuild
)

$ErrorActionPreference = "Stop"
$mobileRoot = Split-Path -Parent $PSScriptRoot
$firebaseConfig = Join-Path $mobileRoot 'android\app\google-services.json'
if (-not (Test-Path $firebaseConfig -PathType Leaf)) {
    throw "Firebase 설정 파일이 없어 실서버용 디버그 APK 빌드를 중단합니다: $firebaseConfig"
}
$productionEnvironment = Join-Path $mobileRoot '.env.production.local'
$configuredApiBase = [Environment]::GetEnvironmentVariable('VITE_API_BASE_URL', 'Process')
if (-not $configuredApiBase -and (Test-Path $productionEnvironment -PathType Leaf)) {
    $configuredApiBase = Get-Content $productionEnvironment |
        Where-Object { $_ -match '^\s*VITE_API_BASE_URL\s*=\s*https://.+' } |
        Select-Object -First 1
}
if (-not $configuredApiBase) {
    throw "VITE_API_BASE_URL이 없어 실서버용 디버그 APK 빌드를 중단합니다. .env.production.local에 HTTPS API 주소를 설정해 주세요."
}
$jdkCandidates = @()
if ($env:JAVA_HOME) {
    $jdkCandidates += $env:JAVA_HOME
}
else {
    $jdkCandidates += Get-ChildItem (Join-Path $env:ProgramFiles 'Eclipse Adoptium') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object FullName
}
$javaHome = $null
foreach ($candidate in $jdkCandidates) {
    $java = Join-Path $candidate 'bin\java.exe'
    if (-not (Test-Path $java -PathType Leaf)) { continue }
    # Java writes its version to stderr even on success. Windows PowerShell 5.1
    # turns that into error records, so inspect the native exit code explicitly.
    try {
        $ErrorActionPreference = 'Continue'
        $versionLines = & $java -version 2>&1
        $javaExitCode = $LASTEXITCODE
        $versionOutput = ($versionLines | ForEach-Object { $_.ToString() }) -join "`n"
    }
    finally {
        $ErrorActionPreference = 'Stop'
    }
    if ($javaExitCode -ne 0) {
        throw "java -version failed (exit $javaExitCode): $java"
    }
    if ($versionOutput -match '(?m)^(?:openjdk|java) version "(\d+)(?:[.\-+\"]|$)' -and [int]$Matches[1] -ge 21) {
        $javaHome = $candidate
        break
    }
}

if (-not $javaHome) {
    throw "JDK 21+ is required. Set JAVA_HOME to an installed JDK 21 or later."
}

$env:JAVA_HOME = $javaHome
if (-not $env:ANDROID_HOME) {
    $env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
}
if (-not (Test-Path (Join-Path $env:ANDROID_HOME "platforms\android-35"))) {
    throw "Android SDK Platform 35를 찾을 수 없습니다: $env:ANDROID_HOME"
}

Push-Location $mobileRoot
try {
    if (-not $SkipWebBuild) {
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "npm run build failed (exit $LASTEXITCODE)."
        }
    }
    npx cap sync android
    if ($LASTEXITCODE -ne 0) {
        throw "npx cap sync android failed (exit $LASTEXITCODE)."
    }
    Push-Location ".\android"
    try {
        & ".\gradlew.bat" assembleDebug
        if ($LASTEXITCODE -ne 0) {
            throw "Gradle debug APK 빌드가 실패했습니다."
        }
    }
    finally {
        Pop-Location
    }
    Write-Output "APK: $mobileRoot\android\app\build\outputs\apk\debug\app-debug.apk"
}
finally {
    Pop-Location
}
