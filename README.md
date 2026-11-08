# AstrBot Haro 插件

把主人私聊交给独立 Haro Host，共用长期记忆和原生 thinker；支持文本、附件、进度、一次审批和文件回传。AstrBot 管平台登录与消息呈现，Haro Host 管身份、记忆和执行。插件只连接 Host，不启动或关闭 Node。





## 安装

计划发布地址：[MonkeyUnderMountain/astrbot_plugin_haro](https://github.com/MonkeyUnderMountain/astrbot_plugin_haro)。当前已完成本地仓库准备，尚未创建或推送这个远端；以下链接安装／在线更新需要先完成发布。

发布后，在 AstrBot 的插件页选择“从链接安装”，填入上述仓库地址。也可以手动把下面八个文件放到 `data/plugins/astrbot_plugin_haro/`，然后重载插件：

| 文件 | 职责 |
|---|---|
| [main.py](main.py) | 注册事件、核验来源、提取历史引用、复制当前附件、呈现回复和固定目标发送 |
| [bridge.py](bridge.py) | 本机 TCP JSONL、双向认证、回执和自动重连 |
| [memes.py](memes.py) | Meme Manager 5.3.5／5.3.6选图、独立目的地路由、授权图片冻结、历史确认与清理 |
| [targets.py](targets.py) | Telegram／QQ官方／微信本人目标、平台检查、共用图片出口和单次QQ请求 |
| [metadata.yaml](metadata.yaml) | 名称、版本与更新仓库地址 |
| [_conf_schema.json](_conf_schema.json) | 插件配置表单 |
| README.md | 本说明 |
| [CHANGELOG.md](CHANGELOG.md) | 按版本的更新内容、兼容要求和已知限制 |

Haro Host 另行准备，源码在 [Haro 开发仓库](https://github.com/MonkeyUnderMountain/assistant-bot-haro-develop)。Host 要求 Node.js 24.22.0+；插件使用 AstrBot API 与 Python 标准库，Telegram 功能使用 AstrBot Telegram 适配器已有的依赖。

AstrBot API 曾静态核对4.29.4；当前 TCP 客户端尚未在真实实例完成安装／更新验收。





## 连接 Haro

Haro 和 AstrBot 是两个各自运行的程序：**Haro 先打开一个本机端口等待，AstrBot 中的这个插件再主动连接它；输入和回复都走连接。** Host 就是独立启动的 Haro 程序。当前使用本机 TCP 长连接，每条数据编码为一行 JSON（JSONL）。

你在 Telegram 发消息后的路径是：Telegram → AstrBot → 本插件 → 本机 TCP → Haro → thinker；回答由 Haro 经同一连接交回插件，再由 AstrBot 发回原私聊。

### 配置分别在哪里填写

先分清三处配置：

| 在哪里填写 | 填什么 |
|---|---|
| AstrBot 的 Telegram 平台配置 | Telegram Bot Token 与机器人连接信息 |
| Haro 的外部 config.json | thinker、记忆路径、runtimeDir、channels.astrbot.owners |
| 本插件的配置表单 | 只填 haro_connection_file，即 Haro 自动生成的连接文件绝对路径 |

### 启动独立 Host

先准备 Haro 的外部 config.json 和人工身份文件，再在 Haro 源码目录启动 Host。下面用同一套虚构路径举例，命令为 Bash，路径按运行 Node 的实际平台填写：

```bash
node src/server.mjs --config "C:/HaroData/test/config.json"
```

Haro 配置需要包含非空的 `channels.astrbot.owners` 列表，例如 `["telegram:123456789"]`，把数字换成你的用户 UID；没有另一个 enabled 开关。身份、记忆与 thinker 的完整准备见 Haro 开发仓库的 `docs/guides/astrbot-setup.md`。

需要模型凭据文件时，在 node 后、src/server.mjs 前加入 `--env-file="C:/HaroData/test/secrets.env"`；fixture 接线测试不需要这个参数。

### 填写插件表单并检查连接

例如 config.json 位于 C:/HaroData/test/config.json，里面的 runtimeDir 为 ./runtime，Host 就会**自动生成 C:/HaroData/test/runtime/astrbot.json**。看到 Host 输出 ready、该文件已出现后，在 AstrBot 的 Haro 插件配置表单里填写这个文件的绝对路径：

```json
{
  "haro_connection_file": "C:/HaroData/test/runtime/astrbot.json"
}
```

config.json 是你写给 Haro 的配置；astrbot.json 是 Haro 写给插件的连接资料。**插件表单指向 astrbot.json，不指向 config.json。** 连接资料含本机地址、自动选择的端口和认证令牌；不需要手写内容或手填端口，聊天内容也不通过这个文件传递。

v0.1.2 起可以先安装插件再配置：未填或路径不是绝对路径时，插件正常加载，日志提示去填写，配置按钮仍可用。保存表单后 AstrBot 自动重载插件，合法路径会启动后台连接；连接成功时日志出现“已连接独立 Haro Host”，再从 owners 对应的 Telegram 用户私聊 Bot，发 /status 查看 Haro 状态。owners 决定谁能输入，普通回复自动回来源；主动外发的 targets 可在接通后另行配置。

### 路径要求与后台重连

这个连接文件含本机凭据，保存在私人目录。AstrBot 和 Host 须能看见相同文件路径与同一回环网络；当前只连接 `127.0.0.1`，即这台电脑自己，不支持直接连接远程 Host。Haro 启动终端需保持运行。

路径配置有效后，从首次连接起就每秒重读连接文件并重试。Host 暂未运行或连接文件尚未生成不会导致插件加载失败；Host 稍后启动即可自动连接。插件出现在已安装列表不代表已经连上，首次连接前还没有 Host 提供的主人列表，不接管聊天。关闭或重载插件只断开客户端，Host 继续运行；旧审批失效，未知结果不会自动补发。





## 更新

发布新版本后，在 AstrBot 的“已安装”页面找到 Haro，点击“更新”获取代码；“重载”只重新加载已有代码，不下载版本。手动安装的副本也可重新覆盖上述八个文件后重载。版本变化先查看[更新日志](CHANGELOG.md)；下面保留关键升级说明。

插件与 Host 分别更新。更新 Haro JavaScript、外部配置或凭据文件后，应正常关闭并重新启动 Host；插件会重连。旧版插件若曾负责启动 Node，应先正常卸载并确认旧子进程已退出，再启动独立 Host 和配置本版插件，避免两份实例使用同一份记忆。

v0.1.2 修复首次安装的配置入口：空／相对路径保持正常加载，合法路径后台连接，首次离线不再因30秒等待而加载失败。本机协议和消息路由保持不变。若此前已加载失败，更新插件后点击重载，再打开配置填写路径。

当前messageContext可选保存本轮平台消息、被回复消息和原生quote；Haro业务入口在认证后、入队前校验和快照一次。Host与插件须配套线路协议version:2，旧插件不再连接；发现文件仍为version:1，插件表单没有新增项。

当前v0.1.9继续只显示三行简短审批说明＋允许本次／拒绝按钮，不发送详情附件。hello／ready只核对version:2；ready提供唯一数值Telegram审批人，来源、送达确认、取消与超时继续保留。QQ使用已有登录会话直接单次HTTP提交，接口兼容在开发测试核对，不运行源码指纹审查。源码更新不表示已发布、安装或重载日用实例。





## 使用与排查

普通回复固定回可信输入来源；主人身份由 Host 的 owners 配置决定。主动发送只允许人工绑定的本人Telegram／QQ官方／个人微信targetId，插件再次核对具体实例、UID和固定origin。

可以先发送 `/status` 检查连接，再用虚构内容执行 `/remember 虚构薄荷每三天浇水`、`/new`、`读取记忆`。插件加载失败看 AstrBot 日志；连接失败核对 Host ready、连接文件路径、网络和路径共享；无回复时核对平台主人 ID 与 AstrBot 当前配置是否允许 Haro 插件。

在Telegram回复旧消息并附上问题，或使用原生引用片段时，插件分别提取可获得的旧消息与片段；缺失、降级和截断会标记，不把处理时间冒充历史时间。原文／片段各最多4000 Unicode码点，附件描述最多1000码点。历史附件只传类型与caption／文件名，不下载、不作为新附件。仅引用无新正文时，输入会提示thinker询问处理意图；引用中的命令不参与程序命令解析，也不改变主人、审批或回复目标。其他AstrBot平台仅有通用Reply降级，本能力尚未在真实Telegram流程验收。

引用信息没有专门入库，也不能据此查询旧回复的投递失败原因；模型回答可能复述引用并照常存档。Host已有/delivery与delivery_query，可查询从记录开始的新投递，不能倒查旧无记录故障。Haro插件不追加下载旧引用附件，但AstrBot更早的预处理可能已本地化历史媒体。

收到审批时，只看简短的操作、已知原因与目录说明，并选择允许本次或拒绝；不显示原生命令参数／补丁全文，也没有详情文件入口。只有卡片发送已确认、原来源主人同意且任务仍有效时才允许本次；5分钟、停止、断线、发送失败或unknown均不放行。提前点允许仍要等回执，此前再点拒绝可以撤销。规则已禁止的操作自动拒绝，没有允许按钮；无按钮的有效单次请求可用/approve ID accept或decline。

审批不写临时文件，也不新增审批历史库；早期文件方案已移除。当前只允许Host指定的唯一Telegram主人在原来源审批，其他平台明确拒绝；实际投递和按钮仍须验收，Context返回True不等于主人已收到或阅读。

Haro专属AstrBot配置档的完整模板、覆盖项、生成器及应用步骤在[开发仓库的配置指南](https://github.com/MonkeyUnderMountain/assistant-bot-haro-develop/blob/main/docs/guides/astrbot-haro-profile.md)。该档利用AstrBot路由、插件集和平台能力，关闭内置模型重复响应；它不替代平台登录、Haro外部配置或本插件的连接表单。





## 开发与发布

源码统一在 Haro 主仓库的 `integrations/astrbot_plugin_haro/` 维护和联调。在主仓库运行：

```bash
node scripts/sync-astrbot-plugin.mjs
```

命令只同步八文件：main.py、bridge.py、memes.py、targets.py、metadata.yaml、_conf_schema.json、README.md、CHANGELOG.md，到`.release/astrbot_plugin_haro/`；该目录作为小仓库的工作目录，Git历史单独维护。同步不创建远端、不提交或推送。每次发布同时更新metadata、注册版本和CHANGELOG中的版本记录，说明变更、兼容要求与已知限制；同步脚本要求日志最新版本与metadata一致。完成主仓库回归与Python检查后，再检查发布副本并按授权提交、推送。

目前尚未指定对外许可证，由主人在公开分发前确定。





## 2026-11-06 统一发送（v0.1.5未发布）

配套Host只提供message_send({requestId,targetId?,content?})，content为text/file/image/meme；省略targetId回当前可信来源，主动消息只选人工本人目标。thinker决定内容、边界与顺序，调用立即提交，不再按空行拆final或登记延后补图。每轮最多20个新意图，当前图库最多一张；同目的地共享FIFO与2–5秒节奏，原生Telegram单条≤4096个UTF-16单位，旧来源保守≤1200。整条完整代码围栏可转PRE，混合内容原样纯文本。

文字独立归档，旧v1控制账本防重；文件按类型／名称／冻结bytes防重，实际发送同份内容，尝试后清理。含content调用后final不再另发，可正常为空；只查询旧ID不抑制final。failed/unknown不重发，不能换ID或别名规避；原生Telegram文字用现有客户端取有效平台ID，其他路径仅渠道确认。

Host所有业务出站共用MessageSender，Python的离线、失败、话题、SDK、Context和Meme图片提交全部走HaroPlugin._send。控制和审批不等待普通FIFO。审批只允许配置owners中唯一数值Telegram主人号，零个或多个即关闭；QQ、终端及其他号不能审批，不跨来源转发。按钮和文字审批都检查，旧Host缺审批人字段时也拒绝。

连接文件版本和插件表单不变，schema6／备份v7不变；Host与插件需配套更新，本轮未覆盖或重载日用实例。七文件本地发布包另同步，不代表远端发布或真实平台验收。





## 2026-11-07 主动目标与定时（v0.1.6未发布）

新增targets.py，主动目标支持本人Telegram、QQ官方、个人微信；仍由人工填写owners及targets，模型只选择已绑定ID。QQ只执行经兼容检查的一次HTTP请求，响应ID有效才记平台确认；媒体首版≤10MiB。微信需要AstrBot已有真实入站上下文，发送不承诺固定有效期或配额。

主动目标有按send包ID的取消门禁，读取循环不会被上传阻塞；Host停止／断线后不能继续下一步消息提交，SDK鉴权等待后也重查。已经提交HTTP的回执保留真实结果。定时Telegram任务仍只能由唯一主人在原来源审批，QQ／微信不能审批。

配套Host提供schedule_create/list/update/cancel，到点进入原有单队列；Host须运行，离线错过不补跑，恢复备份后计划暂停。存储现为schema7／haro-memory-v8，旧库须离线预览、显式转换。本轮源码与本地导出不代表已更新正式实例或验证真实平台。





## 2026-11-07 跨平台图库图片（v0.1.7未发布）

图库路由独立于Telegram文字呈现。Telegram／QQ官方／个人微信的可信普通私聊，以及人工绑定的本人主动目标，均可用meme_categories查询目的地分类，并用message_send的meme内容立即发送一张。主动目标不继承触发来源的包或人格；目标缺图库时明确不可用。Host与插件须配套支持meme-manager-v2，旧协议不回退；连接文件、表单与schema7／v8不变。

Meme Manager只接受已核验的5.3.5与5.3.6，只负责选图和转换；其他版本或未加载会返回MEME_VERSION_UNSUPPORTED。Haro核验其授权图库来源、图片格式与大小，冻结后交给普通图片共用的出口。QQ上限10MiB、有效响应ID才记平台确认；Telegram／微信为渠道确认。仅成功后记录上游历史；历史或清理失败单独写日志。取消阻止后续提交，已经发送的请求保留真实结果，failed／unknown不自动重发。

选图、发图不需要额外代码审批，模型只提供分类，不获得素材路径或新增文件权限。正式实例尚未更新，真实三平台显示仍待部署验收。
