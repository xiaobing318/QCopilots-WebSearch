# Installs only on a clean machine and removes only the state this run owns.
$options = @{}
for ($n = 0; $n -lt $args.Count; $n += 2) {
    $key = [string]$args[$n]
    if ($key -notin @('--CandidateMsi','--BaselineMsi','--OutputDir') -or $n + 1 -ge $args.Count -or $options.ContainsKey($key)) {
        throw 'Use --CandidateMsi FILE --BaselineMsi FILE --OutputDir DIR.'
    }
    $options[$key] = [string]$args[$n + 1]
}
foreach ($key in @('--CandidateMsi','--BaselineMsi','--OutputDir')) {
    if (!$options.ContainsKey($key)) { throw "Missing $key" }
}
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert([bool]$ok, [string]$message) { if (!$ok) { throw $message } }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
Assert ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Run elevated.'
Assert ([Environment]::Is64BitProcess) 'Run 64-bit PowerShell.'
$osInfo = Get-CimInstance Win32_OperatingSystem
Assert ($osInfo.ProductType -eq 1 -and [int]$osInfo.BuildNumber -ge 22000) 'Windows 11 desktop acceptance is required.'
$ServiceName = 'QCopilotsWebSearch'
$started = Get-Date
$installRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'QCopilots-WebSearch'))
$dataRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'QCopilots-WebSearch'))
$exe = Join-Path $installRoot 'bin\qcopilots-websearch.exe'
$config = Join-Path $dataRoot 'service.json'
$token = Join-Path $dataRoot 'client.token'
$owned = $false
$trackedRoots = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$wi = New-Object -ComObject WindowsInstaller.Installer
function Read-Msi([string]$path) {
    $full = (Resolve-Path -LiteralPath $path).ProviderPath
    Assert ([IO.Path]::GetExtension($full) -eq '.msi') 'Expected an MSI file.'
    $db = $wi.OpenDatabase($full, 0)
    try {
        $properties = @{}
        $view = $db.OpenView(('SELECT {0}Property{0}, {0}Value{0} FROM {0}Property{0}' -f [char]96))
        $view.Execute()
        while ($null -ne ($record = $view.Fetch())) {
            $properties[[string]$record.StringData(1)] = [string]$record.StringData(2)
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        }
        $view.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        $view = $db.OpenView(('SELECT {0}Name{0}, {0}StartName{0} FROM {0}ServiceInstall{0}' -f [char]96))
        $view.Execute()
        $record = $view.Fetch()
        Assert ($null -ne $record) 'MSI has no service.'
        $properties.ServiceName = [string]$record.StringData(1)
        $properties.ServiceAccount = [string]$record.StringData(2)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        Assert ($null -eq $view.Fetch()) 'Expected exactly one service.'
        $view.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        $properties.Path = $full
        $properties.Sha256 = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
        return $properties
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($db) }
}
function Registered([string]$code) {
    foreach ($product in $wi.ProductsEx($code, 'S-1-1-0', 7)) { return $true }
    return $false
}
function Owned-Processes {
    @(Get-CimInstance Win32_Process -Filter "Name='qcopilots-websearch.exe'" |
        Where-Object { $_.ExecutablePath -eq $exe -and $_.CreationDate -ge $started })
}
function Owned-Service {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if ($service) { Assert ($owned -and $service.PathName -match ('^"?' + [regex]::Escape($exe) + '"?(?:\s|$)')) 'Unexpected service ownership.' }
    return $service
}
function Wait-Service([string]$state, [int]$seconds) {
    $controller = Get-Service -Name $ServiceName
    try { $controller.WaitForStatus($state, [TimeSpan]::FromSeconds($seconds)) }
    finally { $controller.Dispose() }
}
function Stop-OwnedService {
    [void](Owned-Service)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $controller = Get-Service -Name $ServiceName
    try {
        $controller.Stop()
        $controller.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(10))
    } finally { $controller.Dispose() }
    Assert ($watch.Elapsed.TotalSeconds -le 10) 'Service stop exceeded ten seconds.'
    return $watch.Elapsed.TotalSeconds
}
function Wait-Absent {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        if (!(Get-CimInstance Win32_Service -Filter "Name='$ServiceName'")) { return }
        Start-Sleep -Milliseconds 100
    } while ($watch.Elapsed.TotalSeconds -lt 10)
    throw 'Service remained registered beyond ten seconds.'
}
function Invoke-Msi([hashtable]$metadata, [string]$mode, [string]$label, [int]$expected = 0, [string]$failpoint = '') {
    Assert ((Get-FileHash -LiteralPath $metadata.Path -Algorithm SHA256).Hash -eq $metadata.Sha256) 'MSI bytes changed.'
    $log = Join-Path $runDir ($label + '.log')
    $msiArgs = @($mode, ('"' + $metadata.Path + '"'), '/qn', '/norestart', 'REBOOT=ReallySuppress', '/L*v', ('"' + $log + '"'))
    if ($failpoint) { $msiArgs += "QCWS_TEST_FAILPOINT=$failpoint" }
    Write-Host $label
    $process = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" -ArgumentList $msiArgs -WindowStyle Hidden -PassThru
    try {
        if (!$process.WaitForExit(300000)) {
            $process.Kill()
            [void]$process.WaitForExit(5000)
            throw 'MSI transaction did not finish within five minutes. Its owned client was stopped.'
        }
        $code = $process.ExitCode
    } finally { $process.Dispose() }
    foreach ($root in @($installRoot, $dataRoot)) { if (Test-Path -LiteralPath $root) { [void]$trackedRoots.Add($root) } }
    Assert ($code -eq $expected) "$label exited $code, expected $expected. See $log."
    if ($failpoint) {
        $action = if ($failpoint -eq 'afterInitialize') { 'QcwsFailAfterInitialize' } else { 'QcwsFailAfterStartServices' }
        Assert ([IO.File]::ReadAllText($log) -match ("CustomAction\s+" + $action + "\s+returned actual error code\s+1\b")) 'Injected action did not return 1.'
    }
}
function Assert-Running([hashtable]$metadata) {
    Assert (Registered $metadata.ProductCode) 'Expected product registration is missing.'
    Wait-Service 'Running' 30
    $service = Owned-Service
    Assert ($service.StartName -eq "NT SERVICE\$ServiceName" -and $service.StartMode -eq 'Auto') 'Service account/start mode changed.'
    $version = & $exe --Version 2>&1
    Assert ($LASTEXITCODE -eq 0 -and "$version" -match ([regex]::Escape($metadata.ProductVersion))) 'Installed binary version mismatch.'
    $settings = [IO.File]::ReadAllText($config) | ConvertFrom-Json
    $secretValue = [IO.File]::ReadAllText($token).Trim()
    try {
        $uri = 'http://' + $settings.control.host + ':' + $settings.control.port + '/health/live'
        $live = Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers @{ Authorization = 'Bearer ' + $secretValue } -TimeoutSec 5
        Assert ($live.StatusCode -eq 200 -and ($live.Content | ConvertFrom-Json).live) 'Live health check failed.'
    } finally { $secretValue = $null }
    $modules = (Get-Process -Id $service.ProcessId).Modules | Where-Object { $_.ModuleName -like 'Qt6*.dll' }
    Assert (@($modules).Count -ge 3) 'Qt libraries were not observable.'
    foreach ($module in $modules) { Assert ($module.FileName.StartsWith($installRoot + '\', [StringComparison]::OrdinalIgnoreCase)) 'A Qt library came from a development environment.' }
    $leftovers = @(Get-ChildItem -LiteralPath $dataRoot -Force -File | Where-Object { $_.Name -like '.qcws-install-*' -or $_.Name -like '*.new' -or $_.Name -like '*.tmp' })
    Assert ($leftovers.Count -eq 0) 'Installer receipt/staging file remained.'
    $installed = @($wi.ProductsEx($metadata.ProductCode, '', 4))
    Assert ($installed.Count -eq 1 -and $installed[0].InstallProperty('VersionString') -eq $metadata.ProductVersion) 'Registered version mismatch.'
}
function Snapshot([string[]]$paths) {
    $result = @{}
    foreach ($path in $paths) { $result[$path] = @{ Hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash; Acl = (Get-Acl -LiteralPath $path).Sddl } }
    return $result
}
function Assert-Snapshot([hashtable]$snapshot) {
    foreach ($path in $snapshot.Keys) {
        Assert ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $snapshot[$path].Hash) "Changed file: $path"
        Assert ((Get-Acl -LiteralPath $path).Sddl -eq $snapshot[$path].Acl) "Changed ACL: $path"
    }
}
function Remove-OwnedTree([string]$root) {
    $absolute = [IO.Path]::GetFullPath($root).TrimEnd('\')
    Assert ($owned -and $trackedRoots.Contains($absolute) -and (@($installRoot, $dataRoot) -contains $absolute)) 'Refusing unowned delete.'
    if (!(Test-Path -LiteralPath $absolute)) { return }
    Assert ((Resolve-Path -LiteralPath $absolute).ProviderPath.TrimEnd('\') -eq $absolute) 'Cleanup root changed.'
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($absolute)
    while ($pending.Count) {
        $directory = $pending.Pop()
        Assert (!((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Reparse point in cleanup tree.'
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            Assert (!($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Reparse point in cleanup tree.'
            Assert ($item.FullName.StartsWith($absolute + '\', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup child escaped root.'
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
    Remove-Item -LiteralPath $absolute -Recurse -Force
}
$candidate = Read-Msi $options['--CandidateMsi']
$baseline = Read-Msi $options['--BaselineMsi']
foreach ($metadata in @($candidate, $baseline)) {
    [void][guid]::Parse($metadata.ProductCode)
    [void][guid]::Parse($metadata.UpgradeCode)
    Assert ($metadata.ProductVersion -match '^\d+\.\d+\.\d+$') 'Require a three-field MSI version.'
}
Assert ($candidate.ProductName -eq 'QCopilots-WebSearch' -and $baseline.ProductName -eq $candidate.ProductName) 'Expected production packages.'
Assert ($candidate.ServiceName -eq $ServiceName -and $baseline.ServiceName -eq $ServiceName) 'MSI service names disagree.'
Assert ($candidate.ServiceAccount -eq "NT SERVICE\$ServiceName" -and $baseline.ServiceAccount -eq $candidate.ServiceAccount) 'Unexpected MSI service account.'
Assert ($candidate.ProductCode -ne $baseline.ProductCode -and $candidate.UpgradeCode -eq $baseline.UpgradeCode) 'Invalid upgrade identities.'
Assert ([version]$candidate.ProductVersion -gt [version]$baseline.ProductVersion) 'Candidate version must increase.'
Assert (!(Get-CimInstance Win32_Service -Filter "Name='$ServiceName'")) 'Existing user service found. No changes made.'
foreach ($root in @($installRoot, $dataRoot)) { Assert (!(Get-Item -LiteralPath $root -Force -ErrorAction SilentlyContinue)) "Existing user path: $root" }
Assert (@($wi.RelatedProducts($candidate.UpgradeCode)).Count -eq 0) 'Related product already registered.'
foreach ($port in @(8088,8089)) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    $listener.Server.ExclusiveAddressUse = $true
    try { $listener.Start() } catch { throw "Required service port $port is already in use. No changes made." }
    finally { $listener.Stop() }
}
foreach ($product in $wi.ProductsEx('', 'S-1-1-0', 7)) {
    Assert ($product.ProductCode -notin @($candidate.ProductCode, $baseline.ProductCode)) 'Product already registered.'
    Assert ($product.InstallProperty('ProductName') -ne $candidate.ProductName) 'Existing named product found.'
}
$output = [IO.Path]::GetFullPath($options['--OutputDir'])
foreach ($path in @($candidate.Path, $baseline.Path, $output)) {
    foreach ($root in @($installRoot, $dataRoot)) {
        Assert ($path -ne $root -and !$path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) 'Artifacts must be outside install/data roots.'
    }
}
$runDir = Join-Path $output ('msi-acceptance-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($runDir)
$owned = $true
$failure = $null
$cleanupFailures = [Collections.Generic.List[string]]::new()
$stopSeconds = $null
try {
    Invoke-Msi $candidate '/i' '01-fail-after-initialize' 1603 'afterInitialize'
    Wait-Absent
    Assert (!(Registered $candidate.ProductCode)) 'Initial rollback left product registration.'
    Assert (!(Test-Path -LiteralPath $dataRoot) -and !(Test-Path -LiteralPath $installRoot)) 'Initial rollback left directories.'
    Invoke-Msi $baseline '/i' '02-install-baseline'
    Assert-Running $baseline
    $stopSeconds = Stop-OwnedService
    $json = [IO.File]::ReadAllText($config) | ConvertFrom-Json
    $json.cache.search_ttl_seconds = 317
    [IO.File]::WriteAllText($config, ($json | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    $sentinel = Join-Path (Join-Path $dataRoot 'state') ('acceptance-' + [guid]::NewGuid().ToString('N') + '.txt')
    [IO.File]::WriteAllText($sentinel, [guid]::NewGuid().ToString(), [Text.UTF8Encoding]::new($false))
    Start-Service -Name $ServiceName
    Assert-Running $baseline
    $baselineFiles = Snapshot @($exe, $config, $token, $sentinel)
    $preservedFiles = Snapshot @($config, $token, $sentinel)
    Invoke-Msi $candidate '/i' '03-fail-after-start-services' 1603 'afterStartServices'
    Assert (!(Registered $candidate.ProductCode)) 'Failed candidate stayed registered.'
    Assert-Running $baseline
    Assert-Snapshot $baselineFiles
    Invoke-Msi $candidate '/i' '04-upgrade-same-candidate'
    Assert (!(Registered $baseline.ProductCode)) 'Baseline remained registered after upgrade.'
    Assert-Running $candidate
    Assert-Snapshot $preservedFiles
    Invoke-Msi $candidate '/x' '05-uninstall'
    Wait-Absent
    Assert (!(Registered $candidate.ProductCode)) 'Uninstall left product registration.'
    Assert (@(Owned-Processes).Count -eq 0) 'Uninstall left a process.'
    Assert-Snapshot $preservedFiles
} catch { $failure = $_ }
finally {
    foreach ($metadata in @($candidate, $baseline)) {
        try { if (Registered $metadata.ProductCode) { Invoke-Msi $metadata '/x' ('cleanup-' + $metadata.ProductVersion) } }
        catch { $cleanupFailures.Add($_.Exception.Message) }
    }
    try {
        $service = Owned-Service
        if ($service) {
            try { [void](Stop-OwnedService) }
            catch { foreach ($process in Owned-Processes) { Stop-Process -Id $process.ProcessId -Force } }
            $cleanupFailures.Add('MSI cleanup left a service registration. Stopped and retained for diagnosis.')
        }
        foreach ($process in Owned-Processes) { Stop-Process -Id $process.ProcessId -Force }
        Assert (!(Registered $candidate.ProductCode) -and !(Registered $baseline.ProductCode)) 'Cleanup has registered products.'
        Wait-Absent
        foreach ($root in @($installRoot, $dataRoot)) { if (Test-Path -LiteralPath $root) { Remove-OwnedTree $root } }
        Assert (@(Owned-Processes).Count -eq 0) 'Cleanup left a process.'
    } catch { $cleanupFailures.Add($_.Exception.Message) }
    @{ Passed = (!$failure -and !$cleanupFailures.Count); Candidate = $candidate; Baseline = $baseline;
       StopSeconds = $stopSeconds; WindowsBuild = $osInfo.BuildNumber; CleanupErrors = @($cleanupFailures); Error = "$failure" } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'result.json') -Encoding UTF8
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($wi)
}
if ($failure) { throw $failure }
if ($cleanupFailures.Count) { throw ($cleanupFailures -join [Environment]::NewLine) }
Write-Host "PASS. Evidence: $runDir"
