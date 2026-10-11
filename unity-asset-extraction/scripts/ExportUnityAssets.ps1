[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]] $InputPath,
    [Parameter(Mandatory = $true)][string] $OutputPath,
    [string] $PythonPath = 'python',
    [ValidateSet('unitypy', 'assetripper')][string] $Backend = 'unitypy',
    [string] $AssetRipperPath,
    [string] $BackendSha256,
    [switch] $InventoryOnly,
    [ValidateRange(0.01, 86400)][double] $TimeoutSeconds = 120,
    [ValidateRange(1, 2147483647)][long] $MaxFiles = 10000,
    [ValidateRange(1, [long]::MaxValue)][long] $MaxBytes = 2147483648,
    [ValidateRange(1, [long]::MaxValue)][long] $MaxOutputBytes = 4294967296,
    [ValidateRange(1, 1000000)][double] $MaxZipRatio = 200,
    [ValidateRange(1, 2147483647)][long] $MaxLogBytes = 8388608
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'extract_unity_assets.py'
$cli = @('-B', $script, '--output', $OutputPath, '--backend', $Backend,
    '--timeout', $TimeoutSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--max-files', "$MaxFiles", '--max-bytes', "$MaxBytes", '--max-output-bytes', "$MaxOutputBytes",
    '--max-zip-ratio', $MaxZipRatio.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--max-log-bytes', "$MaxLogBytes")
foreach ($sourcePath in $InputPath) { $cli += @('--input', $sourcePath) }
if ($InventoryOnly) { $cli += '--inventory-only' }
if ($AssetRipperPath) { $cli += @('--assetripper', $AssetRipperPath) }
if ($BackendSha256) { $cli += @('--backend-sha256', $BackendSha256) }
& $PythonPath @cli
exit $LASTEXITCODE
