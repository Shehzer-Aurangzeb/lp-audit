# lp-audit launcher (Windows). `lp-audit help` lists the commands.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$env:LP_AUDIT_DIR = $Root
$Compose = @("compose", "--project-directory", $Root, "-f", (Join-Path $Root "docker-compose.yml"))

function Need-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "Docker is not installed. Install Docker Desktop (WSL 2), then run: lp-audit setup"; exit 1
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Write-Host "Docker is not running. Start Docker Desktop and try again."; exit 1 }
}

$cmd = if ($args.Count -gt 0) { $args[0] } else { "help" }
$rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

switch ($cmd) {
    { $_ -in "help", "-h", "--help" } { Get-Content (Join-Path $PSScriptRoot "help.txt"); break }
    "setup" {
        $envFile = Join-Path $Root ".env"
        if (-not (Test-Path $envFile)) {
            Copy-Item (Join-Path $Root ".env.example") $envFile
            Write-Host "Created .env. Open it and fill in your keys: $envFile"
        }
        Need-Docker
        if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Host "Ollama: found" }
        else { Write-Host "Ollama: not found. Install it from ollama.com, then: ollama pull qwen2.5:3b" }
        docker @Compose build; docker @Compose run --rm -T audit; break
    }
    "update" { git -C $Root pull --ff-only; Need-Docker; docker @Compose build; docker @Compose run --rm -T audit; break }
    "build"  { Need-Docker; docker @Compose build; break }
    "test"   { Need-Docker; docker @Compose run --rm -T audit; break }
    "open"   { New-Item -ItemType Directory -Force (Join-Path $Root "reports") | Out-Null; Invoke-Item (Join-Path $Root "reports"); break }
    "code" {
        $dir = (Get-Location).Path
        if ($rest.Count -gt 0 -and (Test-Path $rest[0] -PathType Container)) {
            $dir = (Resolve-Path $rest[0]).Path
            $rest = if ($rest.Count -gt 1) { $rest[1..($rest.Count - 1)] } else { @() }
        }
        Need-Docker
        $env:CODE = $dir
        docker @Compose run --rm -e "CODE_NAME=$(Split-Path -Leaf $dir)" audit code /code @rest
        break
    }
    "launch" {
        # lp-audit launch <url> --code <folder>: the folder is mounted at /code
        $dir = $null; $pass = @()
        for ($i = 0; $i -lt $rest.Count; $i++) {
            if ($rest[$i] -eq "--code" -and $i + 1 -lt $rest.Count) { $dir = $rest[$i + 1]; $pass += @("--code", "/code"); $i++ }
            else { $pass += $rest[$i] }
        }
        Need-Docker
        if ($dir) {
            if (-not (Test-Path $dir -PathType Container)) { Write-Host "Not a folder: $dir"; exit 1 }
            $env:CODE = (Resolve-Path $dir).Path
            docker @Compose run --rm -e "CODE_NAME=$(Split-Path -Leaf $env:CODE)" audit launch @pass
        } else { docker @Compose run --rm audit launch @pass }
        exit $LASTEXITCODE
    }
    "watch" {
        # Run by Task Scheduler (see "weekly"). A window opens only when something needs attention.
        $ErrorActionPreference = "Continue"   # docker writes progress to stderr; that is not an error
        $url = $rest -join " "
        function Show-Alert($text) {
            Add-Type -AssemblyName PresentationFramework
            [System.Windows.MessageBox]::Show($text, "lp-audit weekly check") | Out-Null
        }
        docker info *> $null
        if ($LASTEXITCODE -ne 0) { Show-Alert "The weekly check of $url did not run: Docker Desktop is not running.`nStart Docker, then run: lp-audit watch $url"; exit 1 }
        $out = docker @Compose run --rm -T audit watch @rest 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        $out | Write-Host
        if ($code -eq 3) {
            $lines = ($out | Where-Object { $_ -like "ALERT:*" }) -join "`n"
            Show-Alert "Real visitors got slower on $url`n`n$lines`n`nReport: $Root\reports"
        } elseif ($code -ne 0) { Show-Alert "The weekly check of $url failed. Run it by hand to see why: lp-audit watch $url" }
        exit $code
    }
    "weekly" {
        if ($rest.Count -lt 1) { Write-Host "Use: lp-audit weekly <url>    stop it: lp-audit weekly <url> --stop"; exit 1 }
        $url = $rest[0]; $u = [uri]$url
        $name = "lp-audit weekly " + $u.Host + ($u.AbsolutePath -replace "[^A-Za-z0-9.-]", "_")
        if ($rest -contains "--stop") {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Stopped: $name"; break
        }
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" watch $url"
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 11am
        # StartWhenAvailable: if the laptop was off on Monday, it runs the next time it is on
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "Done. Every Monday 11:00 (or the next time the laptop is on): lp-audit watch $url"
        Write-Host "A window opens only when real visitors got slower. Docker Desktop must be running."
        Write-Host "Stop it: lp-audit weekly $url --stop"
        break
    }
    default { Need-Docker; docker @Compose run --rm audit @args }
}
