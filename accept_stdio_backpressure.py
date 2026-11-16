"""Independent exact-runtime acceptance that observes a genuinely full stdout pipe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--Executable', type=Path, required=True)
parser.add_argument('--Template', type=Path, required=True)
parser.add_argument('--EvidenceLibrary', type=Path, required=True)
parser.add_argument('--Output', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.EvidenceLibrary))
from runtime_evidence import fingerprint, host_info

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def bounded(action, seconds=10):
    result, failure = [], []
    def execute():
        try:
            result.append(action())
        except BaseException as error:
            failure.append(error)
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError('Owned pipe operation exceeded its deadline')
    if failure:
        raise failure[0]
    return result[0]

def write_all(stream, payload):
    remaining = memoryview(payload)
    while remaining:
        count = stream.write(remaining)
        assert count
        remaining = remaining[count:]

def observer(stream):
    sizing = {}
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        dword_pointer = ctypes.POINTER(wintypes.DWORD)
        kernel.GetNamedPipeInfo.argtypes = [wintypes.HANDLE] + [dword_pointer] * 4
        kernel.GetNamedPipeInfo.restype = wintypes.BOOL
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                        dword_pointer, dword_pointer, dword_pointer]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        outgoing, incoming = wintypes.DWORD(), wintypes.DWORD()
        assert kernel.GetNamedPipeInfo(handle, None, ctypes.byref(outgoing), ctypes.byref(incoming), None), ctypes.get_last_error()
        capacity = max(outgoing.value, incoming.value)
        def available():
            count = wintypes.DWORD()
            assert kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(count), None), ctypes.get_last_error()
            return count.value
        def prefix():
            data, read = ctypes.create_string_buffer(64), wintypes.DWORD()
            assert kernel.PeekNamedPipe(handle, data, 64, ctypes.byref(read), None, None), ctypes.get_last_error()
            return data.raw[:read.value]
    else:
        import array
        import fcntl
        import termios
        previous = fcntl.fcntl(stream.fileno(), fcntl.F_GETPIPE_SZ)
        requested = os.sysconf('SC_PAGESIZE')
        capacity = fcntl.fcntl(stream.fileno(), fcntl.F_SETPIPE_SZ, requested)
        assert capacity == fcntl.fcntl(stream.fileno(), fcntl.F_GETPIPE_SZ)
        assert capacity == requested
        sizing = {'previous_bytes': previous, 'requested_bytes': requested, 'actual_bytes': capacity}
        def available():
            count = array.array('i', [0])
            fcntl.ioctl(stream.fileno(), termios.FIONREAD, count, True)
            return count[0]
        prefix = None
    assert 0 < capacity <= 1024 * 1024, 'Unexpected pipe capacity, refusing an unproved test'
    return capacity, available, prefix, sizing

def request(identifier):
    return (json.dumps({'jsonrpc': '2.0', 'id': identifier, 'method': 'ping'}, separators=(',', ':')) + '\n').encode()

def exercise(mode):
    record = {'mode': mode, 'passed': False, 'forced_cleanup': False}
    with tempfile.TemporaryDirectory(prefix='qcws observed pipe ') as temporary:
        config = json.loads(args.Template.read_text('utf-8'))
        config['cache']['directory'] = str(Path(temporary) / 'cache')
        config['clients'] = [{'id': 'observed-pipe', 'token_env': 'QCWS_PIPE_TOKEN'}]
        path = Path(temporary) / 'runtime.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        child = subprocess.Popen([str(args.Executable), '--ConfigPath', str(path), '--Stdio'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            env={**os.environ, 'QCWS_PIPE_TOKEN': secrets.token_hex(32)})
        try:
            init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'observed-pipe', 'version': '1'}}}
            bounded(lambda: write_all(child.stdin, (json.dumps(init) + '\n').encode()))
            assert json.loads(bounded(child.stdout.readline))['id'] == 1
            identifier = 'p' * 60000
            assert len(json.dumps(identifier).encode()) <= 65536
            if mode == 'drained-control':
                # A concurrent writer lets the reader drain even small input/output pipes.
                error = []
                def send():
                    try:
                        write_all(child.stdin, request(identifier))
                    except BaseException as exc:
                        error.append(str(exc))
                writer = threading.Thread(target=send, daemon=True)
                writer.start()
                reply = bounded(child.stdout.readline)
                writer.join(10)
                assert not writer.is_alive() and not error
                parsed = json.loads(reply)
                assert parsed['id'] == identifier and parsed['result'] == {} and 'error' not in parsed
                record['valid_response_bytes'] = len(reply)
            elif mode == 'full-pipe-eof':
                capacity, available, prefix, sizing = observer(child.stdout)
                count = max(8, capacity // 60000 + 2)
                assert count <= 32
                payload = b''.join(request(f'{number:02}' + 'p' * 59998) for number in range(count))
                bounded(lambda: write_all(child.stdin, payload))
                samples = []
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    samples.append(available())
                    assert child.poll() is None, 'Service exited before EOF'
                    if len(samples) >= 22 and len(set(samples[-22:])) == 1 and samples[-1] >= capacity:
                        break
                    time.sleep(0.05)
                assert len(samples) >= 22 and len(set(samples[-22:])) == 1 and samples[-1] >= capacity
                assert count * 60000 > samples[-1], 'All expected output fit, no pressure established'
                if prefix is not None:
                    visible = prefix()
                    assert b'pppppppp' in visible, 'Observed output was not the legal large reply'
                    record['peek_prefix_hex'] = visible.hex()
                record.update(pipe_capacity_bytes=capacity, available_byte_samples=samples,
                              controlled_pipe_sizing=sizing,
                              stable_observation_minimum_seconds=1.05,
                              request_count=count, minimum_expected_response_bytes=count * 60000,
                              stdout_read_bytes_before_eof=0)
            else:
                child.stdout.close()
                bounded(lambda: write_all(child.stdin, request(identifier)))
            started = time.monotonic()
            if mode != 'closed-reader':
                child.stdin.close()
            result = child.wait(timeout=5)
            elapsed = time.monotonic() - started
            assert result == (1 if mode == 'closed-reader' else 0), result
            assert elapsed < 5
            if mode == 'full-pipe-eof':
                remaining = child.stdout.read()
                # CancelSynchronousIo may discard a pending Windows write that Peek
                # exposed before EOF. Linux retains bytes already written to the pipe.
                assert len(remaining) < count * 60000, 'Output was not canceled while pending'
                record['unread_bytes_after_exit'] = len(remaining)
                record['unread_sha256'] = hashlib.sha256(remaining).hexdigest()
            record.update(exit_code=result, stop_seconds=round(elapsed, 4), passed=True)
        except BaseException as error:
            record['error'] = repr(error)
        finally:
            if child.poll() is None:
                record['forced_cleanup'] = True
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
    return record

report = {'schema_version': 1, 'script_sha256': sha(Path(__file__)), 'host': host_info(),
          'runtime': fingerprint(args.Executable), 'executable_sha256': sha(args.Executable),
          'cases': [], 'passed': False,
          'references': ['https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-getnamedpipeinfo',
                         'https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-peeknamedpipe',
                         'https://man7.org/linux/man-pages/man2/F_GETPIPE_SZ.2const.html']}
try:
    for mode in ('drained-control', 'full-pipe-eof', 'closed-reader'):
        report['cases'].append(exercise(mode))
    assert all(case['passed'] and not case['forced_cleanup'] for case in report['cases']), report['cases']
    assert report['runtime'] == fingerprint(args.Executable)
    report['passed'] = True
finally:
    args.Output.parent.mkdir(parents=True, exist_ok=True)
    args.Output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
print(json.dumps({key: report[key] for key in ('passed', 'host', 'cases')}, indent=2))
