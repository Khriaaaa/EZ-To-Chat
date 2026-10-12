# eztochat 单机化重构计划

日期：2026-10-12
决定：**所有方案只考虑单机（一台 Windows 包圆全部），不再考虑任何 NAS/双机形态。**
历史背景：现在的双机链路（虚机 guest 跑 `wx_collect.ps1` + NAS 经 `tools/ga.py` 拉）
作为参照保留在 git 历史里，代码路径上不再维护。

## 目标形态

一台 Windows 机器上：本机轮询 winapp UIA → 解析 → 入库 → watch 触发 → generate（带
history.db 上下文）→ 面板展示。安装包一条装完（见 §打包）。

## 现状盘点（重构起点）

链路各环已实测验证（2026-10-12）：

- `wx_collect.ps1`（285 行，guest 侧）一轮 5 步：hwnd 挑选 → scroll 翻页 → chat/title/sessions
  inspect → screenshot → window origin，整轮合成一个 `round.json` 原子落盘。里面有大量实战
  踩坑结论（见 §必须原样保留的教训）。
- `wx_collector.py`（1115 行）cmd_poll 拉取 + `_obj()` 解 winapp 的 JSON 字符串壳 +
  `parse_chat`/`window_origin`/`chat_bounds`/`selected_session`/`parse_and_store` 入库，锁
  `_StoreLock`。
- `orchestrator/watch.py` 看门狗 + `orchestrator/generate.py` 生成器 + `web/server.py` 面板 +
  `store/history.db` 历史上下文（`orchestrator/render_history.py`）。

## 重构动作

### 0. 重要修正（2026-10-12 计划评审时发现）

`wx_collector.py` 里**已经有单机分支**：`IN_VM = os.name == "nt"`，Windows 直跑时
`ga_get = shutil.copyfile`、`ga_ps = 本地 powershell`，`start` 走 `Start-Process`
（判断与微信同会话 → 直接起；跨会话才借 PsExec）。也就是说单机采集不是从零造，
是把这条已存在但没验收过的路径跑通，再按下面各节补缺口。

所以提交 1 改成：**真机验收现有 IN_VM 路径**（本地起循环 → poll → 入库 → 自检），
把验收中暴露的坑补掉；而不是新写 `local_collect.py`。ps1 收不收编进 Python 后置，
等验收看它够不够用。


### 2. wx_collector.py 瘦身

- 删：`cmd_sync/cmd_poll/start/stop` 里所有 ga.py / guest 桥相关代码。
- 留：`_obj()` 壳剥、`parse_chat`、`window_origin`、`chat_bounds`、`selected_session`、
  `parse_and_store`、`_StoreLock` —— 这些是纯解析，单机照用。
- `local_collect.py` import 这些函数，不复制。

### 3. watch / generate / 面板原样迁移

- 五条不变量（pick_session 唯一规则、validate 必须在候选链里 raise、GenLock+指纹去重、
  watch 的 seen 必须有序容器、(mtime,size) 缓存）照守，代码不动。
- provider 配置（base_url / api key / model）改为随安装包落地 + 首次启动面板里填。

### 4. 打包（installer.nsh 已有关系）

- Python 运行时 + 本项目打成 NSIS；已有 `desktop/build/installer.nsh` v3 框架
  （卸载时清 `%APPDATA%\wx-reply-assistant` 那套验收已过）。
- 要加的是 Python runtime + winapp-cli 打包，安装完第一次启动时自检（winapp 在不在、
  微信登录没）。

## 必须原样保留的教训（从 ps1 注释里逐条确认过）

1. **hwnd 挑选规则**：剔除 owned window（ownerHwnd==0 才是顶层），登录窗/提示弹窗靠这个
   区分；一个顶层都没有 = 掉登录，退回最大窗口并在 steps 里写明，不能误报为采到了。
2. **scroll 每轮一小格**：UIA 只暴露视口内已实例化的气泡，窗口没滚到底时底部新消息根本
   不在树里；一次跳一整个视口会永久漏掉中间的消息。
3. **「返回非空 JSON」≠ 采到了**：掉登录后登录窗的树照样几千字符。判据必须是
   `ChatTextItemView` 真的出现在 chat dump 里，否则记 `NO-BUBBLES`。
4. **截图直写唯一路径**（毫秒级文件名）：不要落共享固定路径再 Copy-Item —— 当年
   QGA 句柄泄漏把共享路径焊死，所有轮 shot=NONE；这条在单机 Python 直读下自然消失，
   但文件命名唯一性原则保留（同理适用于任何共享固定路径写文件）。
5. **单实例锁**：两份采集循环同时跑是历史脏数据根因。ps1 的 CreateNew 原子锁 + 陈旧锁
   清理这套单机必须留着（换文件锁就够，不用复刻 PID 存活判定，但锁语义不能丢）。
6. **每轮盯同一个窗口**：hwnd 选定后 5 步全部用 `-w <hwnd>`，不能让 winapp 自选目标。
7. **窗口会被拖动/缩放**：`window_origin` 必须每轮实测，不能用缓存值 —— 用户拖一下
   窗口，像素判左右立刻全废（历史上踩过）。

## 单机特有的真机验证项（新风险）

- 用户真机 DPI/缩放和虚机不同：`window_origin` 按 UIA physical-screen-pixels 算，
  理论 DPI 无关，但必须真机跑一轮验证。
- 用户自己同时在使用微信：截屏键鼠干扰比无人值守虚机大；窗口被切走/锁屏走
  NO-BUBBLES 降级（同 3），不写脏数据。
- 掉登录要人工扫码 ——界的硬前提，单机不变。

## 分支/提交计划

预计 3 个提交：
1. `collector/wx_collector.py` IN_VM 单机真机验收（起循环/native poll/入库/自检）+ 验收中
   暴露的坑补掉（ps1 收不收编进 Python 视验收结果）。
2. provider 配置随包落地 + 面板首填引导。
3. NSIS 打包加 Python runtime + winapp-cli，卸载清理路径校验。

先提交 1（验收现有路径，风险最小），2、3 顺后。

## 跨容器/虚机的资产交接（单机后这些全部随包走）

- `store/history.db`（26.7MB 历史库）单机部署时打包进安装包数据目录或首次导入。
- LLM provider 配置（base_url/key/model）现在在 NAS 的 Hermes config.yaml 里，单机
  要搬到自己侧的配置或首次启动面板里填。

