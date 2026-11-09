"""Free a BOUNDED amount of RAM by lowering a process's working-set ceiling.

EmptyWorkingSet() releases everything down to the minimum, which throws away all the hot
pages. SetProcessWorkingSetEx with a maximum below the current working set makes the
balance-set manager release only what we ask for (needed + margin) and leave the rest mapped
and warm. File-backed pages (mmap'd GGUF experts) are the first to go; private commit stays.

Windows-only by nature: SetProcessWorkingSetSizeEx and the psapi enumeration below have no
POSIX equivalent - Linux has no per-process working-set ceiling, and the kernel already
evicts file-backed pages under pressure. On POSIX this prints the reason and exits 0,
because dow_spool calls it unconditionally before every batch (DOW_TRIM_*); a crash there
would read as a stack fault. Importing this module on any OS never touches WinDLL.

usage: mem_trim.py --proc strata --needed 14000 [--margin 1024] [--floor 512] [--quiet]
"""
import argparse, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack_paths as sp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proc", default="strata")
    ap.add_argument("--needed", type=int, required=True, help="MB of physical RAM to make room for")
    ap.add_argument("--margin", type=int, default=1024, help="extra MB beyond --needed")
    ap.add_argument("--floor", type=int, default=512, help="min working set to leave, MB")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if sp.OS != "windows":
        print("mem_trim: working-set ceilings (SetProcessWorkingSetSizeEx) are Windows-only; "
              "no-op on %s" % sp.OS, flush=True)
        return
    _trim_windows(a)
    time.sleep(3)          # let the balance-set manager actually page out before we return


def _trim_windows(a):
    # Every Windows call lives under sp.OS == "windows"; on POSIX this function is defined but
    # never entered, so ctypes.WinDLL("kernel32") at import time (the old crash) can't happen.
    import ctypes
    from ctypes import wintypes as wt

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_SET_INFORMATION = 0x0008
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    SET_PROCESS_WS_QUOTA_LIMITED = 0x00000001

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    # Without these restypes/argtypes ctypes defaults to C int and TRUNCATES the 64-bit HANDLE,
    # which makes every call silently fail (snapshot empty, OpenProcess 0).
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.SetProcessWorkingSetSizeEx.argtypes = [wt.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wt.DWORD]
    k32.SetProcessWorkingSetSizeEx.restype = wt.BOOL
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
    psapi.EnumProcesses.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    psapi.EnumProcesses.restype = wt.BOOL
    psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.c_wchar_p, wt.DWORD]
    psapi.GetModuleFileNameExW.restype = wt.DWORD

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t)]

    def pids(name):
        """EnumProcesses + GetModuleFileNameExW. Toolhelp's PROCESSENTRY32W rejects every dwSize
        this Python build produces (ERROR_BAD_LENGTH), so avoid the variable-length struct."""
        want = name.lower() + ".exe"
        arr = (wt.DWORD * 8192)()
        got = wt.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(got)):
            return []
        out = []
        for pid in arr[: got.value // 4]:
            if not pid:
                continue
            h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not h:
                continue
            buf = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleFileNameExW(h, None, buf, 260):
                if buf.value.rsplit("\\", 1)[-1].lower() == want:
                    out.append(pid)
            k32.CloseHandle(h)
        return out

    def probe(h):
        m = PMC(); m.cb = ctypes.sizeof(PMC)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb)
        return m.WorkingSetSize, m.PagefileUsage, m.MinimumWorkingSetSize, m.MaximumWorkingSetSize

    want = (a.needed + a.margin) * 2**20
    for pid in pids(a.proc):
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            if not a.quiet:
                print("%s pid %d: OpenProcess failed err=%d" % (a.proc, pid, ctypes.get_last_error()), flush=True)
            continue
        ws, pf, lo, hi = probe(h)
        if ws < want:
            print("pid %d ws %.1f GB < want %.1f GB; leaving alone" % (pid, ws / 2**20, want / 2**20), flush=True)
            k32.CloseHandle(h); continue
        newmax = ws - want
        newmin = max(a.floor * 2**20, min(newmax, ws // 8))
        ok = k32.SetProcessWorkingSetSizeEx(h, ctypes.c_size_t(newmin), ctypes.c_size_t(newmax),
                                           SET_PROCESS_WS_QUOTA_LIMITED)
        print("pid %d ws %.1f -> ceiling %.1f GB (floor %.2f GB, pf %.1f GB) ok=%s" %
              (pid, ws / 2**30, newmax / 2**30, newmin / 2**30, pf / 2**30, bool(ok)), flush=True)
        k32.CloseHandle(h)


if __name__ == "__main__":
    main()
