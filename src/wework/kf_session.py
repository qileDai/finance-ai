"""微信客服会话 roomid 解析（支持 kf:{open_kfid}:{wm} 与旧格式 kf:{wm}）"""

from __future__ import annotations

from config.settings import settings


def build_kf_roomid(open_kfid: str, external_userid: str) -> str:
    return f"kf:{open_kfid}:{external_userid}"


def parse_kf_roomid(roomid: str) -> tuple[str, str] | None:
    """解析 kf roomid → (open_kfid, external_userid)"""
    if not roomid.startswith("kf:"):
        return None
    rest = roomid[3:]
    if not rest:
        return None
    if ":" in rest:
        open_kfid, external_userid = rest.split(":", 1)
        if open_kfid and external_userid:
            return open_kfid, external_userid
        return None
    # 旧格式 kf:wmXXX
    default_kfid = settings.wework_kf_default_open_kfid
    if not default_kfid:
        return None
    return default_kfid, rest


def is_kf_session(roomid: str) -> bool:
    return roomid.startswith("kf:")


def resolve_kf_open_kfid(roomid: str, *, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    parsed = parse_kf_roomid(roomid)
    if parsed:
        return parsed[0]
    return settings.wework_kf_default_open_kfid
