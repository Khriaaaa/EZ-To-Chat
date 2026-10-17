# wx_collect.ps1 - WeChat read-only collector loop (guest side, PowerShell 5.1)
#
# Usage (must run in interactive session 1, normally launched by wx_collector.py start):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\dl\wx_collect.ps1 [-OutDir C:\dl\wxc] [-IntervalSec 2] [-Rounds 0]
#   -Rounds 0 = loop forever; N>0 = stop after N rounds.
#
# Outputs in $OutDir:
#   round.json     —— 一轮的完整快照（chat/title/sessions/window/shot 全在里面，原子写）
#   shot_*.png + shot.name、heartbeat.txt、collector.log（5MB 轮转）
# 只读：从不按键、不点击。
#
# 为什么是「一轮一个 round.json」：老版本把 chat.json / title.json / sessions.json / window.json
# 分别原子写，NAS 侧再逐个拉。访客每 2 秒重写一遍，而一次拉取要花好几秒，
# 于是 NAS 会拿到「chat 属于第 N 轮、shot 属于第 N+2 轮」的拼图，甚至拉到写着会话列表的 chat.json
# （两段命令输出共用一个 cmd.out.tmp 时的产物）。sender 判错、消息挂错会话都是这么来的。
# 现在整轮内容在访客侧一次性合成一个 JSON 原子落盘，NAS 只拉这一个文件 + 它点名的那张截图。
param(
    [string]$OutDir = 'C:\dl\wxc',
    [int]$IntervalSec = 2,
    [int]$Rounds = 0,
    # 单机版会带自己那份 winapp.exe；NAS 版走老路径 C:\winapp-cli
    [string]$Winapp = 'C:\winapp-cli\winapp.exe'
)

$ErrorActionPreference = 'Continue'
$CmdTimeoutMs = 30000
$LogMax = 5MB

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$LogFile = Join-Path $OutDir 'collector.log'
$LockFile = Join-Path $OutDir 'collector.lock'

function Now-Utc { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

function Write-Log([string]$msg) {
    try {
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt $LogMax)) {
            $old = $LogFile + '.1'
            if (Test-Path $old) { Remove-Item $old -Force -ErrorAction SilentlyContinue }
            Move-Item $LogFile $old -Force -ErrorAction SilentlyContinue
        }
        Add-Content -Path $LogFile -Value ((Now-Utc) + ' ' + $msg) -ErrorAction Stop
    } catch { }
}

# ---- 单实例锁 --------------------------------------------------------------
# 同时跑两份采集循环是历史上一堆脏数据的根因：两份都往 cmd.out.tmp 写 winapp 输出，
# 交错后 chat.json 里会混进别的命令的输出（实测拿到过会话列表）；
# shot.name 也互相覆盖，NAS 拉到的截图和 chat 不是同一轮。
# 这里用「锁文件 + PID 存活」判定，比从 NAS 侧查 Win32_Process.CommandLine 可靠
# （那条路在跨会话查 CommandLine 时会拿不到命令行，于是判定成「没在跑」而反复重复拉起）。
function Get-OtherAlive {
    if (-not (Test-Path $LockFile)) { return $null }
    $p = Get-Content $LockFile -Raw -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    $p = $p.Trim()
    if ($p -notmatch '^\d+$') { return $null }
    $id = [int]$p
    if ($id -eq $PID) { return $null }
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { return $id }
    return $null
}

$other = Get-OtherAlive
if ($other) {
    Write-Log ('another collector pid=' + $other + ' alive; exit (pid=' + $PID + ')')
    exit 0
}
# 锁文件在、但里面的 PID 已经死了（上次被 kill、虚机重启）：那是陈旧锁，删掉再抢。
# 没有这一步的话，CreateNew 会被一个死进程留下的锁文件永久挡住，采集再也起不来。
if (Test-Path $LockFile) {
    $staleRaw = Get-Content $LockFile -Raw -ErrorAction SilentlyContinue
    $staleId = 0
    if ($staleRaw -and ($staleRaw.Trim() -match '^\d+$')) { $staleId = [int]$staleRaw.Trim() }
    $staleAlive = $false
    if ($staleId -gt 0) { $staleAlive = [bool](Get-Process -Id $staleId -ErrorAction SilentlyContinue) }
    if ($staleId -eq 0) {
        # 空/半截的锁：另一个进程刚 CreateNew、还没把 PID 写进去（亚秒窗口）。
        # 只按内容判「陈旧」就会在这里删掉它，然后谁都抢到锁 —— 两份采集同时跑。
        # 所以看年纪：15 秒内的空锁当「正在写」，退出等它（评审 B22）。
        $lockAge = 999
        try { $lockAge = ((Get-Date) - (Get-Item $LockFile).LastWriteTime).TotalSeconds } catch {}
        if ($lockAge -lt 15) {
            Write-Log ('lock file just created (age=' + [int]$lockAge + 's content=' + $staleRaw + '); exit (pid=' + $PID + ')')
            exit 0
        }
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
        Write-Log ('stale empty lock removed (age=' + [int]$lockAge + 's)')
    } elseif (-not $staleAlive) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
        Write-Log ('stale lock removed (holder=' + $staleRaw + ')')
    }
}
# 原子独占创建：CreateNew 在文件已存在时直接抛 IOException，谁先建谁赢，
# 不存在「双方都读回自己的 PID」的裁决失效。
# 旧写法是「读 -> 写 PID -> 睡 400ms -> 读回比对」：两个进程赶在锁文件空缺的
# 亚秒窗口里同时启动时，会双双读到空、双双写入、双双读回自己 —— 双双认定锁是
# 自己的，于是同时跑两份采集循环（历史上那批脏数据的根因）。
$lockTaken = $false
try {
    $fs = [IO.File]::Open($LockFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $sw = New-Object IO.StreamWriter($fs)
    $sw.Write([string]$PID)
    $sw.Flush()
    $sw.Close()
    $lockTaken = $true
} catch {
    $lockTaken = $false
}
if (-not $lockTaken) {
    $holder = (Get-Content $LockFile -Raw -ErrorAction SilentlyContinue)
    Write-Log ('lock file already exists (holder=' + $holder + '); exit (pid=' + $PID + ')')
    exit 0
}
# 清掉上次崩溃留下的临时文件（每个进程一份，按 PID 命名）
Get-ChildItem -Path (Join-Path $OutDir 'cmd.*.tmp') -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Run winapp with timeout. Returns hashtable: ok, code, stdout
function Invoke-Winapp([string[]]$ArgList) {
    $res = @{ ok = $false; code = -1; stdout = '' }
    # 临时文件名带 PID：即便真有两份在跑，也不会互相写进对方的输出文件
    $so = Join-Path $OutDir ('cmd.' + $PID + '.out.tmp')
    $se = Join-Path $OutDir ('cmd.' + $PID + '.err.tmp')
    try {
        Remove-Item $so, $se -Force -ErrorAction SilentlyContinue
        $p = Start-Process -FilePath $Winapp -ArgumentList $ArgList -NoNewWindow -PassThru `
             -RedirectStandardOutput $so -RedirectStandardError $se
        $null = $p.Handle   # cache handle so ExitCode is readable after exit
        if (-not $p.WaitForExit($CmdTimeoutMs)) {
            try { $p.Kill() } catch { }
            $res.code = -2   # timeout
            return $res
        }
        $res.code = $p.ExitCode
        if (Test-Path $so) { $res.stdout = [IO.File]::ReadAllText($so) }
        $res.ok = ($p.ExitCode -eq 0)
    } catch {
        $res.code = -3
        $res.stdout = [string]$_
    }
    return $res
}

# Atomic text write: .tmp then Move-Item
function Save-Atomic([string]$Name, [string]$Text) {
    $dst = Join-Path $OutDir $Name
    $tmp = $dst + '.tmp'
    [IO.File]::WriteAllText($tmp, $Text, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Path $tmp -Destination $dst -Force
}

function Step-Name([string]$label, $r) {
    if ($r.ok) { return ($label + '=ok') }
    return ($label + '=FAIL(code ' + $r.code + ')')
}

Write-Log ('start OutDir=' + $OutDir + ' IntervalSec=' + $IntervalSec + ' Rounds=' + $Rounds + ' pid=' + $PID)

$done = 0
try {
while ($true) {
    $steps = @()
    $rid = (Get-Date -Format 'yyyyMMdd_HHmmss_fff')
    $chatText = $null; $titleText = $null; $sessText = $null; $winText = $null; $shotName = $null
    try {
        # a. HWND —— 用 JSON 挑「真正的主窗口」。
        #    老写法是从文本输出里抓第一个 HWND：微信一旦有登录窗/提示弹窗，
        #    第一个 HWND 往往是 mmui::XDialog（330×219 那个提示框），
        #    于是截图拍错窗口，NAS 侧的像素判左右直接全废。
        #    规则：剔掉登录窗和弹窗，剩下按面积取最大；一个都不剩（掉登录了）
        #    就退回最大窗口，并在 steps 里写明，别让 NAS 侧误以为采到了。
        $hwnd = $null
        $r = Invoke-Winapp @('ui', 'list-windows', '-a', 'Weixin', '--json')
        if ($r.ok -and $r.stdout.Length -gt 2) {
            try {
                $wins = @($r.stdout | ConvertFrom-Json) | Where-Object { $_.hwnd }
                # list-windows 给的 className 全是 Qt51514QWindowIcon，认不出登录窗；
                # 能认的是 ownerHwnd：提示弹窗是主窗口的 owned window（非 0），
                # 主窗口/登录窗才是顶层（0）。所以：顶层里取面积最大的。
                $real = @($wins | Where-Object { [int]$_.ownerHwnd -eq 0 })
                $pool = @(if ($real.Count -gt 0) { $real } else { $wins })
                $pick = $pool | Sort-Object { [int]$_.width * [int]$_.height } -Descending | Select-Object -First 1
                if ($pick) { $hwnd = [string]$pick.hwnd }
            } catch { $steps += 'hwnd=PARSE_FAIL' }
        }
        if ($hwnd) { $steps += ('hwnd=' + $hwnd) } else { $steps += ('hwnd=FAIL(code ' + $r.code + ')') }
        # 后面每一步都盯同一个窗口，别让 winapp 自己按「最大窗口」乱选
        $tgt = @(if ($hwnd) { @('-w', $hwnd) } else { @('-a', 'Weixin') })

        # b0. 先把消息列表往下拨一点，再读。
        #     UIA 只暴露「视口里已实例化」的气泡：窗口没滚到底时，底部的新消息**根本不在树里**
        #     （实测：会话列表都提示 2 条新消息了，chat_message_list 里还只有老的那几条，
        #     NAS 侧因此判「没有新消息」）。所以每轮先滚一小格 = 约一条消息的高度：
        #     宁可分几轮慢慢追到底，也不要一次跳一个视口——跳过去中间的消息就永久漏了。
        $r = Invoke-Winapp (@('ui', 'scroll', 'chat_message_list') + $tgt + @('--wheel', '-1', '--json'))
        $steps += (Step-Name 'scroll' $r)
        Start-Sleep -Milliseconds 150

        # b. chat
        #    "返回了非空 JSON" 不能当作采到了：微信掉登录后，登录窗口/提示框的树照样几千字符，
        #    于是每一轮都报 chat=ok，NAS 侧一直以为在正常采集 —— 假通过，比报错更坏。
        #    判据改成"这一屏里真的有没有聊天气泡"。
        $r = Invoke-Winapp (@('ui', 'inspect', 'chat_message_list') + $tgt + @('-d', '12', '--json'))
        if ($r.ok -and $r.stdout.Length -gt 2) { $chatText = $r.stdout } else { $r.ok = $false }
        if ($r.ok -and $r.stdout -notmatch 'ChatTextItemView') {
            $chatText = $null
            $r.ok = $false
            $steps += 'chat=NO-BUBBLES(可能已掉登录)'
        } else {
            $steps += (Step-Name 'chat' $r)
        }

        # b2. 聊天标题栏的联系人真名（UIA 直读；会话列表高亮在"搜索打开聊天"时会跟不上）
        $r = Invoke-Winapp (@('ui', 'inspect', 'current_chat_name_label') + $tgt + @('-d', '2', '--json'))
        if ($r.ok -and $r.stdout.Length -gt 2) { $titleText = $r.stdout } else { $r.ok = $false }
        $steps += (Step-Name 'title' $r)

        # c. sessions
        $r = Invoke-Winapp (@('ui', 'inspect', 'session_list') + $tgt + @('-d', '3', '--json'))
        if ($r.ok -and $r.stdout.Length -gt 2) { $sessText = $r.stdout } else { $r.ok = $false }
        $steps += (Step-Name 'sessions' $r)

        # d. screenshot: write STRAIGHT to the per-round unique path via winapp --output.
        #    Why: the old flow had winapp write its fixed default
        #    C:\Windows\system32\screenshot.png and then Copy-Item it out. That shared
        #    fixed path is what a leaked QGA read handle ends up sitting on, and once it
        #    cannot be overwritten EVERY round fails: shot.name=NONE, no image, and the
        #    NAS side then cannot tell a left bubble from a right one (sender=unknown).
        #    Never touch the shared path at all. The name carries milliseconds so two
        #    rounds inside the same second can never collide.
        if ($hwnd) {
            $shotOk = $false
            for ($try = 1; $try -le 3 -and -not $shotOk; $try++) {
                $name = 'shot_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + $try + '.png'
                $dst = Join-Path $OutDir $name
                $r = Invoke-Winapp @('ui', 'screenshot', '-w', $hwnd, '--output', $dst, '--json')
                if ($r.ok -and (Test-Path $dst)) {
                    $shotName = $name
                    Set-Content -Path (Join-Path $OutDir 'shot.name') -Value $name -Encoding ASCII
                    Get-ChildItem -Path (Join-Path $OutDir 'shot_*.png') -ErrorAction SilentlyContinue |
                        Sort-Object LastWriteTime -Descending | Select-Object -Skip 12 |
                        Remove-Item -Force -ErrorAction SilentlyContinue
                    $shotOk = $true
                } else {
                    $r.ok = $false; $r.code = -4
                    Start-Sleep -Milliseconds 500
                }
            }
            if (-not $shotOk) {
                Set-Content -Path (Join-Path $OutDir 'shot.name') -Value 'NONE' -Encoding ASCII
            }
            $steps += (Step-Name 'shot' $r)
        } else {
            $steps += 'shot=SKIP(no hwnd)'
        }

        # e. window origin (so the NAS side can map UIA coords to screenshot pixels)
        $r = Invoke-Winapp (@('ui', 'inspect') + $tgt + @('-d', '1', '--json'))
        if ($r.ok -and $r.stdout.Length -gt 2) { $winText = $r.stdout } else { $r.ok = $false }
        $steps += (Step-Name 'window' $r)
    } catch {
        $steps += ('EXC=' + ([string]$_ -replace '[^\x20-\x7E]', '?'))
    }

    # 一轮一个快照，原子写：NAS 拉到的永远是完整且同一轮的各份数据
    $snap = @{
        rid      = $rid
        utc      = (Now-Utc)
        hwnd     = $hwnd
        shot     = $shotName
        chat     = $chatText
        title    = $titleText
        sessions = $sessText
        window   = $winText
        steps    = ($steps -join ' ')
    }
    try {
        Save-Atomic 'round.json' ($snap | ConvertTo-Json -Depth 3 -Compress)
    } catch {
        $steps += 'SNAP_FAIL'
    }

    $done++
    Write-Log ('round ' + $done + ' rid=' + $rid + ' ' + ($steps -join ' '))
    try { Set-Content -Path (Join-Path $OutDir 'heartbeat.txt') -Value (Now-Utc) -Encoding ASCII } catch { }

    if ($Rounds -gt 0 -and $done -ge $Rounds) { break }
    Start-Sleep -Seconds $IntervalSec
}
} finally {
    Write-Log ('exit pid=' + $PID + ' rounds=' + $done)
    try {
        $cur = Get-Content $LockFile -Raw -ErrorAction SilentlyContinue
        if ($cur -and $cur.Trim() -eq [string]$PID) { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
    } catch { }
}
