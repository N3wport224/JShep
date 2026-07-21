#Requires -Version 5.1
<#
.SYNOPSIS
  One-command local setup for the AI SDR backend (Windows PowerShell).

.DESCRIPTION
  1. Checks Docker Desktop + Docker Compose are installed and running
     (prints install instructions and exits if not).
  2. Creates .env from .env.example on first run, interactively prompting
     for the credentials most people need (Outlook SMTP/IMAP, HubSpot,
     Google Places) - press Enter on any prompt to skip it and configure
     it later by editing .env directly.
  3. Builds and starts every service (docker compose up --build -d), waits
     for Postgres to report healthy, waits for the one-shot `migrate`
     container to apply the database schema (alembic upgrade head), waits
     for the API to respond, then prints the dashboard URL and how to
     sign in.

.PARAMETER Yes
  Never prompt; accept defaults / leave blank.

.PARAMETER Reconfigure
  Re-run the credential prompts even if .env already exists.

.EXAMPLE
  .\setup.ps1
  .\setup.ps1 -Yes
  .\setup.ps1 -Reconfigure

.NOTES
  If Windows blocks the script from running, launch it with:
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
  or allow local scripts for your user once with:
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
param(
    [switch]$Yes,
    [switch]$Reconfigure
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$NonInteractive = [bool]$Yes
if ([Console]::IsInputRedirected) {
    $NonInteractive = $true
}

function Write-Info    { param($msg) Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok      { param($msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-WarnMsg { param($msg) Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Fail    { param($msg) Write-Host "[X] $msg" -ForegroundColor Red }
function Write-Heading { param($msg) Write-Host ""; Write-Host $msg -ForegroundColor White }

# ---------------------------------------------------------------------------
# 1. Prerequisite checks
# ---------------------------------------------------------------------------
Write-Heading "1/3 Checking prerequisites"

function Show-DockerInstallInstructions {
    Write-Host ""
    Write-Fail "Docker is required and wasn't found on this machine."
    Write-Host ""
    Write-Host "  Windows" -ForegroundColor White
    Write-Host "    1. Download Docker Desktop: https://www.docker.com/products/docker-desktop/"
    Write-Host "    2. Run the installer (it will enable WSL2 if needed - accept any prompt"
    Write-Host "       to restart your machine)."
    Write-Host "    3. Launch Docker Desktop from the Start menu and wait for it to say"
    Write-Host "       'Docker Desktop is running' in the system tray."
    Write-Host "    4. Re-run this script: .\setup.ps1"
    Write-Host ""
}

$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Show-DockerInstallInstructions
    exit 1
}
$dockerVersion = (docker --version) 2>$null
Write-Ok "Docker is installed ($dockerVersion)"

docker info *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker is installed but doesn't seem to be running."
    Write-Host "  Start Docker Desktop from the Start menu, wait for it to finish starting, then re-run this script."
    exit 1
}
Write-Ok "Docker daemon is running"

$ComposeCmd = $null
docker compose version *>$null
if ($LASTEXITCODE -eq 0) {
    $ComposeCmd = @("docker", "compose")
} elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $ComposeCmd = @("docker-compose")
} else {
    Write-Host ""
    Write-Fail "Docker Compose is required and wasn't found."
    Write-Host "  Docker Compose ships with Docker Desktop - if you just installed Docker"
    Write-Host "  Desktop, restart it."
    exit 1
}
Write-Ok "Docker Compose is available ($($ComposeCmd -join ' '))"

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    $exe = $ComposeCmd[0]
    $prefixArgs = @()
    if ($ComposeCmd.Length -gt 1) { $prefixArgs = $ComposeCmd[1..($ComposeCmd.Length - 1)] }
    & $exe @prefixArgs @ComposeArgs
}

# ---------------------------------------------------------------------------
# 2. Environment configuration
# ---------------------------------------------------------------------------
Write-Heading "2/3 Configuring environment (.env)"

function Set-EnvVar {
    param([string]$Key, [string]$Value)
    $path = ".env"
    $line = "$Key=$Value"
    if (Test-Path $path) {
        $content = Get-Content $path
        $pattern = "^$([regex]::Escape($Key))="
        if ($content | Select-String -Pattern $pattern -Quiet) {
            $content = $content | ForEach-Object {
                if ($_ -match $pattern) { $line } else { $_ }
            }
            Set-Content -Path $path -Value $content
            return
        }
    }
    Add-Content -Path $path -Value $line
}

function Get-EnvVar {
    param([string]$Key)
    if (-not (Test-Path ".env")) { return "" }
    $match = Get-Content ".env" | Select-String -Pattern "^$([regex]::Escape($Key))=" | Select-Object -Last 1
    if (-not $match) { return "" }
    return ($match.ToString() -replace "^$([regex]::Escape($Key))=", "")
}

function New-Secret {
    -join ((1..3) | ForEach-Object { [guid]::NewGuid().ToString("N") }) | ForEach-Object { $_.Substring(0, 64) }
}

function Read-Prompt {
    param([string]$Question, [string]$Default = "")
    if ($NonInteractive) { return $Default }
    if ($Default) {
        $answer = Read-Host "  $Question [$Default]"
    } else {
        $answer = Read-Host "  $Question (press Enter to skip)"
    }
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer
}

function Read-PromptSecret {
    param([string]$Question)
    if ($NonInteractive) { return "" }
    $secure = Read-Host "  $Question (press Enter to skip, input hidden)" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$FirstTimeSetup = $false
if (-not (Test-Path ".env")) {
    $FirstTimeSetup = $true
    Copy-Item ".env.example" ".env"
    Write-Ok "Created .env from .env.example"
} elseif ($Reconfigure) {
    $FirstTimeSetup = $true
    Write-Ok ".env already exists - re-running credential prompts (-Reconfigure)"
} else {
    Write-Ok ".env already exists - leaving it as-is (use -Reconfigure to re-prompt)"
}

if ($FirstTimeSetup) {
    if ($NonInteractive) {
        Write-WarnMsg "Non-interactive mode: leaving credential fields blank in .env."
        Write-WarnMsg "Edit .env yourself before the app will be able to send/receive email or use AI features."
    } else {
        Write-Host ""
        Write-Host "  A few quick questions to get you running - press Enter on any"
        Write-Host "  question to skip it and fill it in later by editing .env."
        Write-Host ""
    }

    Write-Host "  AI provider (required for the app to draft/classify anything)" -ForegroundColor White
    $anthropicKey = Read-PromptSecret "Anthropic API key (from https://console.anthropic.com/)"
    if ($anthropicKey) {
        Set-EnvVar "ANTHROPIC_API_KEY" $anthropicKey
        Write-Ok "Anthropic API key saved"
    } else {
        Write-WarnMsg "No Anthropic API key set - add ANTHROPIC_API_KEY to .env before using the app"
    }

    Write-Host ""
    Write-Host "  Outlook email (SMTP for sending, IMAP for reading replies)" -ForegroundColor White
    $outlookEmail = Read-Prompt "Outlook email address" ""
    if ($outlookEmail) {
        $outlookPassword = Read-PromptSecret "Outlook password or app password"
        Set-EnvVar "SMTP_HOST" "smtp.office365.com"
        Set-EnvVar "SMTP_PORT" "587"
        Set-EnvVar "SMTP_USE_TLS" "true"
        Set-EnvVar "SMTP_USERNAME" $outlookEmail
        Set-EnvVar "FROM_EMAIL" $outlookEmail
        Set-EnvVar "IMAP_HOST" "outlook.office365.com"
        Set-EnvVar "IMAP_PORT" "993"
        Set-EnvVar "IMAP_USERNAME" $outlookEmail
        if ($outlookPassword) {
            Set-EnvVar "SMTP_PASSWORD" $outlookPassword
            Set-EnvVar "IMAP_PASSWORD" $outlookPassword
        }
        Write-Ok "Outlook SMTP/IMAP configured for $outlookEmail"
        Write-WarnMsg "If your Microsoft account uses MFA, use an app password, not your normal password:"
        Write-WarnMsg "  https://support.microsoft.com/en-us/account-billing/manage-app-passwords"
    } else {
        Write-WarnMsg "Skipped - outbound sending and reply polling won't work until SMTP_*/IMAP_* are set in .env"
    }

    Write-Host ""
    Write-Host "  HubSpot CRM sync (optional)" -ForegroundColor White
    $hubspotToken = Read-PromptSecret "HubSpot private app access token"
    if ($hubspotToken) {
        Set-EnvVar "HUBSPOT_ACCESS_TOKEN" $hubspotToken
        Set-EnvVar "CRM_PROVIDER" "hubspot"
        Write-Ok "HubSpot CRM sync enabled"
    } else {
        Write-WarnMsg "Skipped - Positive/Interested leads won't be pushed to a CRM"
    }

    Write-Host ""
    Write-Host "  Google Places (for automated lead discovery, optional)" -ForegroundColor White
    $placesKey = Read-PromptSecret "Google Places API key"
    if ($placesKey) {
        Set-EnvVar "GOOGLE_PLACES_API_KEY" $placesKey
        Set-EnvVar "LEAD_DISCOVERY_ENABLED" "true"
        Write-Ok "Automated lead discovery enabled"
    } else {
        Write-WarnMsg "Skipped - automated daily lead discovery stays off (LEAD_DISCOVERY_ENABLED=false)"
    }

    Write-Host ""
    Write-Host "  Dashboard admin access" -ForegroundColor White
    $adminKey = New-Secret
    $jwtSecret = New-Secret
    Set-EnvVar "ADMIN_API_KEY" $adminKey
    Set-EnvVar "JWT_SECRET" $jwtSecret
    Write-Ok "Generated a secure admin API key and JWT signing secret"
}

# ---------------------------------------------------------------------------
# 3. Build, boot, and migrate
# ---------------------------------------------------------------------------
Write-Heading "3/3 Building and starting the stack"

Write-Info "Running: $($ComposeCmd -join ' ') up --build -d"
Invoke-Compose up --build -d
if ($LASTEXITCODE -ne 0) {
    Write-Fail "docker compose up failed - see the output above."
    exit 1
}

$AppPort = Get-EnvVar "APP_PORT"
if (-not $AppPort) { $AppPort = "8000" }

function Wait-ForContainerHealth {
    param([string]$Container, [int]$TimeoutSeconds = 90)
    Write-Info "Waiting for $Container to become healthy..."
    $waited = 0
    while ($waited -lt $TimeoutSeconds) {
        $status = (docker inspect --format='{{.State.Health.Status}}' $Container 2>$null)
        if ($status -eq "healthy") {
            Write-Ok "$Container is healthy"
            return $true
        }
        Start-Sleep -Seconds 2
        $waited += 2
    }
    Write-WarnMsg "$Container didn't report healthy within ${TimeoutSeconds}s (status: $status) - continuing anyway"
    return $false
}

Wait-ForContainerHealth -Container "sdr-postgres" -TimeoutSeconds 90 | Out-Null

Write-Info "Waiting for the database migration to finish..."
$migrateWaited = 0
$migrateTimeout = 120
$migrateExit = ""
while ($migrateWaited -lt $migrateTimeout) {
    $migrateExit = (docker inspect --format='{{.State.ExitCode}}' sdr-migrate 2>$null)
    $migrateRunning = (docker inspect --format='{{.State.Running}}' sdr-migrate 2>$null)
    if ($migrateExit -and $migrateRunning -ne "true") {
        break
    }
    Start-Sleep -Seconds 2
    $migrateWaited += 2
}

if ($migrateExit -eq "0") {
    Write-Ok "Database schema is up to date (alembic upgrade head succeeded)"
} else {
    Write-Fail "Database migration did not complete successfully (exit code: $(if ($migrateExit) { $migrateExit } else { 'unknown' }))"
    Write-Host "  Check the logs with: $($ComposeCmd -join ' ') logs migrate"
    Write-Host "  You can retry it with: $($ComposeCmd -join ' ') run --rm migrate"
}

Write-Info "Waiting for the API to respond..."
$apiWaited = 0
$apiTimeout = 90
$apiUp = $false
while ($apiWaited -lt $apiTimeout) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$AppPort/health" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $apiUp = $true; break }
    } catch {
        # not up yet
    }
    Start-Sleep -Seconds 2
    $apiWaited += 2
}

Write-Host ""
if ($apiUp) {
    Write-Host "Setup complete - the AI SDR backend is running." -ForegroundColor Green
} else {
    Write-WarnMsg "The API didn't respond within ${apiTimeout}s yet - it may still be starting."
    Write-WarnMsg "Check progress with: $($ComposeCmd -join ' ') logs -f sdr-backend"
}
Write-Host ""
Write-Host "  Dashboard:  http://localhost:$AppPort/dashboard" -ForegroundColor White
$adminKeyInEnv = Get-EnvVar "ADMIN_API_KEY"
if ($adminKeyInEnv) {
    Write-Host "  Admin key:  $adminKeyInEnv" -ForegroundColor White
    Write-Host ""
    Write-Host "  The dashboard requires this admin key. Either:"
    Write-Host "    - open http://localhost:$AppPort/dashboard?token=$adminKeyInEnv in your browser, or"
    Write-Host "    - use a REST client / curl with header: X-API-Key: $adminKeyInEnv"
} else {
    Write-Host ""
    Write-Host "  No ADMIN_API_KEY is set - the dashboard is unauthenticated. Set ADMIN_API_KEY in .env for anything beyond local testing." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  API docs:   http://localhost:$AppPort/docs" -ForegroundColor White
Write-Host "  Approval queue (leads that replied positively, waiting on you to approve/reject the AI's"
Write-Host "  drafted response) is on the dashboard above, or via GET /approvals with the admin key."
Write-Host ""
Write-Host "  Useful commands:"
Write-Host "    $($ComposeCmd -join ' ') logs -f          # tail all logs"
Write-Host "    $($ComposeCmd -join ' ') ps               # see service status"
Write-Host "    $($ComposeCmd -join ' ') down             # stop everything"
Write-Host "    .\setup.ps1 -Reconfigure            # re-run the credential prompts"
Write-Host ""
