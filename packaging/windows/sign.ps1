<#
Authenticode-sign View's Windows files with Azure Artifact Signing.

Windows 11's Smart App Control (and App Control for Business policies on
managed PCs) refuse to load an unsigned .exe, .dll or .pyd that Microsoft
has no reputation data for. View 0.1.0 shipped hundreds of unsigned files
(the GTK/GStreamer/Python runtime from MSYS2), so it died at startup with
"An Application Control policy has blocked this file". Every binary we
ship needs a trusted signature.

  sign.ps1 -Path dist\View                    every not-yet-signed .exe/.dll/.pyd in the folder
  sign.ps1 -Path dist\View-Setup-0.1.0.exe    one file (Inno Setup's SignTool calls this)

Files that already carry a valid signature (python.org's Python, Microsoft's
onnxruntime) keep it. Needs these environment variables:
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET  an app registration with the
      "Artifact Signing Certificate Profile Signer" role on the signing account
  ARTIFACT_SIGNING_ENDPOINT   e.g. https://eus.codesigning.azure.net/
  ARTIFACT_SIGNING_ACCOUNT    the signing account name
  ARTIFACT_SIGNING_PROFILE    the certificate profile name
#>
param([Parameter(Mandatory)][string[]]$Path)
$ErrorActionPreference = 'Stop'
$ModuleVersion = '0.1.8'

if (-not (Get-Module -ListAvailable ArtifactSigning | Where-Object Version -eq $ModuleVersion)) {
    Install-Module ArtifactSigning -RequiredVersion $ModuleVersion -Force -Scope CurrentUser `
        -Repository PSGallery
}
Import-Module ArtifactSigning -RequiredVersion $ModuleVersion

function Get-Binaries([string[]]$Paths) {
    foreach ($p in $Paths) {
        if (Test-Path -LiteralPath $p -PathType Container) {
            Get-ChildItem -LiteralPath $p -Recurse -File -Include *.exe, *.dll, *.pyd
        } else {
            Get-Item -LiteralPath $p
        }
    }
}
function Get-Unsigned($Files) {
    @($Files | Where-Object { (Get-AuthenticodeSignature -LiteralPath $_.FullName).Status -ne 'Valid' })
}

$files = @(Get-Binaries $Path)
$todo = Get-Unsigned $files
Write-Host "sign.ps1: $($todo.Count) of $($files.Count) files need a signature"
if ($todo.Count -gt 0) {
    Invoke-ArtifactSigning -Endpoint $env:ARTIFACT_SIGNING_ENDPOINT `
        -CodeSigningAccountName $env:ARTIFACT_SIGNING_ACCOUNT `
        -CertificateProfileName $env:ARTIFACT_SIGNING_PROFILE `
        -Files ($todo.FullName -join ',') -FileDigest SHA256 `
        -TimestampRfc3161 'http://timestamp.acs.microsoft.com' -TimestampDigest SHA256
}
$left = Get-Unsigned $todo
if ($left.Count -gt 0) {
    $left | ForEach-Object { Write-Host "  still unsigned: $($_.FullName)" }
    throw "$($left.Count) file(s) are still not signed"
}
