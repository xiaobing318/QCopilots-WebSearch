"""Inventory archive bytes with host-independent path and link validation."""
import hashlib
import posixpath
import stat
import subprocess
import tarfile
import zipfile


def canonical_member(raw, windows=False):
    if not isinstance(raw, str) or any(c in raw for c in ('\\', ':', '\x00')):
        raise RuntimeError('Unsafe package member')
    while raw.startswith('./'):
        raw = raw[2:]
    parts = raw.split('/')
    if not raw or raw.startswith('/') or any(p in ('', '.', '..') for p in parts):
        raise RuntimeError('Unsafe package member')
    if windows:
        reserved = {'CON','PRN','AUX','NUL','CONIN$','CONOUT$',
                    *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}
        for part in parts:
            if (part.endswith((' ', '.')) or part.split('.',1)[0].upper() in reserved
                    or any(ord(c) < 32 or c in '<>"|?*' for c in part)):
                raise RuntimeError('Ambiguous Windows package path')
    return '/'.join(parts)


def package_files(package):
    suffix = package.suffix.lower()
    windows = suffix == '.zip'
    items, seen, links, directories = [], set(), {}, set()
    key = lambda name: name.casefold() if windows else name

    def directory(raw):
        name = raw.rstrip('/')
        if name not in ('.', './'):
            name = canonical_member(name, windows)
            if key(name) in seen:
                raise RuntimeError('File and directory share a package path')
            directories.add(key(name))

    def add(raw, stream=None, target=None, hard=False):
        name = canonical_member(raw, windows)
        if key(name) in seen or key(name) in directories:
            raise RuntimeError('Duplicate package member')
        seen.add(key(name))
        item = {'type':'file','name':name,'bom-ref':'file:' + hashlib.sha256(name.encode()).hexdigest()}
        if stream is not None:
            item['hashes'] = [{'alg':'SHA-256','content':hashlib.file_digest(stream, 'sha256').hexdigest()}]
        if target is not None:
            if not target or any(c in target for c in ('\\', ':', '\x00')):
                raise RuntimeError('Unsafe package link')
            if target.startswith('/'):
                if windows:
                    raise RuntimeError('Absolute portable-package link')
                resolved = canonical_member(target.lstrip('/'))
            else:
                resolved = canonical_member(posixpath.normpath(posixpath.join(
                    '' if hard else posixpath.dirname(name), target)), windows)
            links[name] = resolved
            item['properties'] = [{'name':'qcws:link-target','value':target},
                                  {'name':'qcws:link-kind','value':'hard' if hard else 'symbolic'}]
        items.append(item)

    if suffix == '.zip':
        with zipfile.ZipFile(package) as archive:
            for entry in sorted(archive.infolist(), key=lambda value: value.filename):
                raw, mode = entry.orig_filename, stat.S_IFMT(entry.external_attr >> 16)
                if entry.is_dir() or mode == stat.S_IFDIR:
                    if entry.file_size:
                        raise RuntimeError('Directory has an unexpected payload')
                    directory(raw)
                    continue
                if mode not in (0, stat.S_IFREG, stat.S_IFLNK):
                    raise RuntimeError('Unsupported ZIP member kind')
                with archive.open(entry) as stream:
                    if mode == stat.S_IFLNK:
                        value = stream.read(4097)
                        if len(value) > 4096:
                            raise RuntimeError('Oversized ZIP link')
                        add(raw, target=value.decode('utf-8', 'strict'))
                    else:
                        add(raw, stream=stream)
    elif suffix == '.deb':
        process = subprocess.Popen(['dpkg-deb','--fsys-tarfile',str(package)], stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
                for entry in archive:
                    if entry.isdir():
                        if entry.size:
                            raise RuntimeError('Directory has an unexpected payload')
                        directory(entry.name)
                    elif entry.isfile():
                        with archive.extractfile(entry) as stream:
                            add(entry.name, stream=stream)
                    elif entry.issym() or entry.islnk():
                        add(entry.name, target=entry.linkname, hard=entry.islnk())
                    else:
                        raise RuntimeError('Unsupported DEB member kind')
            if process.wait(timeout=30):
                raise RuntimeError('Cannot read DEB contents')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    else:
        raise RuntimeError('Expected ZIP or DEB package')
    if not items:
        raise RuntimeError('Empty package inventory')
    names = {item['name'] for item in items}
    for name in names:
        parent = posixpath.dirname(name)
        while parent:
            if key(parent) in seen:
                raise RuntimeError('Package file occupies a parent directory')
            parent = posixpath.dirname(parent)
    for origin, target in links.items():
        visited = {origin}
        while True:
            if target not in names or target in visited:
                raise RuntimeError('Missing or cyclic link target')
            visited.add(target)
            if target not in links:
                break
            target = links[target]
    return items


def require_windows_payload_match(zip_items, msi_items):
    executable = 'bin/qcopilots-websearch.exe'
    roots = [item['name'][:-len(executable)] for item in zip_items
             if item['name'] == executable or item['name'].endswith('/' + executable)]
    if len(roots) != 1:
        raise RuntimeError('ZIP executable root is ambiguous')

    def payload(items, prefix):
        result = {}
        for item in items:
            name = item['name']
            if not name.startswith(prefix):
                raise RuntimeError('File outside application installation root')
            relative = canonical_member(name[len(prefix):], windows=True)
            hashes = [value['content'] for value in item.get('hashes', []) if value['alg'] == 'SHA-256']
            if len(hashes) != 1 or relative.casefold() in result:
                raise RuntimeError('Windows payload must contain unique regular files')
            result[relative.casefold()] = hashes[0]
        return result

    zipped = payload(zip_items, roots[0])
    installed = payload(msi_items, '[ProgramFiles64Folder]/QCopilots-WebSearch/')
    if zipped != installed:
        raise RuntimeError('ZIP/MSI payload mismatch')
    required = {'bin/libssl-3-x64.dll', 'bin/libcrypto-3-x64.dll',
                'share/qcopilots-websearch/licenses/openssl/license.txt'}
    if not required <= set(zipped):
        raise RuntimeError('Missing bundled OpenSSL DLL or license')
    return {'files':len(zipped),'consistent':True}
