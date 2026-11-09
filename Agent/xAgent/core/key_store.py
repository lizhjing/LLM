"""API 密钥安全存取:Windows 凭据管理器(CredRead/CredWrite,经 ctypes 调用)。

密钥保存在系统凭据库中,由 DPAPI 加密并与当前用户绑定,不落在任何文件里,
因此不会随 config.ini 进入代码仓库或 dist 打包产物。凭据 blob 以 UTF-16-LE
编码写入,与 Windows 凭据编辑器的写回格式保持兼容。

非 Windows 平台自动降级:save/delete 返回 False、read 返回 None,
密钥改由环境变量提供(core.config 的三级密钥解析仍然可用)。
"""

import sys

# 凭据目标名:密钥在 Windows 凭据管理器中的条目标识
TARGETS = {
    "llm": "xAgent/llm-api-key",
    "search": "xAgent/search-api-key",
}

_AVAILABLE = sys.platform == "win32"

if _AVAILABLE:
    import ctypes
    from ctypes import wintypes

    _CRED_TYPE_GENERIC = 1
    # 随本机当前用户配置文件持久化(不跨机器漫游)
    _CRED_PERSIST_LOCAL_MACHINE = 2

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CREDENTIAL(ctypes.Structure):
        """wincred.h 的 CREDENTIALW 结构(仅使用通用凭据相关字段)。"""

        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredFree.restype = None
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]


def credential_target(name: str) -> str:
    """返回密钥在凭据管理器中的条目名;未知名称抛出 KeyError。"""
    return TARGETS[name]


def read_key(name: str) -> str | None:
    """读取密钥;条目不存在、平台不支持或系统调用失败时返回 None。"""
    if not _AVAILABLE:
        return None
    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi32.CredReadW(TARGETS[name], _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return None  # 含 ERROR_NOT_FOUND(条目不存在)与其他系统级失败
    try:
        cred = cred_ptr.contents
        if not cred.CredentialBlobSize:
            return None
        address = ctypes.cast(cred.CredentialBlob, ctypes.c_void_p).value
        blob = ctypes.string_at(address, cred.CredentialBlobSize)
        return blob.decode("utf-16-le", errors="replace")
    finally:
        _advapi32.CredFree(cred_ptr)


def save_key(name: str, key: str) -> bool:
    """写入/覆盖密钥;平台不支持或系统调用失败时返回 False。"""
    if not _AVAILABLE or not key:
        return False
    blob = key.encode("utf-16-le")
    # CredWrite 只在调用期间读取 blob,buffer 需保持存活到调用结束
    buffer = ctypes.create_string_buffer(blob)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = TARGETS[name]
    cred.Comment = "xAgent API key"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    return bool(_advapi32.CredWriteW(ctypes.byref(cred), 0))


def delete_key(name: str) -> bool:
    """删除密钥;条目不存在或平台不支持时返回 False。"""
    if not _AVAILABLE:
        return False
    return bool(_advapi32.CredDeleteW(TARGETS[name], _CRED_TYPE_GENERIC, 0))
