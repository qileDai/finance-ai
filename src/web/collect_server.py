"""H5 材料收集表单 + 管理后台 + 企微回调统一 HTTP 服务"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config.settings import PROJECT_ROOT, settings
from src.materials.checklist import format_progress_text
from src.materials.form_parser import fields_to_material_rows, parse_registration_form
from src.storage.db import ExternalGroupStore
from src.wework.callback_handler import (
    parse_external_callback_xml,
    parse_kf_callback_xml,
)
from src.wework.client import WXBizMsgCrypt
from src.wework.kf_worker import KfSyncWorker
from src.wework.message_router import MessageRouter

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/wework/external/callback"
# 公网企微回调别名（如 http://szyingtai.cn/webhook）
WEBHOOK_PATH = "/webhook"
FORM_PREFIX = "/collect/form/"
ADMIN_PATH = "/admin/groups"


def _is_callback_path(path: str) -> bool:
    """是否企微回调路径（含公网 /webhook 别名）"""
    p = (path or "").rstrip("/") or "/"
    if p == "/":
        return True
    if p == CALLBACK_PATH or p.startswith(CALLBACK_PATH + "/"):
        return True
    if p == WEBHOOK_PATH or p.startswith(WEBHOOK_PATH + "/"):
        return True
    return False


@dataclass
class UnifiedWebServer:
    router: MessageRouter
    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    kf_worker: KfSyncWorker | None = None
    host: str = "0.0.0.0"
    port: int = 8081
    _crypt: WXBizMsgCrypt | None = None

    def __post_init__(self) -> None:
        token = settings.wework_external_callback_token_resolved
        aes_key = settings.wework_external_callback_aes_key_resolved
        corp_id = settings.wework_corp_id
        if token and aes_key and corp_id:
            self._crypt = WXBizMsgCrypt(token, aes_key, corp_id)

    def start(self, *, blocking: bool = True) -> None:
        crypt = self._crypt
        router = self.router
        store = self.store
        kf_worker = self.kf_worker
        form_template = (PROJECT_ROOT / "templates" / "company_registration_form.md").read_text(
            encoding="utf-8"
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.debug("HTTP %s", fmt % args)

            def _send_html(self, html: str, code: int = 200) -> None:
                body = html.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, data: dict, code: int = 200) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def _form_disabled_response(self) -> None:
                html = (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<title>在线表单已关闭</title></head><body>"
                    "<h1>在线表单已关闭</h1>"
                    "<p>请在企业微信客户群内发送 <code>/填表</code> 获取填写模板，"
                    "按模板粘贴到群内提交；证件请直接上传图片或 PDF。</p>"
                    "</body></html>"
                )
                self._send_html(html, 404)

            def do_GET(self):
                path = urlparse(self.path).path
                if _is_callback_path(path):
                    return self._handle_callback_get()
                if path.startswith(FORM_PREFIX):
                    if not settings.collect_form_enabled:
                        return self._form_disabled_response()
                    token = path[len(FORM_PREFIX) :].strip("/")
                    return self._handle_form_get(token)
                if path == ADMIN_PATH or path == "/admin":
                    return self._handle_admin()
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                path = urlparse(self.path).path
                if _is_callback_path(path):
                    return self._handle_callback_post()
                if path.startswith(FORM_PREFIX):
                    if not settings.collect_form_enabled:
                        return self._form_disabled_response()
                    token = path[len(FORM_PREFIX) :].strip("/")
                    return self._handle_form_post(token)
                self.send_response(404)
                self.end_headers()

            def _handle_callback_get(self):
                if crypt is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                query = parse_qs(urlparse(self.path).query)
                try:
                    plain = crypt.verify_url(
                        query.get("msg_signature", [""])[0],
                        query.get("timestamp", [""])[0],
                        query.get("nonce", [""])[0],
                        query.get("echostr", [""])[0],
                    )
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(plain.encode())
                    logger.info("[外部群] URL 验证成功")
                except Exception as e:
                    logger.error("[外部群] URL 验证失败: %s", e)
                    self.send_response(403)
                    self.end_headers()

            def _handle_callback_post(self):
                if crypt is None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")
                    return
                import xml.etree.ElementTree as ET

                body_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(body_len)
                try:
                    root = ET.fromstring(body)
                    encrypt = root.find("Encrypt")
                    if encrypt is None or encrypt.text is None:
                        raise ValueError("缺少 Encrypt")
                    xml_text = crypt.decrypt(encrypt.text)
                    kf_evt = parse_kf_callback_xml(xml_text)
                    if kf_evt and kf_worker is not None:
                        logger.info(
                            "[kf] 收到回调 open_kfid=%s，异步 sync",
                            kf_evt.open_kfid,
                        )
                        kf_worker.on_kf_callback(kf_evt.open_kfid, kf_evt.token)
                    else:
                        evt = parse_external_callback_xml(xml_text)
                        if evt:
                            router.route_external_chat_event(evt)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")
                except Exception as e:
                    logger.error("[外部群] 回调处理失败: %s", e)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"success")

            def _handle_form_get(self, token: str):
                group = store.get_group_by_token(token)
                if not group:
                    return self._send_html("<h1>链接无效或已过期</h1>", 404)
                roomid = group["roomid"]
                progress = format_progress_text(store.get_materials(roomid))
                html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>香港公司注册资料</title>
<style>
body{{font-family:sans-serif;max-width:720px;margin:2em auto;padding:0 1em}}
textarea{{width:100%;height:420px;font-family:monospace}}
pre{{background:#f5f5f5;padding:1em;white-space:pre-wrap}}
button{{padding:0.6em 1.2em;font-size:1em}}
</style></head><body>
<h1>香港公司注册资料填写</h1>
<p>群 ID: {roomid}</p>
<pre>{progress}</pre>
<h2>填写模板</h2>
<pre>{form_template}</pre>
<form method="post">
<label>粘贴已填写内容（键=值）：</label>
<textarea name="content" placeholder="公司英文名=ABC Limited&#10;注册地址=..."></textarea>
<br><br><button type="submit">提交</button>
</form>
</body></html>"""
                self._send_html(html)

            def _handle_form_post(self, token: str):
                group = store.get_group_by_token(token)
                if not group:
                    return self._send_html("<h1>链接无效</h1>", 404)
                roomid = group["roomid"]
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                params = parse_qs(body)
                content = params.get("content", [""])[0]
                result = parse_registration_form(content)
                if not result.ok:
                    errs = result.missing + result.errors
                    msg = "<br>".join(errs)
                    return self._send_html(f"<h1>提交失败</h1><p>{msg}</p><a href=''>返回</a>", 400)
                for row in fields_to_material_rows(result.fields):
                    fk = row.pop("field_key")
                    store.upsert_material(roomid, fk, **row)
                store.set_group_status(roomid, "COLLECTING")
                progress = format_progress_text(store.get_materials(roomid))
                sm = router.state_machine
                owner = group.get("owner_userid")
                try:
                    sm.external.send_session_text(
                        roomid,
                        f"【材料更新】表单已提交。\n{progress}",
                        sender_userid=owner or None,
                    )
                except Exception as e:
                    logger.warning("群通知失败: %s", e)
                html = f"<h1>提交成功</h1><pre>{progress}</pre><p>请返回微信群查看进度，可发送 /进度 查询。</p>"
                self._send_html(html)

            def _handle_admin(self):
                query = parse_qs(urlparse(self.path).query)
                channel = query.get("channel", ["all"])[0]
                rows = store.list_all_materials_summary(channel=channel)
                filter_links = (
                    f"<p>筛选: "
                    f"<a href='?channel=all'>全部</a> | "
                    f"<a href='?channel=group'>群(wr*)</a> | "
                    f"<a href='?channel=kf'>客服(kf:*)</a>"
                    f" （当前: {channel}）</p>"
                )
                trs = "".join(
                    f"<tr><td>{r.get('channel', '')}</td>"
                    f"<td>{r.get('open_kfid') or ''}</td>"
                    f"<td>{r.get('roomid')}</td>"
                    f"<td>{self._kf_account_label(r)}</td>"
                    f"<td>{r.get('status')}</td><td>{r.get('material_count')}</td>"
                    f"<td>{r.get('company_name') or ''}</td></tr>"
                    for r in rows
                )
                html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>外部群/客服管理</title>
<style>table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:8px}}</style>
</head><body><h1>外部客户群 / 微信客服 材料管理</h1>
{filter_links}
<table><tr><th>通道</th><th>open_kfid</th><th>roomid</th><th>账号/名称</th><th>状态</th><th>材料项</th><th>公司</th></tr>
{trs or '<tr><td colspan=7>暂无数据</td></tr>'}
</table></body></html>"""
                self._send_html(html)

            def _kf_account_label(self, row: dict) -> str:
                name = row.get("name") or ""
                kfid = row.get("open_kfid") or ""
                if kfid:
                    acc = settings.get_kf_account(str(kfid))
                    if acc and acc.label:
                        return f"{name} ({acc.label})"
                return str(name)

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        collect_mode = "H5 在线表单" if settings.collect_form_enabled else "群内粘贴（H5 已关闭）"
        logger.info(
            "[Web] 已启动 http://%s:%s (回调 %s 与 %s; 材料收集: %s)",
            self.host,
            self.port,
            WEBHOOK_PATH,
            CALLBACK_PATH,
            collect_mode,
        )
        logger.info(
            "[Web] 公网回调示例: http://szyingtai.cn%s （须反代到本机 %s%s）",
            WEBHOOK_PATH,
            self.port,
            WEBHOOK_PATH,
        )
        if blocking:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                server.shutdown()
        else:
            threading.Thread(target=server.serve_forever, daemon=True).start()
