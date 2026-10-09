$ErrorActionPreference = 'Stop'

# -- State
$listeners = @(Get-NetTCPConnection -LocalPort 9290 -State Listen -ErrorAction SilentlyContinue)

# -- Functions
foreach ($serviceProcessId in ($listeners.OwningProcess | Sort-Object -Unique)) {
    $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $serviceProcessId"
    if ($serviceProcess.Name -notmatch '^python(w)?\.exe$' -or
        $serviceProcess.CommandLine -notmatch '(run\.py|uvicorn.+server:app)') {
        throw "Port 9290 belongs to an unexpected process ($serviceProcessId); refusing to stop it."
    }
    $termination = Start-Process -FilePath taskkill.exe -ArgumentList @('/F', '/T', '/PID', $serviceProcessId) -WindowStyle Hidden -Wait -PassThru
    if ($termination.ExitCode -ne 0) {
        throw "Failed to stop Shopify Manager process tree ($serviceProcessId)."
    }
    Write-Host "Stopped Shopify Manager (PID: $serviceProcessId)" -ForegroundColor Green
}

if ($listeners.Count -eq 0) {
    Write-Host 'No running Shopify Manager detected.' -ForegroundColor Yellow
}
