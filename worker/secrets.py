"""Persistent API credentials use the OS user's vault. Linux services can supply environment keys."""

import ctypes
import hashlib
import os
import sys
from pathlib import Path
from .storage import atomic_text

ENV_KEYS = {
    "gemini": ("GEMINI_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
    "omniroute": ("OMNIROUTE_API_KEY",),
}


def env_key(provider):
    return next((os.environ[k] for k in ENV_KEYS[provider] if os.environ.get(k)), "")


def _service(directory):
    return (
        "com.pmb.research."
        + hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:16]
    ).encode()


def _mac(directory, provider, replacement=None):
    from ctypes import c_void_p, c_uint32, byref

    sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    service, account = _service(directory), provider.encode()
    size, data, item = c_uint32(), c_void_p(), c_void_p()
    result = sec.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        byref(size),
        byref(data),
        byref(item),
    )
    try:
        if replacement is None:
            if result == -25300:
                return ""
            if result != 0:
                raise ValueError("macOS Keychain could not unlock this credential.")
            return ctypes.string_at(data, size.value).decode()
        value = replacement.encode()
        if result == -25300:
            result = sec.SecKeychainAddGenericPassword(
                None,
                len(service),
                service,
                len(account),
                account,
                len(value),
                value,
                None,
            )
        elif result == 0:
            result = sec.SecKeychainItemModifyAttributesAndData(
                item, None, len(value), value
            )
        if result:
            raise ValueError("macOS Keychain could not save the credential.")
    finally:
        if data:
            sec.SecKeychainItemFreeContent(None, data)
        if item:
            cf.CFRelease(item)


def _windows(directory, provider, replacement=None):
    import base64
    from ctypes import wintypes, byref

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    path = Path(directory) / (provider + ".dpapi")
    if replacement is None and not path.exists():
        return ""
    raw = (
        replacement.encode()
        if replacement is not None
        else base64.b64decode(path.read_text())
    )
    array = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), array)
    dest = Blob()
    api = ctypes.windll.crypt32
    func = api.CryptProtectData if replacement is not None else api.CryptUnprotectData
    if not func(byref(source), None, None, None, None, 1, byref(dest)):
        raise ValueError("Windows could not access this user's encrypted credential.")
    try:
        result = ctypes.string_at(dest.data, dest.size)
        if replacement is None:
            return result.decode()
        atomic_text(path, base64.b64encode(result).decode())
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def save_secret(directory, provider, value):
    if sys.platform == "darwin":
        return _mac(directory, provider, value)
    if sys.platform == "win32":
        return _windows(directory, provider, value)
    raise ValueError(
        "Set the provider API key in the worker service environment on this OS."
    )


def get_secret(directory, provider, *, allow_env=True):
    value = env_key(provider) if allow_env else ""
    if value:
        return value
    if sys.platform == "darwin":
        return _mac(directory, provider)
    if sys.platform == "win32":
        return _windows(directory, provider)
    return ""
