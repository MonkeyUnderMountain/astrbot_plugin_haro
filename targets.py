"""人工绑定的本人目标。平台会话、凭据由AstrBot持有；发送不自动重试。"""
import ast
import base64
import hashlib
import inspect
import json
import textwrap
from functools import lru_cache
from pathlib import Path

from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp


def _ast_shape(value):
    # Python 3.13改变了ast.dump空字段输出；忽略空字段，保持3.12与新版解释器的契约一致。
    if isinstance(value, ast.AST):
        return [type(value).__name__, [[key, _ast_shape(item)] for key, item in ast.iter_fields(value) if item is not None and item != []]]
    return [_ast_shape(item) for item in value] if isinstance(value, list) else value


@lru_cache(maxsize=8)
def _qq_compatible(method):
    # qq-botpy 1.2.1的request在retry_time>2时不再请求。只认可已核过的函数，升级后先重新验证。
    try:
        if hasattr(method, "__wrapped__"):
            return False
        source = ast.parse(textwrap.dedent(inspect.getsource(method.__code__)))
        function = next(node for node in ast.walk(source) if isinstance(node, ast.AsyncFunctionDef) and node.name == "request")
        digest = hashlib.sha256(json.dumps(_ast_shape(function), separators=(",", ":")).encode()).hexdigest()
        return digest == "516550b7cd6ea0d2a67903c17ef942f4c9af75791935d751419334469778e81b"
    except (OSError, TypeError, AttributeError, StopIteration, SyntaxError):
        return False


def qq_http(platform):
    try:
        http = platform.get_client().api._http
        return http if _qq_compatible(http.request.__func__) else None
    except AttributeError:
        return None


class _TargetCancelled(Exception):
    pass


class _QQOnce:
    """每次鉴权等待结束后重查取消；共享AstrBot客户端，不改它的全局方法或凭据。"""
    def __init__(self, http, guard):
        self.http, self.guard = http, guard
    def __getattr__(self, name):
        return getattr(self.http, name)
    async def check_session(self):
        await self.http.check_session()
        if not self.guard():
            raise _TargetCancelled()
    async def request(self, route, retry_time=2, **kwargs):
        return await self.http.request.__func__(self, route, retry_time=retry_time, **kwargs)


def inspect_target(context, owners, target):
    owner, instance = target.get("ownerId", ""), target.get("platformId", "")
    platform_name, separator, uid = owner.partition(":")
    if (owner not in owners or not separator or platform_name not in ("telegram", "qq_official", "weixin_oc")
            or not uid or len(uid) > 512 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in uid)
            or not instance or ":" in instance or (platform_name == "telegram" and (not uid.isdigit() or int(uid) <= 0))
            or target.get("platform", platform_name) != platform_name
            or target.get("origin") != f"{instance}:FriendMessage:{uid}"):
        return None, "TARGET_UNAVAILABLE"
    platform = context.get_platform_inst(instance)
    if platform is None or platform.meta().name != platform_name:
        return None, "TARGET_UNAVAILABLE"
    if platform_name == "qq_official" and qq_http(platform) is None:
        return None, "QQ_SDK_UNSUPPORTED"
    # 仅检查是否已有上下文，不复制/记录token。实际有效性仍由微信在发送时判断。
    if platform_name == "weixin_oc" and not getattr(platform, "_context_tokens", {}).get(uid):
        return None, "WEIXIN_CONTEXT_REQUIRED"
    return platform, None


async def _qq_send(platform, uid, item, guard):
    from botpy.http import Route
    from botpy.errors import AuthenticationFailedError, ForbiddenError, NotFoundError, MethodNotAllowedError, SequenceNumberError
    http = qq_http(platform)
    if http is None:
        return {"kind": "failed", "code": "QQ_SDK_UNSUPPORTED"}
    http = _QQOnce(http, guard)
    try:
        body = {"msg_type": 0, "content": item["text"]} if item["type"] == "text" else None
        if body is None:
            file = Path(item["path"])
            if file.stat().st_size > 10 * 1024 * 1024:
                return {"kind": "failed", "code": "QQ_FILE_TOO_LARGE"}
            data = base64.b64encode(file.read_bytes()).decode("ascii")
            if not guard():
                return {"kind": "failed", "code": "CANCELLED"}
            uploaded = await http.request(Route("POST", "/v2/users/{openid}/files", openid=uid), retry_time=2,
                allow_redirects=False, json={"file_type": 1 if item["type"] == "image" else 4,
                    "srv_send_msg": False, "file_data": data, "file_name": item.get("name", file.name)})
            if not isinstance(uploaded, dict) or not isinstance(uploaded.get("file_info"), str) or not uploaded["file_info"]:
                return {"kind": "unknown", "code": "DELIVERY_UNCONFIRMED"}
            body = {"msg_type": 7, "content": " ", "media": {"file_info": uploaded["file_info"]}, "msg_seq": 1}
        if not guard():
            return {"kind": "failed", "code": "CANCELLED"}
        response = await http.request(Route("POST", "/v2/users/{openid}/messages", openid=uid),
            retry_time=2, allow_redirects=False, json=body)
        message_id = response.get("id") if isinstance(response, dict) else None
        if (isinstance(message_id, str) and message_id.strip() and len(message_id.encode("utf-16-le")) // 2 <= 256
                and not any(ord(char) < 32 or ord(char) == 127 for char in message_id)):
            return {"kind": "ok", "messageId": message_id}
        return {"kind": "unknown", "code": "DELIVERY_UNCONFIRMED"}
    except _TargetCancelled:
        return {"kind": "failed", "code": "CANCELLED"}
    except (AuthenticationFailedError, ForbiddenError, NotFoundError, MethodNotAllowedError, SequenceNumberError):
        return {"kind": "failed", "code": "PLATFORM_REJECTED"}
    except Exception:
        return {"kind": "unknown", "code": "DELIVERY_UNCONFIRMED"}





async def send_image(plugin, *, origin, platform, uid, path, guard):
    """来源图片、主动目标和图库表情共用出口；QQ保留单次提交及真实ID。"""
    item = {"type": "image", "path": str(path), "name": Path(path).name}
    if not guard():
        return {"kind": "failed", "code": "CANCELLED"}
    try:
        if platform.meta().name == "qq_official":
            return await plugin._send(operation=lambda active: _qq_send(platform, uid, item, active), guard=guard)
        sent = await plugin._send(origin=origin, chain=MessageChain([Comp.Image.fromFileSystem(str(path))]), guard=guard)
        return {"kind": "ok"} if sent is True else {"kind": "failed", "code": "PLATFORM_REJECTED"}
    except Exception as error:
        rejected = False
        if platform.meta().name == "telegram":
            from telegram.error import BadRequest, Forbidden, InvalidToken
            rejected = isinstance(error, (BadRequest, Forbidden, InvalidToken))
        return {"kind": "failed" if rejected else "unknown", "code": "PLATFORM_REJECTED" if rejected else "DELIVERY_UNCONFIRMED"}


async def deliver_target(plugin, packet):
    target = plugin.bridge.targets.get(packet["targetId"])
    content = packet.get("content", [])
    if not target or packet.get("origin") != target["origin"] or len(content) != 1 or content[0].get("type") not in ("text", "file", "image", "approval"):
        return {"kind": "failed", "code": "TARGET_UNAVAILABLE"}
    platform, reason = inspect_target(plugin.context, plugin.bridge.owners, target)
    if platform is None:
        return {"kind": "failed", "code": reason}
    item = content[0]
    if item["type"] == "approval" and (platform.meta().name != "telegram" or target["ownerId"] != plugin.bridge.approver_owner_id
            or item.get("format") != "summary-v1" or not isinstance(item.get("summary"), str)):
        return {"kind": "failed", "code": "APPROVAL_SOURCE_DENIED"}
    if item["type"] == "text" and (not isinstance(item.get("text"), str) or not 0 < len(item["text"]) <= 1200):
        return {"kind": "failed", "code": "TARGET_UNAVAILABLE"}
    generation = plugin.bridge.generation
    def active():
        current, _ = inspect_target(plugin.context, plugin.bridge.owners, target)
        return (plugin.bridge.connected and plugin.bridge.generation == generation and current is platform
                and packet.get("_active", lambda: True)())
    if item["type"] == "image":
        return await send_image(plugin, origin=target["origin"], platform=platform, uid=target["ownerId"].partition(":")[2], path=item["path"], guard=active)
    if platform.meta().name == "qq_official":
        # 不走AstrBot会重试且吞回执的send_by_session；仍从插件唯一实际提交出口调用。
        return await plugin._send(operation=lambda guard: _qq_send(platform, target["ownerId"].partition(":")[2], item, guard), guard=active)
    if item["type"] == "approval":
        # 固定本人目标来自人工配置；按钮仍须原来源与唯一Telegram主人核验。
        plugin._origins.add(target["origin"])
        parts = plugin._approval_parts(item)
    elif item["type"] == "text":
        parts = [Comp.Plain(item["text"])]
    elif item["type"] == "file":
        parts = [Comp.File(file=item["path"], name=item["name"])]
    try:
        sent = await plugin._send(origin=target["origin"], chain=MessageChain(parts), guard=active)
        return {"kind": "ok"} if sent is True else {"kind": "failed", "code": "PLATFORM_REJECTED"}
    except Exception as error:
        rejected = False
        if platform.meta().name == "telegram":
            from telegram.error import BadRequest, Forbidden, InvalidToken
            rejected = isinstance(error, (BadRequest, Forbidden, InvalidToken))
        return {"kind": "failed" if rejected else "unknown", "code": "PLATFORM_REJECTED" if rejected else "DELIVERY_UNCONFIRMED"}
