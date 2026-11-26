<#
.SYNOPSIS
  Step 1 of UpgradeLens: checks the config and prerequisites. Same as phase0-setup.ps1 -CheckOnly.

.EXAMPLE
  .\scripts\check-prereq.ps1
#>
param(
    [string]$Config = (Join-Path $PSScriptRoot "..\config\upgrade-lens.json")
)

& (Join-Path $PSScriptRoot "phase0-setup.ps1") -Config $Config -CheckOnly
exit $LASTEXITCODE
