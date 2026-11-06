"""连接同机独立 Haro；断线重新读取连接文件，不启动、关闭或重放 Host 任务。"""
import asyncio
import hashlib
import hmac
import json
import re
from pathlib import Path


class HaroBridge:
    def __init__(self, connection_file, deliver, log=lambda text: None,
                 disconnected=lambda: None, inspect_targets=lambda targets: [], cancel_meme=lambda key: None):
        self.connection_file = Path(connection_file)
        self.deliver = deliver
        self.log = log
        self.disconnected = disconnected
        self.inspect_targets = inspect_targets
        self.cancel_meme = cancel_meme
        self._meme_tasks = {}
        self.owners = set()
        self.approver_owner_id = None
        self.targets = {}
        self.inbox = None
        self.connected = False
        self.generation = 0
        self._writer = None
        self._task = None
        self._write_lock = asyncio.Lock()

    async def start(self):
        # 加载插件不等待 Host；配置入口必须始终可用，连接失败交给已有重试循环。
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def send(self, packet, generation=None):
        if not self.connected:
            raise RuntimeError("Haro 未连接")
        await self._write(packet, self.generation if generation is None else generation)

    async def _write(self, packet, generation=None):
        async with self._write_lock:
            if generation is not None and (generation != self.generation or not self.connected):
                raise RuntimeError("Haro 连接已更换，本次任务未接收")
            if not self._writer or self._writer.is_closing():
                raise RuntimeError("Haro 连接已关闭")
            self._writer.write((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._writer.drain()

    def _connection(self):
        value = json.loads(self.connection_file.read_text(encoding="utf-8"))
        if (value.get("version") != 1 or value.get("host") != "127.0.0.1"
                or type(value.get("port")) is not int or not 0 < value["port"] < 65536
                or not re.fullmatch(r"[a-f0-9]{64}", value.get("token", ""))):
            raise RuntimeError("无效 Haro 连接文件")
        return value

    async def _run(self):
        reported = False
        while True:
            try:
                config = self._connection()
                reader, self._writer = await asyncio.open_connection(config["host"], config["port"], limit=2 * 1024 * 1024)
                challenge = json.loads(await asyncio.wait_for(reader.readline(), 5))
                nonce = challenge.get("nonce", "")
                def proof(side):
                    return hmac.new(config["token"].encode(), (side + ":" + nonce).encode(), hashlib.sha256).hexdigest()
                if (challenge.get("type") != "challenge" or not re.fullmatch(r"[a-f0-9]{64}", nonce)
                        or not hmac.compare_digest(challenge.get("proof", ""), proof("server"))):
                    raise RuntimeError("Haro 服务端认证失败")
                await self._write({"type": "hello", "proof": proof("client"), "capabilities": ["approval-summary-v1", "approval-owner-v1", "telegram-text-v1", "meme-manager-v1"]})
                packet = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if packet.get("type") != "ready":
                    raise RuntimeError("Haro 握手未完成")
                self.owners = set(packet["owners"])
                candidate = packet.get("approverOwnerId")
                telegram_owners = {owner for owner in self.owners if re.fullmatch(r"telegram:[1-9]\d*", owner)}
                self.approver_owner_id = candidate if telegram_owners == {candidate} else None
                self.inbox = packet["inbox"]
                self.targets = {item["id"]: item for item in packet.get("targets", [])}
                self.generation += 1
                self.connected = True
                await self.send({"type": "target_status", "targets": self.inspect_targets(self.targets.values())})
                self.log("已连接独立 Haro Host")
                reported = False
                await self._read(reader)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not reported:
                    self.log(f"Haro 未连接，等待重连：{type(error).__name__}")
                    reported = True
            finally:
                self.connected = False
                self.approver_owner_id = None
                self.generation += 1
                await self._cancel_memes()
                self.targets.clear()
                self.disconnected()
                if self._writer:
                    self._writer.close()
                    try:
                        await asyncio.wait_for(self._writer.wait_closed(), 1)
                    except Exception:
                        pass
                    self._writer = None
            await asyncio.sleep(1)





    # ========== 出站回执与客户端收尾 ==========

    async def _read(self, reader):
        while line := await reader.readline():
            packet = json.loads(line)
            if packet.get("type") == "meme_cancel":
                key = packet.get("routeKey")
                self.cancel_meme(key)
                task = self._meme_tasks.get(key)
                if task:
                    task.cancel()
                continue
            if packet.get("type") != "send":
                continue
            if packet.get("format") == "meme-manager-v1":
                key = packet.get("routeKey")
                if key in self._meme_tasks:
                    await self.send({"type": "receipt", "id": packet["id"], "kind": "failed"})
                    continue
                task = asyncio.create_task(self._receipt(packet, self.generation))
                self._meme_tasks[key] = task
                def finished(done, key=key):
                    if self._meme_tasks.get(key) is done:
                        del self._meme_tasks[key]
                    if not done.cancelled():
                        done.exception()  # 断线后的回执不可重放，取出异常避免悬空任务。
                task.add_done_callback(finished)
            else:
                await self._receipt(packet, self.generation)

    async def _receipt(self, packet, generation):
        try:
            result = await asyncio.wait_for(self.deliver(packet), 12)
            if isinstance(result, bool):
                result = {"kind": "ok" if result else "unknown"}
        except asyncio.CancelledError:
            if self.connected and generation == self.generation:
                await self.send({"type": "receipt", "id": packet["id"], "kind": "unknown", "code": "CANCELLED"}, generation=generation)
            raise
        except Exception as error:
            self.log(f"Haro 渠道发送结果不明：{type(error).__name__}")
            result = {"kind": "unknown"}
        await self.send({"type": "receipt", "id": packet["id"], **result}, generation=generation)

    async def _cancel_memes(self):
        tasks = list(self._meme_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._meme_tasks.clear()

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.connected = False
        self.owners.clear()
        self.targets.clear()
