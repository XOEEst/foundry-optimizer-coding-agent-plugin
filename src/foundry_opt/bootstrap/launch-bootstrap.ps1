[CmdletBinding(PositionalBinding=$false)]
param(
  [Parameter(Mandatory=$true)][string]$Repository,
  [string]$ExpectedLockSha256,
  [string]$Pin,
  [string]$Ref = "main",
  [string]$WorkRoot = ".",
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardedArgs
)
$ErrorActionPreference = "Stop"

$resolved = if ($Pin) { $Pin } else { "refs/heads/$Ref" }
if ($Pin -and $Pin -match '^[0-9a-f]{40}$') { $sha = $Pin } else {
  $lsRemote = & git ls-remote $Repository $resolved
  if (-not $lsRemote) { throw "Unable to resolve ref" }
  $sha = ($lsRemote | Select-Object -First 1).Split("`t")[0]
}
if ($sha.Length -ne 40) { throw "Expected full SHA" }
if ($Pin -and -not $ExpectedLockSha256) {
  throw "An explicit pin requires the expected uv.lock SHA-256"
}
$checkout = Join-Path $WorkRoot "foundry-opt-$sha"
if (Test-Path $checkout) {
  Remove-Item -Recurse -Force $checkout
}
New-Item -ItemType Directory -Force -Path $checkout | Out-Null
& git -C $checkout init | Out-Null
& git -C $checkout remote add origin $Repository
& git -C $checkout fetch --depth 1 origin $sha | Out-Null
& git -C $checkout checkout --detach $sha | Out-Null
$head = (& git -C $checkout rev-parse HEAD).Trim()
if ($head.Trim() -ne $sha) { throw "Commit verification failed" }
$lock = Join-Path $checkout "uv.lock"
if (-not (Test-Path $lock)) { throw "uv.lock missing" }
$lockHash = (Get-FileHash -Path $lock -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ExpectedLockSha256 -and $ExpectedLockSha256.ToLowerInvariant() -ne $lockHash) { throw "uv.lock hash mismatch" }
& uv sync --frozen --project $checkout | Out-Null
$env:FOUNDRY_OPT_RUNTIME_REPOSITORY = $Repository
$env:FOUNDRY_OPT_RUNTIME_COMMIT = $sha
$env:FOUNDRY_OPT_RUNTIME_LOCK_SHA256 = $lockHash
& uv run --no-sync --project $checkout foundry-opt @ForwardedArgs
exit $LASTEXITCODE
