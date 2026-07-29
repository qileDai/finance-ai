"""企业微信会话内容存档客户端"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT, settings
from src.storage.db import ExternalGroupStore
from src.wework.message_router import ArchiveFileMessage, ArchiveTextMessage, MessageRouter

logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    _RSA_AVAILABLE = True
except ImportError:
    RSA = None  # type: ignore
    PKCS1_v1_5 = None  # type: ignore
    _RSA_AVAILABLE = False


@dataclass
class ArchiveClient:
    """会话内容存档拉取（live 需官方 Finance SDK + RSA 私钥）"""

    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    _sdk: Any = None
    _sdk_handle: Any = None
    _rsa_cipher: Any = None
    _running: bool = False
    _thread: threading.Thread | None = None

    @property
    def mode(self) -> str:
        return settings.wework_external_mode_resolved

    @property
    def sdk_available(self) -> bool:
        path = self._resolve_sdk_path()
        return path is not None and path.exists()

    def _resolve_sdk_path(self) -> Path | None:
        configured = (settings.wework_archive_sdk_path or "").strip()
        if configured:
            p = Path(configured)
            if p.exists():
                return p
        root = PROJECT_ROOT
        candidates = [
            root / "vendor" / "wework-sdk" / "WeWorkFinanceSdk.dll",
            root / "vendor" / "wework-sdk" / "libWeWorkFinanceSdk.so",
            root / "vendor" / "wework-sdk" / "WeWorkFinanceSdk_C.dll",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _load_rsa_cipher(self) -> bool:
        if self._rsa_cipher is not None:
            return True
        if not _RSA_AVAILABLE:
            logger.error("解密存档需要 pycryptodome: pip install pycryptodome")
            return False
        key_path = Path(settings.wework_archive_private_key_path)
        if not key_path.exists():
            logger.error("RSA 私钥文件不存在: %s", key_path)
            return False
        try:
            pem = key_path.read_text(encoding="utf-8")
            rsa_key = RSA.import_key(pem)
            self._rsa_cipher = PKCS1_v1_5.new(rsa_key)
            logger.info("存档 RSA 私钥已加载")
            return True
        except Exception as e:
            logger.exception("加载 RSA 私钥失败: %s", e)
            return False

    def _decrypt_random_key(self, encrypt_random_key_b64: str) -> str | None:
        if not self._load_rsa_cipher():
            return None
        try:
            encrypted = base64.b64decode(encrypt_random_key_b64)
            decrypted = self._rsa_cipher.decrypt(encrypted, None)
            if not decrypted:
                logger.warning("RSA 解密 random_key 失败")
                return None
            return decrypted.decode("utf-8")
        except Exception as e:
            logger.warning("RSA 解密 random_key 异常: %s", e)
            return None

    def _bind_sdk(self) -> bool:
        if self._sdk is not None and self._sdk_handle is not None:
            return True
        sdk_path = self._resolve_sdk_path()
        if not sdk_path:
            logger.warning("未找到 Finance SDK，存档 live 模式不可用")
            return False
        try:
            import ctypes

            class Slice_t(ctypes.Structure):
                _fields_ = [("buf", ctypes.c_char_p), ("len", ctypes.c_int)]

            self.Slice_t = Slice_t

            if sys.platform == "win32":
                os.add_dll_directory(str(sdk_path.parent))
            self._sdk = ctypes.CDLL(str(sdk_path))

            self._sdk.NewSdk.restype = ctypes.c_void_p
            self._sdk.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            self._sdk.Init.restype = ctypes.c_int

            self._sdk.NewSlice.restype = ctypes.POINTER(Slice_t)
            self._sdk.FreeSlice.argtypes = [ctypes.POINTER(Slice_t)]

            self._sdk.GetChatData.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulonglong,
                ctypes.c_uint,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.POINTER(Slice_t),
            ]
            self._sdk.GetChatData.restype = ctypes.c_int

            # 官方 SDK: DecryptData(encrypt_key, encrypt_msg, slice)
            self._sdk.DecryptData.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.POINTER(Slice_t),
            ]
            self._sdk.DecryptData.restype = ctypes.c_int
            self._sdk.DestroySdk.argtypes = [ctypes.c_void_p]
            self._sdk.DestroySdk.restype = None

            handle = self._sdk.NewSdk()
            corp_id = settings.wework_corp_id.encode("utf-8")
            secret = settings.wework_archive_secret.encode("utf-8")
            ret = self._sdk.Init(handle, corp_id, secret)
            if ret != 0:
                logger.error("Finance SDK Init 失败: %d", ret)
                return False
            self._sdk_handle = handle
            logger.info("Finance SDK 已加载: %s", sdk_path)
            return True
        except Exception as e:
            logger.exception("加载 Finance SDK 失败: %s", e)
            return False

    def _decrypt_chat_msg(self, encrypt_key: str, encrypt_chat_msg: str) -> dict[str, Any] | None:
        if not self._bind_sdk():
            return None
        import ctypes

        slice_out = self._sdk.NewSlice()
        try:
            ret = self._sdk.DecryptData(
                encrypt_key.encode("utf-8"),
                encrypt_chat_msg.encode("utf-8"),
                slice_out,
            )
            if ret != 0:
                logger.debug("DecryptData 返回 %d", ret)
                return None
            raw = ctypes.string_at(slice_out.contents.buf, slice_out.contents.len)
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.debug("解密聊天消息失败: %s", e)
            return None
        finally:
            self._sdk.FreeSlice(slice_out)

    def fetch_batch(self) -> tuple[list[ArchiveTextMessage], list[ArchiveFileMessage]]:
        """拉取一批存档消息（文本 + 文件元数据）"""
        if not settings.wework_archive_configured:
            return [], []
        if not self._bind_sdk():
            return [], []

        import ctypes

        seq = self.store.get_archive_seq()
        slice_out = self._sdk.NewSlice()
        try:
            ret = self._sdk.GetChatData(
                self._sdk_handle,
                seq,
                100,
                b"",
                b"",
                10,
                slice_out,
            )
            if ret != 0:
                logger.warning("GetChatData 返回 %d", ret)
                return [], []

            raw = ctypes.string_at(slice_out.contents.buf, slice_out.contents.len)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return [], []
        except Exception as e:
            logger.exception("GetChatData 异常: %s", e)
            return [], []
        finally:
            self._sdk.FreeSlice(slice_out)

        if payload.get("errcode", 0) != 0:
            logger.warning("GetChatData err: %s", payload)
            return [], []

        chat_data = payload.get("chatdata") or []
        texts: list[ArchiveTextMessage] = []
        files: list[ArchiveFileMessage] = []
        max_seq = seq

        for item in chat_data:
            item_seq = int(item.get("seq", 0))
            max_seq = max(max_seq, item_seq)

            random_key = self._decrypt_random_key(item.get("encrypt_random_key", ""))
            if not random_key:
                continue
            decrypted = self._decrypt_chat_msg(random_key, item.get("encrypt_chat_msg", ""))
            if not decrypted:
                continue
            msgid = str(item.get("msgid", ""))
            text_msg = self._parse_text(decrypted, msgid)
            if text_msg:
                texts.append(text_msg)
                continue
            file_msg = self._parse_file(decrypted, msgid)
            if file_msg:
                files.append(file_msg)

        if max_seq > seq:
            self.store.set_archive_seq(max_seq)

        if texts or files:
            logger.info("存档本轮: %d 文本, %d 文件", len(texts), len(files))
        return texts, files

    def download_media(self, sdkfileid: str) -> bytes | None:
        """通过 Finance SDK 下载媒体文件"""
        if not sdkfileid or not self._bind_sdk():
            return None
        if not hasattr(self._sdk, "GetMediaData"):
            logger.debug("SDK 无 GetMediaData")
            return None
        import ctypes

        class MediaData_t(ctypes.Structure):
            _fields_ = [
                ("outindexbuf", ctypes.c_char_p),
                ("out_len", ctypes.c_int),
                ("data", ctypes.c_char_p),
                ("data_len", ctypes.c_int),
                ("is_finish", ctypes.c_int),
            ]

        self._sdk.NewMediaData.restype = ctypes.POINTER(MediaData_t)
        self._sdk.FreeMediaData.argtypes = [ctypes.POINTER(MediaData_t)]
        self._sdk.GetMediaData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(MediaData_t),
        ]
        self._sdk.GetMediaData.restype = ctypes.c_int

        chunks: list[bytes] = []
        index = ""
        while True:
            media = self._sdk.NewMediaData()
            try:
                ret = self._sdk.GetMediaData(
                    self._sdk_handle,
                    sdkfileid.encode("utf-8"),
                    index.encode("utf-8") if index else b"",
                    b"",
                    30,
                    media,
                )
                if ret != 0:
                    break
                if media.contents.data and media.contents.data_len > 0:
                    chunks.append(ctypes.string_at(media.contents.data, media.contents.data_len))
                if media.contents.outindexbuf and media.contents.out_len > 0:
                    index = ctypes.string_at(media.contents.outindexbuf, media.contents.out_len).decode(
                        "utf-8", errors="replace"
                    )
                if media.contents.is_finish:
                    break
            finally:
                self._sdk.FreeMediaData(media)
        return b"".join(chunks) if chunks else None

    @staticmethod
    def _is_external_sender(from_id: str) -> bool:
        fid = (from_id or "").strip()
        return fid.startswith("wm") or fid.startswith("wo")

    def _parse_text(self, data: dict[str, Any], fallback_msgid: str) -> ArchiveTextMessage | None:
        if data.get("action") not in (None, "send"):
            return None

        msgtype = data.get("msgtype") or data.get("msgType")
        if msgtype != "text":
            return None

        text_obj = data.get("text") or {}
        content = text_obj.get("content") if isinstance(text_obj, dict) else str(text_obj)
        if not content or not str(content).strip():
            return None

        content = str(content).strip()
        if content.startswith("【AI 助手】"):
            return None

        roomid = data.get("roomid") or data.get("room_id") or ""
        if not roomid:
            return None

        from_id = data.get("from") or data.get("from_user") or ""
        if not self._is_external_sender(from_id):
            logger.debug("跳过非外部联系人消息 from=%s roomid=%s", from_id, roomid)
            return None

        msgid = data.get("msgid") or fallback_msgid or f"arch_{roomid}_{int(time.time())}"
        return ArchiveTextMessage(
            msgid=str(msgid),
            roomid=str(roomid),
            from_id=str(from_id),
            content=content,
        )

    def _parse_file(self, data: dict[str, Any], fallback_msgid: str) -> ArchiveFileMessage | None:
        if data.get("action") not in (None, "send"):
            return None
        msgtype = data.get("msgtype") or ""
        if msgtype not in ("image", "file"):
            return None
        roomid = data.get("roomid") or ""
        if not roomid:
            return None
        from_id = data.get("from") or ""
        if not self._is_external_sender(from_id):
            return None
        sdkfileid = ""
        filename = "upload.bin"
        if msgtype == "image":
            img = data.get("image") or {}
            sdkfileid = img.get("sdkfileid") or img.get("md5sum") or ""
            filename = f"{sdkfileid[:12] or 'image'}.jpg"
        else:
            fobj = data.get("file") or {}
            sdkfileid = fobj.get("sdkfileid") or ""
            filename = fobj.get("filename") or "document.pdf"
        if not sdkfileid:
            return None
        msgid = data.get("msgid") or fallback_msgid
        return ArchiveFileMessage(
            msgid=str(msgid),
            roomid=str(roomid),
            from_id=str(from_id),
            msgtype=str(msgtype),
            sdkfileid=str(sdkfileid),
            filename=filename,
        )

    def start_polling(
        self,
        router: MessageRouter,
        *,
        interval: int | None = None,
        blocking: bool = False,
    ) -> None:
        """启动存档轮询 worker"""
        if self.mode != "live":
            logger.info("存档 worker 未启动（当前模式: %s）", self.mode)
            return
        if not settings.wework_archive_configured:
            logger.warning("存档未配置 WEWORK_ARCHIVE_SECRET / 私钥，worker 未启动")
            return
        if not self.sdk_available:
            logger.warning("存档 SDK 未找到，worker 未启动")
            return
        if not self._load_rsa_cipher():
            logger.warning("RSA 私钥未就绪，worker 未启动")
            return

        poll = interval or settings.wework_archive_poll_interval

        def _loop() -> None:
            self._running = True
            logger.info("存档 worker 已启动，轮询间隔 %ds", poll)
            while self._running:
                try:
                    texts, files = self.fetch_batch()
                    for msg in texts:
                        router.route_archive_text(msg)
                    for fmsg in files:
                        data = self.download_media(fmsg.sdkfileid)
                        if data:
                            router.route_archive_file(fmsg, data)
                        else:
                            logger.warning("媒体下载失败 msgid=%s sdkfileid=%s", fmsg.msgid, fmsg.sdkfileid)
                except Exception as e:
                    logger.exception("存档轮询异常: %s", e)
                time.sleep(poll)

        if blocking:
            _loop()
        else:
            self._thread = threading.Thread(target=_loop, daemon=True, name="archive-worker")
            self._thread.start()

    def stop_polling(self) -> None:
        self._running = False
        if self._sdk and self._sdk_handle:
            try:
                self._sdk.DestroySdk(self._sdk_handle)
            except Exception:
                pass
            self._sdk_handle = None

    def close(self) -> None:
        self.stop_polling()
