'use strict';
// 微信回复助手 —— 桌面壳（Electron）
//
// 这个壳只干三件事：
//   1. 把 Python 后端拉起来（采集 / 自动生成 / 面板，由 assistant.py 统一管）
//   2. 等它健康后，直接开一个窗口进面板 —— 带进门票，不用密码、没有地址栏
//   3. 关窗口 = 收进托盘；真退出 = 把后端一起收掉
//
// 采集脚本、微信、后端都在同一个登录会话里跑，不借 PsExec（那是跨会话才要的）。
// 运行环境（Python + 依赖 + winapp.exe）随安装包一起带，用户机器上不需要装任何东西。

const { app, BrowserWindow, Tray, Menu, dialog, shell, nativeImage } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const PORT = Number(process.env.WXREPLY_PORT || 8801);
const DEV = !app.isPackaged;

// 数据目录固定成 ASCII 名字：中文目录在日志/命令行里会变乱码，排查时很烦。
// （快捷方式、窗口标题仍然是「微信回复助手」，用户看到的不变。）
app.setPath('userData', path.join(app.getPath('appData'), 'wx-reply-assistant'));
const RES = DEV ? path.resolve(__dirname, '..') : process.resourcesPath;
const PY_ROOT = DEV ? RES : path.join(RES, 'py');
const BUNDLED_PY = path.join(RES, 'python', 'python.exe');
const BUNDLED_WINAPP = path.join(RES, 'winapp', 'winapp.exe');
// 体检补下来的运行时放这（自带那份被删/被杀软清掉时的退路）
const RUNTIME_DIR = path.join(app.getPath('userData'), 'runtime');
const PREFLIGHT = DEV ? path.join(__dirname, 'preflight.ps1') : path.join(RES, 'preflight.ps1');
const ICON_PNG = path.join(__dirname, 'build', 'icon.png');
const ICON_ICO = path.join(__dirname, 'build', 'icon.ico');

let win = null;
let tray = null;
let python = null;          // { cmd, args } —— -3 这种要带前缀参数
let quitting = false;
let busy = false;           // 正在起/停后端
let logsDir = null;
let logFile = null;

// ---------------------------------------------------------------- 日志
function openLog() {
  logsDir = path.join(app.getPath('userData'), 'logs');
  try { fs.mkdirSync(logsDir, { recursive: true }); } catch {}
  logFile = path.join(logsDir, 'desktop.log');
}

function log() {
  const line = new Date().toISOString() + ' ' +
    Array.from(arguments).map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
  try { fs.appendFileSync(logFile, line + '\n', 'utf8'); } catch {}
  if (DEV) console.log(line);
}

// ---------------------------------------------------------------- 运行环境
function probePython(cand) {
  try {
    const r = spawnSync(cand.cmd, cand.args.concat(['-c', 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)']),
      { timeout: 15000, windowsHide: true });
    return r.status === 0;
  } catch { return false; }
}

// 版本够 ≠ 能跑：面板要 PIL/numpy/yaml，缺一个都起不来（自检 10061）。
// 只验版本号会挑中一个跑不了的运行时，所以这里真 import 一次。
function probePythonDeps(cand) {
  try {
    const r = spawnSync(cand.cmd, cand.args.concat(['-c', 'import PIL, numpy, yaml']),
      { timeout: 20000, windowsHide: true });
    return r.status === 0;
  } catch { return false; }
}

function findPython() {
  const env = process.env.WXREPLY_PYTHON;
  const cands = [];
  if (env) cands.push({ cmd: env, args: [] });
  if (fs.existsSync(BUNDLED_PY)) cands.push({ cmd: BUNDLED_PY, args: [] });
  const runtimePy = path.join(RUNTIME_DIR, 'python', 'python.exe');
  if (fs.existsSync(runtimePy)) cands.push({ cmd: runtimePy, args: [] });
  cands.push({ cmd: 'py', args: ['-3'] });
  cands.push({ cmd: 'python', args: [] });
  cands.push({ cmd: 'C:\\Py311\\python.exe', args: [] });
  // 先挑「版本够 + 依赖齐」的；一个都没有再退回只看版本（WXREPLY_PYTHON 指定的一般走这条），
  // 并记一条日志 —— 依赖不全还硬用会以 10061 静默失败，日志里得留痕。
  for (const c of cands) {
    if (probePython(c) && probePythonDeps(c)) { log('python:', c.cmd, c.args.join(' ')); return c; }
  }
  for (const c of cands) {
    if (probePython(c)) { log('python(依赖不全，仍用):', c.cmd, c.args.join(' ')); return c; }
  }
  return null;
}

function childEnv() {
  const env = Object.assign({}, process.env, {
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    // 数据放用户目录，不写安装目录（安装目录可能只读、升级还会被清）
    WXREPLY_STORE: path.join(app.getPath('userData'), 'store'),
  });
  if (fs.existsSync(BUNDLED_WINAPP)) env.WXREPLY_WINAPP = BUNDLED_WINAPP;
  else {
    const runtimeWa = path.join(RUNTIME_DIR, 'winapp', 'winapp.exe');
    if (fs.existsSync(runtimeWa)) env.WXREPLY_WINAPP = runtimeWa;
  }
  return env;
}

// 跑一次 assistant.py 的子命令，收完整输出
function runPython(args, timeoutMs) {
  return new Promise((resolve) => {
    const p = spawn(python.cmd, python.args.concat(args), {
      cwd: PY_ROOT, env: childEnv(), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    p.stdout.on('data', d => { out += d.toString('utf8'); });
    p.stderr.on('data', d => { out += d.toString('utf8'); });
    const t = setTimeout(() => { try { p.kill(); } catch {} }, timeoutMs || 30000);
    p.on('error', e => { clearTimeout(t); resolve({ code: -1, out: out + '\n' + e.message }); });
    p.on('close', code => { clearTimeout(t); resolve({ code: code, out: out }); });
  });
}

function getJson(url, timeoutMs) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs || 3000 }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on('timeout', () => { req.destroy(); resolve(0); });
    req.on('error', () => resolve(0));
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitHealth(totalMs) {
  const deadline = Date.now() + totalMs;
  while (Date.now() < deadline) {
    if (await getJson('http://127.0.0.1:' + PORT + '/api/health', 2500) === 200) return true;
    await sleep(800);
  }
  return false;
}

async function entryUrl() {
  const r = await runPython(['assistant.py', 'url', '--port', String(PORT)], 20000);
  const m = (r.out || '').match(/http:\/\/127\.0\.0\.1:\d+\/\?k=\S+/);
  return m ? m[0] : '';
}

function setStatus(text) {
  if (!win || win.isDestroyed()) return;
  win.webContents.executeJavaScript('window.__setStatus(' + JSON.stringify(text) + ')')
    .catch(() => {});
}

// 8801 上残留的监听先清掉：Windows 允许两个 socket 同绑一个端口（SO_REUSEADDR），
// 旧面板没退干净时请求会随机落到旧的那份上 —— 症状很邪门，比如「刚签出来的票子
// 被说成过期」。只清我们自己的，判据只认证据：exe 落在应用目录里，或命令行同时
// 指向应用目录 + 本应用脚本。不能只看进程名 python/pythonw —— 那会把用户自己的、
// 恰好监听本端口的 python 服务一起杀掉（审计 F-10）。
function freePort() {
  // 前缀以 \ 收尾（复核 R2-5）：没有它，wx-reply-assistant-backup\ 里的进程
  // 会被 wx-reply-assistant\ 的前缀匹配命中而误杀。
  const myDir = (path.dirname(process.execPath).toLowerCase().replace(/'/g, "''")
                 .replace(/[\\/]+$/, "") + "\\");
  const ps = [
    `$dir = '${myDir}'`,
    `Get-NetTCPConnection -LocalPort ${PORT} -State Listen -ErrorAction SilentlyContinue | ForEach-Object {`,
    `  $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue`,
    `  if (-not $p) { return }`,
    `  $cmd = ''`,
    `  try { $cmd = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p.Id) -ErrorAction SilentlyContinue).CommandLine } catch {}`,
    `  $mine = ($p.Path -and $p.Path.ToLower().StartsWith($dir)) -or`,
    `          ($cmd -and $cmd.ToLower().Contains($dir) -and $cmd -match 'assistant\\.py|server\\.py|wx_collector\\.py')`,
    `  if ($mine) {`,
    `    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue`,
    `    Write-Output ('freed ' + $p.Id + ' ' + $p.ProcessName)`,
    `  }`,
    `}`,
  ].join('\n');
  try {
    const r = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                        { windowsHide: true, timeout: 15000, encoding: 'utf8' });
    const out = ((r.stdout || '') + (r.stderr || '')).trim();
    if (out) log('freePort:', out);
  } catch (e) { log('freePort 失败:', String(e)); }
}

// ---------------------------------------------------------------- 环境体检
// 跑 preflight.ps1：不带 fix 只体检；带 fix 会顺手补齐（缺运行时就从官方源重下、
// 微信没开就拉起来、端口被自己旧进程占着就清掉、数据目录没有就建）。
// 返回 null = 脚本不在或没吐出 JSON（源码方式跑、或者杀软把脚本吃了），
// 这时静默放过 —— 体检是帮忙的，不能因为它挂了就打不开面板。
function runPreflight(fix) {
  return new Promise((resolve) => {
    if (!fs.existsSync(PREFLIGHT)) { log('preflight 不在，跳过:', PREFLIGHT); return resolve(null); }
    const args = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', PREFLIGHT,
                  '-ResDir', RES,
                  '-DataDir', app.getPath('userData'),
                  '-Port', String(PORT)];
    if (fix) args.push('-Fix');
    try {
      const r = spawnSync('powershell.exe', args, { windowsHide: true, timeout: 240000, encoding: 'utf8' });
      const s = r.stdout || '';
      const i = s.indexOf('{'), j = s.lastIndexOf('}');
      if (i >= 0 && j > i) {
        try { return resolve(JSON.parse(s.slice(i, j + 1))); }
        catch (e) { log('preflight JSON 解析失败:', String(e), s.slice(0, 300)); }
      }
      log('preflight 没输出 JSON rc=' + r.status, (r.stderr || '').slice(0, 400));
      resolve(null);
    } catch (e) { log('preflight 跑不起来:', String(e)); resolve(null); }
  });
}

function reportPreflight(pf) {
  const line = (it) => (it.state === 'ok' ? '[ok] ' : '[!!] ') + it.name + ' —— ' + it.detail;
  log('preflight blockers=' + pf.blockers + ' fixed=' + pf.fixed + ' background=' + pf.background + '\n' +
      pf.items.map(line).join('\n') +
      (pf.notes && pf.notes.length ? '\n备注: ' + pf.notes.join(' | ') : ''));
  const bad = pf.items.filter(i => i.state !== 'ok');
  if (!bad.length) return;
  dialog.showMessageBox({
    type: pf.blockers ? 'warning' : 'info',
    title: '微信回复助手 · 环境体检',
    message: pf.blockers ? (pf.blockers + ' 项要处理') : '有缺失，已在后台补',
    detail: bad.map(line).join('\n') + '\n\n面板照常打开，这几项不影响你先配模型。',
    buttons: ['知道了'],
  }).catch(() => {});
}

// ---------------------------------------------------------------- 起 / 停后端
async function boot() {
  if (busy) return;
  busy = true;
  try {
    setStatus('清理上一次运行的残留…');
    log('--- boot ---');
    // 上一次没退干净（崩了、强杀过）时用它收尾，顺手把旧面板停掉
    await runPython(['assistant.py', 'down'], 25000);
    freePort();

    setStatus('正在拉起采集和面板…');
    const up = await runPython(['assistant.py', 'up', '--port', String(PORT), '--no-open'], 200000);
    log('up rc=' + up.code + '\n' + up.out);

    if (!(await waitHealth(40000))) {
      dialog.showMessageBox({
        type: 'error', title: '微信回复助手',
        message: '后端没起来',
        detail: '窗口起不来，多半是 Python 环境或 8801 端口被占。\n\n最近日志：\n' +
                (up.out || '(空)').slice(-1200),
        buttons: ['好'],
      });
      setStatus('没起来，看一下日志');
      return;
    }

    const url = await entryUrl() || ('http://127.0.0.1:' + PORT + '/');
    log('load', url.replace(/k=[0-9a-f]+/, 'k=***'));
    await win.loadURL(url);
  } finally {
    busy = false;
  }
}

async function shutdown() {
  log('--- down ---');
  try {
    await new Promise((resolve) => {
      const p = spawn(python.cmd, python.args.concat(['assistant.py', 'down']), {
        cwd: PY_ROOT, env: childEnv(), windowsHide: true, stdio: 'ignore', detached: false,
      });
      const t = setTimeout(() => { try { p.kill(); } catch {} resolve(); }, 25000);
      p.on('close', () => { clearTimeout(t); resolve(); });
      p.on('error', () => { clearTimeout(t); resolve(); });
    });
  } catch (e) { log('down 失败:', String(e)); }
}

// ---------------------------------------------------------------- 窗口 / 托盘
function createWindow() {
  win = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 880,
    minHeight: 600,
    title: '微信回复助手',
    backgroundColor: '#1A1A1A',
    autoHideMenuBar: true,
    show: false,
    icon: fs.existsSync(ICON_PNG) ? ICON_PNG : undefined,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.once('ready-to-show', () => { win.show(); });
  win.on('close', (e) => {
    if (!quitting) { e.preventDefault(); win.hide(); }   // 关窗口只是收进托盘
  });
  // 面板里的外链（比如 GitHub）丢给系统浏览器，别在壳里开
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url) && !/^http:\/\/127\.0\.0\.1/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // 导航拦截：壳里只许留在本面板。setWindowOpenHandler 只管子窗口，整窗导航
  // （location.href / 302 / meta refresh）得靠 will-navigate/will-redirect ——
  // 不然将来任何一处富文本疏漏都能把无地址栏的窗口带去钓鱼页（审计 F-17）。
  const panelOrigin = 'http://127.0.0.1:' + PORT;
  const allowNav = (url) => url === panelOrigin || url.startsWith(panelOrigin + '/') || url.startsWith(panelOrigin + '?');
  win.webContents.on('will-navigate', (e, url) => {
    if (!allowNav(url)) { e.preventDefault(); log('blocked navigate:', url); }
  });
  win.webContents.on('will-redirect', (e, url) => {
    if (!allowNav(url)) { e.preventDefault(); log('blocked redirect:', url); }
  });
  win.loadFile(path.join(__dirname, 'loading.html'));
}

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createTray() {
  const icon = fs.existsSync(ICON_ICO) ? ICON_ICO : ICON_PNG;
  if (!fs.existsSync(icon)) return;
  tray = new Tray(nativeImage.createFromPath(icon));
  tray.setToolTip('微信回复助手');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开面板', click: showWindow },
    { label: '重启后端', click: () => { boot().then(showWindow); } },
    { label: '看日志', click: () => { try { shell.openPath(logsDir); } catch {} } },
    { type: 'separator' },
    { label: '退出', click: () => { app.quit(); } },
  ]));
  tray.on('click', () => { win && win.isVisible() ? win.hide() : showWindow(); });
  tray.on('double-click', showWindow);
}

// ---------------------------------------------------------------- 入口
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { showWindow(); });

  app.setAppUserModelId('com.khriaaaa.wxreply');
  if (process.env.WXREPLY_NOSANDBOX === '1') {
    // 给「以 SYSTEM 身份在虚机里做验收」用，普通用户用不到
    app.commandLine.appendSwitch('no-sandbox');
  }

  app.whenReady().then(async () => {
    openLog();
    Menu.setApplicationMenu(null);
    createWindow();
    createTray();
    // 先体检：缺什么补什么（自带运行时、微信、端口、数据目录）。
    // 必须排在 findPython 前面 —— 补下来的那份 Python 也得能被找到。
    const pf = await runPreflight(true);
    python = findPython();
    if (!python) {
      dialog.showMessageBoxSync({
        type: 'error', title: '微信回复助手',
        message: '没找到 Python 运行环境',
        detail: '安装包应该自带一份 Python。如果是源码方式跑（npm start），\n' +
                '请先装好 Python 3.11+ 并勾选 Add to PATH，或用 WXREPLY_PYTHON 指定解释器。',
        buttons: ['好'],
      });
      app.exit(1);
      return;
    }
    if (pf) reportPreflight(pf);
    boot();
  });

  app.on('window-all-closed', (e) => { /* 收进托盘，不退 */ });

  app.on('before-quit', async (e) => {
    if (quitting) return;
    e.preventDefault();
    quitting = true;
    if (win && !win.isDestroyed()) win.hide();
    await shutdown();
    app.exit(0);
  });
}
