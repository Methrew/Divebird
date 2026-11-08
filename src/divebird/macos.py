"""macOS 專用的系統功能：以 ctypes 呼叫系統框架，不需要額外的套件。"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_OBJC = "/usr/lib/libobjc.A.dylib"
_FOUNDATION = "/System/Library/Frameworks/Foundation.framework/Foundation"
_objc = None


def _runtime():
    global _objc
    if _objc is None:
        ctypes.cdll.LoadLibrary(_FOUNDATION)        # 載入 NSString、NSURL 等類別
        objc = ctypes.cdll.LoadLibrary(_OBJC)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        _objc = objc
    return _objc


def _send(obj, selector: str, restype=ctypes.c_void_p, argtypes=(), *args):
    """呼叫 Objective-C 方法（objc_msgSend 要依每個方法的參數型別轉型後才能呼叫）。"""
    objc = _runtime()
    fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
    return fn(obj, objc.sel_registerName(selector.encode()), *args)


def _cls(name: str):
    return _runtime().objc_getClass(name.encode())


def _nsstring(text: str):
    return _send(_cls("NSString"), "stringWithUTF8String:", ctypes.c_void_p, (ctypes.c_char_p,), text.encode("utf-8"))


def quarantine_origin(url: str) -> str:
    """記錄在隔離屬性中的下載來源：只留通訊協定、主機與路徑，不含帳號密碼、查詢參數（可能含權杖）與片段。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme not in ("http", "https") or not host:
            return ""
        if ":" in host:                     # IPv6 位址要加回中括號
            host = f"[{host}]"
        if parts.port:
            host += f":{parts.port}"
    except ValueError:                      # 埠號不合法等
        return ""
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def mark_quarantine(path: Path | str, url: str = "") -> bool:
    """替下載完成的檔案加上「隔離」屬性（com.apple.quarantine），跟瀏覽器下載的檔案一樣：
    之後開啟 App、磁碟映像檔或安裝檔時，Gatekeeper 會先檢查並顯示「從網際網路下載」的警告。
    擴充功能會攔截瀏覽器的下載改由 Divebird 下載，少了這一步就等於略過了 Gatekeeper。

    使用系統的 NSURLQuarantinePropertiesKey（由系統決定屬性內容並記錄來源）。
    成功時回傳 True；不是 macOS 或加不上時回傳 False（不影響下載本身）。"""
    if sys.platform != "darwin":
        return False
    try:
        objc = _runtime()
        pool = objc.objc_autoreleasePoolPush()      # 在下載執行緒上呼叫：自己建立 autorelease pool
        try:
            props = _send(_cls("NSMutableDictionary"), "dictionary")
            entries = [("LSQuarantineAgentName", _nsstring("Divebird")),
                       ("LSQuarantineType", _nsstring("LSQuarantineTypeWebDownload"))]
            origin = quarantine_origin(url)
            if origin:
                entries.append(("LSQuarantineDataURL", _send(_cls("NSURL"), "URLWithString:", ctypes.c_void_p,
                                                             (ctypes.c_void_p,), _nsstring(origin))))
            for key, value in entries:
                if value:
                    _send(props, "setObject:forKey:", None, (ctypes.c_void_p, ctypes.c_void_p), value, _nsstring(key))
            file_url = _send(_cls("NSURL"), "fileURLWithPath:", ctypes.c_void_p, (ctypes.c_void_p,),
                             _nsstring(os.fspath(path)))
            return bool(_send(file_url, "setResourceValue:forKey:error:", ctypes.c_bool,
                              (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
                              props, _nsstring("NSURLQuarantinePropertiesKey"), None))
        finally:
            objc.objc_autoreleasePoolPop(pool)
    except Exception:  # noqa: BLE001
        return False
