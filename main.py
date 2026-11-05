"""AstrBot 处理消息呈现；身份、记忆和原生执行由独立 Haro Host 持有。"""
import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .bridge import HaroBridge


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





@register("haro", "Haro contributors", "Haro 原生助手桥接", "0.1.3")
class HaroPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.bridge = None
        self._origins = set()

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
            self._origins.clear, self._inspect_targets,
        )
        await self.bridge.start()

    def _source(self, event):
        return {"platform": event.get_platform_name(), "platformId": event.get_platform_id(), "sender": str(event.get_sender_id()),
                "origin": event.unified_msg_origin}

    def _owner(self, source):
        return self.bridge and f"{source['platform']}:{source['sender']}" in self.bridge.owners

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        # 按钮交给下面的专用处理器，不让普通消息监听抢先吞掉回调。
        if event.get_button_interaction() is not None:
            return
        source = self._source(event)
        if not self._owner(source):
            return
        event.stop_event()
        if not self.bridge.connected:
            await event.send(event.plain_result("Haro 未连接，请检查独立 Host 是否运行。"))
            return
        generation = self.bridge.generation
        try:
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
                                    **({"messageContext": context} if context else {})}, generation=generation)
        except Exception as error:
            logger.error(f"Haro 入站失败：{type(error).__name__}")
            await event.send(event.plain_result("本次提交未确认，请检查 Haro 状态和插件日志，避免直接重复提交。"))

    @filter.button_action("haro:approval")
    async def on_approval(self, event: AstrMessageEvent):
        source = self._source(event)
        if not self._owner(source) or not self.bridge.connected or source["origin"] not in self._origins:
            return
        event.stop_event()
        interaction = event.get_button_interaction()
        if interaction is not None:
            await self.bridge.send({"type": "approval", **source,
                                    "approvalId": interaction.data.get("id"),
                                    "decision": interaction.data.get("decision")})





    # ========== Haro 出站与插件收尾 ==========

    async def _deliver(self, packet):
        if "targetId" in packet:
            return await self._deliver_target(packet)
        origin = packet["origin"]
        if origin not in self._origins:
            return False
        parts = []
        for item in packet["content"]:
            if item["type"] == "text":
                parts.append(Comp.Plain(item["text"]))
            elif item["type"] == "file":
                parts.append(Comp.File(file=item["path"], name=item["name"]))
            elif item["type"] == "image":
                parts.append(Comp.Image.fromFileSystem(item["path"]))
            elif item["type"] == "approval":
                lines = ["Haro 原生工具审批"]
                for key, label in (("command", "命令"), ("cwd", "目录"), ("reason", "原因"), ("details", "详情")):
                    if item.get(key):
                        lines.append(f"{label}：{item[key]}")
                parts.append(Comp.Plain("\n".join(lines)[:3500]))
                choices = [("decline", "拒绝")]
                if item.get("allowOnce", True):
                    choices.insert(0, ("accept", "允许这一次"))
                parts.append(Comp.ActionRow(
                    buttons=[Comp.Button(id="haro:approval", label=label,
                                         action=Comp.Button.CallbackAction(data={"id": item["id"], "decision": value}))
                             for value, label in choices],
                    action_timeout=300,
                    fallback_text="；".join(f"/approve {item['id']} {value}" for value, _ in choices),
                ))
        return await self.context.send_message(origin, MessageChain(parts)) is True

    def _target_available(self, target):
        owner = target.get("ownerId", "")
        instance = target.get("platformId", "")
        if (owner not in self.bridge.owners or not owner.startswith("telegram:")
                or not owner[9:].isdigit() or not instance or ":" in instance
                or target.get("origin") != f"{instance}:FriendMessage:{owner[9:]}"):
            return False
        platform = self.context.get_platform_inst(instance)
        return platform is not None and platform.meta().name == "telegram"

    def _inspect_targets(self, targets):
        return [{"id": target["id"], "available": self._target_available(target)} for target in targets]

    async def _deliver_target(self, packet):
        target = self.bridge.targets.get(packet["targetId"])
        content = packet.get("content", [])
        if (not target or not self._target_available(target) or packet.get("origin") != target["origin"]
                or len(content) != 1 or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str) or not 0 < len(content[0]["text"]) <= 1200):
            return {"kind": "failed", "code": "TARGET_UNAVAILABLE"}
        try:
            sent = await self.context.send_message(target["origin"], MessageChain([Comp.Plain(content[0]["text"])]))
            return {"kind": "ok"} if sent is True else {"kind": "failed", "code": "PLATFORM_REJECTED"}
        except Exception as error:
            # Telegram 明确拒绝与结果不明分开；不绕过 AstrBot 读取 Bot Token。
            from telegram.error import BadRequest, Forbidden, InvalidToken
            rejected = isinstance(error, (BadRequest, Forbidden, InvalidToken))
            return {"kind": "failed" if rejected else "unknown", "code": "PLATFORM_REJECTED" if rejected else "DELIVERY_UNCONFIRMED"}

    async def terminate(self):
        if self.bridge:
            await self.bridge.close()
        self._origins.clear()
