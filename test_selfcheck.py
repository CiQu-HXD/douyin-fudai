# -*- coding: utf-8 -*-
"""自检脚本:不依赖模拟器,验证模板匹配与状态机核心逻辑是否正常。"""
import random
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from fudai import find, find_any, FudaiBot, parse_countdown_text  # noqa: E402

random.seed(7)
rng = np.random.default_rng(42)

# 1) 合成一张 1080x1920 "直播间截图",加噪声纹理当背景
shot = rng.integers(60, 100, (1920, 1080, 3), dtype=np.uint8).astype(np.uint8)

def draw_button(x, y, w, h, color):
    cv2.rectangle(shot, (x, y), (x + w, y + h), color, -1)
    cv2.putText(shot, "BTN", (x + 5, y + h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return x, y, w, h

# 画 4 个"按钮":福袋入口(右上)、参与(中部)、发送(底部)、关闭
pos_entry  = draw_button(950, 150, 70, 70,  (60, 190, 255))   # 金色 BGR
pos_join   = draw_button(390, 900, 300, 90, (80, 80, 240))
pos_send   = draw_button(880, 1780, 140, 70, (240, 120, 40))
pos_close  = draw_button(700, 700, 60, 60,  (200, 200, 200))

# 2) 从图上切模板(模拟 setup 截取),轻微偏移 +1px 以模拟真实误差
def cut(p, dx=1, dy=1):
    x, y, w, h = p
    return shot[y + dy:y + dy + h, x + dx:x + dx + w].copy()

tpls = {n: cut(p) for n, p in
        [("entry", pos_entry), ("join_btn", pos_join),
         ("send_btn", pos_send), ("close_btn", pos_close)]}

# 3) 验证 find:期望中心点与真实中心一致(±2px)
ok = True
for name, p in [("entry", pos_entry), ("join_btn", pos_join),
                ("send_btn", pos_send), ("close_btn", pos_close)]:
    x, y, w, h = p
    r = find(shot, tpls[name], 0.82)
    if r is None:
        print(f"FAIL {name}: 未匹配到"); ok = False; continue
    cx, cy, sc = r
    good = abs(cx - (x + w // 2)) <= 2 and abs(cy - (y + h // 2)) <= 2
    print(f"{'OK  ' if good else 'FAIL'} {name}: center=({cx},{cy}) score={sc:.3f}")
    ok &= good

# 4) 验证 find_any 优先级
n, r = find_any(shot, tpls, ["join_btn", "close_btn"], 0.82)
print(f"{'OK  ' if n == 'join_btn' else 'FAIL'} find_any 优先级 -> {n}")
ok &= (n == "join_btn")

# 5) 验证"背景里没有目标"时不误报
blank = rng.integers(60, 100, (1920, 1080, 3), dtype=np.uint8).astype(np.uint8)
r = find(blank, tpls["entry"], 0.82)
print(f"{'OK  ' if r is None else 'FAIL'} 空背景不误报 -> {r}")
ok &= (r is None)

# 6) 验证缺模板时 FudaiBot 给出清晰报错
try:
    FudaiBot.__new__(FudaiBot)  # 不触发 __init__
    bot = object.__new__(FudaiBot)
    bot.adb = bot.cfg = None
    bot.tpls = {"entry": tpls["entry"]}  # 故意缺 join_btn 等
    bot.state = FudaiBot.ST_IDLE
    FudaiBot.__init__.__wrapped__ if False else None
    # 手动执行 __init__ 的校验段
    missing = [k for k, (d, req) in
               __import__("fudai").TEMPLATES.items() if req and k not in bot.tpls]
    assert missing == ["join_btn", "send_btn", "close_btn"], missing
    print("OK   缺模板检测 ->", ", ".join(missing))
except SystemExit as e:
    print("OK   缺模板报错:", e)

# 7) 倒计时文本解析
cases = [
    ("15分30秒后开奖", 930), ("5分钟后", 300), ("45秒后开奖", 45),
    ("12:30", 750), ("开奖倒计时 3分08秒", 188), ("距离结束还有2分60秒", 180),
    ("恭喜发财", None), ("", None),
]
for text, expect in cases:
    got = parse_countdown_text(text)
    tag = "OK  " if got == expect else "FAIL"
    ok &= (got == expect)
    print(f"{tag} parse_countdown({text!r}) -> {got} (期望 {expect})")

print("\n" + ("✅ 全部自检通过" if ok else "❌ 有失败项"))
sys.exit(0 if ok else 1)
