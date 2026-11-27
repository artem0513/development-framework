"""Real owned-process regressions; teardown never targets an unheld saved PID."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from ase.core import execute


class HeldProcess:
    """Keep the exact test process identity through assertions and cleanup."""
    def __init__(self, pid):
        self.pid = pid
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.TerminateProcess.restype = wintypes.BOOL
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            self.handle = os.pidfd_open(pid)

    def exited(self, timeout=0):
        if os.name == "nt":
            result = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
            if result not in (0, 258):
                raise OSError("WaitForSingleObject failed")
            return result == 0
        import select
        return bool(select.select([self.handle], [], [], timeout)[0])

    def close(self):
        try:
            if not self.exited():
                if os.name == "nt":
                    self.kernel.TerminateProcess(self.handle, 91)
                else:
                    signal.pidfd_send_signal(self.handle, signal.SIGKILL)
                if not self.exited(2):
                    raise AssertionError("Owned test process did not stop during teardown")
        finally:
            if os.name == "nt":
                self.kernel.CloseHandle(self.handle)
            else:
                os.close(self.handle)


PARENT = r'''
import os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / "parent.pid").write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", "from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text('ready');time.sleep(20)", str(root / "child.ready")])
(root / "child.pid").write_text(str(child.pid))
deadline = time.monotonic() + 4
while not (root / "release").exists():
    if time.monotonic() > deadline: raise RuntimeError("test observer never released parent")
    time.sleep(.005)
'''


@unittest.skipUnless(os.name == "nt" or (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")),
                     "Exact native process identity handles required")
class ProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ase-process-ownership-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.held = {}
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # Wait for every captured process before TemporaryDirectory removes cwd.
        for process in reversed(list(self.held.values())):
            process.close()

    def observe(self):
        if (not self.held and (self.root / "child.ready").exists()
                and all((self.root / (role + ".pid")).exists() for role in ("parent", "child"))):
            for role in ("parent", "child"):
                self.held[role] = HeldProcess(int((self.root / (role + ".pid")).read_text()))
        return bool(self.held)

    def test_cancel_after_parent_exit_zero_still_kills_descendant(self):
        def stop_after_parent_exit():
            if self.observe():
                (self.root / "release").touch()
                return self.held["parent"].exited()
            return False

        result = execute([sys.executable, "-c", PARENT, str(self.root)], self.root,
                         self.root / "run.log", 6, cancel_requested=stop_after_parent_exit)
        self.assertTrue(self.held)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["error"], "PROCESS_CANCELLED")
        self.assertTrue(self.held["child"].exited(2), "Descendant survived its cancelled parent")
        self.assertLess(result["duration_seconds"], 5)

    def test_normal_exit_preserves_authorized_background_descendant(self):
        def observe_without_cancelling():
            if self.observe():
                (self.root / "release").touch()
            return False

        result = execute([sys.executable, "-c", PARENT, str(self.root)], self.root,
                         self.root / "run.log", 6, cancel_requested=observe_without_cancelling)
        self.assertTrue(self.held)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["error"])
        self.assertFalse(self.held["child"].exited(), "Normal completion killed its background resource")
        self.assertLess(result["duration_seconds"], 5)

    def test_keyboard_interrupt_cleans_up_parent_and_descendant(self):
        def interrupt_after_readiness():
            if self.observe():
                raise KeyboardInterrupt
            return False

        with self.assertRaises(KeyboardInterrupt):
            execute([sys.executable, "-c", PARENT, str(self.root)], self.root,
                    self.root / "run.log", 6, cancel_requested=interrupt_after_readiness)
        self.assertTrue(self.held)
        self.assertTrue(self.held["parent"].exited(2))
        self.assertTrue(self.held["child"].exited(2))


if __name__ == "__main__":
    unittest.main()
