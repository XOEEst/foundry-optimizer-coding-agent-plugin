$ErrorActionPreference = "Stop"
param(
  [Parameter(Mandatory=$true)][string]$Repository,
  [string]$Pin,
  [string]$Ref = "main",
  [string]$WorkRoot = ".",
  [string]$Runtime = "python -m foundry_opt.cli"
)

$resolved = if ($Pin) { $Pin } else { $Ref }
$lsRemote = & git ls-remote $Repository $resolved
if (-not $lsRemote) { throw "Unable to resolve ref" }
$sha = ($lsRemote | Select-Object -First 1).Split("`t")[0]
if ($sha.Length -ne 40) { throw "Expected full SHA" }
$archiveUrl = "$($Repository.TrimEnd('/').Replace('.git',''))/archive/$sha.zip"
$archivePath = Join-Path $WorkRoot "foundry-opt-$sha.zip"
Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$extractRoot = Join-Path $WorkRoot "foundry-opt-$sha"
[System.IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $extractRoot)
$checkout = Get-ChildItem -Path $extractRoot | Select-Object -First 1
$head = & git -C $checkout.FullName rev-parse HEAD
if ($head.Trim() -ne $sha) { throw "Commit verification failed" }
$lock = Join-Path $checkout.FullName "uv.lock"
if (-not (Test-Path $lock)) { throw "uv.lock missing" }
$lockHash = (Get-FileHash -Path $lock -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output (ConvertTo-Json @{ sha = $sha; archive = $archiveUrl; uv_lock_sha256 = $lockHash; runtime = $Runtime } -Compress)
