"""Cross-platform peak-memory counters used by isolated workload probes.

Sampling a child every few milliseconds can miss a short allocation spike and
forces an arbitrary polling interval into the resource contract.  Workers call
the operating system's own high-water counter instead:

* Windows exposes ``PeakWorkingSetSize`` through ``GetProcessMemoryInfo``.
* Linux and macOS expose ``ru_maxrss`` through ``getrusage``.

The returned value is the process peak, not the current resident set.
"""

from __future__ import annotations

import os
import sys


def process_peak_rss_bytes() -> int:
    """Return the current process' lifetime peak resident memory in bytes."""
    if os.name == "nt":
        return _windows_peak_working_set()
    return _posix_peak_rss()


def _posix_peak_rss() -> int:
    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes while Linux reports KiB.
    return peak if sys.platform == "darwin" else peak * 1024


def _windows_peak_working_set() -> int:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)
