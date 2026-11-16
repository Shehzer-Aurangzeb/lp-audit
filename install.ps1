# Windows: makes the "lp-audit" command work in any folder (adds bin\ to your user PATH).
# Run once from the lp-audit folder:  powershell -ExecutionPolicy Bypass -File install.ps1
$bin = Join-Path $PSScriptRoot "bin"
$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (($path -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(";") + ";" + $bin), "User")
    Write-Host "Added $bin to your PATH."
} else {
    Write-Host "$bin is already on your PATH."
}
Write-Host "Close this window, open a new PowerShell, then run:  lp-audit setup"
