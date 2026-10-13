# setup-windows.ps1 —— Windows 侧环境体检 / 补齐（微信跑在这台机器上时）
#
# 两种用法：
#   1) 源码方式（在 Windows 上以管理员身份开 PowerShell）
#        powershell -NoProfile -ExecutionPolicy Bypass -File setup-windows.ps1
#        powershell -NoProfile -ExecutionPolicy Bypass -File setup-windows.ps1 -NoAuto
#   2) 编译成 exe 双击（scripts/dist/wxreply-setup.exe，用 ps2exe 打）
#        双击即跑；没有控制台时自动改用窗口，中文不乱码，能选中复制
#
#   默认：体检完，缺什么就自己后台下什么（winapp CLI 走 GitHub Releases，
#         PsExec64 走 live.sysinternals.com），顺手把睡眠关掉；窗口底下有进度条，
#         补完自动重新体检一遍，标题跟着变
#   -NoAuto：只体检，不下载、不改系统
#   -WinappUrl / -PsexecUrl：换下载源（国内镜像、内网文件服务器）
#
# 注：这个脚本本身不需要在交互会话里跑；但 UIA 相关的命令（winapp ui ...）
#     必须注入到用户登录的 session 1 才有内容 —— 见 INSTALL.md 3.3。
param(
    [string]$WorkDir   = 'C:\dl\wxc',
    [string]$WinappDir = 'C:\winapp-cli',
    [string]$Psexec    = 'C:\dl\PsExec64.exe',
    [switch]$Auto,                    # 兼容老写法：现在默认就会补，加不加都一样
    [switch]$NoAuto,                  # 只体检，不下载、不改系统
    [switch]$Gui,
    [switch]$NoGui,                   # 强制走控制台输出（重定向到文件时用）
    [switch]$DownloadOnly,            # 内部用：后台下载子进程
    [string]$WinappUrl = '',          # 覆盖 winapp zip 地址
    [string]$PsexecUrl = ''           # 覆盖 PsExec64 地址
)

$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$script:curUrl = ''
$script:buf   = New-Object System.Collections.ArrayList
$script:rows  = @()
$AllowChange  = -not $NoAuto
$ProgressFile = Join-Path $env:TEMP 'wxreply-setup.progress'
# winapp 下载锁死在具体版本 + SHA256（审计 F-01）；升版本时同步改
# desktop/preflight.ps1 与 desktop/scripts/make-vendor.py 里的同名常量
$WA_TAG        = 'v0.7.1'
$WA_ZIP_SHA256 = 'd925d1e32cdc320b6d271f653fd2cd3c05b5d7558deb20d1b548bead77900fcf'
$GH_API       = "https://api.github.com/repos/microsoft/winappCli/releases/tags/$WA_TAG"
$PSEXEC_URL   = if ($PsexecUrl) { $PsexecUrl } else { 'https://live.sysinternals.com/PsExec64.exe' }
$MIN_PSEXEC   = 100000

# 有没有控制台？编译成 exe（-noConsole）双击跑时没有，这时结果走窗口显示。
# 访问 [Console]::WindowWidth 在无控制台进程里会抛「句柄无效」。
$HasConsole = $true
try { $null = [Console]::WindowWidth } catch { $HasConsole = $false }
if ($NoGui) { $Gui = $false } elseif (-not $HasConsole) { $Gui = $true }

# 所有输出都过这一层：有控制台就写控制台，同时攒着给窗口显示用。
function Say([string]$msg, [string]$color = 'Gray') {
    $script:buf.Add($msg) | Out-Null
    if (-not $Gui) { Write-Host $msg -ForegroundColor $color }
}

function Add-Row([string]$Item, [string]$State, [string]$Detail) {
    $script:rows += [pscustomobject]@{ 项目 = $Item; 状态 = $State; 说明 = $Detail }
}

function Write-Step([string]$msg) { Say "`n=== $msg" 'Cyan' }

# ---------------------------------------------------------------- 进度文件
# 后台子进程往这一个文件里写一行；父进程读它显示进度。
# 格式：phase|got|total|state|msg（msg 放最后，允许带空格）
function Set-Progress([string]$phase, [long]$got, [long]$total, [string]$state, [string]$msg) {
    try {
        [IO.File]::WriteAllText($ProgressFile, "$phase|$got|$total|$state|$msg", [Text.Encoding]::UTF8)
    } catch { }
}

function Read-Progress {
    try {
        if (-not (Test-Path $ProgressFile)) { return $null }
        $t = [IO.File]::ReadAllText($ProgressFile)
        if (-not $t) { return $null }
        $p = $t.Split('|')
        if ($p.Count -lt 5) { return $null }
        return $p
    } catch { return $null }   # 正被写的时候读会失败，跳过这轮就行
}

# 进度行 -> 给人看的一句话
function Format-Progress($p) {
    $name = switch ($p[0]) { 'winapp' { 'winapp CLI' } 'psexec' { 'PsExec64' } 'init' { '准备' } 'done' { '完成' } default { $p[0] } }
    if ($p[3] -eq 'done') { return '下载完成' }
    if ($p[3] -eq 'fail') { return "下载失败：$($p[4])" }
    if ($p[3] -eq 'exp')  { return "$name 解压中…" }
    $got = [double]$p[1]; $tot = [double]$p[2]
    if ($tot -gt 0) { return ("$name 下载中 {0:N1} / {1:N1} MB" -f ($got / 1MB), ($tot / 1MB)) }
    return "$name 下载中…"
}

# ---------------------------------------------------------------- 下载（子进程里跑）
function Describe-Err($rec) {
    # PowerShell 会把 HttpWebRequest 的错包好几层（MethodInvocationException -> WebException -> ...），
    # 直接 $_.Exception.Message 出来是「使用"0"个参数调用"GetResponse"时发生异常:」这种没用的
    $e = $rec.Exception; $m = $e.Message; $i = 0
    while ($e.InnerException -and $i -lt 4) {
        $e = $e.InnerException
        if ($e.Message) { $m = $e.Message }
        $i++
    }
    return $m
}

function Save-WithProgress([string]$url, [string]$dst, [string]$phase) {
    $script:curUrl = $url
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.UserAgent      = 'wx-reply-assistant-setup'
    $req.Timeout        = 60000
    $req.ReadWriteTimeout = 60000
    $resp = $req.GetResponse()
    $total = [long]$resp.ContentLength
    $in  = $resp.GetResponseStream()
    $out = [IO.File]::Create($dst)
    $buf = New-Object byte[] 262144
    $got = 0; $last = [DateTime]::MinValue
    try {
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
            $out.Write($buf, 0, $n); $got += $n
            if (([DateTime]::UtcNow - $last).TotalMilliseconds -ge 600) {
                Set-Progress $phase $got $total 'dl' ''
                $last = [DateTime]::UtcNow
            }
        }
    } finally {
        $out.Close(); $in.Close(); $resp.Close()
    }
    Set-Progress $phase $got $total 'dl' ''
}

function Get-WinappPath {
    $c = Get-Command winapp -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $WinappDir 'winapp.exe'
    if (Test-Path $p) { return $p }
    return $null
}

function Assert-ZipSafe([string]$zipPath) {
    # 防 zip-slip：条目名不许绝对路径/盘符/含 ..（Expand-Archive 的行为随 .NET 版本
    # 漂移，审计 F-01 待确认项 T-1 —— 自己先把入口卡死）
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $z = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($e in $z.Entries) {
            $n = $e.FullName
            if ($n -match '^[/\\]' -or $n -match '^[A-Za-z]:' -or (($n -split '[/\\]') -contains '..')) {
                throw "zip 条目路径不安全：$n"
            }
        }
    } finally { $z.Dispose() }
}

function Test-PsexecSigned([string]$p) {
    # 签名校验（审计 F-01）：PsExec 会被当可执行文件启动，必须是微软签名且证书链完整
    try {
        $sig = Get-AuthenticodeSignature -FilePath $p
        return ($sig.Status -eq 'Valid' -and $sig.SignerCertificate -and ($sig.SignerCertificate.Subject -match 'Microsoft'))
    } catch { return $false }
}

function Download-Winapp {
    $zip = Join-Path $env:TEMP 'wxreply-winapp.zip'
    $url = $WinappUrl
    if (-not $url) {
        $rel = Invoke-RestMethod -UseBasicParsing -TimeoutSec 60 -Uri $GH_API `
               -Headers @{ 'User-Agent' = 'wx-reply-assistant-setup' }
        $asset = $rel.assets | Where-Object { $_.name -match 'x64\.zip$' } | Select-Object -First 1
        if (-not $asset) { throw "release $($rel.tag_name) 里没有 x64 zip 资产" }
        $url = $asset.browser_download_url
    }
    Set-Progress 'winapp' 0 0 'dl' ''
    Save-WithProgress $url $zip 'winapp'
    $h = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $WA_ZIP_SHA256) {
        throw "winapp 包哈希不对（期望 $WA_ZIP_SHA256，实际 $h）—— 上游更新或下载链路被改，拒绝解压"
    }
    Assert-ZipSafe $zip
    Set-Progress 'winapp' 0 0 'exp' ''
    if (-not (Test-Path $WinappDir)) { New-Item -ItemType Directory -Path $WinappDir -Force | Out-Null }
    Expand-Archive -Path $zip -DestinationPath $WinappDir -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    $exe = Get-ChildItem -Path $WinappDir -Recurse -Filter 'winapp.exe' | Select-Object -First 1
    if (-not $exe) { throw "解压完里面没有 winapp.exe —— 手动看下 $WinappDir 的结构" }
}

function Download-Psexec {
    $dir = Split-Path $Psexec -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Save-WithProgress $PSEXEC_URL $Psexec 'psexec'
    if (-not (Test-Path $Psexec)) { throw '下载没落地，检查网络或代理' }
    if ((Get-Item $Psexec).Length -lt $MIN_PSEXEC) { throw '下回来的文件太小，可能被拦截或断流了' }
    if (-not (Test-PsexecSigned $Psexec)) {
        # 坏文件不留盘（复核 F-01 旁路）：留着的话下次 Test-Path 会把它当「已装」放过去
        Remove-Item $Psexec -Force -ErrorAction SilentlyContinue
        throw "PsExec 数字签名校验不过 —— 已删除可疑文件，拒绝使用（可能被劫持/篡改）"
    }
}

# ================================================================ 内部模式：后台下载
if ($DownloadOnly) {
    try {
        Set-Progress 'init' 0 0 'dl' ''
        if (-not (Get-WinappPath)) { Download-Winapp }
        if (-not (Test-Path $Psexec)) { Download-Psexec }
        elseif (-not (Test-PsexecSigned $Psexec)) { Download-Psexec }   # 旧的没验过/被换过：重下一份
        Set-Progress 'done' 0 0 'done' 'ok'
        exit 0
    } catch {
        $msg = Describe-Err $_
        if ($script:curUrl) { $msg = "连不上 $($script:curUrl)：$msg" }
        Remove-Item (Join-Path $env:TEMP 'wxreply-winapp.zip') -Force -ErrorAction SilentlyContinue
        # 半截文件留着会变成「看起来装了」的坑，清掉
        if (Test-Path $Psexec) {
            try { if ((Get-Item $Psexec).Length -lt $MIN_PSEXEC) { Remove-Item $Psexec -Force } } catch { }
        }
        Set-Progress 'fail' 0 0 'fail' $msg
        exit 1
    }
}

# ================================================================ 体检
function Invoke-Checks {
    $script:rows = @()
    Say '微信回复助手 · Windows 侧环境体检' 'Green'

    # ---------------------------------------------------------------- 会话
    $sid = (Get-Process -Id $PID).SessionId
    Add-Row '当前会话' $(if ($sid -eq 1) { 'ok' } else { '注意' }) "session $sid"

    # ---------------------------------------------------------------- QEMU guest agent
    Write-Step 'QEMU guest agent'
    $ga = Get-Service | Where-Object { $_.Name -eq 'QEMU-GA' -or $_.DisplayName -like '*QEMU Guest Agent*' } |
          Where-Object { $_.Name -notlike '*VSS*' } | Select-Object -First 1
    if ($ga) {
        if ($ga.Status -eq 'Running') {
            Add-Row 'guest agent' 'ok' "$($ga.Name) 正在运行"
        } else {
            Add-Row 'guest agent' 'FAIL' "$($ga.Name) 是 $($ga.Status)，先 Start-Service $($ga.Name)"
        }
    } else {
        Add-Row 'guest agent' 'FAIL' '没找到 QEMU-GA 服务 —— 装 virtio-win 里的 qemu-guest-agent'
    }

    # ---------------------------------------------------------------- winapp CLI
    Write-Step 'winapp CLI（读 UIA 用）'
    $script:winapp = Get-WinappPath
    if ($script:winapp) {
        $ver = (& $script:winapp --version 2>&1 | Select-Object -First 1)
        Add-Row 'winapp' 'ok' "$($script:winapp)（版本 $ver）"
    } elseif ($AllowChange) {
        Add-Row 'winapp' '缺' "没有 —— 装到 $WinappDir（94 MB 左右，看下面的进度条）"
    } else {
        Add-Row 'winapp' 'FAIL' "没找到 —— 期望 $WinappDir\winapp.exe 或在 PATH 里"
        Say '  装法（任选一种）：' 'Yellow'
        Say '    winget install Microsoft.winappcli --source winget'
        Say '    或到 https://github.com/microsoft/winappCli/releases/latest 下 winappcli-x64.zip 解压'
        Say '    想让它自己装：去掉 -NoAuto 重跑'
    }

    # ---------------------------------------------------------------- PsExec64
    Write-Step 'PsExec64（注入交互会话用）'
    if (Test-Path $Psexec) {
        if (Test-PsexecSigned $Psexec) {
            $kb = [math]::Round((Get-Item $Psexec).Length / 1KB)
            Add-Row 'PsExec64' 'ok' "$Psexec（$kb KB，签名通过）"
        } elseif ($AllowChange) {
            # 已存在的文件也要验签（复核 F-01 旁路）：状态记「缺」→ 走后台重下覆盖
            Add-Row 'PsExec64' '缺' "已存在但签名校验不过 —— 重新下载覆盖"
        } else {
            Add-Row 'PsExec64' 'FAIL' "已存在但签名校验不过（可疑）：$Psexec —— 删掉重跑"
        }
    } elseif ($AllowChange) {
        Add-Row 'PsExec64' '缺' "没有 —— 装到 $Psexec"
    } else {
        Add-Row 'PsExec64' 'FAIL' "没找到 $Psexec"
        Say '  想让它自己装：去掉 -NoAuto 重跑（从 live.sysinternals.com 取）' 'Yellow'
    }

    # ---------------------------------------------------------------- 工作目录
    Write-Step '工作目录'
    if (Test-Path $WorkDir) {
        Add-Row '工作目录' 'ok' $WorkDir
    } else {
        try {
            New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
            Add-Row '工作目录' '已建' $WorkDir
        } catch {
            Add-Row '工作目录' 'FAIL' "$WorkDir 建不出来：$($_.Exception.Message)"
        }
    }

    # ---------------------------------------------------------------- 电源
    Write-Step '电源设置（读屏不能被睡眠打断）'
    try {
        # 别按字段名找「交流」那一行 —— 中文系统上字段名是本地化的（当前交流电源设置索引），
        # 英文匹配会一个都抓不到。改用位置：powercfg 的输出里最后两个 0x 值固定是
        # 交流、直流（倒数第二个就是交流）。
        $raw = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>&1 | Out-String)
        $hex = @([regex]::Matches($raw, '0x[0-9a-fA-F]{8}') | ForEach-Object { $_.Value })
        if ($hex.Count -ge 2) {
            $sleepMin = [Convert]::ToInt32($hex[$hex.Count - 2], 16) / 60
            if ($sleepMin -eq 0) {
                Add-Row '睡眠' 'ok' '交流电下已设为「从不」'
            } elseif ($AllowChange) {
                powercfg /change standby-timeout-ac 0 | Out-Null
                powercfg /change monitor-timeout-ac 0 | Out-Null
                powercfg /change disk-timeout-ac 0    | Out-Null
                Add-Row '睡眠' '已改' '交流电下睡眠/关屏/关盘全部设为「从不」'
            } else {
                Add-Row '睡眠' '注意' "交流电下现在是 $sleepMin 分钟后睡眠（加 -NoAuto 改，去掉就让脚本自己改）"
            }
        } else {
            Add-Row '睡眠' '注意' '没解析出电源设置，建议手动跑一次 powercfg /change standby-timeout-ac 0'
        }
    } catch {
        Add-Row '睡眠' '注意' "查不出来：$($_.Exception.Message)"
    }

    # ---------------------------------------------------------------- 微信窗口
    Write-Step '微信窗口'
    if ($script:winapp) {
        if ($sid -ne 1) {
            Add-Row '微信窗口' '跳过' "当前是 session $sid，UIA 读不到东西；要在 session 1 里跑或经 PsExec -i 1 注入"
        } else {
            $out = (& $script:winapp ui list-windows -a Weixin 2>&1 | Out-String)
            if ($out -match 'HWND\s+(\d+)') {
                Add-Row '微信窗口' 'ok' "HWND $($Matches[1])"
            } else {
                Add-Row '微信窗口' 'FAIL' '没找到 Weixin 窗口 —— 微信没开，或窗口是「微信」而不是「Weixin」（实测两者都有，代码会都试）'
            }
        }
    } else {
        Add-Row '微信窗口' '跳过' 'winapp 还没就位'
    }
}

# ---------------------------------------------------------------- 汇总
function Render-Summary {
    Write-Step '体检结果'
    # 不用 Format-Table —— 中文是双宽字符，按字符数补空格会歪。
    # 这里按显示宽度自己补，输出在任何控制台宽度下都是齐的。
    function Pad-Disp([string]$s, [int]$width) {
        $dw = 0
        foreach ($ch in $s.ToCharArray()) {
            if ([int]$ch -gt 0x2E80) { $dw += 2 } else { $dw += 1 }
        }
        return $s + (' ' * [Math]::Max(1, $width - $dw))
    }
    $markOf = @{ ok = '[ok]'; '已装' = '[ok]'; '已建' = '[ok]'; '已改' = '[ok]'; '缺' = '[..]'; FAIL = '[!!]'; '注意' = '[--]'; '跳过' = '[--]' }
    foreach ($r in $script:rows) {
        $mark = if ($markOf.ContainsKey($r.状态)) { $markOf[$r.状态] } else { '[--]' }
        $color = switch ($mark) { '[ok]' { 'Green' } '[!!]' { 'Red' } '[..]' { 'Yellow' } default { 'Yellow' } }
        # 整行一次写出去 —— Write-Host -NoNewline 分段写的话，
        # Start-Transcript / 重定向到文件时每段会被记成一行，文件里就散架了
        Say ('  ' + $mark + '  ' + (Pad-Disp $r.项目 16) + $r.说明) $color
    }
    Say ''
    $extra = @($script:rows | Where-Object { $_.状态 -in @('已装', '已建', '已改') })
    if ($extra.Count -gt 0) { Say "本次改了 $($extra.Count) 处（看上面标 [ok] 且说明里写了装/建/改的行）。" }

    $bad = @($script:rows | Where-Object { $_.状态 -in @('FAIL', '缺') })
    if ($bad.Count -eq 0) {
        Say '没有 FAIL 项。接着把 NAS 侧的 config.local.yaml 填好，就能开采集。' 'Green'
        Say '（别忘了微信本身要登录着，并且打开一个会话）'
    } else {
        $need = @($script:rows | Where-Object { $_.状态 -eq '缺' })
        if ($need.Count -gt 0) {
            Say "$($bad.Count) 项要处理，其中 $($need.Count) 项下面会自动下载补齐。" 'Yellow'
        } else {
            Say "$($bad.Count) 项 FAIL，看上面说明那一列。" 'Yellow'
        }
        Say '只想体检、不让它动系统：加 -NoAuto 重跑。'
    }
}

function Start-Downloads {
    Remove-Item $ProgressFile -Force -ErrorAction SilentlyContinue
    $cargs = @('-DownloadOnly', '-WinappDir', $WinappDir, '-Psexec', $Psexec)
    if ($WinappUrl) { $cargs += @('-WinappUrl', $WinappUrl) }
    if ($PsexecUrl) { $cargs += @('-PsexecUrl', $PsexecUrl) }
    Say '  已在后台开始下载，进度看下面。' 'Yellow'
    # 源码方式跑时自己是 powershell.exe，得让它去执行这个 .ps1；
    # 编译成 exe 时 $MyInvocation 给的是脚本内容而不是路径，那就直接重开自己
    $sp = $MyInvocation.MyCommand.Definition
    if ($sp -and $sp.Length -lt 400 -and (Test-Path $sp) -and $sp -like '*.ps1') {
        return Start-Process -FilePath 'powershell.exe' `
            -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sp) + $cargs) `
            -WindowStyle Hidden -PassThru
    }
    $self = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    return Start-Process -FilePath $self -ArgumentList $cargs -WindowStyle Hidden -PassThru
}

# 控制台里等下载：每 5 秒报一次进度，下完重新体检
function Show-ProgressConsole($proc) {
    $cap = (Get-Date).AddMinutes(45)
    $lastPrint = (Get-Date).AddSeconds(-10)
    $lastText = ''
    while (-not $proc.HasExited -and (Get-Date) -lt $cap) {
        Start-Sleep -Milliseconds 800
        $st = Read-Progress
        if ($st) {
            $text = Format-Progress $st
            if ($text -ne $lastText -and ((Get-Date) - $lastPrint).TotalSeconds -ge 5) {
                Say "  $text" 'Yellow'; $lastText = $text; $lastPrint = Get-Date
            }
        }
    }
    $st = Read-Progress
    if ($st) { Say "  $(Format-Progress $st)" 'Yellow' }
}

# ================================================================ 跑
Invoke-Checks
Render-Summary

$toFetch = @($script:rows | Where-Object { $_.状态 -eq '缺' })
$script:dlProc = $null
if ($toFetch.Count -gt 0 -and $AllowChange) { $script:dlProc = Start-Downloads }

if (-not $Gui) {
    if ($script:dlProc) {
        Show-ProgressConsole $script:dlProc
        $script:buf.Clear()
        Invoke-Checks
        Render-Summary
    }
    return
}

# ---------------------------------------------------------------- 窗口显示（没有控制台时）
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form            = New-Object System.Windows.Forms.Form
$form.Text       = '微信回复助手 · 环境体检'
$form.Size       = New-Object System.Drawing.Size(820, 600)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'Sizable'

$box             = New-Object System.Windows.Forms.TextBox
$box.Multiline   = $true
$box.ReadOnly    = $true
$box.ScrollBars  = 'Both'
$box.WordWrap     = $false
$box.Dock        = 'Fill'
foreach ($fname in 'Microsoft YaHei UI', 'Microsoft YaHei', 'SimSun') {
    try { $box.Font = New-Object System.Drawing.Font($fname, 10); break } catch { }
}
$box.Text        = ($script:buf -join "`r`n")
$box.Select(0, 0)

$bar             = New-Object System.Windows.Forms.FlowLayoutPanel
$bar.Dock        = 'Bottom'
$bar.Height      = 46
$bar.FlowDirection = 'RightToLeft'
$bar.Padding     = New-Object System.Windows.Forms.Padding(8)

$btnClose        = New-Object System.Windows.Forms.Button
$btnClose.Text   = '关闭'
$btnClose.Width  = 96
$btnClose.Add_Click({ $form.Close() })

$btnCopy         = New-Object System.Windows.Forms.Button
$btnCopy.Text    = '复制结果'
$btnCopy.Width   = 96
$btnCopy.Add_Click({ [System.Windows.Forms.Clipboard]::SetText($box.Text) })

$btnAuto         = New-Object System.Windows.Forms.Button
$btnAuto.Text    = '重新检测'
$btnAuto.Width   = 120

$pb              = New-Object System.Windows.Forms.ProgressBar
$pb.Dock          = 'Bottom'
$pb.Height        = 16
$pb.Style         = 'Continuous'

$lbl             = New-Object System.Windows.Forms.Label
$lbl.Dock         = 'Bottom'
$lbl.Height       = 24
$lbl.TextAlign    = 'MiddleLeft'
$lbl.Padding      = New-Object System.Windows.Forms.Padding(10, 0, 0, 0)

$bar.Controls.Add($btnClose)
$bar.Controls.Add($btnCopy)
$bar.Controls.Add($btnAuto)
$form.Controls.Add($box)
$form.Controls.Add($bar)
$form.Controls.Add($pb)
$form.Controls.Add($lbl)

function Refresh-Report {
    $script:buf.Clear()
    Invoke-Checks
    Render-Summary
    $box.Text = ($script:buf -join "`r`n")
    $bad2 = @($script:rows | Where-Object { $_.状态 -in @('FAIL', '缺') })
    $form.Text = if ($bad2.Count -eq 0) { '微信回复助手 · 环境体检 —— 全部通过' } else { "微信回复助手 · 环境体检 —— 有 $($bad2.Count) 项要处理" }
    return $bad2.Count
}

Refresh-Report | Out-Null

$t = New-Object System.Windows.Forms.Timer
$t.Interval = 500
$t.Add_Tick({
    $st = Read-Progress
    if ($st) {
        if ($st[3] -eq 'dl' -and [long]$st[2] -gt 0) {
            $pb.Style = 'Continuous'
            $pb.Value = [int][Math]::Min(100, [Math]::Round(100 * [long]$st[1] / [long]$st[2]))
        } elseif ($st[3] -eq 'exp') {
            $pb.Style = 'Marquee'
        }
        $lbl.Text = Format-Progress $st
    }
    $p = $script:dlProc
    if ($p -and $p.HasExited) {
        $t.Stop()
        $st = Read-Progress
        $n = Refresh-Report
        if ($st -and $st[3] -eq 'done' -and $n -eq 0) {
            $pb.Style = 'Continuous'; $pb.Value = 100
            $lbl.Text = '补齐完成，已重新体检一遍'
        } elseif ($st -and $st[3] -eq 'fail') {
            $pb.Style = 'Continuous'; $pb.Value = 0
            $lbl.Text = "没补上：$($st[4])"
        } else {
            $lbl.Text = "有 $n 项还没过，点「重新检测」再试一次"
        }
        $btnAuto.Enabled = $true
    }
})

$btnAuto.Add_Click({
    $need = @($script:rows | Where-Object { $_.状态 -eq '缺' })
    if ($AllowChange -and $need.Count -gt 0) {
        $script:dlProc = Start-Downloads
        $lbl.Text = '重新开始下载…'
        $t.Start()
    } else {
        Refresh-Report | Out-Null
    }
})

if ($script:dlProc) {
    $lbl.Text = '缺的东西已在后台下载…'
    $t.Start()
} else {
    $lbl.Text = '没有要补的'
}

$form.Add_Shown({ $form.Activate() })
[void]$form.ShowDialog()
