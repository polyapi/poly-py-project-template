# Dot-source this file once, then run flow setup. No execution-policy changes.
if (Get-Command flow -ErrorAction SilentlyContinue) {
    Write-Warning 'flow already exists; keeping the existing command.'
    return
}

function global:flow {
    $flowRoot = (Get-Location).Path
    while (-not (Test-Path -LiteralPath "$flowRoot/cli/flow.py") -or
           -not (Test-Path -LiteralPath "$flowRoot/pyproject.toml")) {
        $flowParent = Split-Path -Parent $flowRoot
        if (-not $flowParent -or $flowParent -eq $flowRoot) {
            Write-Error 'Run flow inside a Python Flow project.'
            $global:LASTEXITCODE = 1
            return
        }
        $flowRoot = $flowParent
    }
    if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
        Write-Error 'Install uv and add it to PATH first.'
        $global:LASTEXITCODE = 1
        return
    }
    $flowOldUv = $env:UV_PROJECT_ENVIRONMENT
    $flowStatus = 1
    Push-Location -LiteralPath $flowRoot
    try {
        $env:UV_PROJECT_ENVIRONMENT = '.venv'
        if ($args.Count -gt 0 -and $args[0] -eq 'setup') {
            & uv sync --locked
            if ($LASTEXITCODE -ne 0) { $flowStatus = $LASTEXITCODE; return }
        }
        $flowActivate = Join-Path $flowRoot '.venv/Scripts/Activate.ps1'
        if (-not (Test-Path -LiteralPath $flowActivate)) {
            $flowActivate = Join-Path $flowRoot '.venv/bin/Activate.ps1'
        }
        if (-not (Test-Path -LiteralPath $flowActivate)) {
            Write-Error 'Run flow setup first to create .venv.'
            return
        }
        if ($env:VIRTUAL_ENV -ne (Join-Path $flowRoot '.venv')) {
            . $flowActivate
        }
        if ($args.Count -gt 0 -and $args[0] -eq 'setup') {
            $flowSetupArgs = @($args | Select-Object -Skip 1)
            & uv run --locked python cli/flow.py setup --shell powershell --profile $PROFILE.CurrentUserAllHosts @flowSetupArgs
            $flowStatus = $LASTEXITCODE
            if ($flowStatus -ne 0) { return }
            $flowEnv = & uv run --locked python -m cli.setup --print-envpath
            $flowStatus = $LASTEXITCODE
            if ($flowStatus -eq 0) { $env:ENVPATH = $flowEnv }
            return
        }
        $flowEnv = & uv run --locked python -m cli.setup --print-envpath
        $flowStatus = $LASTEXITCODE
        if ($flowStatus -ne 0) { return }
        $env:ENVPATH = $flowEnv
        & uv run --locked python cli/flow.py @args
        $flowStatus = $LASTEXITCODE
    }
    finally {
        $env:UV_PROJECT_ENVIRONMENT = $flowOldUv
        Pop-Location
        $global:LASTEXITCODE = $flowStatus
    }
}
