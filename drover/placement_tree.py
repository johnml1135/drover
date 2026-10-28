"""Placement tree operations."""
import os
import subprocess
import signal
import time

from .placement_paths import (
    IS_WINDOWS,
)


KILL_AFTER_S = 15


class PosixTree:
    def __init__(self, proc):
        self.proc, self.pgid = proc, proc.pid

    def _group_alive(self):
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _sig(self, sig):
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def kill(self, grace=KILL_AFTER_S):
        """SIGTERM the group, then SIGKILL whatever is left after grace seconds."""
        self._sig(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.proc.poll()            # reap the leader so a zombie does not keep the group "alive"
            if not self._group_alive():
                break
            time.sleep(0.05)
        if self._group_alive():          # not after the group is gone: the pgid could be reused
            self._sig(signal.SIGKILL)
        self.proc.wait()

    def reap_stragglers(self):
        """The worker exited; end anything it left behind in the group."""
        if self._group_alive():
            self._sig(signal.SIGKILL)

    def metrics(self):
        from .metrics import proc_group
        return proc_group(self.pgid)

    def close(self):
        pass


WindowsTree = None


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.OpenJobObjectW.restype = wintypes.HANDLE
    _k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_BASIC_AND_IO_ACCOUNTING_CLASS = 8
    JOB_EXTENDED_LIMIT_CLASS = 9

    def _resume(handle):
        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
        return nt.NtResumeProcess(int(handle))

    class WindowsTree:
        """A Job Object that kills its whole tree on TerminateJobObject, or when the runner dies (KILL_ON_CLOSE)."""

        def __init__(self, proc):
            self.proc, self.job = proc, None
            try:
                self.job = self._make_job(proc)
                if _resume(proc._handle) != 0:      # the worker started suspended so no child escapes the job
                    raise OSError("could not resume the suspended worker")
            except BaseException:
                self.close_and_kill()
                raise

        @staticmethod
        def _make_job(proc):
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _EXT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_CLASS, ctypes.byref(info),
                                                ctypes.sizeof(info)) \
                    or not _k32.AssignProcessToJobObject(job, int(proc._handle)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(job)
                raise ctypes.WinError(err)
            return job

        def metrics(self):
            class Accounting(ctypes.Structure):
                _fields_ = [(n, ctypes.c_int64) for n in ("user", "kernel", "period_user", "period_kernel")] + [
                    (n, wintypes.DWORD) for n in ("faults", "total", "active", "terminated")]
            class AccountingAndIO(ctypes.Structure):
                _fields_ = [("BasicInfo", Accounting), ("IoInfo", _IOC)]
            accounting, ext = AccountingAndIO(), _EXT()
            for kind, obj in ((JOB_BASIC_AND_IO_ACCOUNTING_CLASS, accounting), (JOB_EXTENDED_LIMIT_CLASS, ext)):
                if not _k32.QueryInformationJobObject(self.job, kind, ctypes.byref(obj), ctypes.sizeof(obj), None):
                    raise ctypes.WinError(ctypes.get_last_error())
            basic = accounting.BasicInfo
            return ((basic.user + basic.kernel) / 10_000_000, ext.PeakJobMemoryUsed,
                    accounting.IoInfo.rb + accounting.IoInfo.wb)

        def close_and_kill(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
                _k32.CloseHandle(self.job)
                self.job = None
            self.proc.kill()
            self.proc.wait()

        def kill(self, grace=KILL_AFTER_S):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

        def reap_stragglers(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)

        def close(self):
            if self.job:
                _k32.CloseHandle(self.job)
                self.job = None
