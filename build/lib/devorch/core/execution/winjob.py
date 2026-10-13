"""Windows Job Object: kills a process and every descendant atomically.

`taskkill /T` walks a snapshot of the process tree, so a process created while it runs survives (reproduced:
a test that keeps forking left 6 children behind after a timeout). A process assigned to a job can't escape it
(its children join the job), and terminating the job kills them all at once. The job is also closed with
KILL_ON_JOB_CLOSE, so nothing survives if devorch itself dies.

Windows only; on other platforms ``Job.attach`` returns None. Failures to create/assign return None — the
caller falls back to taskkill, i.e. the old best-effort behavior.
"""

from __future__ import annotations

import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class Job:
    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @staticmethod
    def attach(pid: int) -> Job | None:
        """Put the running process ``pid`` in a new kill-on-close job; None if that isn't possible."""
        if os.name != "nt":
            return None
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        proc = None
        try:
            if not _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError
            proc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc or not _k32.AssignProcessToJobObject(job, proc):
                raise OSError
        except OSError:
            _k32.CloseHandle(job)
            return None
        finally:
            if proc:
                _k32.CloseHandle(proc)
        return Job(job)

    def terminate(self) -> bool:
        """Kill every process in the job (the process and all its descendants)."""
        return self._handle is not None and bool(_k32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        """Release the job; anything still running in it is killed (KILL_ON_JOB_CLOSE)."""
        if self._handle is not None:
            _k32.CloseHandle(self._handle)
            self._handle = None
