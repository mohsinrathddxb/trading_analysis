[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$TargetDirectory,

    [Parameter(Mandatory = $true)]
    [int]$AppProcessId,

    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug"
)

$ErrorActionPreference = "Stop"
$resolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
$resolvedTargetDirectory = [System.IO.Path]::GetFullPath($TargetDirectory).TrimEnd("\")
$projectPrefix = $resolvedProjectRoot + [System.IO.Path]::DirectorySeparatorChar
$stateRoot = Join-Path $env:LOCALAPPDATA "TelegramStrikeMonitor"
$statusPath = Join-Path $stateRoot "update-status.json"
$logPath = Join-Path $stateRoot "update.log"
$workRoot = Join-Path $resolvedProjectRoot ".update-work"
$stagingDirectory = Join-Path $workRoot "staging"
$backupDirectory = Join-Path $workRoot "backup"
$projectPath = Join-Path $resolvedProjectRoot "src\TelegramStrikeMonitor.App\TelegramStrikeMonitor.App.csproj"
$solutionPath = Join-Path $resolvedProjectRoot "TelegramStrikeMonitor.sln"
$pythonPath = Join-Path $resolvedProjectRoot "python\.venv\Scripts\python.exe"
$requirementsPath = Join-Path $resolvedProjectRoot "python\requirements.txt"
$deploymentStarted = $false

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

function Write-UpdateLog {
    param([string]$Message)
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Write-UpdateStatus {
    param(
        [bool]$Succeeded,
        [string]$Message
    )
    [pscustomobject]@{
        Succeeded = $Succeeded
        Message = $Message
        Timestamp = [DateTimeOffset]::Now.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

function Assert-SafeWorkspacePath {
    param([string]$Path)
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    if (-not $resolvedPath.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe updater path outside the project workspace: $resolvedPath"
    }
    if ($resolvedPath.Equals($resolvedProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The updater cannot operate on the project root itself."
    }
    return $resolvedPath
}

function Reset-SafeDirectory {
    param([string]$Path)
    $safePath = Assert-SafeWorkspacePath $Path
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $safePath -Force | Out-Null
    return $safePath
}

function Invoke-CheckedCommand {
    param(
        [string]$Description,
        [string]$FilePath,
        [string[]]$Arguments
    )
    Write-UpdateLog $Description
    $previousErrorPreference = $ErrorActionPreference
    $commandExitCode = -1
    try {
        # Python's unittest runner writes normal progress to stderr. Keep that
        # output in the log without turning successful test lines into
        # terminating PowerShell errors.
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $outputLine = if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            }
            else {
                $_.ToString()
            }
            Write-UpdateLog $outputLine
        }
        $commandExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($commandExitCode -ne 0) {
        throw "$Description failed with exit code $commandExitCode."
    }
}

function Start-MonitorApplication {
    $applicationPath = Join-Path $resolvedTargetDirectory "TelegramStrikeMonitor.exe"
    if (-not (Test-Path -LiteralPath $applicationPath)) {
        throw "The application executable was not found after update: $applicationPath"
    }
    Start-Process -FilePath $applicationPath -WorkingDirectory $resolvedTargetDirectory
}

try {
    if (-not (Test-Path -LiteralPath $solutionPath)) {
        throw "Solution not found: $solutionPath"
    }
    if (-not (Test-Path -LiteralPath $projectPath)) {
        throw "Application project not found: $projectPath"
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Python virtual environment not found: $pythonPath"
    }
    Assert-SafeWorkspacePath $resolvedTargetDirectory | Out-Null

    Write-UpdateLog "Update requested for $resolvedTargetDirectory."
    $existingProcess = Get-Process -Id $AppProcessId -ErrorAction SilentlyContinue
    if ($null -ne $existingProcess) {
        Write-UpdateLog "Waiting for application process $AppProcessId to exit."
        Wait-Process -Id $AppProcessId -Timeout 30 -ErrorAction SilentlyContinue
    }
    if (Get-Process -Id $AppProcessId -ErrorAction SilentlyContinue) {
        throw "The running application did not exit within 30 seconds."
    }

    Push-Location $resolvedProjectRoot
    try {
        $trackedChanges = @(& git status --porcelain --untracked-files=no)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect the Git working tree."
        }

        if ($trackedChanges.Count -eq 0) {
            Invoke-CheckedCommand "Fetching updates from GitHub origin/main." "git" @("fetch", "origin", "main")
            $behindText = (& git rev-list --count "HEAD..origin/main").Trim()
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to compare the installed source with origin/main."
            }
            $behindCount = [int]$behindText
            if ($behindCount -gt 0) {
                Invoke-CheckedCommand "Applying $behindCount GitHub update commit(s)." "git" @("merge", "--ff-only", "origin/main")
            }
            else {
                Write-UpdateLog "GitHub source is already current."
            }
        }
        else {
            Write-UpdateLog "Local tracked source changes detected; GitHub pull skipped to preserve them."
        }

        Invoke-CheckedCommand "Synchronizing Python dependencies." $pythonPath @("-m", "pip", "install", "--disable-pip-version-check", "-r", $requirementsPath)
        Invoke-CheckedCommand "Running Python regression tests." $pythonPath @("-m", "unittest", "discover", "-s", "python\tests", "-v")

        New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
        $stagingDirectory = Reset-SafeDirectory $stagingDirectory
        Invoke-CheckedCommand "Building the desktop application." "dotnet" @("build", $projectPath, "-c", $Configuration, "-o", $stagingDirectory, "--nologo")

        $stagedApplication = Join-Path $stagingDirectory "TelegramStrikeMonitor.exe"
        if (-not (Test-Path -LiteralPath $stagedApplication)) {
            throw "The staged application executable was not produced."
        }

        if (Test-Path -LiteralPath $backupDirectory) {
            $safeBackup = Assert-SafeWorkspacePath $backupDirectory
            Remove-Item -LiteralPath $safeBackup -Recurse -Force
        }

        Write-UpdateLog "Deploying the verified application build."
        $deploymentStarted = $true
        Move-Item -LiteralPath $resolvedTargetDirectory -Destination $backupDirectory
        New-Item -ItemType Directory -Path $resolvedTargetDirectory -Force | Out-Null
        Copy-Item -Path (Join-Path $stagingDirectory "*") -Destination $resolvedTargetDirectory -Recurse -Force

        Write-UpdateStatus $true "The application was updated and monitoring restarted automatically."
        Write-UpdateLog "Update completed successfully."
        Start-MonitorApplication
    }
    finally {
        Pop-Location
    }
}
catch {
    $failureMessage = $_.Exception.Message
    Write-UpdateLog "UPDATE FAILED: $failureMessage"

    try {
        if ($deploymentStarted -and (Test-Path -LiteralPath $backupDirectory)) {
            Write-UpdateLog "Restoring the previous application build."
            if (Test-Path -LiteralPath $resolvedTargetDirectory) {
                $safeTarget = Assert-SafeWorkspacePath $resolvedTargetDirectory
                Remove-Item -LiteralPath $safeTarget -Recurse -Force
            }
            Move-Item -LiteralPath $backupDirectory -Destination $resolvedTargetDirectory
        }

        Write-UpdateStatus $false "$failureMessage The previous application build was restarted."
        Start-MonitorApplication
    }
    catch {
        Write-UpdateLog "ROLLBACK OR RESTART FAILED: $($_.Exception.Message)"
        Write-UpdateStatus $false "$failureMessage Automatic rollback or restart also failed; inspect update.log."
    }
    exit 1
}
