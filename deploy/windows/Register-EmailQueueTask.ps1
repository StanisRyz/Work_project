[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [string]$TaskName = 'QualityEcosystem-EmailQueue',

    [string]$LogDirectory,

    [ValidateRange(1, 10000)]
    [int]$BatchSize = 100,

    [Parameter(Mandatory = $true)]
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'
$resolvedProject = (Resolve-Path -LiteralPath $ProjectPath).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$managePy = Join-Path $resolvedProject 'manage.py'
if (-not (Test-Path -LiteralPath $managePy -PathType Leaf)) {
    throw "manage.py was not found: $managePy"
}
$runnerPath = Join-Path $PSScriptRoot 'Invoke-EmailQueue.ps1'
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Queue runner was not found: $runnerPath"
}
if (-not $LogDirectory) {
    $LogDirectory = Join-Path $resolvedProject 'var\log'
}
$escapedRunner = $runnerPath.Replace('"', '`"')
$escapedProject = $resolvedProject.Replace('"', '`"')
$escapedPython = $resolvedPython.Replace('"', '`"')
$escapedLog = $LogDirectory.Replace('"', '`"')

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedRunner`" -ProjectPath `"$escapedProject`" -PythonPath `"$escapedPython`" -BatchSize $BatchSize -LogDirectory `"$escapedLog`"" `
    -WorkingDirectory $resolvedProject
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$password = $Credential.GetNetworkCredential().Password
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $Credential.UserName `
        -Password $password `
        -RunLevel Limited `
        -Force | Out-Null
}
finally {
    $password = $null
}

Write-Host "Task '$TaskName' was registered and will run every minute."
Write-Host "Multiple instance policy: IgnoreNew."
Write-Host "Command logs: $LogDirectory"
