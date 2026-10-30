# Run in the current PowerShell session. Never print the password or connection URL.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot '.env'
if (!(Test-Path -LiteralPath $configPath)) {
    throw 'Missing .env file in the project directory.'
}
$passwordLines = @(Get-Content -LiteralPath $configPath -Encoding UTF8 | Where-Object { $_ -match '^\s*POSTGRES_PASSWORD\s*=' })
if ($passwordLines.Count -ne 1) {
    throw 'Expected exactly one POSTGRES_PASSWORD entry in .env.'
}
$localPassword = ($passwordLines[0] -split '=', 2)[1].Trim()
if ($localPassword.Length -ge 2 -and (
    ($localPassword.StartsWith("'") -and $localPassword.EndsWith("'")) -or
    ($localPassword.StartsWith('"') -and $localPassword.EndsWith('"')))) {
    $localPassword = $localPassword.Substring(1, $localPassword.Length - 2)
}
if ([string]::IsNullOrEmpty($localPassword)) {
    throw 'POSTGRES_PASSWORD must not be empty.'
}
$encodedPassword = [uri]::EscapeDataString($localPassword)
$env:DATABASE_URL = 'postgresql+asyncpg://ossguard:' + $encodedPassword + '@127.0.0.1:15432/ossguard'
Remove-Variable localPassword, encodedPassword, passwordLines
Write-Host 'Local database connection configured (password hidden).'
