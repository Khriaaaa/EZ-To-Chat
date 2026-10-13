"""3.2 历史参与上下文：`render_for_ctx` + `load_history_ctx`。

## 设计约束（来自 docs/eztochat-fix-plan.md §2.4/§3.2，写死不许绕）

- 只认 kind 表格：模型看到的每种 kind 长什么样由 RENDER 表定死，**禁止自我扩写**
- 泄漏链接（link_meta）→「分享了一个链接（内容无法预览）」，payload 不许外发
- quote → 原句不可知，老实写「回复了对方的消息」，**不猜原句**
- recall / notice / voice/video/image 等占位不完全一样：人能感觉到的「撤回了」
  在上下文里值得留（对方撤回暗示在遮挡什么），系统通知（红包/验证/转账）不进上下文
- dedup：dup_count>1 的渲染成 「text ×N」，展开条数不变
- 历史只挑**当前活跃会话**，不扫全库；seq 排序为先，不按 ts 排（同秒多条的顺序靠 seq）
- 历史不写回 messages.jsonl，不触发 watch —— 调用方（generate.py）只读

## usage

    from render_history import load_history_ctx
    ctx = load_history_ctx("楚金宇", limit=40)
    if ctx:
        prompt += "\n\n以下是和这个人的历史记录（更早），供参考语气和梗：\n" + ctx
"""
import os
import re
import sqlite3
from pathlib import Path

# 与 generate/server/wx_collector 同义：WXREPLY_STORE 覆盖 store 位置。
# 以前这里写死安装目录下的 store —— 桌面版把数据放在 %APPDATA% 下，这条
# 路径永远不存在，load_history_ctx 会静默返回空（审计 F-19）。
STORE = Path(os.environ.get("WXREPLY_STORE") or
             (Path(__file__).resolve().parent.parent / "store"))

# kind -> 上下文渲染规则. 参数 (text, dup_count). 一个函数收口，别处不许再写第二张表
def _cap(t, n=2000):
    """单条封顶（复核 F-11）：导入的历史里可能有超长消息，别把 prompt 挤爆。"""
    return (t[:n] + " …[截断]") if len(t) > n else t


RENDER = {
    "text":     lambda t, n: (_cap(t) + (f" ×{n}" if n > 1 else "")),
    "emoji":    lambda t, n: f"[表情]{' ×' + str(n) if n > 1 else ''}",
    "image":    lambda t, n: f"[图片]{' ×' + str(n) if n > 1 else ''}",
    "voice":    lambda t, n: f"[语音]{' ×' + str(n) if n > 1 else ''}",
    "video":    lambda t, n: f"[视频]{' ×' + str(n) if n > 1 else ''}",
    "file":     lambda t, n: f"[文件]{' ×' + str(n) if n > 1 else ''}",
    "quote":    lambda t, n: "[回复了对方的一条消息（原句不可知）] " + \
                             _cap(re.sub(r"^\[引用\]\s*", "", t)
                                    .replace("\n", " ")),
    "link":     lambda t, n: "[分享了一个链接]",
    "link_meta": lambda t, n: "[分享了一个链接（内容无法预览）]",
    "recall":   lambda t, n: "[撤回了一条消息（内容不可知）]",
    # 系统通知（验证/红包/拍一拍/打招呼）不进上下文 —— 不是任何人说的话
    "notice":   lambda t, n: None,
}


def render_for_ctx(text, kind, dup_count=1):
    """kind -> 上下文文本。未知 kind 判「未知」不猜，宁可标未知不许编。"""
    fn = RENDER.get(kind)
    if fn is None:
        return f"[未知类型消息({kind})]"
    try:
        out = fn(text, dup_count)
    except Exception:
        return f"[未知类型消息({kind})]"
    return out


def load_history_ctx(session, limit=40, db_path=None):
    """该会话最近 `limit` 条展开消息，渲染成上下文文本.

    条数按「展开后条数」算（dup 展开进 limit 预算），而不是行数 ——
    同一条连发折叠行 ×51 只算一条，但就把该行展开成 51 条重复太蠢：
    定为「折叠行占一条位置，×N 保留」，预算仍按展开条数折算。
    按展开条数往前凑，凑不够 40 条也没关系，有几条凑几条。
    returns "" if 没历史/没该会话 —— generate.py 据此决定是否加历史段
    """
    db = Path(db_path) if db_path else (STORE / "history.db")
    if not db.exists():
        return ""
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "select text, kind, coalesce(dup_count,1) from messages"
            " where session=? order by seq desc", (session,)
        ).fetchall()
    finally:
        con.close()
    # 从新往旧凑到展开条数 >= limit（折叠行 ×N 一下占 N 条展开量，留在行里）
    sel, used = [], 0
    for text, kind, dup in rows:
        sel.append((text, kind, dup))
        used += dup
        if used >= limit:
            break
    sel = sel[::-1]  # seq 升序展示
    out = []
    for text, kind, dup in sel:
        t = render_for_ctx(text, kind, dup)
        if t is None:   # notice 不进
            continue
        out.append(t)
    return "\n".join(out)
