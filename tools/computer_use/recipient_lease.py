"""One Windows desktop lease across profiles and gateway processes."""
from contextlib import contextmanager
import ctypes
import sys

from tools.computer_use.recipient_contract import RecipientError


@contextmanager
def desktop_lease():
    if sys.platform != "win32":
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, "Local\\HermesRecipientBridgeDesktop-v1")
    if not handle:
        raise RecipientError("desktop_lease_unavailable", 503)
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 25000)
        if result not in (0, 0x80):
            raise RecipientError("desktop_busy", 409)
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
