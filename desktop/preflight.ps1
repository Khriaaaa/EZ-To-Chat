# preflight.ps1 —— 启动前环境体检 / 自动补齐
#
# 由 Electron 主进程调用：
#   powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 \
#     -ResDir <resources> -DataDir <%APPDATA%\wx-reply-assistant> -Port 8801 [-Fix]
#
# 不带 -Fix：只体检，报缺什么。
# 带  -Fix：缺什么补什么。轻的当场补（Python 运行时秒级、微信没开就拉起来、端口被自己
#           旧进程占着就清掉、数据目录没有就建）；winapp 那个 94MB 不挡启动，丢后台去下，
#           补完下次启动就位。
# 输出：一行 JSON  {"items":[{id,name,state,detail}...],"blockers":N,"fixed":N,"background":N,"notes":[...]}
#       state: ok=没问题  fix=已自动处理或正在后台补  fail=要人工
#
# 注意：本文件必须带 UTF-8 BOM，否则 PS 5.1 会按 ANSI 读，中文全乱。

param(
    [string]$ResDir  = '',
    [string]$DataDir = '',
    [int]$Port       = 8801,
    [switch]$Fix,
    [string]$DownloadOnly = ''      # 内部用：后台子进程只下一个组件然后退出
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RuntimeDir = Join-Path $DataDir 'runtime'
$UA         = 'wx-reply-assistant'
$PY_VER     = '3.11.9'
# winapp 下载锁死在具体版本 + SHA256（审计 F-01）：不用 releases/latest 动态挑资产，
# 下载链路（第三方镜像/劫持）改一个字节都会在哈希校验那里被挡下。
# 升版本时：改 $WA_TAG，并把 $WA_ZIP_SHA256 换成新 zip 的 sha256（GitHub 附件页可算）。
$WA_TAG        = 'v0.7.1'
$WA_ZIP_SHA256 = 'd925d1e32cdc320b6d271f653fd2cd3c05b5d7558deb20d1b548bead77900fcf'
$WA_API     = "https://api.github.com/repos/microsoft/winappCli/releases/tags/$WA_TAG"

$items = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

function Add-Item([string]$id, [string]$name, [string]$state, [string]$detail) {
    [void]$items.Add([ordered]@{ id = $id; name = $name; state = $state; detail = $detail })
}
function Note([string]$s) { [void]$notes.Add($s) }

Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- 下载
# 直连 GitHub 常年只有几十 KB/s，所以每个组件都备几条快路，按实测速度从快到慢试。
function FetchUrl([string]$url, [string]$dst, [int]$secs) {
    $h = $null
    try {
        $d = Split-Path $dst -Parent
        if ($d -and -not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
        $h = New-Object System.Net.Http.HttpClient
        $h.Timeout = [TimeSpan]::FromSeconds($secs)
        $h.DefaultRequestHeaders.Add('User-Agent', $UA)
        $resp = $h.GetAsync($url).GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) { Note ("HTTP " + [int]$resp.StatusCode + " —— " + $url); return $false }
        $data = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if (-not $data -or $data.Length -eq 0) { Note "下下来是空的 —— $url"; return $false }
        [IO.File]::WriteAllBytes($dst, $data)
        return $true
    } catch {
        Note "下载失败 $url —— $($_.Exception.Message)"
        return $false
    } finally {
        if ($h) { try { $h.Dispose() } catch {} }
    }
}

function FetchAny($urls, [string]$dst, [int]$secs) {
    foreach ($u in $urls) {
        if (-not $u) { continue }
        Note "试着下 $u"
        if (FetchUrl $u $dst $secs) { Note "下成功 $u"; return $true }
        Remove-Item $dst -Force -ErrorAction SilentlyContinue
    }
    return $false
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

function Get-WinappUrls {
    $rel = Invoke-RestMethod -Uri $WA_API -Headers @{ 'User-Agent' = $UA } -TimeoutSec 30
    $asset = $rel.assets | Where-Object { $_.name -match 'x64.*\.zip$' } | Select-Object -First 1
    if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1 }
    if (-not $asset) { throw 'GitHub 上没有 zip 资产' }
    $u = $asset.browser_download_url
    return @(('https://ghfast.top/' + $u), ('https://gh-proxy.com/' + $u), $u)
}

# ---------------------------------------------------------------- 后台子进程模式
if ($DownloadOnly) {
    $lg = Join-Path $RuntimeDir ("{0}-download.log" -f $DownloadOnly)
    if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }
    function Lg($s) { ((Get-Date).ToString('s') + ' ' + $s) | Out-File -FilePath $lg -Encoding utf8 -Append }
    Lg "start $DownloadOnly"
    try {
        if ($DownloadOnly -eq 'winapp') {
            $zip = Join-Path $env:TEMP 'wxreply-winapp.zip'
            $urls = Get-WinappUrls
            Lg ('urls=' + ($urls -join ' | '))
            if (FetchAny $urls $zip 900) {
                $h = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
                if ($h -ne $WA_ZIP_SHA256) {
                    Lg "FAIL 哈希校验不过（期望 $WA_ZIP_SHA256，实际 $h）—— 上游更新或链路被改，先别装"
                    Remove-Item $zip -Force -ErrorAction SilentlyContinue
                } else {
                    Assert-ZipSafe $zip
                    $dst = Join-Path $RuntimeDir 'winapp'
                    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
                    New-Item -ItemType Directory -Path $dst -Force | Out-Null
                    Expand-Archive -Path $zip -DestinationPath $dst -Force
                    Remove-Item $zip -Force -ErrorAction SilentlyContinue
                    # 压缩包里带 331MB 的 .pdb 调试符号，安装包里是剔掉的，这里也剔掉
                    Get-ChildItem $dst -Recurse -Filter '*.pdb' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
                    $exe = Get-ChildItem $dst -Recurse -Filter 'winapp.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($exe) { Lg ('OK ' + $exe.FullName) } else { Lg 'FAIL 解压后没有 winapp.exe' }
                }
            } else { Lg 'FAIL 所有下载源都没成' }
        }
    } catch { Lg ('FAIL ' + $_.Exception.Message) }
    Lg 'done'
    exit 0
}

# ---------------------------------------------------------------- Python 运行时
function Test-Py([string]$p) {
    if (-not $p -or -not (Test-Path $p)) { return '' }
    try { $v = & $p -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>&1 | Select-Object -First 1 } catch { return '' }
    if ("$v" -match '^\d+\.\d+\.\d+$') { return "$v" }
    return ''
}

# 解释器在 ≠ 能跑：依赖（pillow/numpy/pyyaml）没铺全时面板照样起不来（自检报 10061）。
# 所以体检必须真的 import 一次，不能只看版本号 —— 只看版本号正是上一版漏掉裸 Python 的原因。
function Test-PyDeps([string]$p) {
    if (-not $p -or -not (Test-Path $p)) { return $false }
    $o = @()
    try { $o = & $p -c "import PIL, numpy, yaml;print('ok')" 2>&1 } catch { return $false }
    # 逐行 trim 后精确比对，别用 -match —— 报错串里恰好带 "ok" 会假绿
    return (($o | ForEach-Object { "$_".Trim() }) -contains 'ok')
}

$pyPath = Join-Path $ResDir 'python\python.exe'
$pyVer  = Test-Py $pyPath
$fromRuntime = $false
if (-not $pyVer) {
    $alt = Join-Path $RuntimeDir 'python\python.exe'
    $v2  = Test-Py $alt
    if ($v2) { $pyVer = $v2; $pyPath = $alt; $fromRuntime = $true }
}
$pyDeps = $false
if ($pyVer) { $pyDeps = Test-PyDeps $pyPath }
if ($pyVer -and $pyDeps) {
    Add-Item 'python' '自带 Python 运行时' 'ok' ("$pyVer" + $(if ($fromRuntime) { '（补下来的那份）' } else { '' }))
} else {
    $why = '没了'
    if ($pyVer) { $why = '依赖不全（pillow/numpy/pyyaml import 不了）' }
    $done = $false
    # 用安装包自带的补包本地解 —— 官方 embeddable 裸包不带 pillow/numpy/pyyaml，
    # 解出来面板起不来（自检会报 10061），所以这里不走网络。
    if ($Fix) {
        $pack = Join-Path $ResDir 'python-pack.zip'
        if (Test-Path $pack) {
            $dst = Join-Path $RuntimeDir 'python'
            if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            try { Expand-Archive -Path $pack -DestinationPath $dst -Force } catch { Note "解 Python 补包失败 —— $($_.Exception.Message)" }
            $np = Join-Path $dst 'python.exe'
            $v3 = Test-Py $np
            if ($v3 -and (Test-PyDeps $np)) { Add-Item 'python' '自带 Python 运行时' 'ok' "$why，已从本地补包还原 $v3"; $done = $true }
            elseif ($v3) { Note '补包解出来了，但依赖还是 import 不了' }
        } else { Note "本地补包不在：$pack" }
    }
    if (-not $done) { Add-Item 'python' '自带 Python 运行时' 'fail' "$why，本地补包也没补上 —— 重装一遍安装包" }
}

# ---------------------------------------------------------------- winapp（94MB，丢后台）
$waPath = Join-Path $ResDir 'winapp\winapp.exe'
if (-not (Test-Path $waPath)) {
    $alt = Join-Path $RuntimeDir 'winapp\winapp.exe'
    if (Test-Path $alt) { $waPath = $alt }
}
if (Test-Path $waPath) {
    Add-Item 'winapp' '自带 winapp（读微信界面用）' 'ok' $waPath
} elseif ($Fix) {
    $started = $false
    try {
        $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ResDir "{1}" -DataDir "{2}" -DownloadOnly winapp' -f $PSCommandPath, $ResDir, $DataDir
        Start-Process -FilePath $ps -ArgumentList $argLine -WindowStyle Hidden | Out-Null
        $started = $true
    } catch { Note "起后台下载失败 —— $($_.Exception.Message)" }
    if ($started) {
        Add-Item 'winapp' '自带 winapp（读微信界面用）' 'fix' '没了，已在后台补（约 94MB，看网络要几分钟）；补完下次启动就位'
    } else {
        Add-Item 'winapp' '自带 winapp（读微信界面用）' 'fail' '没找到，也起不了后台下载 —— 重装一遍安装包'
    }
} else {
    Add-Item 'winapp' '自带 winapp（读微信界面用）' 'fix' '没了，下次启动会自动补'
}

# ---------------------------------------------------------------- 程序文件
if (Test-Path (Join-Path $ResDir 'py\assistant.py')) {
    Add-Item 'app' '程序文件' 'ok' 'py\assistant.py 在位'
} else {
    Add-Item 'app' '程序文件' 'fail' '安装目录里没有 py\assistant.py —— 重装一遍安装包'
}

# ---------------------------------------------------------------- 微信客户端
$wxCands = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root) {
        $wxCands += (Join-Path $root 'Tencent\Weixin\Weixin.exe')
        $wxCands += (Join-Path $root 'Tencent\WeChat\WeChat.exe')
    }
}
$wx = $wxCands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $wx) {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $hit = Get-ItemProperty $keys -ErrorAction SilentlyContinue |
           Where-Object { $_.DisplayName -match '微信|WeChat|Weixin' } | Select-Object -First 1
    if ($hit -and $hit.DisplayIcon) {
        $c = ($hit.DisplayIcon -split ',')[0].Trim('"').Trim()
        if ($c -and (Test-Path $c)) { $wx = $c }
    }
}
if (-not $wx) {
    Add-Item 'wechat' '微信客户端' 'fail' '这台机器没装微信 —— 先装并登录（weixin.qq.com），采集才有对象'
} else {
    $p = Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) {
        Add-Item 'wechat' '微信客户端' 'ok' "已在运行（#$($p.Id)）"
    } elseif ($Fix) {
        try {
            Start-Process -FilePath $wx
            Start-Sleep -Seconds 4
            $p2 = Get-Process -Name Weixin, WeChat -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($p2) { Add-Item 'wechat' '微信客户端' 'ok' "没开，已拉起来（#$($p2.Id)）" }
            else { Add-Item 'wechat' '微信客户端' 'fail' "装了但没起来：$wx" }
        } catch { Add-Item 'wechat' '微信客户端' 'fail' "起不来 —— $($_.Exception.Message)" }
    } else {
        Add-Item 'wechat' '微信客户端' 'fix' "装了但没开：$wx"
    }
}

# ---------------------------------------------------------------- 数据目录
if (Test-Path $DataDir) {
    Add-Item 'datadir' '数据目录可写' 'ok' $DataDir
} elseif ($Fix) {
    try { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null } catch { Note "建数据目录失败 —— $($_.Exception.Message)" }
    if (Test-Path $DataDir) { Add-Item 'datadir' '数据目录可写' 'ok' "没有，已建 $DataDir" }
    else { Add-Item 'datadir' '数据目录可写' 'fail' "建不了 $DataDir" }
} else {
    Add-Item 'datadir' '数据目录可写' 'fix' '还没有，启动时自动建'
}

# ---------------------------------------------------------------- 端口
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Add-Item 'port' "面板端口 $Port" 'ok' '空着'
} else {
    $owner = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    # 「是不是自己」只认证据（复核 F-10）：命令行要同时指向安装目录 + 本应用脚本，或可执行文件落在安装目录里。
    # 不能只看进程名是 python/pythonw —— 那会把用户机器上任何占用本端口的 python 服务一起杀掉。
    $mine = $false
    if ($owner) {
        $cmd = ''
        try { $cmd = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue).CommandLine } catch {}
        # 前缀必须以「\」收尾（复核 R2-5）：不然 ...\wx-reply-assistant-backup\
        # 也会被 ...\wx-reply-assistant 的前缀匹配命中，整目录误杀。
        $dirN = ($ResDir.TrimEnd('\') + '\').ToLower()
        if ($cmd -and $dirN.Length -gt 1 -and $cmd.ToLower().Contains($dirN) -and $cmd -match 'assistant\.py|server\.py') { $mine = $true }
        elseif ($owner.Path -and $dirN.Length -gt 1 -and $owner.Path.ToLower().StartsWith($dirN)) { $mine = $true }
    }
    if ($mine -and $Fix) {
        try { Stop-Process -Id $owner.Id -Force -ErrorAction Stop; Add-Item 'port' "面板端口 $Port" 'ok' "清掉了上次残留的 $($owner.ProcessName) #$($owner.Id)" }
        catch { Add-Item 'port' "面板端口 $Port" 'fail' "清不掉 $($owner.ProcessName) #$($owner.Id)" }
    } elseif ($mine) {
        Add-Item 'port' "面板端口 $Port" 'fix' "被上次残留的 $($owner.ProcessName) #$($owner.Id) 占着"
    } else {
        $nm = if ($owner) { "$($owner.ProcessName) #$($owner.Id)" } else { "PID $($conn.OwningProcess)" }
        Add-Item 'port' "面板端口 $Port" 'fail' "被别的程序占着（$nm）—— 关掉它，或用 WXREPLY_PORT 换端口"
    }
}

# ---------------------------------------------------------------- VC++ 运行库
$vc = @('vcruntime140.dll', 'vcruntime140_1.dll') | Where-Object { -not (Test-Path (Join-Path $env:SystemRoot "System32\$_")) }
if ($vc.Count -eq 0) {
    Add-Item 'vcrun' 'VC++ 运行库' 'ok' '在'
} else {
    Add-Item 'vcrun' 'VC++ 运行库' 'fail' ("缺 " + ($vc -join '、') + " —— 装一下 Microsoft Visual C++ 2015-2022 可再发行组件（x64）")
}

# ---------------------------------------------------------------- 输出
$bad = @($items | Where-Object { $_.state -eq 'fail' })
$out = [ordered]@{
    items      = $items
    blockers   = $bad.Count
    fixed      = @($items | Where-Object { $_.state -eq 'fix' }).Count
    background = @($items | Where-Object { $_.id -eq 'winapp' -and $_.state -eq 'fix' }).Count
    notes      = $notes
}
$out | ConvertTo-Json -Depth 6 -Compress
