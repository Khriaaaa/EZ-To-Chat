# 同类项目调研：能照搬的地方（2026-10-12）

调研目标：eztochat（微信回复助手）现在卡在「读不到消息」，同时用户要的「选可采集联系人」没实现。
这份文档只收**能直接落到我们代码里**的东西，附带来源与验收方式。

## 结论先行

1. 我们踩的「UI 树只有一个空壳窗口（elementCount=1）」不是我们写错了，是微信 4.x 的**自绘渲染**导致的普遍现象：Qt Quick 之后控件不再有 HWND，UIAutomation 只能看到空壳。wxauto（7.3k star）就是因此于 2025-10 停止维护。
2. 同版本（微信 4.1.15.13）上有一个**活跃维护、Apache-2.0** 的项目走通了这条路：`fanyuantaier/wechatauto-replica`。它的两招正好是我们缺的——
   - 读消息**不走 UI 树，直接解密本地 SQLCipher 4 数据库**（只读内存扫 key + HMAC 校验 + 解密缓存）；
   - 要用 UI 时，**热激活 Weixin.dll 里的 Qt 无障碍门**（写 screen-reader 标志，按版本表记 RVA），把 `mmui::*` 树真正"叫出来"。
3. 「选可采集联系人」在同类项目里是标配，实现方式一致：**一份监听名单 + 每个会话一个持久化锚点 + 增量轮询**（`AddListenChat` / `AddListenAll`、`contact_config.json` + `message_anchor_state.json`）。这条我们可以直接照抄设计，不需要发明。

## 同类项目一览

| 项目 | 路线 | 微信 4.x | 许可 / 活跃度 | 对我们的价值 |
| --- | --- | --- | --- | --- |
| [cluic/wxauto](https://github.com/cluic/wxauto) | 纯 UIA 树 | ❌ 树被自绘打断 | Apache-2.0，7.3k star，2025-10 起停更 | 反面教材 + API 命名参考（`AddListenChat` 这套就在它这里） |
| [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | DB 解密为主 + UIA/OCR 混合发送 | ✅ 实测 4.1.12+，验证于 4.1.15.13 | Apache-2.0，407 star，活跃（v1.2.6.1） | **最高**。与我们的微信版本完全一致 |
| [Hello-Mr-Crab/pywechat](https://github.com/Hello-Mr-Crab/pywechat) | pywinauto 纯 UIA | ✅ 3.9.12 / 4.1.6+（4.1+ 走 `pyweixin`） | 2.0k star，活跃 | 模块划分（Navigator / Messages / Contacts / Monitor）、Agent Skill 打包方式 |
| [LAVARONG/wechat-automation-api](https://github.com/LAVARONG/wechat-automation-api) | uiautomation + Flask | 声称 4.0+ | 活跃，带 SKILL.md | **锁屏/RDP 处理**、元素定位表、错误码与 `--json` 输出 |
| [Avalianfeng/wechat_visual](https://github.com/Avalianfeng/wechat_visual) | UIA + 视觉指纹 + OCR | 4.x | 2 star，小 | **按联系人轮询 + 锚点去重**的完整设计 |
| [lich0821/WeChatFerry](https://github.com/lich0821/wechatferry) | Hook 注入 | ❌ 锁 3.9.12.51 | 活跃，~6.4k star | 唯一有真回调、最小化也能跑的路线；侵入性最高，仅供参考 |
| wechat-decrypt / PyWxDump 系 | DB 解密（只读） | ✅ | 活跃 | 与我们 `store/history.db` 的历史导入互补 |
| 微信 macOS WeChat-MCP | Accessibility API | macOS 4.x | MIT，133 star | 证明 `session_item_{名}` / `chat_input_field` 这类选择器跨平台一致；发送者靠气泡左右像素判 |

## 可照搬清单（按优先级）

### P0-1 读消息改走本地数据库（架构级）
**来源**：wechatauto-replica `wechatauto/db.py`。
**怎么做的**：微信 4.x 把数据放在 `xwechat_files/<账号>/db_storage/` 的 SQLCipher 4 库里（`contact.db`、`message_*.db`、`media_0.db`、`sns.db`）；每个库一个 32 字节 key，存在 Weixin.exe 进程里（`com.Tencent.WCDB.Config.Cipher`）。用**只读内存扫描**定位候选、拿 SQLCipher HMAC 规则验真、解密到临时目录并缓存（首次约 6s，之后即时）；WAL 增量合并带 frame-salt 过滤，避免 `database disk image is malformed`。
**为什么值得抄**：读消息从此**不依赖窗口可见、不依赖锁屏、不依赖 UIA 门**——我们现在最大的三个故障源一次消掉，还能直接拿到全量历史（`history.db` 那套导入器就多余了）。
**注意**：这是对我早前"DB 解密版本脆弱、不做"结论的**推翻**——同版本上有跑通且维护中的实现。key 只读扫描不注入不 hook，风险仍低于 WeChatFerry。
**验收**：同一会话，DB 读出的条数与 `messages.jsonl`/面板显示一致；锁屏状态下照样能读。

### P0-2 要用 UI 时先热激活 Qt 无障碍门
**来源**：wechatauto-replica「UIAutomation tree ✅ after hot-activation — writes the Qt accessibility gate inside Weixin.dll」。
**怎么做**：在 Weixin.dll 里按版本表定位无障碍门函数（4.1.15.13 的 RVA 为 `0xb135c38`），写入 screen-reader 标志，`mmui::*` 树（搜索框、`chat_input_field`、`mmui::ChatSessionCell`）才会真正生成；版本对不上时退化为向量化扫描找门。
**为什么值得抄**：我等于是用 Ghidra 手工走到了同一扇门前（`DB_KEY_STING`、`db_key_caller` 那几支），但没走到"写标志开门"这一步。开门后 UIA 能读能点，发送链的稳定性直接上一个档。
**验收**：同一个窗口，开门前后 `winapp ui inspect` 的 `elementCount` 从 1 变成完整子树。

### P0-3 监听名单 + 每会话锚点（用户要的「选择可采集联系人」）
**来源**：wxauto 的 `AddListenChat` / `AddListenAll`（replica 修过它的两个 bug：全局回调曾按会话注册导致漏消息、`limit=0`/`offset=-1` 曾静默返回错误结果——分页参数现在 fail-closed 返回 `[]`）；wechat_visual 的 `contact_config.json` + `debug/message_anchor_state.json`。
**怎么做的**：目标联系人写在配置里，**每联系人一条"最后已读"锚点**持久化；首次读取以当前最后一条为锚点，之后只返回锚点之上的；跨进程、断点续跑都靠锚点 + 去重保证不重不漏。
**怎么落到我们这**：`store/.targets.json`（名单，可空 = 跟随当前打开窗口）+ `store/.anchors.json`（每会话锚点），替代现在"只有 pick_session 一条规则"。这正好补上 `docs/eztochat-fix-plan.md` 3.1 想做的白名单。
**验收**：名单里三个会话，只有它们的消息进建议；重启进程后不重复、不跳读（可写成本地测试，参照我们既有的 37 项自检风格）。

### P1-1 锁屏 / 断开会话的判定与恢复
**来源**：LAVARONG/wechat-automation-api「远程桌面与锁屏」章节。
**要点**：Windows 锁屏时 UIA 通常不可用；RDP 直接关窗口会让鼠标键盘和 UIA 一起失效，正确做法是 `tscon <会话ID> /dest:console` 把会话交回本地控制台并保持运行（项目里是 `disconnect_rdp.bat`）。
**怎么落到我们这**：采集器现在一律报「可能已掉登录」，误导（用户就被我带偏过一次）。改成三分类判定：**会话被锁** / **窗口最小化** / **没有打开的聊天**，每种给不同的提示与自动恢复动作（锁屏 → 提示去解锁或执行 tscon；最小化 → 调 `ShowWindow(h,9)` 恢复）。
**验收**：故意锁屏，面板显示「会话已锁定，采集暂停」，解锁后自动恢复，不再出现"掉登录"字样。

### P1-2 发送前 read-back 校验 + 剪贴板输入
**来源**：wechatauto-replica（`WxParam.SEND_CONTENT_RATIO` 0.6，回车前先比对输入框内容比例，89 项离线校验 + 实机逐字回读）；LAVARONG（剪贴板 + Ctrl+V 比 SendKeys 更适合中文）。
**怎么落到我们这**：填入通道目前默认关，但将来打开时必须先"回读确认内容进了输入框"再回车，绝不能盲发。
**验收**：构造一次"输入框只进了一半字"的场景，回车必须被拦住。

### P1-3 UIA 线程初始化与宿主兼容
**来源**：wechatauto-replica 更新日志——在当前线程幂等调用 `CoInitializeEx`，修掉从后台线程/宿主进程里实例化时 `CoInitialize not called / cannot load UIAutomationCore.dll` 的崩溃；只认加载了 `Weixin.dll` 的进程的主窗口，避免噪音告警。
**怎么落到我们这**：我们的采集是面板线程 / Electron 子进程拉起来的，正好是这类崩溃的高发场景；照抄这两条能省掉一类难查的偶发失败。

### P2-1 非正式环境的 UIA 激活
**来源**：LAVARONG「系统辅助功能启用」。
**要点**：精简版 / Ghost 系统上 UIA 可能整体不可用，用 Win 键调出「讲述人」开一次再关掉即可激活底层接口。
**怎么落到我们这**：写进首启自检——UIA 探测失败时自动提示这一步（或自动做一次）。

### P2-2 产品化：体检 + 菜单 + 错误码 + Agent Skill
**来源**：wechatauto-replica（无参数运行 = 先体检再给编号菜单，子命令中英双语，`体检`=doctor，`--json` 稳定输出）、LAVARONG（明确错误码如 `CONTACT_NOT_FOUND`，退出码 0/非 0 语义清晰）、pywechat / LAVARONG 都随仓库带 `SKILL.md` 供 agent 安装。
**怎么落到我们这**：用户的原话是「我配置好了我压根不知道怎么用」。面板加一个 **体检/诊断**入口，把「微信登没登、窗口可见吗、目标名单有没有、模型通不通、采集有没有在写盘」一次列清；再给仓库加 `SKILL.md`，让 agent 能一句话装好。

### P2-3 版本漂移防护
**来源**：yage.ai 调研（「微信自动更新是最常见的故障来源」；UIA 路径要么锁版本要么接受随时失效）。
**怎么落到我们这**：首启检查微信版本并记录；版本变了就在面板上主动提示"UI 路径可能失效，建议重启采集或等适配"，而不是让用户对着一个不出结果的界面猜。

## 明确不抄的

- **Hook / 协议注入**（WeChatFerry 那一类）：功能最全（有真回调、最小化可用），但要锁死微信版本（3.9.12.51）且侵入进程，风险等级高一档。我们产品定位是"只读 + 只给建议"，不碰。
- **营销号式高频群发**：与本产品无关，且是封号高发场景。
- **OCR 作为主读取路径**：只能拿到屏幕可见内容、要不停滚动，延迟和准确率都不如 DB 直读；保留为 DB 路径失效时的兜底即可。

## 风险与合规

- 三条路线里，**DB 直读**（只解密本地文件、不碰服务端）检测面最小；**UIA** 与人工操作在系统层几乎无区别，公开案例里低频个人使用被封号的报告几乎没有；**Hook** 的检测面明显更高。
- 我们的定位（只读、建议不自动发送、不改协议）天然落在风险最低的一侧，改动时别越线：不做自动发送、不做群发、不碰协议。
- 抄代码前核对许可：wechatauto-replica / wxauto / pywechat 均为 Apache-2.0，可带署名复用；macOS WeChat-MCP 为 MIT。

## 参考

- https://github.com/fanyuantaier/wechatauto-replica
- https://github.com/cluic/wxauto
- https://github.com/Hello-Mr-Crab/pywechat
- https://github.com/LAVARONG/wechat-automation-api
- https://github.com/Avalianfeng/wechat_visual
- https://yage.ai/share/wechat-uia-platform-feasibility-survey-20260327.html
