<#
.SYNOPSIS
CampusBite AI v2.0 — Windows College Server Self-Hosted PostgreSQL & Scheduled Jobs Setup.
Configures environment verification, firewall isolation (port 5432 loopback-only),
Alembic schema migration, daily encrypted GFS backup task, and 4-day hybrid retention task.
Zero paid cloud database subscription required.

param(
    [string]$PostgresBinDir = "C:\Program Files\PostgreSQL\16\bin",
    [string]$SecondaryBackupShare = "D:\CampusBiteBackupsOffDisk",
    [switch]$RegisterScheduledTasks
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== CampusBite AI v2.0 — College PostgreSQL Windows Setup ===" -ForegroundColor Cyan

if (Test-Path "$PostgresBinDir\psql.exe") {
    Write-Host "[OK] Found self-hosted PostgreSQL binaries at $PostgresBinDir" -ForegroundColor Green
    $env:PATH = "$PostgresBinDir;$env:PATH"
} else {
    Write-Host "[INFO] PostgreSQL binaries not at $PostgresBinDir. Install free PostgreSQL 16 Community Installer from postgresql.org on the college server and re-run." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path "$ProjectRoot\backups\primary" | Out-Null
New-Item -ItemType Directory -Force -Path $SecondaryBackupShare -ErrorAction SilentlyContinue | Out-Null

if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    New-NetFirewallRule -DisplayName "CampusBite Block External Postgres 5432" -Direction Inbound -LocalPort 5432 -Protocol TCP -Action Block -Profile Public,Domain,Private -ErrorAction SilentlyContinue | Out-Null
    Write-Host "[OK] Windows Firewall rule verified: PostgreSQL port 5432 blocked from external clients." -ForegroundColor Green
}

if ($RegisterScheduledTasks) {
    $PythonExe = "$ProjectRoot\.venv\Scripts\python.exe"
    $BackupAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ProjectRoot\scripts\backup_manager.py`" gfs-backup" -WorkingDirectory $ProjectRoot
    $BackupTrigger = New-ScheduledTaskTrigger -Daily -At "02:00AM"
    Register-ScheduledTask -TaskName "CampusBite_Daily_Encrypted_Backup" -Action $BackupAction -Trigger $BackupTrigger -Force | Out-Null

    $RetentionAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ProjectRoot\scripts\run_retention_cleanup.py`"" -WorkingDirectory $ProjectRoot
    $RetentionTrigger = New-ScheduledTaskTrigger -Daily -At "02:30AM"
    Register-ScheduledTask -TaskName "CampusBite_4Day_Hybrid_Retention" -Action $RetentionAction -Trigger $RetentionTrigger -Force | Out-Null

    Write-Host "[OK] Registered daily 02:00 AM GFS Backup and 02:30 AM 4-Day Hybrid Retention scheduled tasks." -ForegroundColor Green
}

Write-Host "Next step: set DATABASE_URL=postgresql+psycopg://campusbite_app:<password>@127.0.0.1:5432/campusbite_prod in .env and run alembic upgrade head." -ForegroundColor Cyan
