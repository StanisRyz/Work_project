[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [ValidateRange(1, 10000)]
    [int]$BatchSize = 100,

    [Parameter(Mandatory = $true)]
    [string]$LogDirectory
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$logPath = Join-Path $LogDirectory ("email-queue-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

Push-Location -LiteralPath $ProjectPath
try {
    "[{0}] Starting email queue processing" -f (Get-Date -Format 's') | Out-File -FilePath $logPath -Append -Encoding utf8
    & $PythonPath manage.py process_notification_deliveries --batch-size $BatchSize *>> $logPath
    $commandExitCode = $LASTEXITCODE
    "[{0}] Exit code: {1}" -f (Get-Date -Format 's'), $commandExitCode | Out-File -FilePath $logPath -Append -Encoding utf8
}
catch {
    $_ | Out-File -FilePath $logPath -Append -Encoding utf8
    $commandExitCode = 1
}
finally {
    Pop-Location
}

exit $commandExitCode
