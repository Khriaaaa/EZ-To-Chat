# 安装教程

README 里的「快速开始」只有五步 —— 因为它假设你已经有一个「能读 UIA 的 Windows」。
这份教程把那半边补上：从一台装着微信的机器，到网页上出现三条候选。

不想逐节读的话，两个脚本会把环境准备好。Linux/NAS 侧：

```bash
bash scripts/install.sh --demo     # venv + 依赖 + config.local.yaml + 语法自检 + 演示数据
```

Windows 侧（默认体检完，缺什么就自己在后台下什么；加 `-NoAuto` 才是只体检）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1
# 只想体检、不让它动系统：  ... -File scripts/setup-windows.ps1 -NoAuto
```

下面每节讲的是它们做了什么，以及它们不做的那部分。

## 动手之前：你可能不需要这份教程

如果只有一台 Windows，**不需要**下面这些步骤 —— 装桌面版就行：

```
https://github.com/Khriaaaa/wx-reply-assistant/releases/latest/download/wx-reply-assistant-setup.exe
```

双击装上，桌面出现「微信回复助手」，打开就是面板。安装包里带着 Python、
三个依赖和读屏工具（winapp.exe），不依赖系统里的任何东西，也不要管理员权限。
数据在 `%APPDATA%\wx-reply-assistant\`。第一次打开会让你填一个模型接口（API Key）。

这份教程剩下部分是给「微信关在一台虚机里、采集和面板在另一台机器上」这种
更折腾也更安全的用法 —— 它把微信和你的日常环境隔开，代价是要维护两台机器。

## 0 装的是什么，装在哪

| 部分 | 跑在哪 | 干什么 |
| --- | --- | --- |
| 微信 + 采集脚本 | 一台 Windows（虚拟机或实体机） | 读屏，把当前会话写成一圈快照 |
| 采集器 / 编排 / 面板 | 一台 Linux（这里是 NAS） | 收消息、调模型、出网页 |
| 模型端点 | 网络的另一头 | 只看「最近若干条」，回三条候选 |

数据是单向的：Windows 那侧只被读，不接收任何写入。填入通道默认关闭，见 README 的「边界」。

## 1 准备清单

- 一台 Windows 10 / 11，装好微信（实测版本 4.1.15.13）
- 一个能读 UIA 的命令行工具（实测 winapp-cli 0.7.1，见 2.2）
- **跑采集器这台机器要能 ssh 到一个能 `sudo virsh` 的宿主** —— 命令是通过宿主上的
  `virsh qemu-agent-command` 送进虚机的，不是直连 Windows。虚机就建在这台宿主上
- Python 3.11+（依赖锁文件 requirements.lock 的要求），三个第三方包：`pillow`、`numpy`、`pyyaml`，其余全是标准库
- 一个 OpenAI 兼容的 `/chat/completions` 端点（地址 + key + 模型名）

## 2 Windows 侧

### 2.0 一条捷径：双击那个 exe

嫌下面几步手动敲命令麻烦，就双击 **`wxreply-setup.exe`** —— 它把 2.1 / 2.2 / 2.3 要做的事
一次跑完，把结果摆在一个窗口里：

下载（一直指向最新一个版本，不用改链接）：

<https://github.com/Khriaaaa/wx-reply-assistant/releases/latest/download/wxreply-setup.exe>

- 逐项体检：QEMU guest agent、winapp CLI、PsExec64、工作目录、电源（别睡眠）、微信窗口
- **缺什么就自己下什么**，不用你点：窗口一出来就开始下 GitHub Releases 上的 winapp CLI
  （~94 MB）和 live.sysinternals.com 上的 PsExec64，底下有进度条；下完自动重新体检一遍，
  标题从「有 N 项要处理」变成「全部通过」。睡眠/关屏/关盘也会顺手设成「从不」
- 不想让它动系统就加 `-NoAuto`（只体检、只报缺什么）；下载源要换成镜像或内网，
  用 `-WinappUrl` / `-PsexecUrl` 指过去
- 双击时会弹一次 UAC —— 改电源计划和写 `C:\dl` 都要管理员，这是正常的
- 没签名，第一次跑可能被 SmartScreen 拦（「更多信息」→「仍要运行」），杀软
  也可能提示「未知发布者」。源码就是 `scripts/setup-windows.ps1`，不放心可以自己编：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1
# 产出 scripts\dist\wxreply-setup.exe（要用 ps2exe，会从 PSGallery 装）
```

不想用 exe 就走下面 2.1 起的手动流程，或者直接跑那个 .ps1 也一样。

### 2.1 让屏幕别睡

读屏靠 UIA 加截图，锁屏、最小化、睡眠都读不到。虚拟机里把电源计划改成「从不睡眠」，
微信窗口保持开着（不用保持最前）。

### 2.2 装一个能读 UIA 的命令行工具

本项目所有窗口操作都走同一套命令。用的是微软的 **winapp CLI**（开源，`microsoft/WinAppCli`），
实测版本 0.7.1。在 Windows 上装：

```powershell
winget install Microsoft.winappcli --source winget
winapp --help                      # 装完验一下
winapp --cli-schema                # 想看完整命令表，这个是机器可读的
```

`winget` 不是每台 Windows 都有（实测那台 Win10 就没带 App Installer）。
没有就走压缩包：[GitHub Releases](https://github.com/microsoft/WinAppCli/releases/latest)
下 `winappcli-x64.zip`（约 94 MB），解压到 `C:\winapp-cli`。
**或者干脆交给 `scripts/setup-windows.ps1`（默认行为），它会去 Releases 取最新版、解压好；
国内拉不动 GitHub 时用 `-WinappUrl <镜像地址>` 换源。**
官方仍在 public preview，命令可能会变；UI 那部分的文档在
[Microsoft Learn](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation)。

要用到的就这几个命令：

| 命令 | 用途 |
| --- | --- |
| `ui list-windows --json` | 找 `Weixin` 窗口，拿 HWND |
| `ui inspect chat_message_list -a Weixin -d 12 --json` | 读当前聊天的正文 |
| `ui inspect session_list -a Weixin -d 3` | 读会话列表，确认打开的是哪个会话 |
| `ui scroll chat_message_list -w <HWND> --wheel -1 --json` | 往下拨一格 |
| `ui screenshot -w <HWND> --output <路径> --json` | 截整个窗口，判发送方用 |

换成别的实现也行，只要这几个命令对得上。名字不一样就改 `collector/wx_collect.ps1` 里的
`Invoke-Winapp` 和 `$Winapp` 两处。

### 2.3 验一下它真读得到

```
C:\winapp-cli\winapp.exe ui inspect chat_message_list -a Weixin -d 12 --json
```

期望输出里有 `mmui::ChatTextItemView`，`name` 就是消息正文；列表顺序即时间顺序。
只有 `ChatItemView`（`name` 像 `昨天 08:36`）说明那是时间分隔条，没有真消息 ——
多半是微信没打开任何会话。

顺便记一条：UIA **不告诉你哪句话是谁说的**，所以发送方还得靠气泡位置和引用关系判，
细节在 [notes.md](notes.md) 第 1 节。

### 2.4 三个写死的路径

要么照这个摆法，要么改代码：

| 路径 | 写在哪 |
| --- | --- |
| `C:\winapp-cli\winapp.exe` | `collector/wx_collect.ps1:24` |
| `C:\dl\PsExec64.exe` | `collector/wx_collector.py`（启动/停止采集的两处） |
| `C:\dl\wxc`（脚本与快照的工作目录） | `collector/wx_collector.py:41` |

PsExec64 是 Sysinternals 的小工具，用来把脚本注入交互会话 —— 3.3 说为什么非它不可。
winget 装的 winapp 不一定落在 `C:\winapp-cli`，先 `where winapp` 拿到真实路径再改
`$Winapp`。

## 3 让这台机器够得着 Windows

### 3.1 虚拟机（QEMU / KVM）

虚机里装好 qemu-guest-agent 并让它常驻（virtio-win 里的 guest agent）。
在宿主上验：

```bash
virsh domuuid <虚机名>                                      # 这个 UUID 要填进配置
sudo virsh qemu-agent-command <uuid> '{"execute":"guest-ping"}'
```

第二条返回 `{"return":{}}` 就算通。

### 3.2 实体机

实体机没有 QGA 可走，用 OpenSSH Server + PsExec。`config.local.yaml` 里的 `vm.ssh`
填 Windows 的账号，`qga_uuid` 留空。

**这条路线实测没走完。** 项目是在虚机 + QGA 上验出来的；实体机只确认了「SSH 能进 +
PsExec 能注入 session 1」这一步，采集循环本身没在实体机上跑过，坑可能不止一个。

### 3.3 为什么非要注入交互会话

通过 QGA（或任何后台服务）起的进程都在 session 0，而微信窗口在用户登录的 session 1
里 —— 从 session 0 读 UIA 拿回来是空的，看起来像「工具坏了」。所以每个窗口操作都要：

```
PsExec64 -accepteula -nobanner -i 1 -d powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dl\wx_collect.ps1
```

`-i 1` 就是「进 session 1 跑」。采集脚本自己管这件事，你只需要把 PsExec64 放对地方。

## 4 把项目放上去

```bash
git clone https://github.com/Khriaaaa/wx-reply-assistant.git
cd wx-reply-assistant
python3 -m venv .venv && .venv/bin/pip install pillow numpy pyyaml
```

目录结构见 README 的「结构」一节。采集器（PowerShell + Python）和编排层都可以单独跑，
只有生成那一步需要网络。

## 5 填虚机连接参数

```bash
cp config.example.yaml config.local.yaml
```

```yaml
vm:
  ssh: user@nas-host        # 能 sudo virsh 的那台，不是 Windows
  ssh_pw: ""                # 留空则走 ssh 公钥认证
  qga_uuid: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # virsh domuuid 拿到的
```

不想写文件也可以用环境变量，优先级更高：`WXREPLY_SSH`、`WXREPLY_SSH_PW`、
`WXREPLY_QGA_UUID`、`WXREPLY_WORK`。

`config.local.yaml` 已经在 `.gitignore` 里，只留在你这台机器上，不会被传到任何地方。

## 6 接模型

两条路，随便挑一条。

**网页上填（省事）** —— 第一次打开面板会自己弹配置框：

- 「厂商」里预置了 16 家（DeepSeek、阿里云百炼、智谱 GLM、月之暗面 Kimi、火山方舟、
  百度千帆、讯飞星火、MiniMax、硅基流动、阶跃星辰、腾讯混元、商汤日日新、
  美团 LongCat、蚂蚁百灵，外加「本地部署」和「自定义」），接口地址已经按官方文档填好
- 粘一个 API Key 进去，点「拉取模型列表」从你自己账号里取可用模型，选一个
- 点「保存并开始」之前它会**真连一次**：先 `GET /models`（不花 token），
  拿不到再用 1 个 token 试一次对话。连不上就不给存，报错直接是厂商原话
- 填完写在 `store/.llm.yaml`（权限 600，已被 `.gitignore` 排除）。面板只会回显 Key 的尾四位，
  不会把明文还给浏览器。之后想换厂商/换 Key，点右上角「设置」

预置表在 `orchestrator/providers_cn.py`，每条都带申请入口和官方文档出处；
端点是逐条实测过的（无 Key 请求回 401 才算通）。厂商改了地址就更新那个文件 ——
表头上的 `VERIFIED_AT` 是这轮核对的日期。

**自己写配置** —— 需要一个 OpenAI 兼容的 `/chat/completions`。配置按这个顺序找：

1. 环境变量 `WXREPLY_CONFIG` 指向的 yaml（给了就只认它）
2. `store/.llm.yaml`（就是网页上填的那份）
3. 项目根下的 `config.local.yaml`
4. 默认的 `/opt/data/config.yaml`

谁拼得出 provider 链就用谁 —— 所以 `config.local.yaml` 只填了 `vm` 段也没事，
会继续往下找，不会把模型那半遮蔽掉。

要的就是三样东西：端点地址、key、模型名。在 `config.local.yaml` 里这样写：

```yaml
custom_providers:
  - name: myllm                       # 自己起名
    base_url: https://api.example.com/v1
    api_key: "sk-..."                 # 你的 key

model:
  default: your-model-name            # 你要用的模型名
  provider: custom:myllm              # 对应上面的 name
```

想再加一层保险，就补 `fallback_providers`（主 provider 挂了按顺序往下试）：

```yaml
fallback_providers:
  - provider: custom:myllm2
    model: your-model-name
    base_url: https://api2.example.com/v1
```

先别急着发请求，拼出来看一眼：

```bash
.venv/bin/python3 orchestrator/generate.py --session <会话名> --dry-run --verbose
```

`--dry-run` 只打印 prompt，不调 API。确认里面是你要的那段对话，再去掉它。

候选链默认按顺序试多个 provider，整条链有总时间预算（默认 150 秒，`WXREPLY_BUDGET`
可改）—— 预算不够时会直接跳过后面的备用 provider，而不是让整轮被外层超时杀掉。

## 7 先看界面（合成数据）

不用连虚机就能看到界面长什么样：

```bash
python3 tools/make_demo_store.py --out store     # 16 条演示消息 + 1 条建议
python3 assistant.py up --port 8801 --no-collector
```

浏览器打开 `http://<这台机器的IP>:8801`，首次启动的随机密码写在 `store/.panel_password`。
面板默认只监听 127.0.0.1，要让别的机器访问得加 `--lan`（走明文 http，聊天记录和 Key 都在
局域网里明文，别往公网映射）。
（`--no-collector` 是「不连虚机」，只想看界面时加。）

## 8 真采集

```bash
python3 collector/wx_collector.py start     # 推脚本进虚机并起循环
python3 collector/wx_collector.py status    # 心跳 + 日志尾 + 已存条数
python3 collector/wx_collector.py poll      # 手动搬一轮
```

每轮的日志里这几个字段都要 `ok` 才算一轮干净：

| 字段 | 含义 | 不正常时 |
| --- | --- | --- |
| `chat` | 聊天正文读到了 | `NO-BUBBLES` = 微信掉登录，或没打开会话 |
| `title` / `sessions` | 会话名与列表 | 报错多半是窗口选错了 |
| `shot` | 截图落到唯一路径 | 失败则发送方只能记 `unknown` |
| `window` / `scroll` | 窗口原点、向下拨一格 | `element_not_found` = 选错窗口 |

存下来的是 `store/messages.jsonl`，每行一条：
`ts_utc` / `session` / `sender`（`me` 或 `them`）/ `text` / `time_hint` / `fp`。

想连生成一起跑，直接：

```bash
python3 assistant.py up            # 采集 + 自动生成 + 面板，一把起
python3 assistant.py status
python3 assistant.py down
```

`watch` 线程的触发条件是「对方的新消息 + 静默 1.2 秒」—— 对方连发三条只会生成一次。

## 9 让它常驻

`assistant.py up` 是后台起进程，不适合交给 systemd。要长期跑，两个前台进程各管一个：

```ini
[Unit]
Description=微信回复助手 · 面板
[Service]
WorkingDirectory=/path/to/wx-reply-assistant
ExecStart=/usr/bin/python3 web/server.py --port 8801 --check-interval 45 --lan
Restart=on-failure

[Install]
WantedBy=default.target
```

另一个 unit 把 `ExecStart` 换成 `/usr/bin/python3 orchestrator/watch.py`。
面板自带自检线程，发现对方来了新消息会自己重新生成一轮，所以 watch 可以不起。

## 10 验收清单

按顺序过一遍，哪一步不对就停在那一节：

| # | 做什么 | 期望 |
| --- | --- | --- |
| 1 | 宿主上 `virsh qemu-agent-command <uuid> '{"execute":"guest-ping"}'` | `{"return":{}}` |
| 2 | 虚机里手动跑一次 `ui inspect chat_message_list` | 有 `mmui::ChatTextItemView` |
| 3 | `python3 tools/make_demo_store.py` 后起面板 | 网页上有三条候选 |
| 4 | `python3 orchestrator/generate.py --session <名> --dry-run` | 打出完整 prompt |
| 5 | `python3 collector/wx_collector.py poll` | `new>0`，且 sender 是 `me` / `them`，不是 `unknown` |

第 5 条是全链路的分水岭：`unknown` 说明截图那一步没成功，看第 8 节的 `shot` 字段。

## 11 排障

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 所有消息都存成 `unknown` | 截图失败（`shot` 非 ok），左右位置判不出来 | 看 `store/cache/` 里有没有当轮截图；工作目录要能被覆盖写 |
| `chat=NO-BUBBLES` | 微信掉登录，或没打开会话 | 登回去、点开一个会话 |
| `scroll=FAIL`，`element_not_found` | 机器上有多个微信窗口，工具自动选了最大的那个 | 保证滚动命令带 `-w <HWND>`，HWND 每轮从 `list-windows` 现取 |
| 脚本里的中文变乱码、引号被砸坏 | `.ps1` 少了 UTF-8 BOM，PS 5.1 按 ANSI 读 | 写文件时前置 `EF BB BF`（`tools/ps1run.py` 已经这么干） |
| `guest-exec: ... Permission denied` | 长脚本塞进了 `-EncodedCommand`，跟内容无关、跟长度有关 | 落地成文件用 `-File` |
| 推大脚本失败 | 整段 base64 塞进一条 QGA 命令行，超过约 8KB 被拒 | 用 `tools/ga.py put` 分片写，写完核对虚机上的实际文件大小 |
| 「文件正被另一进程使用」 | QGA 读文件会在虚机侧漏句柄，那个路径之后写不进去 | 每轮用唯一文件名 + 一个指针文件指向当轮结果 |
| 模型返回空字符串、像没回话 | 带推理的模型 `max_tokens` 给小了 | 调大 `max_tokens`，或换模型 |
| 修了历史行，重启后少了一截 | 直接 `open(w)` 写，中途被杀就只剩前 N 行 | 已经改成临时文件 + 原子替换 |
| 面板候选一直是空的 | 没有「对方的新消息」触发（最后一条是你发的就不生成） | 正常行为；想手动跑一次用 `generate.py` |

每条都对应 `docs/notes.md` 里的一段实测记录，那里写得更细。

## 12 停掉

```bash
python3 assistant.py down                    # 采集 + 生成 + 面板，一起停
python3 collector/wx_collector.py stop       # 只停虚机里的采集循环
```

`stop` 会去虚机里结束采集进程，不会动微信本身。
