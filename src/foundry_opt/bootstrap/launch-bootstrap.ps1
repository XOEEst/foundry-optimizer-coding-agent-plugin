[CmdletBinding(PositionalBinding=$false)]
param(
  [string]$Repository,
  [string]$ExpectedLockSha256,
  [string]$Pin,
  [string]$Ref = "main",
  [string]$WorkRoot = ".",
  [string]$PackagePath = ".",
  [string]$SkillLockPath,
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardedArgs
)
$ErrorActionPreference = "Stop"

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDirectory "..\..\..")).ProviderPath
$launcher = Join-Path $repositoryRoot "plugins\foundry-bootstrap\scripts\install-runtime.ps1"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
  throw "Canonical launcher not found at $launcher. This compatibility wrapper only works from a source checkout."
}

$arguments = @{
  Repository = $Repository
  ExpectedLockSha256 = $ExpectedLockSha256
  Pin = $Pin
  Ref = $Ref
  WorkRoot = $WorkRoot
  PackagePath = $PackagePath
  SkillLockPath = $SkillLockPath
  ForwardedArgs = $ForwardedArgs
}
if (-not $Repository) { $arguments.Remove("Repository") | Out-Null }
if (-not $ExpectedLockSha256) { $arguments.Remove("ExpectedLockSha256") | Out-Null }
if (-not $Pin) { $arguments.Remove("Pin") | Out-Null }
if (-not $PackagePath) { $arguments.Remove("PackagePath") | Out-Null }
if (-not $SkillLockPath) { $arguments.Remove("SkillLockPath") | Out-Null }

& $launcher @arguments
exit $LASTEXITCODE
