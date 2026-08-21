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

function Invoke-CheckedCommand {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [Parameter()][string[]]$ArgumentList = @(),
    [switch]$SuppressOutput
  )
  if ($SuppressOutput) {
    & $FilePath @ArgumentList | Out-Null
  } else {
    & $FilePath @ArgumentList
  }
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    throw "$FilePath exited with code $exitCode"
  }
}

function Test-HexValue {
  param(
    [string]$Value,
    [int]$Length
  )
  return $Value -match "^[0-9a-f]{$Length}$"
}

function Resolve-PackagePath {
  param([string]$Value)
  if (-not $Value) {
    throw "package_path is required"
  }
  if ($Value -eq ".") {
    return "."
  }
  if ([System.IO.Path]::IsPathRooted($Value)) {
    throw "package_path must stay relative to the runtime checkout"
  }
  $segments = $Value -split "[\\/]+"
  if (-not $segments -or ($segments | Where-Object { -not $_ -or $_ -eq "." -or $_ -eq ".." })) {
    throw "package_path must stay relative to the runtime checkout"
  }
  return ($segments -join "/")
}

function Resolve-CanonicalContract {
  if ($SkillLockPath) {
    if ($Repository -or $ExpectedLockSha256 -or $Pin) {
      throw "Use either -SkillLockPath or explicit runtime contract parameters, not both"
    }
    $lockDocument = Get-Content -Raw -LiteralPath $SkillLockPath | ConvertFrom-Json
    if (-not $lockDocument) {
      throw "skill lock document is empty"
    }
    $resolvedRepository = [string]$lockDocument.runtime_repository
    $resolvedPin = [string]$lockDocument.runtime_commit
    $resolvedLock = [string]$lockDocument.uv_lock_sha256
    $resolvedPackagePath = Resolve-PackagePath ([string]$lockDocument.package_path)
    if (-not $resolvedRepository) {
      throw "skill lock is missing runtime_repository"
    }
    if (-not (Test-HexValue $resolvedPin 40)) {
      throw "skill lock runtime_commit must be a full 40 character SHA"
    }
    if (-not (Test-HexValue $resolvedLock 64)) {
      throw "skill lock uv_lock_sha256 must be a 64 character SHA-256"
    }
    return @{
      Repository = $resolvedRepository
      Pin = $resolvedPin.ToLowerInvariant()
      ExpectedLockSha256 = $resolvedLock.ToLowerInvariant()
      PackagePath = $resolvedPackagePath
    }
  }

  if (-not $Repository) {
    throw "Repository is required"
  }
  if (-not $ExpectedLockSha256) {
    throw "An exact uv.lock SHA-256 is required"
  }
  if (-not $Pin) {
    throw "An exact runtime commit is required; floating refs like '$Ref' are not allowed for privileged use"
  }
  if (-not (Test-HexValue $Pin 40)) {
    throw "Pin must be a full 40 character SHA"
  }
  $normalizedLock = $ExpectedLockSha256.ToLowerInvariant()
  if (-not (Test-HexValue $normalizedLock 64)) {
    throw "ExpectedLockSha256 must be a 64 character SHA-256"
  }
  return @{
    Repository = $Repository
    Pin = $Pin.ToLowerInvariant()
    ExpectedLockSha256 = $normalizedLock
    PackagePath = Resolve-PackagePath $PackagePath
  }
}

function Resolve-CheckoutPath {
  param(
    [string]$ResolvedWorkRoot,
    [string]$Sha
  )
  return Join-Path $ResolvedWorkRoot "foundry-opt-$Sha"
}

function Remove-SafeCheckout {
  param(
    [string]$ResolvedWorkRoot,
    [string]$CheckoutPath,
    [string]$Sha
  )
  if (-not (Test-Path -LiteralPath $CheckoutPath)) {
    return
  }
  $resolvedCheckout = (Resolve-Path -LiteralPath $CheckoutPath).ProviderPath
  $leaf = Split-Path -Leaf $resolvedCheckout
  if ($leaf -ne "foundry-opt-$Sha") {
    throw "Refusing to delete unexpected checkout path '$resolvedCheckout'"
  }
  $parent = Split-Path -Parent $resolvedCheckout
  if ($parent -ne $ResolvedWorkRoot) {
    throw "Refusing to delete checkout outside the requested work root"
  }
  Remove-Item -LiteralPath $resolvedCheckout -Recurse -Force
}

function Resolve-ProjectRoot {
  param(
    [string]$CheckoutPath,
    [string]$RelativePath
  )
  if ($RelativePath -eq ".") {
    return $CheckoutPath
  }
  $candidate = Join-Path $CheckoutPath ($RelativePath -replace '/', '\')
  $resolved = (Resolve-Path -LiteralPath $candidate).ProviderPath
  $checkoutUri = [System.Uri](($CheckoutPath.TrimEnd('\') + '\'))
  $resolvedUri = [System.Uri]($resolved)
  if (-not $checkoutUri.IsBaseOf($resolvedUri)) {
    throw "package_path escapes the runtime checkout"
  }
  return $resolved
}

$contract = Resolve-CanonicalContract
$repository = $contract.Repository
$sha = $contract.Pin
$expectedLock = $contract.ExpectedLockSha256
$packagePath = $contract.PackagePath

New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
$resolvedWorkRoot = (Resolve-Path -LiteralPath $WorkRoot).ProviderPath
$checkout = Resolve-CheckoutPath -ResolvedWorkRoot $resolvedWorkRoot -Sha $sha
Remove-SafeCheckout -ResolvedWorkRoot $resolvedWorkRoot -CheckoutPath $checkout -Sha $sha
New-Item -ItemType Directory -Force -Path $checkout | Out-Null

Invoke-CheckedCommand git -ArgumentList @("-C", $checkout, "init") -SuppressOutput
Invoke-CheckedCommand git -ArgumentList @("-C", $checkout, "remote", "add", "origin", $repository)
Invoke-CheckedCommand git -ArgumentList @("-C", $checkout, "fetch", "--depth", "1", "origin", $sha) -SuppressOutput
Invoke-CheckedCommand git -ArgumentList @("-C", $checkout, "checkout", "--detach", $sha) -SuppressOutput
$head = (& git -C $checkout rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
  throw "Unable to verify checked out HEAD"
}
if ($head -ne $sha) {
  throw "Commit verification failed"
}

$projectRoot = Resolve-ProjectRoot -CheckoutPath $checkout -RelativePath $packagePath
$lock = Join-Path $projectRoot "uv.lock"
if (-not (Test-Path -LiteralPath $lock -PathType Leaf)) {
  throw "uv.lock missing"
}
$lockHash = (Get-FileHash -Path $lock -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expectedLock -ne $lockHash) {
  throw "uv.lock hash mismatch"
}

$env:FOUNDRY_OPT_RUNTIME_REPOSITORY = $repository
$env:FOUNDRY_OPT_RUNTIME_COMMIT = $sha
$env:FOUNDRY_OPT_RUNTIME_LOCK_SHA256 = $lockHash
$env:FOUNDRY_OPT_RUNTIME_PACKAGE_PATH = $packagePath

Invoke-CheckedCommand uv -ArgumentList @("sync", "--frozen", "--project", $projectRoot) -SuppressOutput
& uv run --no-sync --project $projectRoot foundry-opt @ForwardedArgs
exit $LASTEXITCODE
