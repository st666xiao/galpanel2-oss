"""Start suspended, assign an unrestricted Job, then resume."""
import ctypes
from ctypes import wintypes as W
import os
import subprocess

from galpanel2.core.domain import LaunchError
from galpanel2.core.launchopts import LaunchSpec


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", ctypes.POINTER(W.BYTE)), ("hStdInput", W.HANDLE),
                ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", W.LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class ACCOUNTING(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int64) for n in ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime",
                "ThisPeriodTotalKernelTime")] + [(n, W.DWORD) for n in ("TotalPageFaultCount", "TotalProcesses",
                "ActiveProcesses", "TotalTerminatedProcesses")]


def _api():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, W.LPVOID, W.LPVOID, W.BOOL, W.DWORD, W.LPVOID,
                            W.LPCWSTR, ctypes.POINTER(STARTUPINFOEX), ctypes.POINTER(PROCESS_INFORMATION)], W.BOOL),
        "CreateJobObjectW": ([W.LPVOID, W.LPCWSTR], W.HANDLE),
        "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, W.LPVOID], W.BOOL),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "InitializeProcThreadAttributeList": ([W.LPVOID, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([W.LPVOID, W.DWORD, ctypes.c_size_t, W.LPVOID, ctypes.c_size_t,
                                        W.LPVOID, W.LPVOID], W.BOOL),
        "DeleteProcThreadAttributeList": ([W.LPVOID], None),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(k, name)
        fn.argtypes, fn.restype = args, result
    return k


def _error(code=None):
    code = ctypes.get_last_error() if code is None else code
    if code == 740:
        return LaunchError("这个程序要求管理员权限。以管理员身份运行要等下一段（p2-2）。")
    return LaunchError("无法启动：" + ctypes.FormatError(code).strip())


class GameProcess:
    def __init__(self, api, process, job, pid):
        self._api, self._process, self._job = api, process, job
        self.pid = pid

    def alive(self) -> bool:
        if self._job is None:
            return False
        info = ACCOUNTING()
        if not self._api.QueryInformationJobObject(self._job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise _error()
        return info.ActiveProcesses > 0

    def release(self) -> None:
        for name in ("_process", "_job"):
            handle = getattr(self, name)
            if handle is not None:
                self._api.CloseHandle(handle)
                setattr(self, name, None)


def launch(spec: LaunchSpec) -> GameProcess:
    if os.name != "nt":
        raise LaunchError("只支持 Windows")
    import msvcrt
    k = _api()
    command = subprocess.list2cmdline([spec.exe_path]) + (" " + spec.args if spec.args else "")
    si, pi = STARTUPINFOEX(), PROCESS_INFORMATION()
    si.StartupInfo.cb, si.StartupInfo.dwFlags = ctypes.sizeof(si), 0x00000100
    job = None
    with open(os.devnull, "r+b", buffering=0) as null:
        handle = msvcrt.get_osfhandle(null.fileno())
        if not k.SetHandleInformation(handle, 1, 1):
            raise _error()
        si.StartupInfo.hStdInput = si.StartupInfo.hStdOutput = si.StartupInfo.hStdError = handle
        # Only NUL may be inherited: never retain the panel's pipes or unrelated handles.
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        storage = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(storage, 1, 0, ctypes.byref(size)):
            raise _error()
        try:
            handles = (W.HANDLE * 1)(handle)
            if not k.UpdateProcThreadAttribute(storage, 0, 0x00020002, handles, ctypes.sizeof(handles), None, None):
                raise _error()
            si.lpAttributeList = ctypes.cast(storage, W.LPVOID)
            # Suspended + no console window + extended handle list, with inherited environment.
            if not k.CreateProcessW(spec.exe_path, ctypes.create_unicode_buffer(command), None, None, True,
                                    0x00000004 | 0x08000000 | 0x00080000, None, spec.cwd,
                                    ctypes.byref(si), ctypes.byref(pi)):
                raise _error()
        finally:
            k.DeleteProcThreadAttributeList(storage)
        try:
            job = k.CreateJobObjectW(None, None)
            if not job or not k.AssignProcessToJobObject(job, pi.hProcess):
                raise _error()
            if k.ResumeThread(pi.hThread) == 0xFFFFFFFF:
                raise _error()
        except BaseException:
            # Only this not-yet-executed suspended process may be ended on setup failure.
            abort_suspended = k.TerminateProcess
            abort_suspended.argtypes, abort_suspended.restype = [W.HANDLE, W.UINT], W.BOOL
            abort_suspended(pi.hProcess, 1)
            k.CloseHandle(pi.hProcess)
            if job:
                k.CloseHandle(job)
            raise
        finally:
            k.CloseHandle(pi.hThread)
    return GameProcess(k, pi.hProcess, job, int(pi.dwProcessId))
