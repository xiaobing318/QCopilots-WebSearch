"""Wait for retained psutil identities without reopening reused numeric PIDs."""
import time


def wait_owned(processes, timeout):
    """Return still-alive or unconfirmed identities. Reap direct children separately.

    psutil.Process.is_running compares PID and creation time. Process.wait may
    reopen only the numeric PID on Windows, including an unrelated replacement.
    A query failure remains unconfirmed and must prevent a successful cleanup.
    """
    remaining = list(processes)
    deadline = time.monotonic() + timeout
    while True:
        active = []
        for process in remaining:
            try:
                if process.is_running() and process.status() != 'zombie':
                    active.append(process)
            except Exception:
                active.append(process)
        remaining = active
        delay = deadline - time.monotonic()
        if not remaining or delay <= 0:
            return remaining
        time.sleep(min(0.05, delay))
