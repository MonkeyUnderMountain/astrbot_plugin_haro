"""Meme Manager只分类、选图；图片交统一出口，来源与Telegram文本呈现无关。"""
import asyncio
import base64
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from astrbot.api import logger
from .targets import inspect_target, send_image


class MemeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class MemeSource:
    """仅持有目的地元数据；不带聊天正文、原事件或平台凭据。"""
    def __init__(self, platform, origin, session_id, persona_id=""):
        self.platform = platform
        self.unified_msg_origin = origin
        self.session_id, self.persona_id = str(session_id), str(persona_id or "")
        self.extras = {"meme_manager_llm_request_origin": "plugin"}
    def get_extra(self, key, default=None):
        return self.extras.get(key, default)
    def set_extra(self, key, value):
        self.extras[key] = value
    def get_platform_name(self):
        return self.platform


def _temp_root():
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    return Path(get_astrbot_temp_path()).resolve()


def _path(value):
    value = os.fspath(value)
    if value.startswith("file://"):
        uri = urlsplit(value)
        if uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
            raise MemeError("MEME_IMAGE_INVALID")
        value = url2pathname(uri.path)
    elif "://" in value:
        raise MemeError("MEME_IMAGE_INVALID")
    path = Path(value)
    if not path.is_absolute():
        raise MemeError("MEME_IMAGE_INVALID")
    return path.absolute()


def _safe_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink()
            or os.path.normcase(str(path.resolve())) != os.path.normcase(str(path))):
        raise MemeError("MEME_IMAGE_INVALID")
    return info


def _temporary(path, root):
    return path.parent == root and re.fullmatch(r"meme_[^/\\]+\.gif", path.name) is not None


def _extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise MemeError("MEME_IMAGE_INVALID")


def _freeze(image, prepared, sources, category, root):
    # 授权来自插件按目的地列出的精确图库名单，模型没有任意路径入口。
    event = prepared.get("delivery_event")
    registry = event.get_extra("meme_manager_delivery_registry", {}) if event else {}
    record = registry.get(id(image)) or registry.get(str(getattr(image, "file", "")))
    if not isinstance(record, dict) or record.get("category") != category:
        raise MemeError("MEME_IMAGE_INVALID")
    source = _path(record["source"])
    if source not in sources:
        raise MemeError("MEME_IMAGE_INVALID")
    _safe_file(source)
    ref = getattr(image, "file", "")
    if isinstance(ref, str) and ref.startswith("base64://"):
        if len(ref) > 28 * 1024 * 1024:
            raise MemeError("FILE_TOO_LARGE")
        data = base64.b64decode(ref[9:], validate=True)
    else:
        selected = _path(getattr(image, "path", None) or ref)
        converted = {_path(value) for value in prepared.get("temp_files", [])}
        if selected != source and not (selected in converted and _temporary(selected, root)):
            raise MemeError("MEME_IMAGE_INVALID")
        before = _safe_file(selected)
        fd = os.open(selected, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise MemeError("MEME_IMAGE_INVALID")
            data = stream.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise MemeError("FILE_TOO_LARGE")
    extension = _extension(data)
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="haro_meme_", suffix=extension, dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _cleanup(prepared, frozen, sources, root):
    # 只删明确返回的转换临时文件与本次冻结副本；清理失败不覆盖实际投递结果。
    files = [(value, False) for value in (prepared or {}).get("temp_files", [])]
    if frozen:
        files.append((frozen, True))
    for value, own in files:
        try:
            path = _path(value)
            allowed = path.parent == root and (path.name.startswith("haro_meme_") if own else _temporary(path, root))
            if path in sources or not allowed:
                raise MemeError("MEME_CLEANUP_FAILED")
            if path.exists() or path.is_symlink():
                _safe_file(path)
                path.unlink()
        except Exception as error:
            logger.warning(f"Haro 表情临时文件未清理：{type(error).__name__}")





class MemeIntegration:
    """独立的来源缓存；主动目标按人工配置建立，取消后不借其他会话。"""
    def __init__(self, owner):
        self.owner, self.routes = owner, {}

    def _remember(self, key, route):
        self.routes[key] = route
        if len(self.routes) > 1024:
            del self.routes[next(iter(self.routes))]
        return route

    def _route(self, platform, origin, uid, instance, source, target=None):
        route = {"platform": platform, "platformId": instance, "origin": origin, "uid": uid, "source": source,
            "generation": self.owner.bridge.generation, "used": False, "sending": False, "cancelled": False,
            "target": dict(target) if target else None}
        client = getattr(platform, "get_client", None)
        if callable(client):
            try:
                route["client"] = client()
            except Exception:
                route["clientUnavailable"] = True
        return route

    def capture(self, event, source, generation):
        if generation != self.owner.bridge.generation or not self.owner._owner(source):
            return None
        try:
            platform = self.owner.context.get_platform_inst(source["platformId"])
        except Exception:
            return None
        if platform is None or platform.meta().name != source["platform"]:
            return None
        facade = MemeSource(source["platform"], source["origin"], getattr(event, "session_id", None) or source["sender"], getattr(event, "persona_id", ""))
        key = str(uuid.uuid4())
        self._remember(key, self._route(platform, source["origin"], source["sender"], source["platformId"], facade))
        return key

    def cancel(self, key):
        route = self.routes.get(key)
        if not route:
            self._remember(key, {"cancelled": True})
            return True
        route["cancelled"] = True
        return not route.get("sending", False)

    def clear(self):
        self.routes.clear()

    def _bind(self, packet):
        key = packet.get("routeKey")
        try:
            uuid.UUID(key)
        except (TypeError, ValueError, AttributeError):
            raise MemeError("MEME_ROUTE_UNAVAILABLE")
        route = self.routes.get(key)
        target_id = packet.get("targetId")
        if route is None and target_id is not None:
            target = self.owner.bridge.targets.get(target_id)
            if not target or packet.get("origin") != target["origin"]:
                raise MemeError("MEME_ROUTE_UNAVAILABLE")
            platform, reason = inspect_target(self.owner.context, self.owner.bridge.owners, target)
            if platform is None:
                raise MemeError(reason)
            uid = target["ownerId"].partition(":")[2]
            source = MemeSource(platform.meta().name, target["origin"], uid)
            route = self._remember(key, self._route(platform, target["origin"], uid, target["platformId"], source, target))
        if (not route or route.get("cancelled") or packet.get("origin") != route["origin"]
                or target_id != (route["target"]["id"] if route["target"] else None)):
            raise MemeError("MEME_ROUTE_UNAVAILABLE")
        return route

    def _ready(self, key, route):
        try:
            if (self.routes.get(key) is not route or route["cancelled"] or route.get("clientUnavailable") or not self.owner.bridge.connected
                    or route["generation"] != self.owner.bridge.generation
                    or self.owner.context.get_platform_inst(route["platformId"]) is not route["platform"]
                    or route["platform"].meta().name + ":" + route["uid"] not in self.owner.bridge.owners):
                return False
            if "client" in route and route["platform"].get_client() is not route["client"]:
                return False
            if route["target"]:
                current = self.owner.bridge.targets.get(route["target"]["id"])
                if current != route["target"] or inspect_target(self.owner.context, self.owner.bridge.owners, current)[0] is not route["platform"]:
                    return False
            return True
        except Exception:
            return False

    def _manager(self):
        metadata = self.owner.context.get_registered_star("meme_manager")
        if not metadata or str(getattr(metadata, "version", "")).lstrip("v") != "5.3.5":
            raise MemeError("MEME_VERSION_UNSUPPORTED")
        manager = metadata.star_cls
        if getattr(manager, "trigger_scope", None) != "chat_and_plugin_llm" or getattr(manager, "emotion_llm_enabled", True):
            raise MemeError("MEME_CONFIG_REQUIRED")
        return manager

    def _categories(self, manager, source):
        mapping = manager._resolve_runtime_pack_context(event=source).get("category_mapping") or {}
        return [{"category": key, "description": str(description).encode("utf-16-le")[:600].decode("utf-16-le", errors="ignore")}
            for key, description in sorted(mapping.items()) if isinstance(key, str) and 0 < len(key.encode("utf-16-le")) <= 160
            and not re.search(r"[&\x00-\x1f\x7f]", key) and manager._get_category_meme_files(source, key)][:200]





    async def deliver(self, packet):
        prepared, frozen, sources, root = None, None, set(), None
        try:
            route = self._bind(packet)
            key = packet["routeKey"]
            if not self._ready(key, route):
                raise MemeError("MEME_ROUTE_UNAVAILABLE")
            manager = self._manager()
            source = route["source"]
            categories = self._categories(manager, source)
            if packet.get("operation") == "categories":
                return {"kind": "ok", "categories": categories}
            category = packet.get("category")
            if packet.get("operation") != "send" or route["used"] or category not in {row["category"] for row in categories}:
                raise MemeError("MEME_REQUEST_REJECTED")
            route["used"] = True
            root = _temp_root()
            sources = {_path(value) for value in manager._get_category_meme_files(source, category)}
            prepared = await asyncio.wait_for(manager.compat_prepare_message(source, "&&" + category + "&&"), 12)
            def active():
                try:
                    return self._ready(key, route) and self._manager() is manager
                except Exception:
                    return False
            if not active():
                raise MemeError("MEME_ROUTE_UNAVAILABLE")
            if prepared.get("emotions") != [category] or not prepared.get("images"):
                raise MemeError("MEME_NOT_SELECTED")
            # 再读图库名单，避免准备期间切换资源包；临时图不扩大模型路径权限。
            sources.intersection_update({_path(value) for value in manager._get_category_meme_files(source, category)})
            image = prepared["images"][0]
            frozen = _freeze(image, prepared, sources, category, root)
            route["sending"] = True
            result = await send_image(self.owner, origin=route["origin"], platform=route["platform"], uid=route["uid"], path=frozen, guard=active)
            if result.get("kind") == "ok":
                try:
                    await asyncio.wait_for(manager._confirm_meme_delivery(prepared.get("delivery_event", source), [image]), 2)
                except Exception as error:
                    logger.warning(f"Haro 表情已发送，图库历史未确认：{type(error).__name__}")
            return result
        except MemeError as error:
            return {"kind": "failed", "code": error.code}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"Haro 表情选择未完成：{type(error).__name__}")
            return {"kind": "failed", "code": "MEME_PREPARE_FAILED"}
        finally:
            if root is not None:
                _cleanup(prepared, frozen, sources, root)
