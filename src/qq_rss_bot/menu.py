"""消息按钮（keyboard）菜单的构造与校验。

为什么要单独一个模块：keyboard 是一套**线上 schema**，和 `scripts/setup_panel.py`
里的指令面板 schema 是同一类东西 —— 都属于「接口返回 200 不等于生效」那一类，
值得单独放、单独测。`commands.py` 也已经七百多行了，不该再长。

**这个平台上「200」从来不等于「生效」，按钮尤其**。三条硬约束：

* **`keyboard` 必须包一层 `content`**：``{"content": {"rows": [...]}}``。
  把 `rows` 直接挂在 `keyboard` 底下 → **HTTP 200、消息发得出去、按钮被静默丢弃**
  （2026-10-16 实测踩到）。这和指令面板的 `children`、自定义菜单的 `items`
  是同一个坑的第三次出现。
  注意 `botpy` 的 `types/inline.py` 里 `Keyboard` 是**内层** `{rows}`，
  `KeyboardPayload` 才是外层 `{id, content}` —— 别拿内层当参数传。
* **`render_data.label` ≤ 10 显示宽度**（中文按 2）。这比指令面板的
  `name ≤14` **还要紧** —— `退订 偶像大师` 宽 13，直接超限。
  所以 label 里只放**编号**（`退订 1`，宽 6），订阅名字放正文；
  正文就在按钮上方，不会看不懂。`clamp_label()` 是兜底，
  保证任何 label 都不会超宽，而不是等平台静默把按钮丢掉。
* **`action.type=2` 的按钮只把文字插进输入框，不会自动发送**
  （`enter: true` 才自动发，而官方注明它**仅单聊可用**）。所以按钮的 `data`
  要写成「点完就能直接发」的形态：带编号的写成完整指令（`/unsub 1`），
  需要用户自己补参数的留一个尾空格（`/find `）。

显示宽度的口径和 `setup_panel._width` 是同一个度量，实现放在这里，两边共用一份，
免得 East-Asian-Width 的规则在项目里存在两份、慢慢漂移。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from . import features
from .db import Subscription

# render_data.label 的显示宽度上限。官方口径是「10 字符，中文按 2 算」，
# 比指令面板的 name ≤14 更紧。
LABEL_LIMIT = 10
# 主卡片里最多平铺几个「退订 N」；超了就折叠成一个「退订…」去二级卡片。
# 4 个订阅时主卡片是 5 行（2 导航 + 2 退订 + 1 控制），再多就没边了。
MAX_INLINE_UNSUB = 4
# 二级卡片的退订按钮每行几个。label 短（`退订 10` 宽 7），一行放得下 3 个。
UNSUB_PER_ROW = 3
# `/set` 的功能开关每行几个。label 是 `1 早安 关`（宽 9），一行两个刚好，
# 六个功能三行 —— 一行一个的话是六行，手机上一屏放不下。
SET_PER_ROW = 2
# 固定文案，别让它成为第二个宽度风险
VISITED_LABEL = "已插入"
UNSUPPORT_TIPS = "当前 QQ 版本不支持按钮，请直接发送上面的指令"

_STYLE_DEFAULT = 0
_STYLE_DANGER = 3  # 白底红字，给「退订」用

# 主卡片前两行：全是静态入口，插的是指令名 + 一个尾空格（等用户补参数）
_NAV_ROWS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("订阅列表", "/list"), ("搜索源", "/find ")),
    (("订阅源", "/sub "), ("改间隔", "/interval ")),
)
# 最后一行：暂停 / 恢复也保持静态 —— 每个订阅都生成一对会让行数翻三倍，
# 而这两个指令用得远比退订少。
_CONTROL_ROW: tuple[tuple[str, str], ...] = (("暂停", "/pause "), ("恢复", "/resume "))


def display_width(text: str) -> int:
    """按官方口径算显示宽度：全角/宽字符算 2，其余算 1。

    官方校验的是宽度不是字符数。用 len() 数的话，「订阅本群所有 RSS 更新」
    这种 11 个字的中文描述会被当成 11 放行，实际宽度 20+，接口才打回来。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clamp_label(text: str, limit: int = LABEL_LIMIT) -> str:
    """按显示宽度截断，截断处补 `…`。

    `…`（U+2026）是 East-Asian **Ambiguous** —— 我们按 1 算，QQ 有可能按 2 算。
    所以给它留 2 个宽度的位置，宁可短一点也不赌平台的口径。
    """
    text = (text or "").strip()
    if display_width(text) <= limit:
        return text
    budget = limit - 2
    out: list[str] = []
    used = 0
    for ch in text:
        w = display_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _cmd_button(btn_id: str, label: str, data: str, *, style: int = _STYLE_DEFAULT) -> dict:
    """一个「指令型」按钮：点一下把 data 填进输入框（不自动发送）。"""
    return {
        "id": btn_id,
        "render_data": {
            # 两个 label 都过 clamp_label：将来谁往 label 里塞了长文本，
            # 也只会被截短，不会变成「200 但按钮没了」
            "label": clamp_label(label),
            "visited_label": clamp_label(VISITED_LABEL),
            "style": style,
        },
        "action": {
            "type": 2,  # 2 = 指令（0=跳转，1=回调需要新 intent）
            # 2 = 所有人。权限统一交给 commands._is_admin 判 —— 那边是 fail-open，
            # 而 permission.type=1（管理员）的语义没实测过，赌错就是按钮点不动。
            "permission": {"type": 2},
            "data": data,
            # click_limit 官方已废弃，不传
            "unsupport_tips": UNSUPPORT_TIPS,
        },
    }


def keyboard(rows: Sequence[Sequence[dict]]) -> dict:
    """把若干行按钮包成消息体里要的 keyboard 结构。

    **这层 content 是唯一不能省的东西** —— 见模块 docstring 第一条。
    """
    return {"content": {"rows": [{"buttons": list(row)} for row in rows if row]}}


def validate_keyboard(kb: dict, *, known_tokens: frozenset[str] | None = None) -> list[str]:
    """本地体检，返回问题列表（空 = 通过）。

    测试里用它做**属性断言**（各种订阅数 × 各种标题都过一遍），
    而不是抽查几个字面量 —— 这类 schema 的坑全都是「某一种输入才触发」。
    """
    problems: list[str] = []
    if not isinstance(kb, dict) or "content" not in kb:
        return ["keyboard 缺少 content 外层（接口会 200 但按钮被丢弃）"]
    if "rows" in kb:
        problems.append("rows 不该直接挂在 keyboard 上（少了 content 外层）")
    rows = kb["content"].get("rows")
    if not isinstance(rows, list) or not rows:
        return [*problems, "content.rows 为空"]

    seen_ids: set[str] = set()
    for r, row in enumerate(rows):
        buttons = row.get("buttons") if isinstance(row, dict) else None
        if not buttons:
            problems.append(f"第 {r + 1} 行没有按钮")
            continue
        for b, btn in enumerate(buttons):
            where = f"第 {r + 1} 行第 {b + 1} 个按钮"
            if not isinstance(btn, dict):
                problems.append(f"{where} 不是对象")
                continue
            bid = btn.get("id")
            if not bid:
                problems.append(f"{where} 缺 id")
            elif bid in seen_ids:
                problems.append(f"{where} id 重复：{bid!r}")
            else:
                seen_ids.add(bid)

            rd = btn.get("render_data") or {}
            for field in ("label", "visited_label"):
                val = rd.get(field)
                if not isinstance(val, str) or not val:
                    problems.append(f"{where} 缺 {field}")
                elif display_width(val) > LABEL_LIMIT:
                    problems.append(
                        f"{where} {field} 宽 {display_width(val)} 超过上限 "
                        f"{LABEL_LIMIT}：{val!r}"
                    )

            act = btn.get("action") or {}
            if act.get("type") != 2:
                problems.append(f"{where} action.type 应为 2（指令）")
            if (act.get("permission") or {}).get("type") != 2:
                problems.append(f"{where} permission.type 应为 2（所有人）")
            if not act.get("unsupport_tips"):
                problems.append(f"{where} 缺 unsupport_tips")
            if "click_limit" in act:
                problems.append(f"{where} 传了已废弃的 click_limit")

            data = act.get("data")
            if not isinstance(data, str) or not data.startswith("/"):
                problems.append(f"{where} data 应是 / 开头的指令：{data!r}")
            elif known_tokens is not None:
                parts = data[1:].split()
                if not parts or parts[0].lower() not in known_tokens:
                    problems.append(f"{where} data 指向未知指令：{data!r}")
    return problems


# ---------- 文案 ----------


def sub_line(pos: int, sub: Subscription) -> str:
    """正文里的一行订阅。`/list` 和按钮卡片共用，观感才一致。"""
    name = sub.title or sub.url
    if len(name) > 40:
        name = name[:39] + "…"
    if not sub.enabled:
        flag = "（已暂停）"
    elif not sub.initialized:
        flag = "（待首抓）"
    else:
        flag = ""
    err = f" ⚠{sub.last_error}" if sub.last_error else ""
    return f"{pos}. {name} / {sub.interval_sec // 60}min{flag}{err}"


def format_subscription_list(subs: Sequence[Subscription]) -> str:
    """`/list` 的正文。`/rss` 按钮发不出去时也用它兜底，所以必须是完整的。"""
    if not subs:
        return "本群还没有订阅，用 /sub 添加。"
    return "\n".join(["本群订阅："] + [sub_line(p, s) for p, s in enumerate(subs, 1)])


# 按钮万一被平台丢掉时，正文本身也要能用 —— 所以把指令原样列出来。
_COMMAND_REF = (
    "订阅 /sub <路由> [分钟] · 退订 /unsub <编号> · 列表 /list\n"
    "暂停 /pause <编号> · 恢复 /resume <编号> · 改间隔 /interval <编号> <分钟>"
)


def build_rss_markdown(subs: Sequence[Subscription]) -> str:
    lines = [f"### 📋 本群订阅（{len(subs)}）", ""]
    if subs:
        lines += [sub_line(p, s) for p, s in enumerate(subs, 1)]
        lines += ["", _COMMAND_REF]
    else:
        lines += ["还没有订阅。点「订阅源」，或者直接发 `/sub <RSSHub 路由>`。", "", _COMMAND_REF]
    return "\n".join(lines)


def build_rss_text(subs: Sequence[Subscription]) -> str:
    """纯文本版菜单。卡片发不出去时兜底 —— 内容必须和卡片**等价**，
    不能只回一句「出错了」，否则用户连本群订了什么都不知道。
    """
    return format_subscription_list(subs) + "\n\n" + _COMMAND_REF


def build_unsub_markdown(subs: Sequence[Subscription]) -> str:
    return "\n".join(
        [f"### 退订哪一个？（共 {len(subs)} 个）", ""]
        + [sub_line(p, s) for p, s in enumerate(subs, 1)]
    )


# ---------- 键盘 ----------


def build_rss_keyboard(subs: Sequence[Subscription]) -> dict:
    """主菜单。退订按钮**按实际订阅现算** —— 点下去插的是完整指令 `/unsub 1`。"""
    rows: list[list[dict]] = [
        [_cmd_button(f"nav_{r}_{i}", label, data) for i, (label, data) in enumerate(row)]
        for r, row in enumerate(_NAV_ROWS)
    ]

    if subs:
        if len(subs) <= MAX_INLINE_UNSUB:
            btns = [
                _cmd_button(f"unsub_{p}", f"退订 {p}", f"/unsub {p}", style=_STYLE_DANGER)
                for p in range(1, len(subs) + 1)
            ]
        else:
            # 折叠成一个入口，真正的列表放二级卡片。
            # 单条消息最多几行按钮官方没写、也没实测过，所以主动把长度压住。
            btns = [_cmd_button("unsub_more", "退订…", "/rss unsub", style=_STYLE_DANGER)]
        rows += [btns[i : i + 2] for i in range(0, len(btns), 2)]

    rows.append(
        [_cmd_button(f"ctl_{i}", label, data) for i, (label, data) in enumerate(_CONTROL_ROW)]
    )
    return keyboard(rows)


def build_unsub_keyboard(subs: Sequence[Subscription]) -> dict:
    btns = [
        _cmd_button(f"unsub_{p}", f"退订 {p}", f"/unsub {p}", style=_STYLE_DANGER)
        for p in range(1, len(subs) + 1)
    ]
    rows = [btns[i : i + UNSUB_PER_ROW] for i in range(0, len(btns), UNSUB_PER_ROW)]
    rows.append([_cmd_button("back", "返回菜单", "/rss")])
    return keyboard(rows)


# ---------- /set（本群的功能开关）----------

# 按钮被平台丢掉时正文得能单独用，所以指令原样写出来
_SET_COMMAND_REF = "手打也行：/set <编号> on|off（例：/set 1 off）"
# ⚠️ 这句必须留着：群里的按钮**只能把指令填进输入框**，不会自动发送
# （`enter: true` 仅单聊可用）。不写清楚的话，用户点完按钮看没反应，
# 会以为按钮坏了 —— 这个坑在 /rss 那边已经踩过一次。
_SET_BUTTON_HINT = "点按钮 → 指令会填进输入框 → **按发送**才生效（群里不能点一下就执行）"


def build_set_markdown(
    states: dict[str, bool],
    *,
    notice: str = "",
    warnings: Sequence[str] = (),
) -> str:
    """`/set` 的正文。开关状态 + 用法。

    `notice` 是刚改完那一句（`✅ 早安语音：本群已关`），
    `warnings` 是「开了也不会生效」那类提醒（见 `_set_warnings`）。
    """
    lines = [f"### ⚙️ 本群的功能开关（{sum(1 for f in features.FEATURES if states.get(f))}"
             f"/{len(features.FEATURES)} 开）", ""]
    for i, feature in enumerate(features.FEATURES, 1):
        mark = "✅ 开" if states.get(feature) else "⬜ 关"
        lines.append(f"{i}. {mark}　{features.NAMES[feature]}")
    if notice:
        lines += ["", notice]
    for w in warnings:
        lines += ["", f"⚠️ {w}"]
    lines += ["", _SET_BUTTON_HINT, _SET_COMMAND_REF]
    return "\n".join(lines)


def build_set_text(
    states: dict[str, bool], *, notice: str = "", warnings: Sequence[str] = ()
) -> str:
    """纯文本兜底（卡片发不出去时）。和 `features.render_settings` 同一个口径，
    多一行刚改完的提示和那几条「开了也不生效」的提醒。
    """
    body = features.render_settings_dict(states)
    head = f"{notice}\n\n" if notice else ""
    tail = "".join(f"\n\n⚠️ {w}" for w in warnings)
    return head + body + tail


def build_set_keyboard(states: dict[str, bool]) -> dict:
    """每个功能一个按钮，**做的是「相反的那个动作」**。

    现在开着 → 按钮是「关」，插 `/set N off`；现在关着 → 按钮是「开」。
    当前状态看正文（`✅ 开` / `⬜ 关`），label 只放编号 + 两字简称 + 动作 ——
    `render_data.label` ≤10 显示宽度，塞不下全名（见 features.SHORT_NAMES）。
    """
    btns = []
    for i, feature in enumerate(features.FEATURES, 1):
        on = bool(states.get(feature))
        label = f"{i} {features.SHORT_NAMES[feature]} {'关' if on else '开'}"
        data = f"/set {i} {'off' if on else 'on'}"
        btns.append(
            _cmd_button(
                f"set_{feature}",
                label,
                data,
                style=_STYLE_DANGER if on else _STYLE_DEFAULT,
            )
        )
    rows = [btns[i : i + SET_PER_ROW] for i in range(0, len(btns), SET_PER_ROW)]
    return keyboard(rows)
