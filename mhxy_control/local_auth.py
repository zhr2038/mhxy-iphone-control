"""Persist the loopback service credential using Windows DPAPI."""

import ctypes
from ctypes import wintypes
from pathlib import Path
import secrets
import tempfile


from .settings import SERVICE_DIR


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]


def dpapi(data, *, read=False):
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    protect = crypt32.CryptUnprotectData if read else crypt32.CryptProtectData
    protect.argtypes = (
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    )
    protect.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32.dll")
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    destination = DataBlob()
    if not protect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.length)
    finally:
        kernel32.LocalFree(destination.data)


def service_token():
    path = SERVICE_DIR / "local-token.dpapi"
    if not path.exists():
        SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        encrypted = dpapi(secrets.token_bytes(32))
        with tempfile.NamedTemporaryFile(dir=SERVICE_DIR, delete=False) as file:
            temporary = Path(file.name)
            file.write(encrypted)
        try:
            temporary.rename(path)  # Windows fails if another process has already published the token.
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    return dpapi(path.read_bytes(), read=True).hex()
