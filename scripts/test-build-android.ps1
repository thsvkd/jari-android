[CmdletBinding()]
param([string]$RealJavaHome)

$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('teum-build-tests-' + [guid]::NewGuid())
$savedEnvironment = @{}
$environmentNames = @('PATH', 'JAVA_HOME', 'ANDROID_HOME', 'LOCALAPPDATA', 'ProgramFiles',
    'TEUM_TEST_LOG', 'TEUM_TEST_FAIL', 'TEUM_TEST_JAVA_VERSION', 'VITE_API_BASE_URL')
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$failures = 0
try {
    $fixture = Join-Path $testRoot 'mobile'
    $bin = Join-Path $testRoot 'bin'
    $fakeJdk = Join-Path $testRoot 'jdk'
    foreach ($path in @("$fixture/scripts", "$fixture/android/app", $bin, "$fakeJdk/bin",
            "$testRoot/local/Android/Sdk/platforms/android-35", "$testRoot/programs")) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
    Copy-Item (Join-Path $PSScriptRoot 'build-android.ps1') "$fixture/scripts/build-android.ps1"
    Set-Content "$fixture/.env.production.local" 'VITE_API_BASE_URL=https://teum.example.test' -Encoding Ascii
    Set-Content "$fixture/android/app/google-services.json" '{}' -Encoding Ascii
    # Native executable boundary: no npm, Capacitor or Gradle installation is invoked.
    foreach ($command in @('npm', 'npx', 'gradlew')) {
        $destination = if ($command -eq 'gradlew') { "$fixture/android/gradlew.bat" } else { "$bin/$command.cmd" }
        @"
@echo off
echo $command %*>>"%TEUM_TEST_LOG%"
echo SDK=%ANDROID_HOME%
if "%TEUM_TEST_FAIL%"=="$command" exit /b 37
exit /b 0
"@ | Set-Content $destination -Encoding Ascii
    }
    @'
using System;
class FakeJava {
    static int Main(string[] args) {
        if (args.Length != 1 || args[0] != "-version") return 99;
        Console.Error.WriteLine("openjdk version \"" + Environment.GetEnvironmentVariable("TEUM_TEST_JAVA_VERSION") + "\"");
        return Environment.GetEnvironmentVariable("TEUM_TEST_FAIL") == "java" ? 37 : 0;
    }
}
'@ | Set-Content "$testRoot/FakeJava.cs" -Encoding Ascii
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    & $compiler /nologo /target:exe "/out:$(Join-Path $fakeJdk 'bin\java.exe')" (Join-Path $testRoot 'FakeJava.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile native Java fixture.' }
    $env:PATH = "$bin;$env:PATH"
    $env:LOCALAPPDATA = "$testRoot/local"
    $env:ProgramFiles = "$testRoot/programs"
    $env:VITE_API_BASE_URL = $null
    $env:TEUM_TEST_LOG = "$testRoot/commands.log"
    $shell = (Get-Process -Id $PID).Path

    function Test-Pipeline {
        param([string]$Name, [string]$FailCommand, [string]$ExpectedCommands,
            [bool]$ExpectSuccess = $false, [string]$JavaVersion = '21.0.12',
            [switch]$DefaultSdk, [switch]$SkipWebBuild, [string]$JavaHome = $fakeJdk,
            [string]$ExpectedError, [switch]$MissingPushConfig, [switch]$MissingApiUrl)
        if ($MissingPushConfig) {
            Remove-Item "$fixture/android/app/google-services.json" -Force -ErrorAction SilentlyContinue
        } else {
            Set-Content "$fixture/android/app/google-services.json" '{}' -Encoding Ascii
        }
        if ($MissingApiUrl) {
            Remove-Item "$fixture/.env.production.local" -Force -ErrorAction SilentlyContinue
        } else {
            Set-Content "$fixture/.env.production.local" 'VITE_API_BASE_URL=https://teum.example.test' -Encoding Ascii
        }
        $env:JAVA_HOME = $JavaHome
        $env:ANDROID_HOME = if ($DefaultSdk) { $null } else { "$testRoot/local/Android/Sdk" }
        $env:TEUM_TEST_FAIL = $FailCommand
        $env:TEUM_TEST_JAVA_VERSION = $JavaVersion
        Set-Content $env:TEUM_TEST_LOG ''
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$fixture/scripts/build-android.ps1")
        if ($SkipWebBuild) { $arguments += '-SkipWebBuild' }
        # Windows PowerShell exposes native stderr as error records. Capture these
        # without letting the test runner stop before it can assert the exit code.
        $ErrorActionPreference = 'Continue'
        $output = (& $shell @arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        $commands = ((Get-Content $env:TEUM_TEST_LOG | Where-Object { $_.Trim() }) -join '|').Trim()
        $successOutput = $output -match 'APK:'
        if (($code -eq 0) -ne $ExpectSuccess -or $successOutput -ne $ExpectSuccess -or $commands -ne $ExpectedCommands -or
            ($ExpectedError -and $output -notmatch $ExpectedError) -or
            ($DefaultSdk -and -not $output.Contains("SDK=$(Join-Path $env:LOCALAPPDATA 'Android\Sdk')"))) {
            $script:failures++
            Write-Output "FAIL $Name : exit=$code; commands=[$commands]; APK=$successOutput"
            Write-Output $output
        } else {
            Write-Output "PASS $Name : exit=$code; commands=[$commands]; APK=$successOutput"
        }
    }

    Test-Pipeline -Name 'missing Firebase config stops before build' -MissingPushConfig -ExpectedCommands '' -ExpectedError 'Firebase'
    Test-Pipeline -Name 'missing API URL stops before build' -MissingApiUrl -ExpectedCommands '' -ExpectedError 'VITE_API_BASE_URL'
    Test-Pipeline -Name 'npm failure stops before sync and Gradle' -FailCommand npm -ExpectedCommands 'npm run build'
    Test-Pipeline -Name 'sync failure stops before Gradle' -FailCommand npx -ExpectedCommands 'npm run build|npx cap sync android'
    Test-Pipeline -Name 'JDK 17 is rejected before build' -JavaVersion '17.0.20' -ExpectedCommands '' -ExpectedError 'JDK 21'
    Test-Pipeline -Name 'SDK discovery uses LOCALAPPDATA' -DefaultSdk -ExpectedCommands 'npm run build|npx cap sync android|gradlew assembleDebug' -ExpectSuccess $true
    Test-Pipeline -Name 'java failure stops before build' -FailCommand java -ExpectedCommands '' -ExpectedError 'java -version failed'
    Test-Pipeline -Name 'unparseable Java version is rejected' -JavaVersion 'unknown' -ExpectedCommands '' -ExpectedError 'JDK 21'
    Test-Pipeline -Name 'later JDK is supported' -JavaVersion '25.0.1' -ExpectedCommands 'npm run build|npx cap sync android|gradlew assembleDebug' -ExpectSuccess $true
    Test-Pipeline -Name 'Gradle failure has no success output' -FailCommand gradlew -ExpectedCommands 'npm run build|npx cap sync android|gradlew assembleDebug'
    Test-Pipeline -Name 'SkipWebBuild still checks sync failure' -SkipWebBuild -FailCommand npx -ExpectedCommands 'npx cap sync android'
    Test-Pipeline -Name 'SkipWebBuild succeeds with sync and Gradle' -SkipWebBuild -ExpectedCommands 'npx cap sync android|gradlew assembleDebug' -ExpectSuccess $true
    if ($RealJavaHome) {
        Test-Pipeline -Name "installed JDK validates: $RealJavaHome" -JavaHome $RealJavaHome -ExpectedCommands 'npm run build|npx cap sync android|gradlew assembleDebug' -ExpectSuccess $true
    }
    if ($failures) { throw "$failures regression test(s) failed." }
}
finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    $resolvedRoot = [IO.Path]::GetFullPath($testRoot)
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedRoot.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $resolvedRoot -Leaf) -like 'teum-build-tests-*') {
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
