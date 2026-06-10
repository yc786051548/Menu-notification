# -*- coding: utf-8 -*-
"""
吃饭小助手：钉钉群菜单推送
用法：
    py push_menu.py breakfast        # 推早餐
    py push_menu.py lunch            # 推午餐
    py push_menu.py dinner           # 推晚餐
    py push_menu.py lunch --date 2026-06-09   # 调试：模拟某天
    py push_menu.py lunch --dry-run            # 只打印不发送
"""
import os
import re
import sys
import json
import argparse
import datetime as dt
from pathlib import Path
from urllib import request, error

# Windows 控制台默认 GBK，重设 stdout 为 UTF-8 以支持 emoji 和中文
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
MENU_PATH = ROOT / "menu.json"

WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MEAL_CN = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
MEAL_HEADER_EMOJI = {"breakfast": "🌅", "lunch": "☀️", "dinner": "🌃"}

FLOORS = ["7F", "9F", "10F"]
SEPARATOR = "++++++++++++++++"
FLOOR_DIVIDER = "================"

# ── 自动分类（flat list → 菜品 / 主食 / 其他） ────────────────────────────

_STAPLE_EXACT = frozenset([
    "米饭", "东北香米", "蒸红薯", "蒸紫薯", "蒸玉米", "煮玉米", "水煮玉米",
    "烤红薯", "玉米", "板栗南瓜", "蒸板栗南瓜", "贝贝瓜", "绿皮南瓜",
    "花生毛豆", "老南瓜", "蒸芋头", "蒸胡萝卜", "桂花马蹄", "油条", "油饼",
])
_STAPLE_SUFFIX = (
    "馒头", "花卷", "大饼", "发糕", "肉龙", "千层饼", "烧饼",
    "麻花", "麻叶", "饼干", "包子", "卷", "饼", "包",
)
_STAPLE_CONTAINS = ("蒸饺", "香米")

_OTHER_EXACT = frozenset([
    "酸奶", "三元酸奶", "豆浆", "水果", "苹果", "香蕉", "西瓜", "油桃",
    "可乐", "水饺", "大拉皮", "青梅绿茶", "红茶", "冰糖雪梨",
    "香梨", "雪梨", "豆腐脑", "茶叶蛋",
])
_OTHER_SUFFIX = (
    "汤", "粥", "羹", "茶", "奶", "米线", "凉皮", "凉面",
    "热干面", "炸酱面", "焖面", "意面", "酸辣粉", "米粉", "面", "粉",
)
_OTHER_CONTAINS = ("凉皮", "大拉皮", "炒饼", "酸辣粉", "米线", "锅贴", "水饺")


def _item_base(item: str) -> str:
    """去掉 emoji，保留汉字/字母/数字/括号/横线"""
    return re.sub(r'[^\u4e00-\u9fff\w()\-]', '', item).strip()


def _categorize(item: str) -> str:
    """分类返回：菜品 / 主食 / 其他"""
    s = _item_base(item)
    # 其他优先
    if s in _OTHER_EXACT:
        return "其他"
    for sfx in _OTHER_SUFFIX:
        if s.endswith(sfx):
            return "其他"
    for kw in _OTHER_CONTAINS:
        if kw in s:
            return "其他"
    # 主食
    if s in _STAPLE_EXACT:
        return "主食"
    for sfx in _STAPLE_SUFFIX:
        if s.endswith(sfx):
            return "主食"
    for kw in _STAPLE_CONTAINS:
        if kw in s:
            return "主食"
    return "菜品"


# ── 渲染 ──────────────────────────────────────────────────────────────────

def _render_section(section) -> str:
    """渲染单餐区块，返回 ♦类别♦ item1 | item2 格式的多行文本。
    支持 list[str]（自动分类）或 dict{类别: list[str]}（直接使用）。
    """
    if not section:
        return "（暂无）"

    if isinstance(section, dict):
        cats = {k: [x for x in v if x and x != "（待填）"]
                for k, v in section.items()}
    else:
        cats: dict = {"菜品": [], "主食": [], "其他": []}
        for item in section:
            if item and item != "（待填）":
                cats[_categorize(item)].append(item)

    lines = []
    for cat, items in cats.items():
        if items:
            lines.append(f"♦{cat}♦ {' | '.join(items)}")
    return "\n\n".join(lines) if lines else "（暂无）"


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def log(msg: str, log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(log_dir / "push.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_skip_day(date: dt.date, api_base: str, skip_holiday: bool,
                skip_makeup: bool, logger) -> tuple[bool, str]:
    """调 timor.tech 节假日 API。返回 (是否跳过, 原因)。
    type 取值: 0 工作日, 1 周末, 2 法定节假日, 3 调休补班(周末上班)
    """
    if not skip_holiday and not skip_makeup:
        return False, ""
    url = f"{api_base.rstrip('/')}/{date.isoformat()}"
    try:
        req = request.Request(url, headers={"User-Agent": "menu-bot"})
        with request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            logger(f"holiday api 返回异常 code: {data}")
            return False, ""
        t = data.get("type", {}).get("type")
        name = data.get("type", {}).get("name", "")
        if skip_holiday and t == 2:
            return True, f"法定节假日（{name}）"
        if skip_makeup and t == 3:
            return True, f"调休补班（{name}），按工作日跳过推送"
        return False, ""
    except (error.URLError, TimeoutError, ValueError) as e:
        logger(f"holiday api 调用失败，按工作日处理: {e}")
        return False, ""


def _floor_section(menu: dict, floor: str, wd_key: str, meal: str,
                   meal_emoji: str, wd_cn: str, meal_cn: str,
                   keyword_for_first: str) -> str:
    """渲染单个楼层的两条餐线，返回多行字符串。
    keyword_for_first: 非空时拼到这层第一行末尾，用于过钉钉关键词过滤。
    """
    floors = menu.get("floors") or {}
    floor_data = floors.get(floor, {})
    day = floor_data.get(wd_key, {}) if isinstance(floor_data, dict) else {}
    block = day.get(meal, {}) if isinstance(day, dict) else {}
    wall = block.get("wall", []) if isinstance(block, dict) else []
    window = block.get("window", []) if isinstance(block, dict) else []
    head_suffix = keyword_for_first if keyword_for_first else ""
    return (
        f"🍀 {floor}-餐线一-靠墙 {meal_emoji} {wd_cn}{meal_cn}{head_suffix}\n\n"
        f"{_render_section(wall)}\n\n"
        f"{SEPARATOR}\n\n"
        f"🍀 {floor}-餐线二-靠窗 {meal_emoji} {wd_cn}{meal_cn}\n\n"
        f"{_render_section(window)}"
    )


def build_markdown(menu: dict, weekday_idx: int, meal: str, keyword: str) -> tuple[str, str]:
    wd_key = WEEKDAY_KEYS[weekday_idx]
    wd_cn = WEEKDAY_CN[weekday_idx]
    meal_cn = MEAL_CN[meal]
    meal_emoji = MEAL_HEADER_EMOJI[meal]

    # 兼容旧结构（无 floors 字段时把整个 menu 当 10F）
    if "floors" not in menu:
        menu = {"floors": {"10F": menu}}

    title = f"{wd_cn}{meal_cn}{keyword}"
    parts = []
    for i, floor in enumerate(FLOORS):
        parts.append(_floor_section(
            menu, floor, wd_key, meal, meal_emoji, wd_cn, meal_cn,
            keyword if i == 0 else "",
        ))
    text = f"\n\n{FLOOR_DIVIDER}\n\n".join(parts)
    return title, text


def send_dingtalk(webhook: str, title: str, text: str, logger) -> bool:
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 自定义机器人返回 {errcode:0,...}；自动化机器人返回 {success:true,data:true}
        if data.get("errcode") == 0 or data.get("success") is True:
            logger(f"钉钉推送成功: {data}")
            return True
        logger(f"钉钉返回错误: {data}")
        return False
    except (error.URLError, TimeoutError, ValueError) as e:
        logger(f"钉钉推送异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("meal", choices=["breakfast", "lunch", "dinner"])
    parser.add_argument("--date", help="YYYY-MM-DD 模拟日期")
    parser.add_argument("--dry-run", action="store_true", help="不发送，只打印")
    args = parser.parse_args()

    cfg = load_json(CONFIG_PATH)
    menu = load_json(MENU_PATH)
    log_dir = ROOT / cfg.get("log_dir", "logs")
    logger = lambda m: log(m, log_dir)

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    weekday_idx = today.weekday()  # 0=Mon ... 6=Sun

    if weekday_idx >= 5:
        logger(f"{today} 是 {WEEKDAY_CN[weekday_idx]}，非工作日，跳过")
        return

    skip, reason = is_skip_day(
        today,
        cfg.get("holiday_api", "https://timor.tech/api/holiday/info/"),
        cfg.get("skip_holiday", True),
        cfg.get("skip_workday_makeup", True),
        logger,
    )
    if skip:
        logger(f"{today} 跳过推送：{reason}")
        return

    keyword = cfg.get("dingtalk", {}).get("keyword", "菜单")
    title, text = build_markdown(menu, weekday_idx, args.meal, keyword)

    if keyword not in text:
        logger(f"警告：消息未包含关键词「{keyword}」，可能被钉钉拦截")

    logger(f"准备推送 {today} {WEEKDAY_CN[weekday_idx]} {MEAL_CN[args.meal]}")
    if args.dry_run:
        print("=" * 40)
        print(title)
        print("-" * 40)
        print(text)
        print("=" * 40)
        return

    # 优先从环境变量读 webhook（GitHub Actions 里走 Secrets），否则用 config.json
    webhook = os.environ.get("DINGTALK_WEBHOOK") or cfg["dingtalk"].get("webhook", "")
    if not webhook:
        logger("缺少 webhook：请设置环境变量 DINGTALK_WEBHOOK 或在 config.json 里填 webhook")
        sys.exit(1)
    ok = send_dingtalk(webhook, title, text, logger)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
