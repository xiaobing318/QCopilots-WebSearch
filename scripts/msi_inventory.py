"""Read installed MSI payloads without executing Windows Installer actions."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

QUERY = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$wi=New-Object -ComObject WindowsInstaller.Installer
$db=$null
function Rows([string]$sql,[string[]]$names) {
  $view=$db.OpenView($sql)
  try {
    [void]$view.Execute()
    while ($null -ne ($record=$view.Fetch())) {
      try {
        $row=[ordered]@{}
        for($i=0;$i -lt $names.Count;$i++) {$row[$names[$i]]=[string]$record.StringData($i+1)}
        [pscustomobject]$row
      } finally {[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)}
    }
  } finally {[void]$view.Close();[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)}
}
try {
  $db=$wi.OpenDatabase($env:QCWS_MSI_INVENTORY_PATH,0)
  [ordered]@{
    files=@(Rows 'SELECT `File`,`Component_`,`FileName`,`FileSize` FROM `File`' @('id','component','name','size'))
    components=@(Rows 'SELECT `Component`,`Directory_` FROM `Component`' @('id','directory'))
    directories=@(Rows 'SELECT `Directory`,`Directory_Parent`,`DefaultDir` FROM `Directory`' @('id','parent','name'))
    properties=@(Rows 'SELECT `Property`,`Value` FROM `Property`' @('name','value'))
    services=@(Rows 'SELECT `Name`,`StartName`,`Arguments`,`Component_` FROM `ServiceInstall`' @('name','account','arguments','component'))
  } | ConvertTo-Json -Depth 8 -Compress
} finally {
  if($null -ne $db){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($db)}
  [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($wi)
}
'''
STANDARD = {'ProgramFilesFolder','ProgramFiles64Folder','CommonFilesFolder','CommonFiles64Folder',
            'CommonAppDataFolder','AppDataFolder','LocalAppDataFolder','WindowsFolder',
            'SystemFolder','System64Folder','TempFolder','DesktopFolder','ProgramMenuFolder','StartMenuFolder'}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def leaf(value):
    if not value or value in ('.','..') or any(c in value for c in '/\\:\x00'):
        raise RuntimeError('Unsafe MSI identifier or filename')
    return value


def index(rows):
    result = {}
    for row in rows:
        if row['id'] in result:
            raise RuntimeError('Duplicate MSI identifier')
        result[row['id']] = row
    return result


def inventory_msi(package, wix, environment=None):
    if os.name != 'nt':
        raise RuntimeError('MSI inventory requires Windows')
    package = Path(package).resolve(strict=True)
    env = dict(os.environ if environment is None else environment)
    env['QCWS_MSI_INVENTORY_PATH'] = str(package)
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',QUERY],
                            env=env,capture_output=True,text=True,encoding='utf-8',check=True)
    metadata = json.loads(result.stdout.lstrip('\ufeff'))
    files, components, directories = (index(metadata[key]) for key in ('files','components','directories'))
    if not files:
        raise RuntimeError('MSI has no installed files')

    def directory(identity, active=()):
        if not identity or identity == 'TARGETDIR':
            return []
        if identity in active:
            raise RuntimeError('MSI Directory cycle')
        row = directories[identity]
        if identity in STANDARD:
            return ['[' + identity + ']']
        parent = directory(row['parent'], active + (identity,))
        name = row['name'].split(':',1)[0].split('|')[-1]
        return parent if name in ('','.') else parent + [leaf(name)]

    package_hash, inventory, seen = sha256(package), [], set()
    with tempfile.TemporaryDirectory(prefix='qcws-msi-inventory-') as temporary:
        root = Path(temporary).resolve()
        payload = root / 'payload'
        # WiX deletes an existing -x directory. Only pass this new scoped child.
        if payload.exists() or payload.parent.resolve() != root:
            raise RuntimeError('Unsafe WiX extraction directory')
        subprocess.run([str(wix),'msi','decompile',str(package),'-acceptEula','wix7','-x',str(payload),
                        '-o',str(root/'package.wxs'),'-intermediateFolder',str(root/'intermediate')],
                       env=env,capture_output=True,check=True)
        extracted = payload / 'File'
        if not extracted.is_dir():
            raise RuntimeError('Missing MSI File payload')
        actual = {}
        for path in extracted.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(extracted.resolve()):
                raise RuntimeError('Unexpected extracted MSI link')
            if path.is_file():
                if path.parent != extracted:
                    raise RuntimeError('Unexpected nested MSI file identifier')
                key = path.name.casefold()
                if key in actual:
                    raise RuntimeError('Duplicate extracted MSI identifier')
                actual[key] = path
        # MSI identifiers may end in dots. Win32 removes trailing dots when WiX
        # writes those identifiers as extraction filenames. Require a bijection.
        normalized = {leaf(identity).rstrip(' .').casefold():identity for identity in files}
        if len(normalized) != len(files) or set(actual) != set(normalized):
            raise RuntimeError('MSI File table differs from extracted payload')
        for identity, row in sorted(files.items()):
            source = actual[leaf(identity).rstrip(' .').casefold()]
            if source.stat().st_size != int(row['size']):
                raise RuntimeError('MSI FileSize mismatch')
            name = '/'.join(directory(components[row['component']]['directory']) + [leaf(row['name'].split('|')[-1])])
            if name.casefold() in seen:
                raise RuntimeError('Duplicate MSI installed path')
            seen.add(name.casefold())
            inventory.append({'type':'file','name':name,
                'bom-ref':'file:' + hashlib.sha256((package_hash + ':' + name).encode()).hexdigest(),
                'hashes':[{'alg':'SHA-256','content':sha256(source)}],
                'properties':[{'name':'qcws:msi-file-id','value':identity}]})
    return inventory, metadata
