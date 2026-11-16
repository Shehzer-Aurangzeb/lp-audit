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
    default { Need-Docker; docker @Compose run --rm audit @args }
}
