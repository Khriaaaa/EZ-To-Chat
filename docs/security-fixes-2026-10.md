# 安全修复记录（2026-10）

对应审计：eztochat 全量安全审计（22 条发现，2026-10-13，外部 agent 深审 + 人工抽查复现）。
本文件记录每条发现的修复状态、验证方式，以及留给 Windows 真机复核的清单。

## 已修

| 编号 | 级别 | 修复 |
|---|---|---|
| F-01 | 高 | winapp 下载锁定版本 + SHA256（`desktop/preflight.ps1` / `scripts/setup-windows.ps1` / `desktop/scripts/make-vendor.py` 三处同名常量，当前 v0.7.1）；PsExec 加微软签名校验；zip 解压前做 zip-slip 条目检查 |
| F-02 | 高 | `.keys.json` 的钥匙在 Windows 上经 DPAPI 加密落盘；解密副本 `Reader.close()` 用后即删；密钥缓存/解密副本收紧 0600；类注释改为如实描述 |
| F-03 | 中 | `generate.py` 复用 `_NoRedirect`：3xx 直接判失败，不再跟随重定向把 Authorization 转发到任意地址 |
| F-04 | 中 | 抽 `collector/storelock.py`：面板 `/api/msg` 追加拿和采集器同一把跨进程文件锁（读改写不再互相抹行） |
| F-05 | 中 | 登录先验密码再判限速：正确密码永远放行；`_fails` 过期条目顺手清理 |
| F-07 | 中 | `scan_config_cipher` 保护位过滤条件修正（原来写反，兜底扫描等于没有）+ 扫描计数日志 |
| F-08 | 中 | `vmcfg`：StrictHostKeyChecking=accept-new（不再 no）；sshpass 路径保留但注明仅兼容用途 |
| F-09 | 中 | electron 33.4.11 → 44.7.0、electron-builder 25.1.8 → 26.15.3（package-lock 已同步）。⚠️ 未真机冒烟，见下方复核清单 |
| F-10 | 中 | 三处杀进程判据收紧（`main.js` freePort / `assistant.py` shutdown / `installer.nsh`）：不再按进程名 python/pythonw 或裸子串 wx-reply-assistant 杀，要求「应用目录 + 本应用脚本名」证据 |
| F-12 | 低 | 面板全响应加 CSP / X-Frame-Options / Referrer-Policy；`_oops` 不再把异常原文回浏览器 |
| F-13 | 低 | 进门票改一次性（`consume_entry_token`）；日志里 `k=` 打码；Location 只允许站内单斜杠路径（协议相对/绝对形式都拦） |
| F-14 | 低 | `/api/msg`、`/api/setup` 的 `_BodyTooLarge` 不再被吞成 400，正确回 413 |
| F-15 | 低 | 新增 `requirements.lock`（uv `--generate-hashes`，pillow/numpy/pyyaml 全哈希锁定）；`install.sh` 有锁走 `--require-hashes` |
| F-16 | 低 | `ga.py` 校验 QGA UUID 格式；`ps1run.py` / `wx_collector.py` 所有拼进 PowerShell 的路径值统一单引号转义（`_psq`） |
| F-17 | 低 | Electron 壳加 `will-navigate` / `will-redirect` 拦截：只许留在面板源 |
| F-18 | 低 | LLM 响应读取封顶 300KB |
| F-19 | 低 | `render_history` 认 `WXREPLY_STORE`（桌面版历史不再静默为空） |
| F-11 | 低 | （部分）live 路径单条消息 2000 字截断；链接 payload 统一收口未动，见待定 |

## 未修 / 待定

| 编号 | 说明 |
|---|---|
| F-06 | `--lan` 明文传输：修复需要上 HTTPS（自签/mkcert）或改走加密隧道 —— 产品决策，待定 |
| F-11b | `render_for_ctx` 统一收口 + prompt 注入提示语：属设计一致性改动，待定 |
| F-10b | 卸载删数据加确认框：「卸载即无痕」是既定取舍，是否加确认由产品决定（现行为：真卸载清、升级保留、`--delete-app-data` 强制清） |
| F-20 | 副本里的预构建 exe / config.local.yaml：发布前核对不入包（`.gitignore` 已排除，坚持发布 SHA256SUMS） |
| F-21 | chmod 在 Windows 上只映射只读位：真实保护是 %APPDATA% 用户 ACL，已写进 F-02 注释 |
| F-22 | SSRF 残余（DNS rebinding 窗口、allow_private_base 全放行）：已知限制，注释已承认 |

## 验证记录（Linux 侧实测）

- 测试套件：test_review_fixes 37/0、test_error_summary_and_ssrf 28/0、test_manual_context 22/0、test_wxdb 22/0（4 skip：本机缺 sqlcipher3）
- 探针复现（修复后回归）：
  - F-03：本地 302 目标不再收到请求，Key 不外泄；正常 200 链路照常
  - F-05：被限速 IP 上正确密码 302 放行；错误密码仍 429
  - F-13：同票第二次使用不再换发 cookie；`//evil.example/` 与绝对形式请求行 Location 均强制站内
  - F-14：70KB body → 413
  - F-04：外部持 `.store.lock` 时 `/api/msg` 阻塞、放锁后 200，消息落盘
  - F-02：dpapi: 条目往返（模拟）、解不开安全失效、close 后副本/stamp 全删
  - F-01：真 PowerShell 7 跑 zip 守卫（`../`、绝对路径、盘符条目全拦；真包放行）；官方直连与 ghfast.top 双源 sha256 一致
- winapp v0.7.1 x64 zip：sha256 `d925d1e32cdc320b6d271f653fd2cd3c05b5d7558deb20d1b548bead77900fcf`（99,013,508 字节）

## 独立复核轮（2026-10-13，opencode / deepseek-v4.1-flash --variant max）

修复完成后把全部改动打包给独立复核（非自证；复核方实查 GitHub API 核对 F-01 哈希、全量 py_compile、核对锁序无死锁环）。发现 8 条，处理如下：

| # | 级别 | 复核发现 | 处理 |
|---|------|----------|------|
| 1 | 严重 | 多账号/异常路径下解密副本不删：collect 只关排序第一的 Reader 且只在成功路径 close；dbcheck 同 | collect 改为全路径清理（排名异常全关、落选立即关、赢家 finally 关）；dbcheck 加 finally。探针 probe_collect 三场景 9/9 |
| 2 | 一般 | DPAPI 失败退回明文缓存，与威胁模型冲突 | 改为不落盘：密钥留内存本轮可用，下次运行重新提取；显式警告。探针已更新 |
| 3 | 一般 | F-01 旁路：已存在的 PsExec 不验签（体检只 Test-Path）；下载失败留坏文件被下次当「已装」 | 体检对现存文件验签（不过 → 记「缺」走重下 / -NoAuto 记 FAIL）；Download-Psexec 验签失败先删文件再抛 |
| 4 | 一般 | 锁文件把 Python 门槛抬到 3.11，install.sh 仍写 3.9+ | install.sh 门槛改 3.11+（与嵌入式运行时、锁一致） |
| 5 | 一般 | preflight 端口清理判据仍偏宽（'assistant.py' 子串单独成立） | 改为「安装目录 + 脚本名」双条件，与 main.js 对齐 |
| 6 | 建议 | assistant.py shutdown 把调用它的父进程（自己）算成待清目标 | 枚举/强杀查询都排除 $PID 与父进程 |
| 7 | 建议 | make-vendor 下载 wheel 无锁无哈希 | 有 requirements.lock 时 --require-hashes 按锁下载（实测可下 win_amd64/cp311 三件） |
| 8 | 建议 | F-11 历史渲染未封顶 | render_history 单条封顶 2000 字（text/quote），已断言长度 |

复核同时确认闭环：F-03/F-04/F-05/F-07/F-12/F-13/F-14/F-16/F-17/F-19；F-17 判据为字符串前缀但无实际绕过形态（接受）。

## 第二轮独立复核（2026-10-13，opencode / glm-5.3-flash --variant max）

第二轮换模型再查：专挑修复本身的新 bug、同模式漏网、两轮都没发现的新问题（复核方自己跑了跨进程竞争、emoji 截断、同 fd 重复锁三组 Linux 实测）。9 条发现，处理如下：

| # | 级别 | 发现 | 处理 |
|---|------|------|------|
| 1 | 一般 | make-vendor 锁文件分支是死代码：锁路径指向 desktop/ 但文件在项目根 → 永远走无锁分支，且静默 | 锁路径改指项目根（os.pardir）；无锁回落改为醒目警告（不再静默） |
| 2 | 一般 | .part 半成品明文库不清理：decrypt_db 写 .part 途中异常（盘满/断电）会留下部分明文库；close() 清理名单漏 .part；写 .part 期间权限不收紧 | decrypt_db 写段包 try/except（异常删 .part）；打开后立刻 fchmod 0600；close()/KeyStore.save/save_anchors 的清理与异常路径都补 .part 删除 |
| 3 | 建议 | cache/db 解密缓存跨进程共享但无互斥：sync（4s 循环）与面板 check_round 子进程并发解密同一 dst / 并发写固定名 .part | collect() 整段包 storelock.StoreLock(cache_dir)，与 messages.jsonl 锁同一套实现；拿不到锁本轮失败 |
| 4 | 建议 | _merge_wal 不验提交帧：未提交事务的合法帧也会被合并进明文库（读到从未存在过的消息） | 帧头 4-8 字节（提交后页数）> 0 才结算；pending 帧遇提交帧统一放行，读到尾未提交的丢弃 |
| 5 | 建议 | 目录前缀匹配不带尾部 \：...\wx-reply-assistant-backup\ 会被 ...\wx-reply-assistant 命中误杀 | preflight 与 main.js 两侧都改为 (dir.TrimEnd('\')+'\') 作前缀 |
| 6 | 建议 | assistant.py $pp 提取无容错：WMI 瞬时失败时 $pp=$null → 父进程不再被排除 → 自杀 | try/catch 包裹，失败 fallback -1（不存在的 PID，语义=不排除） |
| 7 | 信息 | _cap 截断对 emoji/代理对安全（实测无孤代理）；text=NULL 行标签有歧义但不影响安全 | 不动 |
| 8 | 信息 | INSTALL.md:52 仍写 3.9+ | 已同步 3.11+ |
| 9 | 信息 | 三处 WA_TAG/SHA256 对齐无缺陷 | 不动 |

第二轮实测证据：提交帧合并/非提交帧丢弃探针（并修复了帧结算时 merged 双计数——提交帧探针 merged=2 → 1）；跨进程锁探针 B 等 A 1.5s 释放；真实 store 路径 collect 冒烟（锁获取→Unavailable→锁释放）。全测试 37/28/22/22 全绿。

## 第三轮独立复核（2026-10-13，opencode / deepseek-v4.1-flash --variant max）

第三轮换回 deepseek 再查（换视角防轮次叠改）。它自己另建带 sqlcipher3 的隔离 venv 用真库跑探针。6 条发现，处理如下：

| # | 级别 | 发现 | 处理 |
|---|------|------|------|
| 1 | 严重 | os.fchmod 是 Unix-only：Windows 上抛 AttributeError 而非 OSError，except 接不住 → 本地库直读路线在唯一可用平台上整体崩，且 auto 模式不回落界面树 | 扁平化为 _try_chmod 单路径（F-21 已说明 Windows chmod 只映射只读位，本就不是真保护）；模拟无 fchmod 环境实测 decrypt_db 正常 |
| 2 | 一般 | cmd_check 把跨轮 .readmode_state.json 当「本轮走了哪条路」：从 db 切回 ui 后 via 停在 db → 永久误判 running=true、不再拉起访客采集；反向（db 失效误记 ui）会错白跑一轮 PS | 显式 ui 路径也 readmode.record(via="ui")，state 不再陈旧 |
| 3 | 一般 | dbcheck 绕开 R2-3 的缓存锁：与 sync 常驻并发会把 .part/副本竞争原样带回来 | dbcheck 每账号 Reader 段包同一把 storelock.StoreLock(cache/db) |
| 4 | 一般 | cmd_shutdown 两条强杀查询只做「关键字+脚本名」，编辑器打开项目文件会被兜底强杀（未保存内容丢失），旁路安装目录互杀 | 两条查询各加第三条判据：命令行落在本安装根（ROOT 小写+尾 \，PS_DIR 注入，与 main.js/preflight 同一套判据） |
| 5 | 建议 | cmd_check else 绑错条件：--quiet（面板固定用法）下拉起失败也会白 poll 一轮 | 拆成独立 if，失败不再进 else |
| 6 | 信息 | main.js 仍写 3.9+（文案+probe 阈值）；storelock 注释只说一个锁文件；wxdb:810 salt 读法不关句柄 | 文案+阈值同步 3.11；注释补「同一套实现/协议、第二把锁 cache/db/.store.lock」；补 with 关句柄 |

第三轮实测证据：真 SQLCipher 库 WAL 探针（committed 合并、逐行与 sqlcipher3 一致）；删 os.fchmod 模拟 Windows 后 decrypt_db 正常（mode=0600）。另核实 R2-3 锁序无死锁路径、make-vendor/preflight 前缀、$pp 容错与描述一致，F 系列抽查入口无新洞。全测试 37/28/22/22 全绿，新旧行为探针 15/9/14 全过，py/ps1/js 编译解析全过。

## 待 Windows 真机复核（下一轮）

1. F-02：跑一次 db 直读，确认 `.keys.json` 写入 `dpapi:` 前缀、重启后仍能读回
2. F-01：删 winapp 目录触发 preflight 修复，确认哈希校验通过；setup-windows `-DownloadOnly` 全流程
3. F-01：PsExec 签名校验真机过（`Get-AuthenticodeSignature` Valid + Microsoft）；把文件换成假内容再体检，应报「签名不过 → 重新下载覆盖」（-NoAuto 报 FAIL）
4. F-10：`-WX` 构建安装包 + 真机卸载一遍（杀进程循环、数据目录按参数行为）
5. F-09：electron 44 桌面构建 + 启动冒烟（托盘、面板加载、导航拦截日志）
6. F-04：面板补上下文与采集器同步并发跑一轮，确认无丢行
7. F-15：打包机上跑 make-vendor，确认走 requirements.lock（--require-hashes）下载 wheel 成功

## 升级 winapp 的操作

同步改三处常量（同一个 tag 和 sha256）：
`desktop/preflight.ps1`、`scripts/setup-windows.ps1`、`desktop/scripts/make-vendor.py`。
新 zip 的 sha256 算法：`Get-FileHash <zip> -Algorithm SHA256`（或 `sha256sum`）。
