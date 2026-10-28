<#
.SYNOPSIS
  Runs every check that works on a laptop without AWS: Python lint + tests, repo validation,
  Helm lint, Kustomize renders, Terraform fmt/validate, platform-doctor on both fixtures.
.EXAMPLE
  .\gitops-config\scripts\run_local_checks.ps1   # run from the workspace holding all three repos
  .\gitops-config\scripts\run_local_checks.ps1 -ToolsDir C:\tools     # folder holding helm.exe, kustomize.exe, terraform.exe
#>
param([string]$ToolsDir = "")

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ($ToolsDir) { $env:PATH = "$ToolsDir;$ToolsDir\mingit\cmd;$env:PATH" }
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    python -m venv (Join-Path $Root ".venv")
    & $Py -m pip install -q -r (Join-Path $Root "secure-event-api\requirements-dev.txt")
}

$results = [ordered]@{}
function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    & $Body
    $results[$Name] = if ($LASTEXITCODE -eq 0) { "PASS" } else { "FAIL" }
}
function Have([string]$Tool) { [bool](Get-Command $Tool -ErrorAction SilentlyContinue) }

Push-Location (Join-Path $Root "secure-event-api")
Step "app: ruff lint"   { & $Py -m ruff check . ; if ($LASTEXITCODE -eq 0) { & $Py -m ruff format --check . } }
Step "app: unit tests"  { & $Py -m pytest -q -p no:cacheprovider }
Pop-Location

Push-Location (Join-Path $Root "gitops-config\ops")
Step "ops: ruff lint"   { & $Py -m ruff check . ; if ($LASTEXITCODE -eq 0) { & $Py -m ruff format --check . } }
Step "ops: platform-doctor tests" { & $Py -m pytest -q -p no:cacheprovider }
Pop-Location

Step "repo: static validation" { & $Py (Join-Path $PSScriptRoot "validate_repo.py") }

$Gitops = Join-Path $Root "gitops-config"
if (Have helm) {
    Step "helm: lint --strict" { helm lint --strict (Join-Path $Gitops "charts\secure-event-api") }
} else { $results["helm: lint --strict"] = "SKIP (helm not found)" }

if ((Have kustomize) -and (Have helm)) {
    $helmExe = (Get-Command helm).Source
    foreach ($envName in "staging", "production") {
        Step "kustomize: build $envName" {
            kustomize build --enable-helm --helm-command $helmExe (Join-Path $Gitops "environments\$envName") | Out-Null
        }
    }
    Step "kustomize: build monitoring" { kustomize build (Join-Path $Gitops "platform\monitoring\config") | Out-Null }
} else { $results["kustomize builds"] = "SKIP (kustomize/helm not found)" }

if (Have terraform) {
    $env:TF_DATA_DIR = Join-Path $env:TEMP "secure-platform-tfdata"
    Push-Location (Join-Path $Root "infra\terraform")
    Step "terraform: fmt -check" { terraform fmt -check -recursive }
    if (Have git) {
        Step "terraform: validate" { terraform init -backend=false -input=false | Out-Null; terraform validate }
    } else { $results["terraform: validate"] = "SKIP (git needed to fetch modules)" }
    Pop-Location
} else { $results["terraform"] = "SKIP (terraform not found)" }

$Doctor = Join-Path $Root "gitops-config\ops\platform-doctor\platform_doctor.py"
Step "platform-doctor: healthy fixture => exit 0" { & $Py $Doctor --fixture (Join-Path $Root "gitops-config\ops\platform-doctor\fixtures\healthy") | Out-Null }
Step "platform-doctor: incident fixture => exit 2" {
    & $Py $Doctor --fixture (Join-Path $Root "gitops-config\ops\platform-doctor\fixtures\incident") | Out-Null
    $global:LASTEXITCODE = if ($LASTEXITCODE -eq 2) { 0 } else { 1 }
}

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object {
    $color = if ($_.Value -eq "PASS") { "Green" } elseif ($_.Value -like "SKIP*") { "Yellow" } else { "Red" }
    Write-Host ("{0,-46} {1}" -f $_.Key, $_.Value) -ForegroundColor $color
}
if ($results.Values -contains "FAIL") { exit 1 } else { exit 0 }
