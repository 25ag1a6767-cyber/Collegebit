<#
.SYNOPSIS
  CampusBite AI v2.0 — Automated Android APK & AAB Builder (Local Gradle or Expo EAS).
.DESCRIPTION
  Builds the dedicated Customer Android application (`mobile/`):
  - If ANDROID_HOME / Android SDK is installed locally, runs `npx expo prebuild --platform android`
    followed by Gradle `assembleRelease` (.apk) and `bundleRelease` (.aab).
  - Otherwise, if EAS CLI is authenticated, triggers cloud APK/AAB builds via `mobile/eas.json`.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$MobileDir = Join-Path $RepoRoot "mobile"

Set-Location $MobileDir
Write-Host "=== CampusBite AI v2.0 Android Build Pipeline ===" -ForegroundColor Cyan

if (-not (Test-Path "node_modules")) {
  Write-Host "Installing mobile dependencies..."
  npm install
}

Write-Host "Running TypeScript verification..."
npm run typecheck

$SdkRoot = $env:ANDROID_HOME
if (-not $SdkRoot) { $SdkRoot = $env:ANDROID_SDK_ROOT }
$DefaultWinSdk = Join-Path $env:LOCALAPPDATA "Android\Sdk"
if (-not $SdkRoot -and (Test-Path $DefaultWinSdk)) {
  $SdkRoot = $DefaultWinSdk
  $env:ANDROID_HOME = $SdkRoot
}

if ($SdkRoot -and (Test-Path $SdkRoot)) {
  Write-Host "Detected local Android SDK at: $SdkRoot" -ForegroundColor Green
  npx expo prebuild --platform android --non-interactive
  Set-Location (Join-Path $MobileDir "android")
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    .\gradlew.bat assembleRelease bundleRelease
  } else {
    ./gradlew assembleRelease bundleRelease
  }
  Write-Host "APK Output: mobile/android/app/build/outputs/apk/release/app-release.apk" -ForegroundColor Green
  Write-Host "AAB Output: mobile/android/app/build/outputs/bundle/release/app-release.aab" -ForegroundColor Green
} else {
  Write-Warning "Local Android SDK (ANDROID_HOME) is not installed on this host."
  Write-Host "To compile APK & AAB without installing Android Studio locally, run:"
  Write-Host "  npx eas-cli build --platform android --profile preview      # Generates installable .apk"
  Write-Host "  npx eas-cli build --platform android --profile production   # Generates Play Store .aab"
}
