param([switch]$Install)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$runnerRoot = Join-Path $projectRoot '.local-history\actions-runner'
$deployRoot = Join-Path $projectRoot '.local-history\deployment'
$repositoryUrl = 'https://github.com/Qekqq/DevOps_Project'

if ($Install) {
    New-Item -ItemType Directory -Force -Path $runnerRoot, $deployRoot | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $runnerRoot 'config.cmd'))) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $release = Invoke-RestMethod 'https://api.github.com/repos/actions/runner/releases/latest'
        $asset = $release.assets | Where-Object name -Match '^actions-runner-win-x64-[0-9.]+\.zip$' | Select-Object -First 1
        if (-not $asset -or $asset.browser_download_url -notlike 'https://github.com/actions/runner/releases/download/*') {
            throw 'Official Windows runner archive was not found.'
        }
        $expectedHash = ''
        if ($asset.digest -match '^sha256:([0-9a-f]{64})$') { $expectedHash = $Matches[1] }
        if (-not $expectedHash) {
            Write-Host 'Copy the SHA256 checksum from GitHub Settings > Actions > Runners > New runner (Windows x64).'
            $expectedHash = (Read-Host 'SHA256').Trim().ToLowerInvariant()
        }
        if ($expectedHash -notmatch '^[0-9a-f]{64}$') { throw 'Invalid SHA256 checksum.' }
        $archive = Join-Path $runnerRoot $asset.name
        Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
            throw 'Runner checksum mismatch. Archive was not executed.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $runnerRoot
        Remove-Item -LiteralPath $archive
    }
    if (-not (Test-Path -LiteralPath (Join-Path $runnerRoot '.runner'))) {
        Write-Host 'Paste the registration token from GitHub here. It will not be saved to a project file.'
        $secureToken = Read-Host 'Runner registration token' -AsSecureString
        $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $registrationToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
            Push-Location $runnerRoot
            try {
                & .\config.cmd --unattended --url $repositoryUrl --token $registrationToken --name 'devops-production-windows' --labels 'devops-production' --work '_work'
                if ($LASTEXITCODE -ne 0) { throw 'Runner registration failed. A fresh token may be required.' }
            } finally { Pop-Location }
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
            $registrationToken = $null
            $secureToken.Dispose()
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $runnerRoot '.runner'))) {
    Write-Host 'Runner is not registered. Run make setup-runner first.'
    exit 1
}

$existing = Get-Process -Name 'Runner.Listener' -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.StartsWith($runnerRoot + '\', [StringComparison]::OrdinalIgnoreCase)
}
if ($existing) {
    Write-Host 'Runner is already running.'
    exit 0
}

$env:DEVOPS_DEPLOY_HOME = $deployRoot
$env:COMPOSE_DISABLE_ENV_FILE = '1'
$runnerProcess = Start-Process -FilePath (Join-Path $runnerRoot 'run.cmd') -WorkingDirectory $runnerRoot -WindowStyle Hidden -PassThru
Write-Host "Runner started in the background (PID $($runnerProcess.Id)). Check its Idle status in GitHub."
