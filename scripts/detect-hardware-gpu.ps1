[CmdletBinding()]
param(
    [string] $GithubOutput = ""
)

$ErrorActionPreference = "Stop"

$adapters = @(
    Get-CimInstance Win32_VideoController -ErrorAction Stop |
        Where-Object {
            $_.Status -eq "OK" -and
            $_.PNPDeviceID -match '^PCI\\VEN_(10DE|1002|1022|8086)'
        }
)

$available = $adapters.Count -gt 0
$value = $available.ToString().ToLowerInvariant()

if ($GithubOutput) {
    Add-Content -LiteralPath $GithubOutput -Value "available=$value"
}

if ($available) {
    $names = ($adapters | ForEach-Object { $_.Name }) -join ", "
    Write-Host "Hardware GPU available: $names"
} else {
    Write-Warning "No physical Intel, AMD, or NVIDIA PCI display adapter is available. Accelerated CEF visual proofs require a real GPU runner and are not proven by this host."
}
