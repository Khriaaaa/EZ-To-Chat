#!/usr/bin/env python3
"""改动的自测：会话规则 / 降级校验 / JSON 挖取 / score 校验 / 跨进程锁 / 去重 / 缓存 / seen 有序

用法：python3 tests/test_review_fixes.py
只用标准库 + 临时目录，不碰 store/ 里的真实数据（最后一项是只读回归）。
"""
import http.cookiejar
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
ok_n = fail_n = 0


def check(name, cond, detail=''):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f'  [ok]   {name}' + (f'  {detail}' if detail else ''))
    else:
        fail_n += 1
        print(f'  [FAIL] {name}  {detail}')


def wj(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')


def rj(p):
    return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()] if p.exists() else []


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def wait_health(base, tries=40):
    for _ in range(tries):
        try:
            urllib.request.urlopen(base + '/api/health', timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def login(base, store):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    pw = (store / '.panel_password').read_text().strip()
    op.open(urllib.request.Request(base + '/login', data=urllib.parse.urlencode({'password': pw}).encode()), timeout=10)
    return op


def good_obj(score=80):
    return {"strategy": "先接话再给时间", "intent": "约时间", "need": "想知道你几点有空", "tension": 3,
            "candidates": [{"text": "有空呀，几点", "style": "自然", "score": score},
                           {"text": "刚下课，你说", "style": "简短", "score": score - 10},
                           {"text": "今晚有点事，明天行吗", "style": "留余地", "score": score - 20}]}


TMP = pathlib.Path(tempfile.mkdtemp(prefix='wxreply-test-'))
g = load(ROOT / 'orchestrator' / 'generate.py', 'gen_under_test')

print('\n=== 1. pick_session：两个会话，最后一行是我在另一个会话发的 ===')
msgs = [
    {"session": "B", "sender": "them", "text": "到家了", "fp": "fB"},
    {"session": "B", "sender": "me", "text": "好", "fp": "fB2"},
    {"session": "A", "sender": "them", "text": "明天有空吗", "fp": "fA"},
    {"session": "B", "sender": "me", "text": "在吗", "fp": "fB3"},
]
check('取「最后一条对方消息」的会话 A', g.pick_session(msgs) == 'A', str(g.pick_session(msgs)))
check('旧的 msgs[-1] 规则会给出 B（这就是那个 bug）', msgs[-1]['session'] == 'B')
check('没有对方消息时退回最后一条', g.pick_session([{"session": "C", "sender": "me"}]) == 'C')
check('混进坏行也不炸', g.pick_session([None, "x", {"session": "D", "sender": "them"}]) == 'D')

print('\n=== 2. 校验失败要能降级到备用 provider ===')
calls = []
g.load_providers = lambda override=None: [("主", "http://a", "k", "m1"), ("备", "http://b", "k", "m2")]


def fake_try_one(base, key, model, prompt, verbose, timeout=None):
    calls.append(model)
    if model == 'm1':
        return '好的，以下是建议：\n```json\n{"candidates": [{"text": "a", "style": "s", "score": 80}]}\n```\n希望有帮助'
    return json.dumps(good_obj(), ensure_ascii=False)


g._try_one = fake_try_one
obj, label, model = g.call_llm('prompt', False)
check('坏返回后换到备用 provider', model == 'm2' and label == '备', f'calls={calls}')
check('备用 provider 的结果通过校验', obj['candidates'][0]['text'] == '有空呀，几点')

g._try_one = lambda *a, **k: '这不是 JSON'
try:
    g.call_llm('prompt', False)
    check('两个都坏时报错退出', False)
except SystemExit as e:
    # 全是「内容不合规」→ 退出码 4（EXIT_BAD_OUTPUT），不再笼统地返回 1
    check('两个都坏时报错退出（码 4）', e.code == g.EXIT_BAD_OUTPUT, f'code={e.code}')

print('\n=== 3. extract_json：模型多嘴也要挖得出来 ===')
for name, raw in {
    '纯 JSON': json.dumps(good_obj(), ensure_ascii=False),
    '带围栏': '```json\n' + json.dumps(good_obj(), ensure_ascii=False) + '\n```',
    '前后多嘴': '好的，以下是建议：\n' + json.dumps(good_obj(), ensure_ascii=False) + '\n希望有帮助',
    '围栏+多嘴': '好的：\n```json\n' + json.dumps(good_obj(), ensure_ascii=False) + '\n```\n以上。',
}.items():
    try:
        check(f'解析成功：{name}', len(g.parse_and_validate(raw)['candidates']) == 3)
    except Exception as e:
        check(f'解析成功：{name}', False, str(e)[:80])
try:
    g.parse_and_validate('完全没有 JSON 的一段话')
    check('彻底没有 JSON 要失败', False)
except ValueError as e:
    check('彻底没有 JSON 要失败', 'JSON 解析失败' in str(e))

print('\n=== 4. score / tension 校验与排序 ===')
o = g.parse_and_validate(json.dumps(good_obj(80)).replace('"score": 80', '"score": "80%"'))
check('"80%" 接受并转成数字', o['candidates'][0]['score'] == 80, str(o['candidates'][0]['score']))
for bad, label in (({"score": "abc"}, '非数字'), ({"score": 250}, '越界'), ({"score": None}, 'null')):
    c = [{"text": "a", "style": "s", **bad}, {"text": "b", "style": "s", "score": 50},
         {"text": "c", "style": "s", "score": 40}]
    try:
        g.parse_and_validate(json.dumps({**good_obj(), 'candidates': c}))
        check(f'坏 score（{label}）判坏', False)
    except ValueError:
        check(f'坏 score（{label}）判坏', True)
o = g.parse_and_validate(json.dumps({**good_obj(), 'candidates': [
    {"text": "a", "style": "s", "score": 30}, {"text": "b", "style": "s", "score": 90},
    {"text": "c", "style": "s", "score": 60}]}))
check('按分数降序（第一条=推荐回复）', [c['score'] for c in o['candidates']] == [90, 60, 30])
check('tension 坏了不拖垮整轮', g.parse_and_validate(json.dumps({**good_obj(), 'tension': 'x'}))['tension'] == 5)

print('\n=== 5. 跨进程锁 + 同指纹去重（真跑 CLI）===')
ST = TMP / 'store'
MSGS, SUGS = ST / 'messages.jsonl', ST / 'suggestions.jsonl'
wj(MSGS, [
    {"ts_utc": "2026-10-06T01:00:00Z", "session": "B", "sender": "them", "text": "到家了", "fp": "fB1"},
    {"ts_utc": "2026-10-06T01:01:00Z", "session": "B", "sender": "me", "text": "好", "fp": "fB2"},
    {"ts_utc": "2026-10-06T01:02:00Z", "session": "A", "sender": "them", "text": "明天有空吗", "fp": "fA1"},
    {"ts_utc": "2026-10-06T01:03:00Z", "session": "B", "sender": "me", "text": "在吗", "fp": "fB3"},
])
wj(SUGS, [{"ts_utc": "2026-10-06T01:02:30Z", "session": "A", "provider": "t", "model": "t",
           "strategy": "先接话", "intent": "约时间", "need": "想知道几点", "tension": 3,
           "candidates": [{"text": "有空呀，几点", "style": "自然", "score": 80},
                          {"text": "刚下课，你说", "style": "简短", "score": 70},
                          {"text": "今晚有点事", "style": "留余地", "score": 60}],
           "source_msg_count": 4, "source_last_fp": "fA1"}])
stub = TMP / 'stub.yaml'
stub.write_text('model:\n  default: nope\n  provider: custom:nope\n', encoding='utf-8')
env = dict(os.environ, WXREPLY_CONFIG=str(stub), WXREPLY_BUDGET='10')
GEN = str(ROOT / 'orchestrator' / 'generate.py')


def run_gen(extra=()):
    return subprocess.run([PY, GEN, '--messages', str(MSGS), '--out', str(SUGS), *extra],
                          capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)


r = run_gen()
check('同指纹直接跳过（不调模型）', r.returncode == 0 and '[skip]' in r.stderr, f'rc={r.returncode}')
check('跳过时建议库没多出行', len(rj(SUGS)) == 1)
r = run_gen(['--force'])
check('--force 才会真的往下走', '[skip]' not in r.stderr and r.returncode != 0, f'rc={r.returncode}')

holder = TMP / 'hold_lock.py'
holder.write_text(
    "import importlib.util,pathlib,sys,time\n"
    f"spec=importlib.util.spec_from_file_location('g', {str(ROOT / 'orchestrator' / 'generate.py')!r})\n"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    f"with m.GenLock(pathlib.Path({str(ST)!r})):\n"
    "    print('HELD', flush=True); time.sleep(12)\n", encoding='utf-8')
h = subprocess.Popen([PY, str(holder)], stdout=subprocess.PIPE, text=True)
try:
    check('另一个进程已持锁', 'HELD' in h.stdout.readline())
    t0 = time.time()
    r = run_gen(['--force'])
    dt = time.time() - t0
    check('拿不到锁 → 退出码 75', r.returncode == 75, f'rc={r.returncode}')
    check('不排队干等（立刻退出）', dt < 5, f'{dt:.1f}s')
finally:
    h.terminate()
    try:
        h.wait(timeout=5)
    except Exception:
        h.kill()
time.sleep(0.5)
check('锁释放后回到正常路径', run_gen().returncode == 0)

print('\n=== 6. watch 状态：行游标不丢历史，旧格式按首次处理 ===')
w = load(ROOT / 'orchestrator' / 'watch.py', 'watch_under_test')
w.save_state(ST, 2300, [f'k{i:05d}' for i in range(2300)])
saved = json.loads((ST / 'watcher_seen.json').read_text(encoding='utf-8'))
check('v2 结构带行游标', saved.get('version') == 2 and saved.get('lines') == 2300,
      str(saved.get('lines')))
check('游标后的指纹不再截断到 2000', len(saved['done']) == 2300, str(len(saved['done'])))
lines, done = w.load_state(ST)
check('读回来游标与首尾一致',
      lines == 2300 and done[0] == 'k00000' and done[-1] == 'k02299',
      f'{lines}/{done[0]}..{done[-1]}')
(ST / 'watcher_seen.json').write_text(json.dumps(['a', 'b']), encoding='utf-8')
check('旧格式（纯数组）按首次跑处理，不拿它判历史', w.load_state(ST) is None)

print('\n=== 7. read_jsonl 缓存 ===')
srv = load(ROOT / 'web' / 'server.py', 'srv_under_test')
a1 = srv.read_jsonl(MSGS)
check('第二次命中缓存（同一个对象）', srv.read_jsonl(MSGS) is a1)
with MSGS.open('a', encoding='utf-8') as f:
    f.write(json.dumps({"session": "A", "sender": "them", "text": "睡了", "fp": "fA9"}, ensure_ascii=False) + '\n')
a3 = srv.read_jsonl(MSGS)
check('文件一变立刻重新解析', a3 is not a1 and len(a3) == len(a1) + 1)
check('面板与 generate 的会话规则一致', srv.pick_session(a3) == g.pick_session(a3) == 'A')

print('\n=== 8. 面板端到端（临时 store）===')
port = 8917
proc = subprocess.Popen([PY, str(ROOT / 'web' / 'server.py'), '--port', str(port), '--check-interval', '0'],
                        env=dict(os.environ, WXREPLY_STORE=str(ST)), cwd=str(ROOT),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    base = f'http://127.0.0.1:{port}'
    check('面板起来了', wait_health(base))
    op = login(base, ST)
    st = json.loads(op.open(base + '/api/state', timeout=10).read())
    check('显示会话 = A（最后一条对方消息）', st['session'] == 'A', str(st['session']))
    check('A 的建议没被丢掉', len(st['suggestions']) == 3, str(len(st['suggestions'])))
    check('对话参考是 A 的最新那条对方消息', (st.get('last_other') or {}).get('text') == '睡了')
finally:
    if proc.poll() is None:
        proc.terminate()
    try:
        out = proc.communicate(timeout=6)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
    check('默认只监听本机（启动日志）', '监听 127.0.0.1' in (out or ''))

real = ROOT / 'store'
if (real / '.panel_password').exists():
    print('\n=== 9. 真实 store 只读回归 ===')
    port = 8918
    proc = subprocess.Popen([PY, str(ROOT / 'web' / 'server.py'), '--port', str(port), '--check-interval', '0'],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f'http://127.0.0.1:{port}'
        if wait_health(base):
            st = json.loads(login(base, real).open(base + '/api/state', timeout=10).read())
            check('真实数据能正常出状态', bool(st.get('session')),
                  f"session={st.get('session')} 消息={len(st.get('messages') or [])} 候选={len(st.get('suggestions') or [])}")
        else:
            check('真实 store 面板能起来', False)
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=6)

shutil.rmtree(TMP, ignore_errors=True)
print(f'\n合计：通过 {ok_n}，失败 {fail_n}')
sys.exit(1 if fail_n else 0)
