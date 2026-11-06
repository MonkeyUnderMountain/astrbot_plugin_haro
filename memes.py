"""Meme Manager 5.3.5 适配；正文不经过插件清理，图库与临时图片仍由原插件管理。"""
import asyncio
import copy
import re


class MemeSource:
    """只保存当前可信会话的元数据与隔离扩展，不持有聊天正文、附件或原事件。"""
    def __init__(self, event, origin):
        self.unified_msg_origin = origin
        self.session_id = str(getattr(event, "session_id", ""))
        self.persona_id = str(getattr(event, "persona_id", "") or "")
        self.extras = {"meme_manager_llm_request_origin": "plugin"}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_name(self):
        return "telegram"


class _SendContext:
    """兼容接口使用独立实例副本；只将图片送往捕获的同一来源，不改共享插件状态。"""
    def __init__(self, owner, original, guard, origin):
        self.owner, self.original, self.guard, self.origin = owner, original, guard, origin

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def send_message(self, origin, chain, *args, **kwargs):
        if origin != self.origin or not self.guard():
            raise RuntimeError("MEME_ROUTE_UNAVAILABLE")
        if args or kwargs:
            raise RuntimeError("MEME_SEND_ARGUMENTS_UNSUPPORTED")
        result = await self.owner._send(origin=origin, chain=chain, guard=self.guard)
        if result is not True:
            raise RuntimeError("MEME_RECEIPT_UNKNOWN")
        return result


class MemeIntegration:
    def __init__(self, owner):
        self.owner = owner

    def capture(self, event, route):
        route["memeSource"] = MemeSource(event, route["origin"])
        route["memeUsed"] = False

    def cancel(self, key):
        route = self.owner._text_routes.get(key)
        if route:
            route["memeSource"] = None

    def _manager(self):
        metadata = self.owner.context.get_registered_star("meme_manager")
        if not metadata or str(getattr(metadata, "version", "")).lstrip("v") != "5.3.5":
            raise ValueError("MEME_VERSION_UNSUPPORTED")
        manager = metadata.star_cls
        if (getattr(manager, "trigger_scope", None) != "chat_and_plugin_llm"
                or getattr(manager, "emotion_llm_enabled", True)):
            raise ValueError("MEME_CONFIG_REQUIRED")
        return manager

    def _ready(self, route, source):
        try:
            return bool(source and route.get("memeSource") is source and not route["topic"]
                        and self.owner.bridge.connected and route["generation"] == self.owner.bridge.generation
                        and self.owner.context.get_platform_inst(route["platformId"]) is route["platform"]
                        and route["platform"].get_client() is route["client"])
        except Exception:
            return False

    def _categories(self, manager, source):
        # 上游暂无按当前会话列分类的公开接口。以下两处固定在5.3.5，升级需重验。
        context = manager._resolve_runtime_pack_context(event=source)
        mapping = context.get("category_mapping") or {}
        return [{"category": key, "description": str(description).encode("utf-16-le")[:600].decode("utf-16-le", errors="ignore")}
                for key, description in sorted(mapping.items())
                if isinstance(key, str) and 0 < len(key.encode("utf-16-le")) <= 160 and not re.search(r"[&\x00-\x1f\x7f]", key)
                and manager._get_category_meme_files(source, key)][:200]





    # ========== 一轮一次、准备后复核、公开接口负责清理 ==========

    async def deliver(self, packet):
        route = self.owner._text_routes.get(packet.get("routeKey"))
        source = route.get("memeSource") if route else None
        if (not route or "targetId" in packet or packet.get("origin") != route["origin"]
                or not self._ready(route, source)):
            return {"kind": "failed", "code": "MEME_ROUTE_UNAVAILABLE"}
        try:
            manager = self._manager()
            categories = self._categories(manager, source)
        except (ValueError, AttributeError):
            return {"kind": "failed", "code": "MEME_UNAVAILABLE"}
        if packet.get("operation") == "categories":
            return {"kind": "ok", "categories": categories}
        if (packet.get("operation") != "send" or route["memeUsed"]
                or packet.get("category") not in {row["category"] for row in categories}):
            return {"kind": "failed", "code": "MEME_REQUEST_REJECTED"}
        # 在第一个await前占用；unknown、异常和取消都不再次发送。
        route["memeUsed"] = True
        category = packet["category"]
        prepared = None
        sending = False
        # 原插件的公开API、history字典和图库仍复用，context仅在该副本中加目标检查。
        adapter = copy.copy(manager)
        def guard():
            try:
                return self._ready(route, source) and self._manager() is manager
            except (ValueError, AttributeError):
                return False
        adapter.context = _SendContext(self.owner, manager.context, guard, route["origin"])
        # history由原实例懒初始化；不能只留在一次调用的浅副本上。
        adapter._confirm_meme_delivery = manager._confirm_meme_delivery
        try:
            prepared = await adapter.compat_prepare_message(source, "&&" + category + "&&")
            if not self._ready(route, source) or self._manager() is not manager:
                return {"kind": "failed", "code": "MEME_ROUTE_UNAVAILABLE"}
            # 不启用辅助模型/语义请求钩子；有效结果必须仍是指定分类，最多一张。
            if prepared.get("emotions") != [category] or not prepared.get("images"):
                return {"kind": "failed", "code": "MEME_NOT_SELECTED"}
            prepared["images"] = prepared["images"][:1]
            sending = True
            result = await adapter.compat_send_prepared_message(source, prepared, send_text=False, send_images=True)
            return {"kind": "ok" if result.get("sent_images_count") == 1 else "unknown"}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"kind": "unknown" if sending else "failed", "code": "MEME_DELIVERY_UNKNOWN"}
        finally:
            # 成功发送也再走一次无图清理；公开接口按missing_ok清理，不会再发送或计入history。
            if prepared is not None:
                await adapter.compat_send_prepared_message(source, {**prepared, "images": []}, send_text=False, send_images=False)
