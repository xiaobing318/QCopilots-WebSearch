# Test the production MSI in the default and a custom directory on a disposable Windows host.
$required = @('--CandidateMsi', '--CandidateSHA256', '--OutputDir')
$options = @{}
for ($n = 0; $n -lt $args.Count; $n += 2) {
    $key = [string]$args[$n]
    if ($key -notin $required -or $n + 1 -ge $args.Count -or $options.ContainsKey($key) -or
        [string]::IsNullOrWhiteSpace($args[$n + 1])) {
        throw 'Use --CandidateMsi FILE --CandidateSHA256 HASH --OutputDir DIR.'
    }
    $options[$key] = [string]$args[$n + 1]
}
foreach ($key in $required) {
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
$programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
Assert ([IO.Directory]::Exists($programFiles)) 'Cannot locate Program Files.'
$wi = New-Object -ComObject WindowsInstaller.Installer
function Read-Msi([string]$path, [string]$expectedSha256) {
    $full = (Resolve-Path -LiteralPath $path).ProviderPath
    Assert ([IO.Path]::GetExtension($full) -eq '.msi') 'Expected an MSI file.'
    $sha256 = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
    Assert ($expectedSha256 -match '^[a-fA-F0-9]{64}$' -and $sha256 -eq $expectedSha256) 'MSI SHA-256 mismatch.'
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
        Assert ($properties.ProductVersion -match '^[0-9]+\.[0-9]+\.[0-9]+$') 'Require a three-field MSI version.'
        $expectedName = 'QCopilots-WebSearch-{0}-win-amd64.msi' -f $properties.ProductVersion
        $legacyName = 'QCopilots-WebSearch-{0}-win64.msi' -f $properties.ProductVersion
        $packageName = [IO.Path]::GetFileName($full)
        Assert ([string]::Equals($packageName, $expectedName, [StringComparison]::Ordinal) -or
                [string]::Equals($packageName, $legacyName, [StringComparison]::Ordinal)) "MSI filename must match ProductVersion ($expectedName or $legacyName)."
        $view = $db.OpenView(('SELECT {0}Name{0}, {0}StartName{0}, {0}Arguments{0} FROM {0}ServiceInstall{0}' -f [char]96))
        [void]$view.Execute()
        $record = $view.Fetch()
        Assert ($null -ne $record) 'MSI has no service.'
        $properties.ServiceName = [string]$record.StringData(1)
        $properties.ServiceAccount = [string]$record.StringData(2)
        $properties.ServiceArguments = [string]$record.StringData(3)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        Assert ($null -eq $view.Fetch()) 'Expected exactly one service.'
        [void]$view.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        $view = $db.OpenView(('SELECT {0}Directory{0}, {0}Directory_Parent{0}, {0}DefaultDir{0} FROM {0}Directory{0}' -f [char]96))
        [void]$view.Execute()
        while ($null -ne ($record = $view.Fetch())) {
            $key = [string]$record.StringData(1)
            if ($key -in @('INSTALL_ROOT', 'CM_DP_bin', 'QcwsDataRoot', 'QcwsStateRoot')) {
                $properties["${key}Parent"] = [string]$record.StringData(2)
                $target = ([string]$record.StringData(3) -split ':')[0]
                $properties["${key}Name"] = ($target -split '\|')[-1]
            }
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
        }
        [void]$view.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        $properties.Path = $full
        $properties.Sha256 = $sha256
        return $properties
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($db) }
}
function Set-ExpectedInstance([hashtable]$metadata, [bool]$overrideInstallRoot) {
    $version = $metadata.ProductVersion
    [void][guid]::Parse($metadata.ProductCode)
    [void][guid]::Parse($metadata.UpgradeCode)
    Assert ($metadata.ProductName -eq 'QCopilots-WebSearch') 'Expected a production package.'
    $directory = "QCopilots-WebSearch-$version"
    $name = "QCopilotsWebSearch-$version"
    $arguments = '--WindowsService --ServiceName {0} --ConfigPath "[QcwsDataRoot]runtime.json"' -f $name
    Assert ($metadata.ServiceName -ceq $name -and $metadata.ServiceAccount -ceq "NT SERVICE\$name" -and
            $metadata.ServiceArguments -ceq $arguments) 'Unexpected versioned service identity or startup command.'
    Assert ($metadata.INSTALL_ROOTName -ceq $directory -and
            $metadata.INSTALL_ROOTParent -in @('ProgramFiles6432Folder', 'ProgramFiles64Folder') -and
            $metadata.CM_DP_binName -ceq 'bin' -and $metadata.CM_DP_binParent -ceq 'INSTALL_ROOT' -and
            $metadata.QcwsDataRootName -ceq 'data' -and $metadata.QcwsDataRootParent -ceq 'CM_DP_bin' -and
            $metadata.QcwsStateRootName -ceq 'state' -and $metadata.QcwsStateRootParent -ceq 'QcwsDataRoot') 'Unexpected versioned installation or data directory.'
    $metadata.OverrideInstallRoot = $overrideInstallRoot
    $metadata.DefaultInstallRoot = [IO.Path]::GetFullPath((Join-Path $programFiles $directory))
    $metadata.InstallRoot = $metadata.DefaultInstallRoot
    if ($overrideInstallRoot) {
        $custom = "$directory Acceptance " + [guid]::NewGuid().ToString('N')
        $metadata.InstallRoot = [IO.Path]::GetFullPath((Join-Path $programFiles $custom))
    }
    $metadata.BinRoot = Join-Path $metadata.InstallRoot 'bin'
    $metadata.DataRoot = Join-Path $metadata.BinRoot 'data'
    $metadata.Executable = Join-Path $metadata.BinRoot 'qcopilots-websearch.exe'
    $metadata.Config = Join-Path $metadata.DataRoot 'runtime.json'
    $metadata.Token = Join-Path $metadata.DataRoot 'client.token'
    $metadata.AdminToken = Join-Path $metadata.DataRoot 'admin.token'
    $metadata.AdminEndpoint = Join-Path $metadata.DataRoot 'state\admin-port'
    $metadata.InstallAttempted = $false
    $metadata.ObservedInstallRoot = $null
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
function Owned-Processes([hashtable]$metadata) {
    Assert $metadata.InstallAttempted 'Unknown product ownership.'
    @(Get-CimInstance Win32_Process -Filter "Name='qcopilots-websearch.exe'" |
        Where-Object { $_.ExecutablePath -eq $metadata.Executable -and $_.CreationDate -ge $metadata.Started })
}
function Owned-Service([hashtable]$metadata) {
    Assert $metadata.InstallAttempted 'Unknown product ownership.'
    $service = Get-CimInstance Win32_Service -Filter "Name='$($metadata.ServiceName)'"
    if ($service) {
        $invocation = '^{0}\s+--WindowsService\s+--ServiceName\s+{1}\s+--ConfigPath\s+{2}$' -f
            ('"?' + [regex]::Escape($metadata.Executable) + '"?'), [regex]::Escape($metadata.ServiceName),
            ('"?' + [regex]::Escape($metadata.Config) + '"?')
        Assert ($service.PathName -match $invocation) 'Unexpected service ownership or configuration path.'
    }
    return $service
}
function Wait-Service([hashtable]$metadata, [string]$state, [int]$seconds) {
    $controller = Get-Service -Name $metadata.ServiceName
    try { $controller.WaitForStatus($state, [TimeSpan]::FromSeconds($seconds)) }
    finally { $controller.Dispose() }
}
function Stop-OwnedService([hashtable]$metadata) {
    [void](Owned-Service $metadata)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $controller = Get-Service -Name $metadata.ServiceName
    try {
        $controller.Stop()
        $controller.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(10))
    } finally { $controller.Dispose() }
    Assert ($watch.Elapsed.TotalSeconds -le 10) 'Service stop exceeded ten seconds.'
    return $watch.Elapsed.TotalSeconds
}
function Wait-Absent([hashtable]$metadata) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        if (!(Get-CimInstance Win32_Service -Filter "Name='$($metadata.ServiceName)'")) { return }
        Start-Sleep -Milliseconds 100
    } while ($watch.Elapsed.TotalSeconds -lt 10)
    throw 'Service remained registered beyond ten seconds.'
}
function Invoke-Msi([hashtable]$metadata, [string]$mode, [string]$label, [int]$expected = 0, [string]$failpoint = '') {
    Assert ((Get-FileHash -LiteralPath $metadata.Path -Algorithm SHA256).Hash -eq $metadata.Sha256) 'MSI bytes changed.'
    $log = Join-Path $metadata.LogDirectory ($label + '.log')
    $msiArgs = @($mode, ('"' + $metadata.Path + '"'), '/qn', '/norestart', 'REBOOT=ReallySuppress', '/L*v', ('"' + $log + '"'))
    if ($failpoint) { $msiArgs += "QCWS_TEST_FAILPOINT=$failpoint" }
    if ($mode -eq '/i') {
        if ($metadata.OverrideInstallRoot) { $msiArgs += ('INSTALL_ROOT="' + $metadata.InstallRoot + '"') }
        $metadata.InstallAttempted = $true
    } else { [void](Owned-Service $metadata) }
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
    if (Test-Path -LiteralPath $metadata.InstallRoot) { $metadata.ObservedInstallRoot = $metadata.InstallRoot }
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
    Wait-Service $metadata 'Running' 30
    $service = Owned-Service $metadata
    Assert ($service.StartName -eq $metadata.ServiceAccount -and $service.StartMode -eq 'Auto') 'Service account/start mode changed.'
    if ($metadata.OverrideInstallRoot) {
        Assert (!(Test-Path -LiteralPath $metadata.DefaultInstallRoot)) 'Custom INSTALL_ROOT did not contain the whole installation.'
    }
    $version = & $metadata.Executable --Version 2>&1
    $versionPattern = '^QCopilots-WebSearch ' + [regex]::Escape($metadata.ProductVersion) + ', Qt '
    Assert ($LASTEXITCODE -eq 0 -and "$version" -match $versionPattern) 'Installed binary version mismatch.'
    $configCheck = & $metadata.Executable --CheckConfig 2>&1
    Assert ($LASTEXITCODE -eq 0 -and "$configCheck" -ceq 'Configuration valid.') 'Default configuration check failed for the installed binary.'
    $settings = [IO.File]::ReadAllText($metadata.Config) | ConvertFrom-Json
    Assert ($settings.project_version -ceq $metadata.ProductVersion) 'Runtime configuration release does not match the MSI.'
    $template = Join-Path $metadata.InstallRoot 'share\qcopilots-websearch\config\runtime.json'
    Assert (([IO.File]::ReadAllText($template) | ConvertFrom-Json).project_version -ceq $metadata.ProductVersion) 'Packaged runtime configuration release does not match the MSI.'
    $ports = @($settings.business.port, $settings.control.port, $settings.admin.port)
    Assert (@($ports | Select-Object -Unique).Count -eq 3 -and
            @($ports | Where-Object { $_ -lt 1024 -or $_ -gt 65535 }).Count -eq 0) 'Invalid or duplicate configured ports.'
    $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $service.ProcessId -ErrorAction SilentlyContinue)
    Assert (@($listeners | Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') -and $_.LocalPort -in $ports }).Count -eq 3) 'The installed process did not open the three configured loopback ports.'
    Assert ([IO.File]::ReadAllText($metadata.AdminEndpoint).Trim() -ceq "$($settings.admin.port)") 'The settings launcher endpoint does not match the running configuration.'
    $secretValue = [IO.File]::ReadAllText($metadata.Token).Trim()
    try {
        $uri = 'http://' + $settings.control.host + ':' + $settings.control.port + '/health/live'
        $live = Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers @{ Authorization = 'Bearer ' + $secretValue } -TimeoutSec 5
        Assert ($live.StatusCode -eq 200 -and ($live.Content | ConvertFrom-Json).live) 'Live health check failed.'
    } finally { $secretValue = $null }
    $modules = (Get-Process -Id $service.ProcessId).Modules | Where-Object { $_.ModuleName -like 'Qt6*.dll' }
    Assert (@($modules).Count -ge 3) 'Qt libraries were not observable.'
    foreach ($module in $modules) { Assert ($module.FileName.StartsWith($metadata.InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) 'A Qt library came from a development environment.' }
    $leftovers = @(Get-ChildItem -LiteralPath $metadata.DataRoot -Force -File | Where-Object { $_.Name -like '.qcws-install-*' -or $_.Name -like '*.new' -or $_.Name -like '*.tmp' })
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
function Get-InstalledSettingsLink([hashtable]$metadata, [string]$origin) {
    $url = & $metadata.Executable --SettingsUrl
    $linkPattern = '^' + [regex]::Escape($origin) + '/settings/#login=[a-f0-9]{64}$'
    Assert ($LASTEXITCODE -eq 0 -and $url -cmatch $linkPattern) 'The installed settings CLI did not return a login link for its own port.'
    return $url
}
function Assert-Administration([hashtable]$metadata) {
    $sidInfo = & "$env:SystemRoot\System32\sc.exe" showsid $metadata.ServiceName | Out-String
    $serviceSid = [regex]::Match($sidInfo, 'S-1-5-80(-[0-9]+)+').Value
    Assert ($LASTEXITCODE -eq 0 -and $serviceSid) 'The service SID was unavailable.'
    foreach ($directory in @(@{ Path = $metadata.DataRoot; Rights = 0x1200a9 },
                              @{ Path = (Join-Path $metadata.DataRoot 'state'); Rights = 0x1301bf })) {
        $acl = Get-Acl -LiteralPath $directory.Path
        $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Value -eq $serviceSid })
        Assert ($acl.AreAccessRulesProtected -and $rules.Count -eq 1 -and !$rules[0].IsInherited -and
                $rules[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                [int]$rules[0].FileSystemRights -eq $directory.Rights) "The installed service directory ACL changed: $($directory.Path)"
    }
    $adminAcl = Get-Acl -LiteralPath $metadata.AdminToken
    Assert $adminAcl.AreAccessRulesProtected 'The administration credential inherits permissions.'
    $grants = @(Grant-ToInteractive $metadata.AdminToken)
    Assert ($grants.Count -eq 1 -and !$grants[0].IsInherited) 'Missing explicit INTERACTIVE grant.'
    $allowed = [int][Security.AccessControl.FileSystemRights]::Read -bor
               [int][Security.AccessControl.FileSystemRights]::Synchronize
    $rights = [int]$grants[0].FileSystemRights
    Assert (($rights -band (-bnot $allowed)) -eq 0 -and
            ($rights -band [int][Security.AccessControl.FileSystemRights]::ReadData)) 'INTERACTIVE must only read the administration token.'
    foreach ($path in @($metadata.Token, $metadata.Config)) {
        Assert (@(Grant-ToInteractive $path).Count -eq 0) 'INTERACTIVE can access a private service file.'
    }

    $runtime = [IO.File]::ReadAllText($metadata.Config) | ConvertFrom-Json
    $origin = 'http://127.0.0.1:' + $runtime.admin.port
    $url = Get-InstalledSettingsLink $metadata $origin
    $loginCode = $url.Substring($url.Length - 64)
    $page = Invoke-WebRequest -UseBasicParsing -Uri "$origin/settings/" -TimeoutSec 4
    Assert ($page.StatusCode -eq 200 -and $page.Headers['Content-Security-Policy']) 'The installed settings page is unavailable.'
    $json = @{ ContentType = 'application/json'; TimeoutSec = 4 }
    $state = Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Post -Headers @{ Origin = $origin } -Body (@{ code = $loginCode } | ConvertTo-Json -Compress) -SessionVariable session @json
    $settings = Invoke-RestMethod -Uri "$origin/admin/api/v1/settings" -WebSession $session -TimeoutSec 4
    Assert ($settings.search.provider -ceq 'auto' -and !$settings.providers.brave.key_set) 'Fresh installation did not start with the default provider and empty credentials.'
    $headers = @{ Origin = $origin; 'X-QCWS-CSRF' = $state.csrf }
    $patch = @{ revision = $settings.revision; patch = @{ search = @{ provider = 'brave' } } } | ConvertTo-Json -Depth 4 -Compress
    $savedSettings = Invoke-RestMethod -Uri "$origin/admin/api/v1/settings" -Method Put -Headers $headers -WebSession $session -Body $patch @json
    Assert ($savedSettings.search.provider -ceq 'brave') 'The settings edit did not persist.'
    $keyPath = "$origin/admin/api/v1/providers/brave/key"
    $fakeKey = [guid]::NewGuid().ToString('N')
    $keyBody = @{ key = $fakeKey } | ConvertTo-Json -Compress
    $saved = Invoke-RestMethod -Uri $keyPath -Method Put -Headers $headers -WebSession $session -Body $keyBody @json
    Assert ($saved.providers.brave.key_set -and ($saved | ConvertTo-Json -Depth 16) -notmatch $fakeKey) 'The settings response exposed or failed to save a key.'
    $secrets = Join-Path $metadata.DataRoot 'state\secrets.json'
    Assert ((Test-Path -LiteralPath $secrets) -and @(Grant-ToInteractive $secrets).Count -eq 0) 'INTERACTIVE can access the provider secret file.'
    $cleared = Invoke-RestMethod -Uri $keyPath -Method Delete -Headers $headers -WebSession $session -Body '{}' @json
    Assert (!$cleared.providers.brave.key_set) 'The provider credential was not cleared.'
    # Keep a populated secret file for the uninstall and reinstall checks.
    $saved = Invoke-RestMethod -Uri $keyPath -Method Put -Headers $headers -WebSession $session -Body $keyBody @json
    Assert $saved.providers.brave.key_set 'The provider credential was not restored.'
    [void](Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Delete -Headers $headers -WebSession $session -Body '{}' @json)
}
function Invoke-InstalledTokenCli([hashtable]$metadata, [string]$operation, [string[]]$lines) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $metadata.Executable
    $start.Arguments = '--ConfigPath "{0}" --ServiceName "{1}" --ClientId msi-acceptance {2}' -f $metadata.Config, $metadata.ServiceName, $operation
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
function AccessRules($acl) {
    # Windows can change its auto-inherited bookkeeping flag after publishing
    # an ACL. Compare the actual ACEs and inheritance protection.
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
    $dacl = $descriptor.DiscretionaryAcl
    Assert ($null -ne $dacl) 'The installed configuration has no DACL.'
    $bytes = [byte[]]::new($dacl.BinaryLength)
    $dacl.GetBinaryForm($bytes, 0)
    return "$($acl.AreAccessRulesProtected):$([Convert]::ToBase64String($bytes))"
}
function Assert-InstalledTokenConfig([hashtable]$metadata, [string]$bearer, [string]$dacl, [string]$runtimeHash) {
    $crypto = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($crypto.ComputeHash([Text.Encoding]::ASCII.GetBytes($bearer))).Replace('-', '').ToLowerInvariant()
    } finally { $crypto.Dispose() }
    $registryPath = Join-Path $metadata.DataRoot 'state\client-registry.json'
    $text = [IO.File]::ReadAllText($registryPath)
    $registry = $text | ConvertFrom-Json
    $entry = @($registry.clients | Where-Object id -eq 'msi-acceptance')
    Assert ($registry.schema_version -eq 1 -and $entry.Count -eq 1 -and
            $entry[0].token_sha256 -ceq $hash -and $entry[0].token_value -ceq $bearer -and
            $entry[0].generation -cmatch '^[a-f0-9]{32}$') 'The private client registry did not retain the matching credential, digest and identity.'
    Assert (@(Grant-ToInteractive $registryPath).Count -eq 0) 'INTERACTIVE can access the private client registry.'
    Assert ((Get-FileHash -LiteralPath $metadata.Config -Algorithm SHA256).Hash -ceq $runtimeHash) 'Client enrollment changed the deployment configuration.'
    $acl = Get-Acl -LiteralPath $metadata.Config
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    Assert ($owner -in @('S-1-5-32-544', 'S-1-5-18')) 'Updating the installed token left an owner untrusted by the MSI installer.'
    Assert ((AccessRules $acl) -ceq $dacl) 'Updating the installed token changed the runtime file access rules or inheritance protection.'
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
function Assert-InstalledMcp($http, [uri]$uri, [string]$bearer, [hashtable]$initialize, [string]$expectedVersion) {
    $result = Send-InstalledMcp $http $uri $bearer $initialize
    $session = $result.Session
    Assert ($result.Status -eq 200 -and $session -and $result.Body.result.protocolVersion -eq '2025-06-18') 'Installed MCP initialize did not accept the expected token.'
    Assert ($result.Body.result.serverInfo.version -ceq $expectedVersion) 'MCP serverInfo.version does not match the MSI.'
    $result = Send-InstalledMcp $http $uri $bearer @{ jsonrpc = '2.0'; method = 'notifications/initialized' } $session
    Assert ($result.Status -eq 202) 'Installed MCP did not accept the initialized notification.'
    $result = Send-InstalledMcp $http $uri $bearer @{ jsonrpc = '2.0'; id = 2; method = 'tools/list' } $session
    Assert ($result.Status -eq 200 -and (@($result.Body.result.tools.name | Sort-Object) -join ',') -eq 'web_find,web_open,web_search,web_search_status') 'Installed MCP returned an unexpected tools list.'
    $call = @{ jsonrpc = '2.0'; id = 3; method = 'tools/call'; params = @{ name = 'web_search_status'; arguments = @{} } }
    $result = Send-InstalledMcp $http $uri $bearer $call $session
    Assert ($result.Status -eq 200 -and $result.Body.result.structuredContent.contract_version -eq 1) 'Installed MCP web_search_status failed.'
    Assert ((Send-InstalledMcp $http $uri $bearer $null $session).Status -eq 204) 'Installed MCP session cleanup failed.'
}
function New-McpClient {
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $http = [Net.Http.HttpClient]::new($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(5)
    return $http
}
function New-McpInitialize {
    return @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{
        protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{ name = 'msi-acceptance'; version = '1' }
    } }
}
function Assert-InstalledTokenRotation([hashtable]$metadata) {
    Write-Host 'Verify installed token generation, rotation and Bearer MCP access'
    $runtime = [IO.File]::ReadAllText($metadata.Config) | ConvertFrom-Json
    $dacl = AccessRules (Get-Acl -LiteralPath $metadata.Config)
    $runtimeHash = (Get-FileHash -LiteralPath $metadata.Config -Algorithm SHA256).Hash
    $bearer = Invoke-InstalledTokenCli $metadata '--GenerateToken' @('yes')
    Assert-InstalledTokenConfig $metadata $bearer $dacl $runtimeHash
    $http = New-McpClient
    try {
        $uri = "http://127.0.0.1:$($runtime.business.port)/mcp"
        $initialize = New-McpInitialize
        Assert ((Send-InstalledMcp $http $uri '' $initialize).Status -eq 401) 'Installed MCP accepted an unauthenticated client.'
        Assert-InstalledMcp $http $uri ([IO.File]::ReadAllText($metadata.Token).Trim()) $initialize $metadata.ProductVersion
        Assert ((Send-InstalledMcp $http $uri $bearer $initialize).Status -eq 401) 'The generated token became active before service restart.'
        [void](Stop-OwnedService $metadata)
        Start-Service -Name $metadata.ServiceName
        Assert-Running $metadata
        Assert-InstalledMcp $http $uri $bearer $initialize $metadata.ProductVersion

        $replacement = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
        [void](Invoke-InstalledTokenCli $metadata '--SetToken' @($replacement, 'yes'))
        Assert-InstalledTokenConfig $metadata $replacement $dacl $runtimeHash
        Assert (![IO.File]::ReadAllText((Join-Path $metadata.DataRoot 'state\client-registry.json')).Contains($bearer)) 'The private registry retained the credential replaced by the CLI.'
        Assert ((Send-InstalledMcp $http $uri $replacement $initialize).Status -eq 401) 'The rotated token became active before service restart.'
        Assert-InstalledMcp $http $uri $bearer $initialize $metadata.ProductVersion
        [void](Stop-OwnedService $metadata)
        Start-Service -Name $metadata.ServiceName
        Assert-Running $metadata
        Assert ((Send-InstalledMcp $http $uri $bearer $initialize).Status -eq 401) 'The original token remained active after service restart.'
        Assert-InstalledMcp $http $uri $replacement $initialize $metadata.ProductVersion
    } finally { $http.Dispose() }
}
function Send-InstalledClientReveal([string]$origin, $session, [hashtable]$headers, [string]$id, [string]$revision) {
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.CookieContainer = $session.Cookies
    $http = [Net.Http.HttpClient]::new($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(5)
    $http.MaxResponseContentBufferSize = 1048576
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, "$origin/admin/api/v1/clients/reveal")
    try {
        foreach ($name in $headers.Keys) { [void]$request.Headers.TryAddWithoutValidation($name, [string]$headers[$name]) }
        $body = @{ id = $id; revision = $revision } | ConvertTo-Json -Compress
        $request.Content = [Net.Http.StringContent]::new($body, [Text.Encoding]::UTF8, 'application/json')
        $response = $http.SendAsync($request).GetAwaiter().GetResult()
        try {
            $raw = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            return @{
                Status = [int]$response.StatusCode
                NoStore = ($null -ne $response.Headers.CacheControl -and $response.Headers.CacheControl.NoStore)
                Body = $raw | ConvertFrom-Json
                Raw = $raw
            }
        } finally { $response.Dispose() }
    } finally { $request.Dispose(); $http.Dispose() }
}
function Assert-InstalledClientReveal($result, [string]$id, [string]$revision, [string]$bearer) {
    Assert ($result.Status -eq 200 -and $result.NoStore -and $result.Body.id -ceq $id -and
            $result.Body.revision -ceq $revision -and $result.Body.token -ceq $bearer -and
            $result.Body.active -eq $true -and
            (@($result.Body.PSObject.Properties.Name | Sort-Object) -join ',') -eq 'active,id,revision,token') 'Authenticated client reveal returned an unexpected credential, identity or cache policy.'
}
function Assert-InstalledClientView($view) {
    Assert ((@($view.PSObject.Properties.Name | Sort-Object) -join ',') -eq 'clients,pending_restart,revision') 'The client list returned unexpected fields.'
    foreach ($client in $view.clients) {
        Assert ((@($client.PSObject.Properties.Name | Sort-Object) -join ',') -eq 'can_reveal,id,source' -and
                $client.can_reveal -is [bool]) 'The client list disclosed credential or internal identity fields.'
    }
}
function Assert-InstalledRegistryValue([hashtable]$metadata, [string]$id, [string]$bearer) {
    $registryPath = Join-Path $metadata.DataRoot 'state\client-registry.json'
    $registry = [IO.File]::ReadAllText($registryPath) | ConvertFrom-Json
    $entry = @($registry.clients | Where-Object id -eq $id)
    $crypto = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($crypto.ComputeHash([Text.Encoding]::ASCII.GetBytes($bearer))).Replace('-', '').ToLowerInvariant()
    } finally { $crypto.Dispose() }
    Assert ($entry.Count -eq 1 -and $entry[0].token_value -ceq $bearer -and $entry[0].token_sha256 -ceq $hash -and
            @(Grant-ToInteractive $registryPath).Count -eq 0) 'The service registry credential, digest or private permissions were incorrect.'
}
function Assert-InstalledClientRegistry([hashtable]$metadata) {
    Write-Host 'Verify service-account client registry writes and immediate authorization'
    $runtime = [IO.File]::ReadAllText($metadata.Config) | ConvertFrom-Json
    $runtimeHash = (Get-FileHash -LiteralPath $metadata.Config -Algorithm SHA256).Hash
    $runtimeAcl = AccessRules (Get-Acl -LiteralPath $metadata.Config)
    $servicePid = (Owned-Service $metadata).ProcessId
    $origin = 'http://127.0.0.1:' + $runtime.admin.port
    $url = Get-InstalledSettingsLink $metadata $origin
    $json = @{ ContentType = 'application/json'; TimeoutSec = 4 }
    $state = Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Post -Headers @{ Origin = $origin } -Body (@{ code = $url.Substring($url.Length - 64) } | ConvertTo-Json -Compress) -SessionVariable session @json
    $headers = @{ Origin = $origin; 'X-QCWS-CSRF' = $state.csrf }
    $clientsUri = "$origin/admin/api/v1/clients"
    $http = New-McpClient
    try {
        $view = Invoke-RestMethod -Uri $clientsUri -WebSession $session -TimeoutSec 4
        Assert-InstalledClientView $view
        Assert (@($view.clients | Where-Object id -eq 'local').Count -eq 1) 'The initial local identity is missing from administration.'
        $bearer = 'web/' + [guid]::NewGuid().ToString('N').Substring(0, 12)
        $body = @{ revision = $view.revision; id = 'msi-web-acceptance'; token = $bearer } | ConvertTo-Json -Compress
        $view = Invoke-RestMethod -Uri $clientsUri -Method Post -Headers $headers -WebSession $session -Body $body @json
        Assert-InstalledClientView $view
        Assert (!$view.pending_restart -and !($view | ConvertTo-Json -Depth 8).Contains($bearer)) 'Web enrollment was pending or disclosed its credential.'
        $enrolledRevision = $view.revision
        Assert-InstalledRegistryValue $metadata 'msi-web-acceptance' $bearer
        $denied = Send-InstalledClientReveal $origin $session @{ Origin = $origin } 'msi-web-acceptance' $view.revision
        Assert ($denied.Status -eq 403 -and $denied.NoStore -and !$denied.Raw.Contains($bearer)) 'Client reveal accepted a request without CSRF protection or disclosed its credential.'
        Assert-InstalledClientReveal (Send-InstalledClientReveal $origin $session $headers 'msi-web-acceptance' $view.revision) 'msi-web-acceptance' $view.revision $bearer
        $uri = "http://127.0.0.1:$($runtime.business.port)/mcp"
        $initialize = New-McpInitialize
        Assert-InstalledMcp $http $uri $bearer $initialize $metadata.ProductVersion
        $replacement = 'web/' + [guid]::NewGuid().ToString('N').Substring(0, 12)
        $body = @{ revision = $view.revision; token = $replacement } | ConvertTo-Json -Compress
        $view = Invoke-RestMethod -Uri "$clientsUri/msi-web-acceptance" -Method Put -Headers $headers -WebSession $session -Body $body @json
        Assert-InstalledClientView $view
        Assert (!$view.pending_restart -and !($view | ConvertTo-Json -Depth 8).Contains($bearer) -and
                !($view | ConvertTo-Json -Depth 8).Contains($replacement) -and
                (Send-InstalledMcp $http $uri $bearer $initialize).Status -eq 401) 'Web rotation disclosed credentials or did not revoke the previous credential immediately.'
        $stale = Send-InstalledClientReveal $origin $session $headers 'msi-web-acceptance' $enrolledRevision
        Assert ($stale.Status -eq 409 -and $stale.NoStore -and !$stale.Raw.Contains($bearer) -and !$stale.Raw.Contains($replacement)) 'Client reveal accepted a stale revision or disclosed credentials in its error.'
        Assert-InstalledClientReveal (Send-InstalledClientReveal $origin $session $headers 'msi-web-acceptance' $view.revision) 'msi-web-acceptance' $view.revision $replacement
        Assert-InstalledRegistryValue $metadata 'msi-web-acceptance' $replacement
        Assert (![IO.File]::ReadAllText((Join-Path $metadata.DataRoot 'state\client-registry.json')).Contains($bearer)) 'Web rotation retained the replaced credential in the private registry.'
        Assert-InstalledMcp $http $uri $replacement $initialize $metadata.ProductVersion
        $body = @{ revision = $view.revision } | ConvertTo-Json -Compress
        $view = Invoke-RestMethod -Uri "$clientsUri/msi-web-acceptance" -Method Delete -Headers $headers -WebSession $session -Body $body @json
        Assert-InstalledClientView $view
        Assert (!$view.pending_restart -and !($view | ConvertTo-Json -Depth 8).Contains($replacement) -and
                (Send-InstalledMcp $http $uri $replacement $initialize).Status -eq 401) 'Web deletion disclosed its credential or did not revoke it immediately.'
        $deleted = Send-InstalledClientReveal $origin $session $headers 'msi-web-acceptance' $view.revision
        Assert ($deleted.Status -eq 404 -and $deleted.NoStore -and !$deleted.Raw.Contains($bearer) -and !$deleted.Raw.Contains($replacement)) 'Deleted client reveal returned a credential or an unexpected response.'
        $registryPath = Join-Path $metadata.DataRoot 'state\client-registry.json'
        Assert ((Test-Path -LiteralPath $registryPath) -and @(Grant-ToInteractive $registryPath).Count -eq 0) 'The service registry is missing or readable by INTERACTIVE.'
        Assert (![IO.File]::ReadAllText($registryPath).Contains($replacement)) 'Web deletion retained its credential in the private registry.'
        Assert ((Owned-Service $metadata).ProcessId -eq $servicePid) 'Web client changes restarted the service.'
        Assert ((Get-FileHash -LiteralPath $metadata.Config -Algorithm SHA256).Hash -ceq $runtimeHash) 'Web client changes rewrote the deployment configuration.'
        Assert ((AccessRules (Get-Acl -LiteralPath $metadata.Config)) -ceq $runtimeAcl) 'Web client changes modified the deployment configuration ACL.'
    } finally {
        $http.Dispose()
        [void](Invoke-RestMethod -Uri "$origin/admin/api/v1/session" -Method Delete -Headers $headers -WebSession $session -Body '{}' @json)
    }
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
function Assert-InstalledColdStartValidation([hashtable]$metadata) {
    foreach ($file in @('settings.json', 'secrets.json')) {
        Write-Host "Verify SCM startup rejects invalid $file and recovers after restoration"
        $path = Join-Path (Join-Path $metadata.DataRoot 'state') $file
        $original = [IO.File]::ReadAllBytes($path)
        $before = Snapshot @($path)
        [void](Stop-OwnedService $metadata)
        try {
            # Truncate the test-owned file in place so its protected ACL stays intact.
            [IO.File]::WriteAllBytes($path, [byte[]]@(0x7b))
            Start-Service -Name $metadata.ServiceName -ErrorAction SilentlyContinue
            Wait-Service $metadata 'Stopped' 10
            $failed = Owned-Service $metadata
            Assert ($failed.ProcessId -eq 0 -and $failed.ExitCode -eq 1066 -and $failed.ServiceSpecificExitCode -eq 1) "SCM did not report a startup failure for invalid $file."
        } finally {
            try {
                if ((Owned-Service $metadata).State -ne 'Stopped') { [void](Stop-OwnedService $metadata) }
            } finally {
                [IO.File]::WriteAllBytes($path, $original)
                Assert-Snapshot $before
                Start-Service -Name $metadata.ServiceName
                Assert-Running $metadata
            }
        }
    }
}
function Remove-OwnedTree([hashtable]$metadata, [switch]$AfterServiceRollback) {
    $absolute = [IO.Path]::GetFullPath($metadata.InstallRoot).TrimEnd('\')
    Assert ($metadata.InstallAttempted -and $metadata.ObservedInstallRoot -ceq $absolute -and
            [IO.Path]::GetDirectoryName($absolute) -eq $programFiles) 'Refusing unowned delete.'
    if (!(Test-Path -LiteralPath $absolute)) { return }
    Assert ((Resolve-Path -LiteralPath $absolute).ProviderPath.TrimEnd('\') -eq $absolute) 'Cleanup root changed.'
    for ($parent = [IO.Path]::GetDirectoryName($absolute); $parent; $parent = [IO.Path]::GetDirectoryName($parent)) {
        Assert (!((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Reparse point in cleanup ancestry.'
    }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($absolute)
    if ($AfterServiceRollback) {
        # A stopped fresh service can leave its advertised port and SQLite database.
        # The cache uses DELETE journaling and releases its lock when it closes.
        $stateDirectories = @('bin\data\state', 'bin\data', 'bin')
        $stateFiles = @('bin\data\state\admin-port', 'bin\data\state\cache-v1.sqlite')
    }
    while ($pending.Count) {
        $directory = $pending.Pop()
        $entry = Get-Item -LiteralPath $directory -Force
        Assert ($entry.PSIsContainer -and !($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Invalid directory in cleanup tree.'
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            Assert (!($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Reparse point in cleanup tree.'
            Assert ($item.FullName.StartsWith($absolute + '\', [StringComparison]::OrdinalIgnoreCase)) 'Cleanup child escaped root.'
            if ($AfterServiceRollback) {
                $relative = $item.FullName.Substring($absolute.Length + 1)
                $allowed = ($item -is [IO.DirectoryInfo] -and $relative -cin $stateDirectories) -or
                           ($item -is [IO.FileInfo] -and $relative -cin $stateFiles)
                Assert $allowed "Unexpected path or file type after MSI rollback: $relative"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
    if ($AfterServiceRollback) {
        # Validate the entire tree above before deleting anything. Empty-only directory
        # removal also leaves any unexpected late arrival visible as a failure.
        foreach ($relative in $stateFiles) {
            $file = Join-Path $absolute $relative
            if ([IO.File]::Exists($file)) { [IO.File]::Delete($file) }
        }
        foreach ($relative in $stateDirectories) {
            $directory = Join-Path $absolute $relative
            if ([IO.Directory]::Exists($directory)) { [IO.Directory]::Delete($directory) }
        }
        [IO.Directory]::Delete($absolute)
    } else {
        Remove-Item -LiteralPath $absolute -Recurse -Force
    }
}
function Assert-Uninstalled([hashtable]$metadata) {
    Wait-Absent $metadata
    Assert (!(Registered $metadata.ProductCode)) 'Product registration remained.'
    Assert (@(Owned-Processes $metadata).Count -eq 0) 'An installed process remained.'
    Assert (!(Test-Path -LiteralPath $metadata.Executable)) 'The installed executable remained.'
    if ($metadata.OverrideInstallRoot) {
        Assert (!(Test-Path -LiteralPath $metadata.DefaultInstallRoot)) 'Custom installation wrote to the default directory.'
    }
}
function Assert-RolledBack([hashtable]$metadata, [bool]$serviceStarted) {
    Assert-Uninstalled $metadata
    foreach ($path in @($metadata.Config, $metadata.Token, $metadata.AdminToken)) {
        Assert (!(Test-Path -LiteralPath $path)) "Rollback left an installer-created path: $path"
    }
    if ($serviceStarted -and (Test-Path -LiteralPath $metadata.InstallRoot)) {
        Remove-OwnedTree $metadata -AfterServiceRollback
    }
    Assert-CleanHost $metadata
}
function Assert-CleanHost([hashtable]$metadata) {
    Assert (!(Get-CimInstance Win32_Service -Filter "Name='$($metadata.ServiceName)'")) 'The service is already registered.'
    Assert (!(Registered $metadata.ProductCode)) 'The MSI product is already registered.'
    Assert (@($wi.RelatedProducts($metadata.UpgradeCode)).Count -eq 0) 'A related product is already registered.'
    foreach ($root in @($metadata.InstallRoot, $metadata.DefaultInstallRoot) | Select-Object -Unique) {
        Assert ([IO.Path]::GetDirectoryName($root) -eq $programFiles) 'Unexpected installation root.'
        # Enumerating the parent also detects dangling links. An inaccessible parent must fail preflight.
        $existingEntries = [IO.Directory]::GetFileSystemEntries($programFiles, [IO.Path]::GetFileName($root))
        Assert ($existingEntries.Length -eq 0) "Refusing an existing installation path: $root"
    }
}
function Test-MsiLayout([hashtable]$metadata, $result) {
    $failure = $null
    $metadata.Started = Get-Date
    try {
        Assert-CleanHost $metadata
        [void][IO.Directory]::CreateDirectory($metadata.LogDirectory)
        Invoke-Msi $metadata '/i' '01-fail-after-initialize' 1603 'afterInitialize'
        Assert-RolledBack $metadata $false
        $result.Checks.Add('rollback-after-initialize')
        Invoke-Msi $metadata '/i' '02-fail-after-start-services' 1603 'afterStartServices'
        Assert-RolledBack $metadata $true
        $result.Checks.Add('rollback-after-start-services')

        Invoke-Msi $metadata '/i' '03-install'
        Assert-Running $metadata
        $result.Checks.Add('fresh-install-and-service')
        Assert-Administration $metadata
        $result.Checks.Add('administration-and-acls')
        Assert-InstalledColdStartValidation $metadata
        $result.Checks.Add('cold-start-rejection-and-recovery')
        Assert-InstalledClientRegistry $metadata
        Assert-InstalledTokenRotation $metadata
        Assert-InstalledClientRegistry $metadata
        $result.Checks.Add('client-registry-and-token-rotation')

        $retained = Snapshot @($metadata.Config, $metadata.Token, $metadata.AdminToken,
            (Join-Path $metadata.DataRoot 'state\settings.json'),
            (Join-Path $metadata.DataRoot 'state\secrets.json'),
            (Join-Path $metadata.DataRoot 'state\client-registry.json'))
        Invoke-Msi $metadata '/x' '04-uninstall'
        Assert-Uninstalled $metadata
        Assert-Snapshot $retained
        $result.Checks.Add('uninstall-retains-data-and-acls')

        Invoke-Msi $metadata '/i' '05-reinstall'
        Assert-Running $metadata
        Assert-Snapshot $retained
        $result.Checks.Add('same-version-reinstall')
        Invoke-Msi $metadata '/x' '06-uninstall'
        Assert-Uninstalled $metadata
        Assert-Snapshot $retained
        $result.Checks.Add('final-uninstall')
    } catch {
        $failure = $_
        $result.Error = $_.Exception.Message
    } finally {
        if ($metadata.InstallAttempted) {
            try {
                if (Registered $metadata.ProductCode) { Invoke-Msi $metadata '/x' 'cleanup-uninstall' }
            } catch { $result.CleanupErrors.Add($_.Exception.Message) }
            try {
                $service = Owned-Service $metadata
                if ($service -and $service.State -ne 'Stopped') {
                    try { [void](Stop-OwnedService $metadata) }
                    catch { foreach ($process in Owned-Processes $metadata) { Stop-Process -Id $process.ProcessId -Force } }
                }
                foreach ($process in Owned-Processes $metadata) { Stop-Process -Id $process.ProcessId -Force }
                Assert-Uninstalled $metadata
                if (Test-Path -LiteralPath $metadata.InstallRoot) { Remove-OwnedTree $metadata }
                Assert-CleanHost $metadata
                $result.Checks.Add('cleanup')
            } catch { $result.CleanupErrors.Add($_.Exception.Message) }
        }
        $result.Status = if ($failure -or $result.CleanupErrors.Count) { 'failed' } else { 'passed' }
    }
    if ($failure) { throw $failure }
    if ($result.CleanupErrors.Count) { throw ($result.CleanupErrors -join [Environment]::NewLine) }
}

$expected = $options['--CandidateSHA256']
$candidate = Read-Msi $options['--CandidateMsi'] $expected
$output = [IO.Path]::GetFullPath($options['--OutputDir'])
$runDir = Join-Path $output ('msi-acceptance-' + [guid]::NewGuid().ToString('N'))
$layouts = [ordered]@{}
$report = [ordered]@{
    Passed = $false
    Candidate = @{ ProductCode = $candidate.ProductCode; ProductVersion = $candidate.ProductVersion
                   ServiceName = $candidate.ServiceName; SHA256 = $candidate.Sha256; Path = $candidate.Path }
    ScriptSha256 = $scriptSha256
    PowerShellVersion = $PSVersionTable.PSVersion.ToString()
    PowerShellEdition = $PSVersionTable.PSEdition
    WindowsBuild = $osInfo.BuildNumber
    WindowsProductType = $osInfo.ProductType
    Scenarios = [ordered]@{}
    Error = $null
}
foreach ($name in @('default', 'custom')) {
    $layout = $candidate.Clone()
    Set-ExpectedInstance $layout ($name -eq 'custom')
    foreach ($path in @($candidate.Path, $output)) {
        Assert ($path -ne $layout.InstallRoot -and !$path.StartsWith($layout.InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) 'Artifacts must be outside the installation directory.'
    }
    $layout.LogDirectory = Join-Path $runDir $name
    $layouts[$name] = $layout
    $report.Scenarios[$name] = [ordered]@{
        Status = 'not-run'; InstallRoot = $layout.InstallRoot; LogDirectory = $name
        Checks = [Collections.Generic.List[string]]::new()
        CleanupErrors = [Collections.Generic.List[string]]::new()
        Error = $null
    }
}
[void][IO.Directory]::CreateDirectory($runDir)
# Both layouts use the same product and service. Serialize acceptance runs on this host.
$mutex = [Threading.Mutex]::new($false, ('Global\QCopilotsWebSearchMsiAcceptance-' + $candidate.ProductCode))
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    Assert $locked 'Another acceptance run owns this MSI product.'
    foreach ($name in $layouts.Keys) {
        Write-Host "Testing $name installation directory"
        Test-MsiLayout $layouts[$name] $report.Scenarios[$name]
    }
    Assert ((Get-FileHash -LiteralPath $candidate.Path -Algorithm SHA256).Hash -eq $expected) 'MSI bytes changed during acceptance.'
    $report.Passed = $true
} catch {
    $report.Error = $_.Exception.Message
    throw
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($wi)
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'result.json') -Encoding UTF8
}
Write-Host "PASS. Temporary test results: $runDir"
