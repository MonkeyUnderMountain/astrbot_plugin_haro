"""AstrBot 处理消息呈现；身份、记忆和原生执行由独立 Haro Host 持有。"""
import asyncio
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .bridge import HaroBridge
from .memes import MemeIntegration
from .targets import inspect_target, deliver_target, send_image


# 历史材料只提取白名单字段；不取文件、不联网补查，也不参与主人认证或回信路由。
def _message_id(value):
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if value and len(value) <= 256 and "\0" not in value else None


def _original_time(value):
    if isinstance(value, datetime) and value.tzinfo is not None and value.timestamp() > 0:
        return value.isoformat()
    return None


def _has_topic(raw):
    return bool(getattr(raw, "is_topic_message", False) or getattr(raw, "message_thread_id", None) is not None
                or getattr(raw, "direct_messages_topic", None) is not None)


def _reference_text(result, key, value, limit=4000):
    if isinstance(value, str):
        result[key] = value[:limit]
        result["truncated"] = result["truncated"] or len(value) > limit


def _reference(source, event, normalized=False, external=False):
    sender = getattr(source, "from_user", None)
    if sender is None:
        sender = getattr(getattr(source, "origin", None), "sender_user", None)
    sender_id = _message_id(getattr(sender, "id", None) if sender else getattr(source, "sender_id", None))
    if normalized and sender_id == "0":
        sender_id = None  # 通用Reply用0表示未提供发送者。
    try:
        bot_id = _message_id(getattr(getattr(event, "client", None), "id", None))
    except RuntimeError:
        bot_id = None  # Bot未初始化时不调用get_me，也不以昵称推断是当前机器人。
    role = "unknown"
    if sender_id and sender_id == str(event.get_sender_id()):
        role = "owner"
    elif sender_id and bot_id:
        role = "assistant" if sender_id == bot_id else "other"
    result = {"sender": role, "contentType": "unknown", "availability": "unavailable", "truncated": False}
    origin = getattr(source, "origin", None) if external else None
    for key, value in (("messageId", getattr(source, "id" if normalized else "message_id", None) or getattr(origin, "message_id", None)),
                       ("conversationId", getattr(getattr(source, "chat", None) or getattr(origin, "chat", None), "id", None)),
                       ("senderId", sender_id)):
        if (value := _message_id(value)) is not None:
            result[key] = value
    # 通用Reply.time可能是转换时的当前时间，不能拿来填历史时间。
    date = None if normalized else getattr(source, "date", None) or getattr(getattr(source, "origin", None), "date", None)
    if (date := _original_time(date)) is not None:
        result["timestamp"] = date
    body = getattr(source, "message_str", None) if normalized else getattr(source, "text", None)
    body = body or getattr(source, "text" if normalized else "caption", None)
    if isinstance(body, str) and body:
        result["contentType"] = "text"
        _reference_text(result, "text", body)
        result["availability"] = "available"
    chain = (getattr(source, "chain", None) or []) if normalized else []
    if getattr(source, "photo", None) or any(isinstance(item, Comp.Image) for item in chain):
        result["contentType"] = "image"
    media = next((getattr(source, key, None) for key in ("document", "video", "audio", "voice", "animation", "video_note", "sticker")
                  if getattr(source, key, None) is not None), None)
    if media is not None or any(isinstance(item, Comp.File) for item in chain):
        result["contentType"] = "file"
    if media is not None:
        _reference_text(result, "description", getattr(media, "file_name", None), 1000)
    elif normalized:
        file = next((item for item in chain if isinstance(item, Comp.File)), None)
        _reference_text(result, "description", getattr(file, "name", None), 1000)
    if result["contentType"] in ("image", "file"):
        result.update(availability="partial", reason="ATTACHMENT_NOT_FETCHED")
    if normalized:
        result.update(availability="partial" if result.get("text") or result["contentType"] != "unknown" else "unavailable",
                      reason="NORMALIZED_REPLY_ONLY")
    elif external:
        result.update(availability="partial" if result.get("text") or result["contentType"] != "unknown" else "unavailable",
                      reason="EXTERNAL_REPLY")
    elif result["availability"] == "unavailable":
        result["reason"] = "CONTENT_NOT_PROVIDED"
    return result


def message_context(event):
    message = event.message_obj
    raw = getattr(getattr(message, "raw_message", None), "message", None) if event.get_platform_name() == "telegram" else None
    result = {}
    for key, value in (("messageId", getattr(raw, "message_id", None) if raw else getattr(message, "message_id", None)),
                       ("conversationId", getattr(getattr(raw, "chat", None), "id", None) if raw else getattr(message, "session_id", None))):
        if (value := _message_id(value)) is not None:
            result[key] = value
    if raw is not None and (date := _original_time(getattr(raw, "date", None))) is not None:
        result["timestamp"] = date
    reference = getattr(raw, "reply_to_message", None)
    external = getattr(raw, "external_reply", None)
    if reference is not None or external is not None:
        result["replyTo"] = _reference(reference if reference is not None else external, event, external=reference is None)
    else:
        reply_class = getattr(Comp, "Reply", None)
        reply = next((item for item in event.get_messages() if reply_class and isinstance(item, reply_class)), None)
        if reply is not None:
            result["replyTo"] = _reference(reply, event, normalized=True)
    quote = getattr(raw, "quote", None)
    if quote is not None and isinstance(getattr(quote, "text", None), str):
        result["quote"] = {"truncated": False}
        _reference_text(result["quote"], "text", quote.text)
        if type(getattr(quote, "position", None)) is int and 0 <= quote.position <= 9007199254740991:
            result["quote"]["position"] = quote.position  # 原生UTF-16码元位置，不重新切片原文。
        if type(getattr(quote, "is_manual", None)) is bool:
            result["quote"]["isManual"] = quote.is_manual
    return result





@register("haro", "Haro contributors", "Haro 原生助手桥接", "0.1.7")
class HaroPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.bridge = None
        self._origins = set()
        self._text_routes = {}
        self.memes = MemeIntegration(self)

    async def initialize(self):
        if not all(hasattr(Comp, key) for key in ("Button", "ActionRow")):
            raise RuntimeError("当前 AstrBot 缺少交互按钮接口，请使用支持该接口的版本")
        value = self.config.get("haro_connection_file", "")
        if not isinstance(value, str) or not value.strip():
            logger.warning("Haro 尚未配置：请打开插件配置，填写 haro_connection_file 并保存。")
            return
        connection = Path(value.strip()).expanduser()
        if not connection.is_absolute():
            logger.warning("Haro 尚未连接：haro_connection_file 需填写连接文件的绝对路径，请在插件配置中修改并保存。")
            return
        self.bridge = HaroBridge(
            connection, self._deliver, lambda text: logger.info(f"[Haro] {text}"),
            self._clear_routes, self._inspect_targets,
            self.memes.cancel,
        )
        await self.bridge.start()

    def _source(self, event):
        return {"platform": event.get_platform_name(), "platformId": event.get_platform_id(), "sender": str(event.get_sender_id()),
                "origin": event.unified_msg_origin}

    def _owner(self, source):
        return self.bridge and f"{source['platform']}:{source['sender']}" in self.bridge.owners

    def _approver(self, source):
        return (self._owner(source) and source["platform"] == "telegram"
                and f"telegram:{source['sender']}" == getattr(self.bridge, "approver_owner_id", None))

    async def _send(self, *, origin=None, chain=None, event=None, client=None, payload=None, operation=None, guard=lambda: True):
        """唯一平台提交入口。调用方提供已核验路由；提交前重查，超时不重发。"""
        if not guard():
            raise RuntimeError("Haro 发送来源已失效")
        if operation is not None:
            pending = operation(guard)
        elif event is not None:
            pending = event.send(chain)
        elif client is not None:
            pending = client.send_message(**payload)
        else:
            pending = self.context.send_message(origin, chain)
        return await asyncio.wait_for(pending, 12)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        # 按钮交给下面的专用处理器，不让普通消息监听抢先吞掉回调。
        if event.get_button_interaction() is not None:
            return
        source = self._source(event)
        if not self._owner(source):
            return
        event.stop_event()
        if event.message_obj.message_str.strip().startswith("/approve ") and not self._approver(source):
            return
        raw = getattr(getattr(event.message_obj, "raw_message", None), "message", None)
        if source["platform"] == "telegram" and _has_topic(raw):
            # 断线时也不能借用无topic的event.send回主会话。
            presentation = self._text_presentation(event, source, self.bridge.generation)
            if presentation:
                await self._reject_topic(presentation["routeKey"])
            else:
                logger.warning("Haro 私聊话题不受支持，任务未提交，无法可靠定位原话题。")
            return
        if not self.bridge.connected:
            await self._send(event=event, chain=event.plain_result("Haro 未连接，请检查独立 Host 是否运行。"))
            return
        generation = self.bridge.generation
        try:
            presentation = self._text_presentation(event, source, generation)
            meme_key = self.memes.capture(event, source, generation)
            attachments = []
            for component in event.get_messages():
                if isinstance(component, Comp.Image):
                    filename = await component.convert_to_file_path()
                    kind = "image"
                elif isinstance(component, Comp.File):
                    filename = await component.get_file()
                    kind = "file"
                else:
                    continue
                if len(attachments) >= 8:
                    raise ValueError("一次最多接收八个附件")
                original = Path(filename)
                if original.stat().st_size > 10 * 1024 * 1024:
                    raise ValueError("附件超过 10 MiB")
                suffix = original.suffix.lower()
                if len(suffix) > 10:
                    suffix = ".bin"
                target = Path(self.bridge.inbox) / (uuid.uuid4().hex + suffix)
                # 原事件退出后平台临时文件可能被清理，先复制到业务工作区。
                await asyncio.to_thread(shutil.copyfile, original, target)
                attachments.append({"type": kind, "path": str(target)})
            # 唤醒检查会裁掉 event.message_str 的前缀；改用保留前缀的适配器文本。
            if not self.bridge.connected or self.bridge.generation != generation:
                raise RuntimeError("Haro 连接已更换，本次任务未接收")
            self._origins.add(source["origin"])
            context = message_context(event)
            await self.bridge.send({"type": "message", **source, "text": event.message_obj.message_str,
                                    "attachments": attachments,
                                    **({"presentation": presentation} if presentation else {}),
                                    **({"memeRouteKey": meme_key} if meme_key else {}),
                                    **({"messageContext": context} if context else {})}, generation=generation)
        except Exception as error:
            logger.error(f"Haro 入站失败：{type(error).__name__}")
            await self._send(event=event, chain=event.plain_result("本次提交未确认，请检查 Haro 状态和插件日志，避免直接重复提交。"))

    @filter.button_action("haro:approval")
    async def on_approval(self, event: AstrMessageEvent):
        source = self._source(event)
        if not self._approver(source) or not self.bridge.connected or source["origin"] not in self._origins:
            return
        event.stop_event()
        interaction = event.get_button_interaction()
        if interaction is not None and isinstance(interaction.data, dict):
            await self.bridge.send({"type": "approval", **source,
                                    "approvalId": interaction.data.get("id"),
                                    "decision": interaction.data.get("decision")})





    # ========== Haro 出站与插件收尾 ==========

    def _approval_parts(self, item):
        # 只呈现简短说明；按钮仅含审批ID和决定，不生成详情文件或复制原生参数。
        parts = [Comp.Plain(item["summary"])]
        if item.get("allowOnce", True):
            choices = [("accept", "允许本次"), ("decline", "拒绝")]
            parts.append(Comp.ActionRow(
                buttons=[Comp.Button(id="haro:approval", label=label,
                                     action=Comp.Button.CallbackAction(data={"id": item["id"], "decision": value}))
                         for value, label in choices],
                action_timeout=300,
                fallback_text="；".join(f"/approve {item['id']} {value}" for value, _ in choices),
            ))
        return parts

    async def _deliver(self, packet):
        if "format" in packet:
            if packet["format"] == "meme-manager-v2":
                return await self.memes.deliver(packet)
            if packet["format"] != "telegram-text-v1":
                return {"kind": "failed", "code": "PRESENTATION_UNAVAILABLE"}
            return await self._deliver_text(packet)
        if "targetId" in packet:
            return await self._deliver_target(packet)
        origin = packet["origin"]
        if origin not in self._origins:
            return False
        content = packet["content"]
        if len(content) == 1 and content[0].get("type") == "image":
            instance, kind, uid = origin.split(":", 2)
            platform = self.context.get_platform_inst(instance)
            if platform is None or kind != "FriendMessage":
                return {"kind": "failed", "code": "IMAGE_ROUTE_UNAVAILABLE"}
            if platform.meta().name == "qq_official" and "qq_official:" + uid not in self.bridge.owners:
                return {"kind": "failed", "code": "IMAGE_ROUTE_UNAVAILABLE"}
            generation = self.bridge.generation
            return await send_image(self, origin=origin, platform=platform, uid=uid, path=content[0]["path"], guard=lambda:
                self.bridge.connected and self.bridge.generation == generation and origin in self._origins
                and self.context.get_platform_inst(instance) is platform)
        parts = []
        for item in packet["content"]:
            if item["type"] == "text":
                parts.append(Comp.Plain(item["text"]))
            elif item["type"] == "file":
                parts.append(Comp.File(file=item["path"], name=item["name"]))
            elif item["type"] == "image":
                parts.append(Comp.Image.fromFileSystem(item["path"]))
            elif item["type"] == "approval":
                if item.get("format") != "summary-v1" or not isinstance(item.get("summary"), str):
                    return {"kind": "failed", "code": "APPROVAL_HOST_UPDATE_REQUIRED"}
                platform = self.context.get_platform_inst(origin.split(":", 1)[0])
                owner = getattr(self.bridge, "approver_owner_id", None)
                if (platform is None or platform.meta().name != "telegram" or not owner
                        or origin != f"{origin.split(':', 1)[0]}:FriendMessage:{owner[9:]}"):
                    return {"kind": "failed", "code": "APPROVAL_CHANNEL_UNSUPPORTED"}
                parts.extend(self._approval_parts(item))
        generation = self.bridge.generation
        return await self._send(origin=origin, chain=MessageChain(parts), guard=lambda:
            self.bridge.connected and self.bridge.generation == generation and origin in self._origins) is True

    def _target_available(self, target):
        return inspect_target(self.context, self.bridge.owners, target)[0] is not None

    def _inspect_targets(self, targets):
        result = []
        for target in targets:
            platform, reason = inspect_target(self.context, self.bridge.owners, target)
            result.append({"id": target["id"], "available": platform is not None, "reason": reason})
        return result

    async def _deliver_target(self, packet):
        return await deliver_target(self, packet)

    async def terminate(self):
        if self.bridge:
            await self.bridge.close()
        self._clear_routes()





    # ========== Telegram 本人私聊：每个来源绑定一次实际平台调用 ==========

    def _clear_routes(self):
        self._origins.clear()
        self._text_routes.clear()
        self.memes.clear()

    def _text_presentation(self, event, source, generation):
        # 只从本次可信事件取目标；同一UMO也用不同key，不能借用后来消息的目标信息。
        if source["platform"] != "telegram" or not source["sender"].isdigit():
            return None
        raw = getattr(getattr(event.message_obj, "raw_message", None), "message", None)
        chat = getattr(raw, "chat", None)
        sender = getattr(raw, "from_user", None)
        if (getattr(chat, "type", None) != "private" or str(getattr(chat, "id", None)) != source["sender"]
                or str(getattr(sender, "id", None)) != source["sender"]):
            return None
        try:
            platform = self.context.get_platform_inst(source["platformId"])
            get_client = getattr(platform, "get_client", None)
            if platform is None or platform.meta().name != "telegram" or not callable(get_client):
                return None
            client = get_client()
        except Exception:
            return None
        if not callable(getattr(client, "send_message", None)):
            return None
        topic = _has_topic(raw)
        thread_id = getattr(raw, "message_thread_id", None)
        direct_id = getattr(getattr(raw, "direct_messages_topic", None), "topic_id", None)
        topic_args = ({"message_thread_id": thread_id} if type(thread_id) is int and thread_id > 0 else
                      {"direct_messages_topic_id": direct_id} if type(direct_id) is int and direct_id > 0 else {})
        key = str(uuid.uuid4())
        self._text_routes[key] = {"origin": source["origin"], "generation": generation, "platformId": source["platformId"],
                                  "platform": platform, "client": client, "chatId": chat.id, "topic": bool(topic), "topicArgs": topic_args}
        # 只保留有限的来源元数据；过期key明确失败，不回退猜目标或重发。
        if len(self._text_routes) > 1024:
            del self._text_routes[next(iter(self._text_routes))]
        return {"format": "telegram-text-v1", "routeKey": key,
                **({"unavailable": "TELEGRAM_TOPIC_UNSUPPORTED"} if topic else {})}

    async def _reject_topic(self, key):
        route = self._text_routes.pop(key)
        if not route["topicArgs"]:
            logger.warning("Haro 私聊话题不受支持，任务未提交，缺少可靠的回信话题ID。")
            return
        try:
            await self._send(client=route["client"], payload={"chat_id": route["chatId"], **route["topicArgs"],
                "text": "此话题入口暂不支持 Haro，任务未提交。请使用普通私聊。", "parse_mode": None, "entities": []},
                guard=lambda: self.context.get_platform_inst(route["platformId"]) is route["platform"]
                and route["platform"].get_client() is route["client"])
        except Exception as error:
            logger.warning(f"Haro 话题不支持提示未确认：{type(error).__name__}；不转投主会话。")

    async def _deliver_text(self, packet):
        route = self._text_routes.get(packet.get("routeKey"))
        if (not route or "targetId" in packet or packet.get("origin") != route["origin"]
                or route["origin"] not in self._origins or not self.bridge.connected
                or route["generation"] != self.bridge.generation
                or self.context.get_platform_inst(route["platformId"]) is not route["platform"]
                or route["platform"].get_client() is not route["client"]):
            return {"kind": "failed", "code": "TELEGRAM_ROUTE_UNAVAILABLE"}
        if route["topic"]:
            return {"kind": "failed", "code": "TELEGRAM_TOPIC_UNSUPPORTED"}
        content = packet.get("content", [])
        try:
            if len(content) != 1 or content[0].get("type") != "text":
                raise ValueError()
            item = content[0]
            if set(item) - {"type", "text", "pre"} or not isinstance(item.get("text"), str):
                raise ValueError()
            text = item["text"]
            encoded = text.encode("utf-16-le")
            if not any(not char.isspace() and char != "\ufeff" for char in text) or "\0" in text or len(encoded) > 8192:
                raise ValueError()
            entities = []
            if "pre" in item:
                from telegram import MessageEntity
                pre = item["pre"]
                if (not isinstance(pre, dict) or set(pre) - {"offset", "length", "language"}
                        or type(pre.get("offset")) is not int or type(pre.get("length")) is not int
                        or pre["offset"] < 0 or pre["length"] <= 0 or pre["offset"] + pre["length"] > len(encoded) // 2
                        or "language" in pre and (not isinstance(pre["language"], str)
                                                  or not re.fullmatch(r"[\w.+#-]{1,64}", pre["language"]))):
                    raise ValueError()
                # 同时拒绝落在代理对中间的实体边界。
                encoded[:pre["offset"] * 2].decode("utf-16-le")
                encoded[pre["offset"] * 2:(pre["offset"] + pre["length"]) * 2].decode("utf-16-le")
                entities = [MessageEntity(type=MessageEntity.PRE, **pre)]
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return {"kind": "failed", "code": "TELEGRAM_TEXT_INVALID"}
        try:
            result = await self._send(client=route["client"], payload={"chat_id": route["chatId"], "text": text, "parse_mode": None, "entities": entities},
                guard=lambda: self.bridge.connected and route["generation"] == self.bridge.generation
                and route["origin"] in self._origins and self.context.get_platform_inst(route["platformId"]) is route["platform"]
                and route["platform"].get_client() is route["client"])
            message_id = getattr(result, "message_id", None)
            if (type(message_id) is not int or message_id <= 0
                    or getattr(getattr(result, "chat", None), "id", None) != route["chatId"]):
                return {"kind": "unknown", "code": "RECEIPT_UNKNOWN"}
            return {"kind": "ok", "messageId": str(message_id)}
        except Exception as error:
            from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, ChatMigrated
            if isinstance(error, RetryAfter):
                return {"kind": "failed", "code": "TELEGRAM_RATE_LIMITED"}
            if isinstance(error, ChatMigrated):
                return {"kind": "failed", "code": "TELEGRAM_TARGET_CHANGED"}
            if isinstance(error, (BadRequest, Forbidden, InvalidToken)):
                return {"kind": "failed", "code": "TELEGRAM_REJECTED"}
            return {"kind": "unknown", "code": "RECEIPT_UNKNOWN"}
