# Installs only on a clean machine and removes only the state this run owns.
$options = @{}
for ($n = 0; $n -lt $args.Count; $n += 2) {
    $key = [string]$args[$n]
    if ($key -notin @('--CandidateMsi','--BaselineMsi','--CandidateSHA256','--BaselineSHA256','--OutputDir') -or $n + 1 -ge $args.Count -or $options.ContainsKey($key)) {
        throw 'Use --CandidateMsi FILE --CandidateSHA256 HASH --BaselineMsi FILE --BaselineSHA256 HASH --OutputDir DIR.'
    }
    $options[$key] = [string]$args[$n + 1]
}
foreach ($key in @('--CandidateMsi','--BaselineMsi','--CandidateSHA256','--BaselineSHA256','--OutputDir')) {
    if (!$options.ContainsKey($key)) { throw "Missing $key" }
}
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
function Assert([bool]$ok, [string]$message) { if (!$ok) { throw $message } }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
Assert ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Run elevated.'
Assert ([Environment]::Is64BitProcess) 'Run 64-bit PowerShell.'
$osInfo = Get-CimInstance Win32_OperatingSystem
$supportedDesktop = $osInfo.ProductType -eq 1 -and [int]$osInfo.BuildNumber -ge 22000
$supportedServer = $osInfo.ProductType -in @(2,3) -and [int]$osInfo.BuildNumber -ge 20348
Assert ($supportedDesktop -or $supportedServer) 'Use Windows 11 or Windows Server 2022 or newer.'
$ServiceName = 'QCopilotsWebSearch'
$started = Get-Date
$installRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'QCopilots-WebSearch'))
$dataRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'QCopilots-WebSearch'))
$exe = Join-Path $installRoot 'bin\qcopilots-websearch.exe'
$config = Join-Path $dataRoot 'service.json'
$token = Join-Path $dataRoot 'client.token'
$adminToken = Join-Path $dataRoot 'admin.token'
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
        [void]$view.Execute()
        while ($null -ne ($record = $view.Fetch())) {
            $properties[[string]$record.StringData(1)] = [string]$record.StringData(2)
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        }
        [void]$view.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        $view = $db.OpenView(('SELECT {0}Name{0}, {0}StartName{0} FROM {0}ServiceInstall{0}' -f [char]96))
        [void]$view.Execute()
        $record = $view.Fetch()
        Assert ($null -ne $record) 'MSI has no service.'
        $properties.ServiceName = [string]$record.StringData(1)
        $properties.ServiceAccount = [string]$record.StringData(2)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        Assert ($null -eq $view.Fetch()) 'Expected exactly one service.'
        [void]$view.Close()
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
function Read-ProductProperty($product, [string]$name, [object[]]$indices = @()) {
    # ProductsEx can return IDispatch objects without PowerShell-visible type
    # information. Invoke their documented properties through IDispatch.
    return $product.GetType().InvokeMember($name, [Reflection.BindingFlags]::GetProperty,
                                           $null, $product, $indices)
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
    if ($code -ne $expected -and (Test-Path -LiteralPath $log)) {
        $tail = (Get-Content -LiteralPath $log -Tail 120) -join [Environment]::NewLine
        if ($tail.Length -gt 32768) { $tail = $tail.Substring($tail.Length - 32768) }
        Write-Host $tail
    }
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
    Assert ($installed.Count -eq 1 -and (Read-ProductProperty $installed[0] 'InstallProperty' @('VersionString')) -eq $metadata.ProductVersion) 'Registered version mismatch.'
}
function Grant-ToInteractive([string]$path) {
    $acl = Get-Acl -LiteralPath $path
    return @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.IdentityReference.Value -eq 'S-1-5-4' -and
                       $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow })
}
function Assert-Administration {
    $adminAcl = Get-Acl -LiteralPath $adminToken
    Assert $adminAcl.AreAccessRulesProtected 'The administration credential inherits permissions.'
    $grants = @(Grant-ToInteractive $adminToken)
    Assert ($grants.Count -eq 1 -and !$grants[0].IsInherited) 'Missing explicit INTERACTIVE grant.'
    $allowed = [int][Security.AccessControl.FileSystemRights]::Read -bor
               [int][Security.AccessControl.FileSystemRights]::Synchronize
    $rights = [int]$grants[0].FileSystemRights
    Assert (($rights -band (-bnot $allowed)) -eq 0 -and
            ($rights -band [int][Security.AccessControl.FileSystemRights]::ReadData)) 'INTERACTIVE must only read the administration token.'
    foreach ($path in @($token, $config)) {
        Assert (@(Grant-ToInteractive $path).Count -eq 0) 'INTERACTIVE can access a private service file.'
    }

    $url = & $exe --SettingsUrl
    Assert ($LASTEXITCODE -eq 0 -and $url -match '^http://127\.0\.0\.1:8091/settings/#login=([a-f0-9]{64})$') 'The installed settings CLI did not return a local login link.'
    $loginCode = $Matches[1]
    $origin = 'http://127.0.0.1:8091'
    $page = Invoke-WebRequest -UseBasicParsing -Uri "$origin/settings/" -TimeoutSec 4
    Assert ($page.StatusCode -eq 200 -and $page.Headers['Content-Security-Policy']) 'The installed settings page is unavailable.'
    $json = @{ ContentType = 'application/json'; TimeoutSec = 4 }
    $state = Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Post -Headers @{ Origin = $origin } -Body (@{ code = $loginCode } | ConvertTo-Json -Compress) -SessionVariable session @json
    $settings = Invoke-RestMethod -Uri "$origin/admin/api/v1/settings" -WebSession $session -TimeoutSec 4
    $runtime = [IO.File]::ReadAllText($config) | ConvertFrom-Json
    $provider = if ('provider' -in $runtime.PSObject.Properties.Name) { 'searxng' } else { 'auto' }
    Assert ($settings.search.provider -eq $provider) 'The settings response did not load the expected source.'
    $headers = @{ Origin = $origin; 'X-QCWS-CSRF' = $state.csrf }
    $keyPath = "$origin/admin/api/v1/providers/brave/key"
    $fakeKey = [guid]::NewGuid().ToString('N')
    $saved = Invoke-RestMethod -Uri $keyPath -Method Put -Headers $headers -WebSession $session -Body (@{ key = $fakeKey } | ConvertTo-Json -Compress) @json
    Assert ($saved.providers.brave.key_set -and ($saved | ConvertTo-Json -Depth 16) -notmatch $fakeKey) 'The settings response exposed or failed to save a key.'
    $secrets = Join-Path $dataRoot 'state\secrets.json'
    Assert ((Test-Path -LiteralPath $secrets) -and @(Grant-ToInteractive $secrets).Count -eq 0) 'INTERACTIVE can access the provider secret file.'
    [void](Invoke-RestMethod -Uri $keyPath -Method Delete -Headers $headers -WebSession $session -Body '{}' @json)
    [void](Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Delete -Headers $headers -WebSession $session -Body '{}' @json)
}
function Invoke-InstalledTokenCli([string]$operation, [string[]]$lines) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $exe
    $start.Arguments = '--ConfigPath "{0}" --ServiceName "{1}" --ClientId msi-acceptance {2}' -f $config, $ServiceName, $operation
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        foreach ($line in $lines) { $process.StandardInput.WriteLine($line) }
        $process.StandardInput.Close()
        Assert ($process.WaitForExit(10000)) 'Installed token CLI timed out.'
        Assert ($process.ExitCode -eq 0) "Installed token CLI failed for $operation."
        $value = $stdout.GetAwaiter().GetResult().TrimEnd([char[]]"`r`n")
        $diagnostics = $stderr.GetAwaiter().GetResult()
        if ($operation -eq '--GenerateToken') {
            Assert ($value -cmatch '^[a-f0-9]{64}$' -and !$diagnostics.Contains($value)) 'Installed generator did not return a single private token.'
        } else {
            Assert (!$value -and !$diagnostics.Contains($lines[0])) 'Installed setter exposed the supplied token.'
        }
        return $value
    } finally {
        if (!$process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) }
        $process.Dispose()
    }
}
function Assert-InstalledTokenConfig([string]$bearer, [string]$dacl) {
    $crypto = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($crypto.ComputeHash([Text.Encoding]::ASCII.GetBytes($bearer))).Replace('-', '').ToLowerInvariant()
    } finally { $crypto.Dispose() }
    $text = [IO.File]::ReadAllText($config)
    Assert (!$text.Contains($bearer)) 'The installed config contains a plaintext token.'
    $runtime = $text | ConvertFrom-Json
    $entry = @($runtime.clients | Where-Object id -eq 'msi-acceptance')
    Assert ($entry.Count -eq 1 -and @($entry[0].PSObject.Properties.Name).Count -eq 2 -and $entry[0].token_sha256 -ceq $hash) 'The installed client entry does not contain only the expected digest and id.'
    $acl = Get-Acl -LiteralPath $config
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    Assert ($owner -in @('S-1-5-32-544', 'S-1-5-18')) 'Updating the installed token left an owner untrusted by the MSI installer.'
    Assert ($acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access) -eq $dacl) 'Updating the installed token changed the runtime file DACL.'
}
function Send-InstalledMcp($http, [uri]$uri, [string]$bearer, [hashtable]$message, [string]$session = '') {
    $method = if ($null -ne $message) { [Net.Http.HttpMethod]::Post } else { [Net.Http.HttpMethod]::Delete }
    $request = [Net.Http.HttpRequestMessage]::new($method, $uri)
    try {
        [void]$request.Headers.TryAddWithoutValidation('Accept', 'application/json, text/event-stream')
        if ($bearer) { $request.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $bearer) }
        if ($session) {
            [void]$request.Headers.TryAddWithoutValidation('Mcp-Session-Id', $session)
            [void]$request.Headers.TryAddWithoutValidation('MCP-Protocol-Version', '2025-06-18')
        }
        if ($null -ne $message) {
            $request.Content = [Net.Http.StringContent]::new(($message | ConvertTo-Json -Depth 8 -Compress), [Text.Encoding]::UTF8, 'application/json')
        }
        $response = $http.SendAsync($request).GetAwaiter().GetResult()
        try {
            $body = if ($null -ne $response.Content) { $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() } else { '' }
            return @{
                Status = [int]$response.StatusCode
                Body = if ($body) { $body | ConvertFrom-Json } else { $null }
                Session = if ($response.Headers.Contains('Mcp-Session-Id')) { @($response.Headers.GetValues('Mcp-Session-Id'))[0] } else { '' }
            }
        } finally { $response.Dispose() }
    } finally { $request.Dispose() }
}
function Assert-InstalledMcp($http, [uri]$uri, [string]$bearer, [hashtable]$initialize) {
    $result = Send-InstalledMcp $http $uri $bearer $initialize
    $session = $result.Session
    Assert ($result.Status -eq 200 -and $session -and $result.Body.result.protocolVersion -eq '2025-06-18') 'Installed MCP initialize did not accept the expected token.'
    $result = Send-InstalledMcp $http $uri $bearer @{ jsonrpc = '2.0'; method = 'notifications/initialized' } $session
    Assert ($result.Status -eq 202) 'Installed MCP did not accept the initialized notification.'
    $result = Send-InstalledMcp $http $uri $bearer @{ jsonrpc = '2.0'; id = 2; method = 'tools/list' } $session
    Assert ($result.Status -eq 200 -and (@($result.Body.result.tools.name | Sort-Object) -join ',') -eq 'web_find,web_open,web_search,web_search_status') 'Installed MCP returned an unexpected tools list.'
    $call = @{ jsonrpc = '2.0'; id = 3; method = 'tools/call'; params = @{ name = 'web_search_status'; arguments = @{} } }
    $result = Send-InstalledMcp $http $uri $bearer $call $session
    Assert ($result.Status -eq 200 -and $result.Body.result.structuredContent.contract_version -eq 1) 'Installed MCP web_search_status failed.'
    Assert ((Send-InstalledMcp $http $uri $bearer $null $session).Status -eq 204) 'Installed MCP session cleanup failed.'
}
function Assert-InstalledTokenRotation([hashtable]$metadata) {
    Write-Host 'Verify installed token generation, rotation and Bearer MCP access'
    [void](Stop-OwnedService)
    $leases = @(1..3 | ForEach-Object { [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0) })
    try {
        foreach ($listener in $leases) { $listener.Server.ExclusiveAddressUse = $true; $listener.Start() }
        $ports = @($leases | ForEach-Object { $_.LocalEndpoint.Port })
    } finally { foreach ($listener in $leases) { $listener.Stop() } }
    $runtime = [IO.File]::ReadAllText($config) | ConvertFrom-Json
    $runtime.business.port = $ports[0]
    $runtime.business.allowed_hosts = @("127.0.0.1:$($ports[0])")
    $runtime.control.port = $ports[1]
    $runtime.control.allowed_hosts = @("127.0.0.1:$($ports[1])")
    $runtime | Add-Member -Force -NotePropertyName admin -NotePropertyValue @{ port = $ports[2] }
    [IO.File]::WriteAllText($config, ($runtime | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    $dacl = (Get-Acl -LiteralPath $config).GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)
    $bearer = Invoke-InstalledTokenCli '--GenerateToken' @('yes')
    Assert-InstalledTokenConfig $bearer $dacl
    Start-Service -Name $ServiceName
    Assert-Running $metadata

    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $http = [Net.Http.HttpClient]::new($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $uri = "http://127.0.0.1:$($ports[0])/mcp"
        $initialize = @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{
            protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{ name = 'msi-acceptance'; version = '1' }
        } }
        Assert ((Send-InstalledMcp $http $uri '' $initialize).Status -eq 401) 'Installed MCP accepted an unauthenticated client.'
        Assert-InstalledMcp $http $uri $bearer $initialize

        $replacement = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
        [void](Invoke-InstalledTokenCli '--SetToken' @($replacement, 'yes'))
        Assert-InstalledTokenConfig $replacement $dacl
        Assert ((Send-InstalledMcp $http $uri $replacement $initialize).Status -eq 401) 'The rotated token became active before service restart.'
        Assert-InstalledMcp $http $uri $bearer $initialize
        [void](Stop-OwnedService)
        Start-Service -Name $ServiceName
        Assert-Running $metadata
        Assert ((Send-InstalledMcp $http $uri $bearer $initialize).Status -eq 401) 'The original token remained active after service restart.'
        Assert-InstalledMcp $http $uri $replacement $initialize
    } finally { $http.Dispose() }
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
foreach ($pair in @(@{ Key = '--CandidateSHA256'; Msi = $candidate },
                    @{ Key = '--BaselineSHA256'; Msi = $baseline })) {
    $expected = $options[$pair.Key]
    Assert ($expected -match '^[a-fA-F0-9]{64}$' -and $pair.Msi.Sha256 -eq $expected) 'MSI SHA-256 mismatch.'
}
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
foreach ($port in @(8088,8089,8091)) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    $listener.Server.ExclusiveAddressUse = $true
    try { $listener.Start() } catch { throw "Required service port $port is already in use. No changes made." }
    finally { $listener.Stop() }
}
foreach ($product in $wi.ProductsEx('', 'S-1-1-0', 7)) {
    Assert ((Read-ProductProperty $product 'ProductCode') -notin @($candidate.ProductCode, $baseline.ProductCode)) 'Product already registered.'
    Assert ((Read-ProductProperty $product 'InstallProperty' @('ProductName')) -ne $candidate.ProductName) 'Existing named product found.'
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
    $preserved = @($config, $token, $sentinel)
    $baselineHasAdmin = Test-Path -LiteralPath $adminToken
    if ($baselineHasAdmin) { $preserved += $adminToken }
    $baselineFiles = Snapshot ($preserved + @($exe))
    $preservedFiles = Snapshot $preserved
    Invoke-Msi $candidate '/i' '03-fail-after-start-services' 1603 'afterStartServices'
    Assert (!(Registered $candidate.ProductCode)) 'Failed candidate stayed registered.'
    Assert-Running $baseline
    Assert-Snapshot $baselineFiles
    Assert ((Test-Path -LiteralPath $adminToken) -eq $baselineHasAdmin) 'Rollback changed the administration credential.'
    Invoke-Msi $candidate '/i' '04-upgrade-same-candidate'
    Assert (!(Registered $baseline.ProductCode)) 'Baseline remained registered after upgrade.'
    Assert-Running $candidate
    Assert-Administration
    if (!$baselineHasAdmin) { $preservedFiles += Snapshot @($adminToken) }
    Assert-Snapshot $preservedFiles
    Assert-InstalledTokenRotation $candidate
    $preservedFiles[$config] = (Snapshot @($config))[$config]
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
       ScriptSha256 = $scriptSha256; PowerShellVersion = $PSVersionTable.PSVersion.ToString();
       PowerShellEdition = $PSVersionTable.PSEdition;
       StopSeconds = $stopSeconds; WindowsBuild = $osInfo.BuildNumber; WindowsProductType = $osInfo.ProductType;
       CleanupErrors = @($cleanupFailures); Error = "$failure" } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'result.json') -Encoding UTF8
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($wi)
}
if ($failure) { throw $failure }
if ($cleanupFailures.Count) { throw ($cleanupFailures -join [Environment]::NewLine) }
Write-Host "PASS. Temporary test results: $runDir"
