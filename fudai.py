# -*- coding: utf-8 -*-
"""
抖音直播间超级福袋自动抢夺程序(电脑端控制 MuMu 模拟器)
========================================================
原理:电脑端通过 ADB 连接 MuMu 模拟器,定时截图 -> OpenCV 模板匹配
找到福袋入口 / 参与按钮 / 发送按钮,模拟点击参与抽奖;
开奖后无论中没中奖,自动关闭结果弹窗,继续蹲下一轮。

利用抖音新版特性:点弹窗里的"发弹幕参与"按钮后,评论框会自动预填
福袋口令,脚本只需再点一次"发送",无需中文输入。

用法:
    python fudai.py test    # 测试 ADB 连接与截图
    python fudai.py setup   # 交互式截取按钮模板图(首次必做)
    python fudai.py run     # 开始抢福袋
    python fudai.py         # 交互菜单

风险提示:自动参与抽奖违反抖音平台规则,可能导致抽奖资格受限或封号。
仅供学习交流,请自行承担使用风险,建议小号 + 低频率运行。
"""
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent
TPL_DIR = BASE / "templates"
SHOT_DIR = BASE / "screenshots"
CONFIG_PATH = BASE / "config.json"

TPL_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------
DEFAULT_CONFIG = {
    "adb_path": "",                    # 留空自动探测 MuMu 常见安装路径
    "serial": "127.0.0.1:16384",       # MuMu12 默认端口,连不上会自动试其他端口
    "match_threshold": 0.82,           # 模板匹配置信度阈值 0~1
    "tap_jitter": 4,                   # 点击随机偏移像素(拟人化)
    "delay_min": 2.0,                  # 主循环随机等待下限(秒)
    "delay_max": 5.0,                  # 主循环随机等待上限(秒)
    "wait_poll_min": 20,               # 等开奖轮询间隔下限(秒)
    "wait_poll_max": 40,               # 等开奖轮询间隔上限(秒)
    "use_ocr": True,                   # OCR 识别开奖倒计时精准等待;失败自动降级盲等
    "draw_poll_seconds": 6,            # 开奖时刻后密集检查结果的间隔(秒)
    "switch_after_draw": False,        # 默认固定蹲守一个直播间;true=开奖后切换下一个
    "idle_switch_minutes": 0,          # 蹲这么多分钟没福袋就切直播间(0=不切,固定蹲守)
    "min_stay_seconds": 60,            # 每个直播间至少停留秒数,防疯狂滑动
    "wander_mode": True,               # 模拟真人:参与后去隔壁直播间逛,开奖前返回
    "wander_leave_min": 30,            # 参与成功后在原直播间至少再停留秒数
    "wander_leave_max": 90,            # 随机停留到该范围后离开去逛
    "wander_hops": 1,                  # 去几家隔壁直播间(下划原路返回,1 最稳)
    "return_before_seconds": 45,       # 开奖前多少秒开始返回原直播间
    "wander_like": True,               # 闲逛时随机双击点赞(更像真人)
    "state_timeout": 90,               # 单状态卡死超时(秒)
    "win_action": "close",             # 中奖弹窗动作:close=直接关闭继续蹲 / claim=点领取
    "debug_screenshot": True,          # 关键动作是否保存截图存档
}

# MuMu 常见 ADB 端口(MuMu12: 16384 起,每个实例 +32;MuMu6: 7555)
MUMU_SERIALS = [
    "127.0.0.1:16384", "127.0.0.1:16416", "127.0.0.1:16448",
    "127.0.0.1:7555", "127.0.0.1:5555",
]

# 模板清单:文件名 -> (用途中文说明, 是否必需)
TEMPLATES = {
    "entry":     ("直播间里的福袋悬浮图标(带倒计时的金色袋子)", True),
    "join_btn":  ("福袋弹窗里的「发弹幕参与」按钮", True),
    "send_btn":  ("弹出评论框后的「发送」按钮", True),
    "close_btn": ("弹窗右上角的关闭 X(或「我知道了」按钮)", True),
    "win_btn":   ("中奖弹窗的「领取」按钮(可选)", False),
    "lose_btn":  ("未中奖弹窗的关闭按钮(可选,没有就用 close)", False),
}


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"读取 config.json 失败({e}),使用默认配置")
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def log(msg: str):
    print(time.strftime("[%H:%M:%S] ") + str(msg), flush=True)


# ------------------------------------------------------------------
# ADB 封装
# ------------------------------------------------------------------
class ADB:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.adb = cfg["adb_path"]
        self.serial = cfg["serial"]

    # ---- 基础命令 ----
    def _run(self, *args, timeout=15):
        cmd = [self.adb, "-s", self.serial, *args]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    # ---- 连接 ----
    def connect(self) -> bool:
        # 先确认 adb.exe 存在
        if not Path(self.adb).exists():
            found = self._find_adb()
            if not found:
                log(f"找不到 adb.exe,请编辑 {CONFIG_PATH} 的 adb_path")
                return False
            self.adb = found
            self.cfg["adb_path"] = found
            save_config(self.cfg)
            log(f"自动定位 adb: {found}")

        # 若配置端口连不上,自动尝试 MuMu 常见端口
        candidates = [self.serial] + [s for s in MUMU_SERIALS if s != self.serial]
        for s in candidates:
            subprocess.run([self.adb, "connect", s], capture_output=True, timeout=10)
            rc, out, _ = self._run("shell", "echo", "ok", timeout=8)
            if rc == 0 and b"ok" in out:
                if s != self.serial:
                    self.serial = s
                    self.cfg["serial"] = s
                    save_config(self.cfg)
                log(f"ADB 已连接模拟器:{s}")
                return True
        log("连接失败:请确认 MuMu 模拟器已启动,且设置里开启了 ADB 调试")
        return False

    @staticmethod
    def _find_adb() -> str | None:
        # 常见安装位置自动探测
        guesses = [
            r"D:\Program Files\Netease\MuMu\nx_main\adb.exe",
            r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
            r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
            r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
        ]
        for g in guesses:
            if Path(g).exists():
                return g
        return None

    # ---- 截图 ----
    def screenshot(self) -> np.ndarray | None:
        """返回 BGR 图像数组,失败返回 None"""
        try:
            rc, out, err = self._run("exec-out", "screencap", "-p", timeout=20)
            if rc != 0 or not out:
                return None
            img = cv2.imdecode(np.frombuffer(out, dtype=np.uint8), cv2.IMREAD_COLOR)
            return img
        except Exception:
            return None

    # ---- 点击 ----
    def tap(self, x: int, y: int, jitter: int = 0):
        if jitter:
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)
        self._run("shell", "input", "tap", str(x), str(y), timeout=10)
        log(f"    点击 ({x},{y})")

    def back(self):
        self._run("shell", "input", "keyevent", "4", timeout=10)

    def swipe(self, x1, y1, x2, y2, dur=300):
        self._run("shell", "input", "swipe",
                  str(x1), str(y1), str(x2), str(y2), str(dur), timeout=10)

    def screen_size(self):
        if not getattr(self, "_size", None):
            try:
                rc, out, _ = self._run("shell", "wm", "size", timeout=8)
                m = re.search(r"(\d+)x(\d+)", out.decode(errors="ignore"))
                if rc == 0 and m:
                    self._size = (int(m.group(1)), int(m.group(2)))
                else:
                    self._size = (1080, 1920)
            except Exception:
                self._size = (1080, 1920)
        return self._size


# ------------------------------------------------------------------
# 视觉:模板匹配
# ------------------------------------------------------------------
def load_templates() -> dict:
    tpls = {}
    for name in TEMPLATES:
        p = TPL_DIR / f"{name}.png"
        if p.exists():
            tpls[name] = cv2.imread(str(p), cv2.IMREAD_COLOR)
    return tpls


def find(shot: np.ndarray, tpl: np.ndarray, threshold: float):
    """模板匹配,命中返回 (cx, cy, score),未命中返回 None"""
    if shot is None or tpl is None:
        return None
    if shot.shape[0] < tpl.shape[0] or shot.shape[1] < tpl.shape[1]:
        return None
    res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= threshold:
        h, w = tpl.shape[:2]
        return (max_loc[0] + w // 2, max_loc[1] + h // 2, max_val)
    return None


def find_any(shot, tpls: dict, names, threshold):
    """按 names 顺序找第一个命中的模板,返回 (name, pos)"""
    for n in names:
        if n in tpls:
            pos = find(shot, tpls[n], threshold)
            if pos:
                return n, pos
    return None, None


def save_debug(shot, tag: str):
    if shot is None:
        return
    p = SHOT_DIR / f"{time.strftime('%H%M%S')}_{tag}.png"
    cv2.imwrite(str(p), shot)


# ------------------------------------------------------------------
# OCR(RapidOCR 惰性加载,失败自动降级盲等)
# ------------------------------------------------------------------
_OCR = None
_OCR_FAILED = False


def get_ocr():
    global _OCR, _OCR_FAILED
    if _OCR is not None or _OCR_FAILED:
        return _OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
        log("OCR 已加载(RapidOCR)")
    except Exception as e:
        _OCR_FAILED = True
        log(f"OCR 加载失败({e}),倒计时识别降级为盲等轮询")
        log("如需启用 OCR:.venv\\Scripts\\python -m pip install rapidocr-onnxruntime")
    return _OCR


def parse_countdown_text(text: str) -> int | None:
    """从 OCR 文本解析开奖倒计时秒数,识别不出返回 None。
    支持:「15分30秒后开奖」「5分钟后」「45秒」「12:30」等写法。"""
    if not text:
        return None
    m = re.search(r"(\d{1,3})\s*[:：]\s*(\d{2})", text)
    if m:
        secs = int(m.group(1)) * 60 + int(m.group(2))
        return secs if secs > 0 else None
    total = 0
    m = re.search(r"(\d{1,3})\s*分", text)
    if m:
        total += int(m.group(1)) * 60
    m = re.search(r"(\d{1,2})\s*秒", text)
    if m:
        total += int(m.group(1))
    return total if total > 0 else None


# ------------------------------------------------------------------
# 模板截取向导
# ------------------------------------------------------------------
def setup_mode(adb: ADB, cfg: dict):
    log("=== 模板截取向导 ===")
    log("流程:每一步按提示在模拟器里准备好画面,然后回到本窗口按回车,")
    log("再用鼠标在弹出的截图上框选目标区域,按回车确认,按 ESC 跳过。\n")

    for name, (desc, required) in TEMPLATES.items():
        p = TPL_DIR / f"{name}.png"
        mark = "必选" if required else "可选"
        print(f"\n>>> [{mark}] 请截取:{desc}")
        if p.exists():
            ans = input(f"    已存在 {p.name},是否重新截取?(y/N): ").strip().lower()
            if ans != "y":
                continue
        else:
            input("    模拟器画面准备好后按回车继续(直接回车可跳过可选项): ")
        if not required and not p.exists() and ans == "":
            continue

        shot = adb.screenshot()
        if shot is None:
            log("    截图失败!请检查 ADB 连接(python fudai.py test)")
            continue
        h, w = shot.shape[:2]
        log(f"    当前截图分辨率:{w}x{h}(模板必须与运行时分辨率一致)")

        roi = cv2.selectROI(f"select [{name}] - drag, Enter=ok, ESC=skip",
                            shot, showCrosshair=True)
        cv2.destroyAllWindows()
        x, y, rw, rh = roi
        if rw == 0 or rh == 0:
            log(f"    已跳过 {name}")
            continue
        crop = shot[y:y + rh, x:x + rw]
        cv2.imwrite(str(p), crop)
        log(f"    已保存 {p.name}({rw}x{rh})")

    log("\n模板截取完成。文件保存在 templates/ 目录。")
    log("提示:模板框得越小越准(只框按钮/图标本身,别框大片背景)。")


# ------------------------------------------------------------------
# 抢福袋状态机
# ------------------------------------------------------------------
class FudaiBot:
    ST_IDLE, ST_POPUP, ST_INPUT = "蹲福袋", "福袋弹窗", "发弹幕"
    ST_WAIT, ST_WANDER = "等开奖", "闲逛直播间"

    def __init__(self, adb: ADB, cfg: dict):
        self.adb = adb
        self.cfg = cfg
        self.tpls = load_templates()
        self.state = self.ST_IDLE
        self.state_since = time.time()
        self.round = 0
        self.draw_at = None            # 预计开奖时刻(time.time() 秒),None=未知
        self.intensive_until = 0.0     # 开奖后密集检测的截止时刻
        self.wander_plan = None        # 闲逛计划:何时离开、何时返回
        self.entered_live_at = time.time()
        self.stats = self._load_stats()
        if self.stats["participated"] or self.stats["wins"]:
            log(f"历史统计:累计参与 {self.stats['participated']} 次,"
                f"中奖 {self.stats['wins']} 次")
        missing = [n for n, (d, req) in TEMPLATES.items()
                   if req and n not in self.tpls]
        if missing:
            raise SystemExit(
                f"缺少必需模板:{', '.join(missing)}\n"
                f"请先运行:python fudai.py setup"
            )

    # ---- 状态迁移 ----
    def goto(self, st: str):
        log(f"── {self.state} → {st}")
        self.state = st
        self.state_since = time.time()

    def sleep(self, lo=None, hi=None):
        lo = lo if lo is not None else self.cfg["delay_min"]
        hi = hi if hi is not None else self.cfg["delay_max"]
        time.sleep(random.uniform(lo, hi))

    def tap_pos(self, pos):
        x, y, score = pos
        self.adb.tap(x, y, self.cfg["tap_jitter"])

    # ---- 中奖统计 ----
    def _load_stats(self) -> dict:
        p = BASE / "stats.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"participated": 0, "wins": 0, "history": []}

    def _save_stats(self):
        p = BASE / "stats.json"
        try:
            p.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception as e:
            log(f"统计写入失败:{e}")

    def record_win(self):
        self.stats["wins"] += 1
        self.stats["history"].append(time.strftime("%m-%d %H:%M"))
        self.stats["history"] = self.stats["history"][-50:]
        self._save_stats()
        log(f"    已累计中奖 {self.stats['wins']} 次 🎉")

    def print_stats(self):
        s = self.stats
        log(f"== 会话结束 | 共蹲到 {self.round} 轮 | 累计参与 "
            f"{s['participated']} 次 | 累计中奖 {s['wins']} 次 ==")

    # ---- 切换直播间 ----
    def switch_live(self, reason: str):
        if time.time() - self.entered_live_at < self.cfg.get(
                "min_stay_seconds", 60):
            log(f"{reason},但本直播间停留不足 {self.cfg.get('min_stay_seconds', 60)}s,"
                f"再蹲一会")
            self.goto(self.ST_IDLE)
            return
        x = random.randint(420, 660)
        y1 = random.randint(1400, 1600)
        y2 = random.randint(500, 700)
        log(f"{reason},上划切换下一个直播间")
        self.adb.swipe(x, y1, x, y2, random.randint(250, 450))
        self.entered_live_at = time.time()
        self.draw_at = None
        self.sleep(3.5, 5.5)   # 等待新直播间加载
        self.goto(self.ST_IDLE)

    # ---- OCR 识别开奖倒计时 ----
    def estimate_draw_time(self):
        """参与成功后,OCR 福袋弹窗上的倒计时,算出预计开奖时刻。"""
        self.draw_at = None
        self.wander_plan = None
        if not self.cfg.get("use_ocr", True):
            return
        ocr = get_ocr()
        if ocr is None:
            return
        shot = self.shot()
        if shot is None:
            return
        try:
            result, _ = ocr(shot)
        except Exception as e:
            log(f"OCR 异常:{e},本轮盲等")
            return
        if not result:
            log("OCR 无识别结果,本轮盲等")
            return
        best = None
        for line in result:
            secs = parse_countdown_text(line[1])
            if secs and (best is None or secs < best):
                best = secs
        if best and 10 <= best <= 900:
            self.draw_at = time.time() + best
            self.intensive_until = time.time() + 180  # 到点后密集检测 3 分钟
            log(f"OCR 识别到开奖倒计时约 {best // 60}分{best % 60:02d}秒,精准等待中")
            self._make_wander_plan()
        else:
            log(f"OCR 未识别出有效倒计时(候选:{[l[1] for l in result][:3]}),本轮盲等")

    # ---- 模拟真人闲逛计划 ----
    def _make_wander_plan(self):
        cfg = self.cfg
        self.wander_plan = None
        if not self.draw_at or not cfg.get("wander_mode", True):
            return
        leave_at = time.time() + random.uniform(
            cfg.get("wander_leave_min", 30), cfg.get("wander_leave_max", 90))
        return_at = self.draw_at - cfg.get("return_before_seconds", 45)
        if return_at <= leave_at + 30:
            log("距开奖太近,来不及闲逛,原地等待开奖")
            return
        self.wander_plan = {"leave_at": leave_at, "return_at": return_at,
                            "hops": max(1, int(cfg.get("wander_hops", 1))),
                            "hopped": 0}
        log(f"闲逛计划:{time.strftime('%H:%M:%S', time.localtime(leave_at))} "
            f"离开去隔壁逛,{time.strftime('%H:%M:%S', time.localtime(return_at))} "
            f"前返回看开奖")

    def _like(self):
        """双击屏幕随机位置 = 点赞"""
        w, h = self.adb.screen_size()
        x = random.randint(int(w * 0.35), int(w * 0.65))
        y = random.randint(int(h * 0.45), int(h * 0.65))
        self.adb.tap(x, y)
        time.sleep(random.uniform(0.08, 0.18))
        self.adb.tap(x, y)
        log("    双击点赞 ❤")

    def _swipe_up(self):
        w, h = self.adb.screen_size()
        x = random.randint(int(w * 0.4), int(w * 0.6))
        self.adb.swipe(x, int(h * 0.78), x, int(h * 0.30),
                       random.randint(250, 450))

    def _wander_back(self, plan=None):
        """按原路下划返回原直播间(抖音下划=回到上一个直播间)"""
        n = (plan or {}).get("hopped", 0)
        for _ in range(n):
            self._swipe_down()
            self.sleep(1.5, 2.5)
        if plan is not None:
            plan["hopped"] = 0

    def _swipe_down(self):
        w, h = self.adb.screen_size()
        x = random.randint(int(w * 0.4), int(w * 0.6))
        self.adb.swipe(x, int(h * 0.30), x, int(h * 0.78),
                       random.randint(250, 450))

    # 闲逛直播间:参与完去隔壁逛,开奖前返回
    def tick_wander(self, th):
        self.state_since = time.time()   # 闲逛自行管理时序,豁免超时复位
        plan = self.wander_plan
        now = time.time()
        # 开奖时刻丢失(异常复位) → 立即回原直播间等结果
        if not self.draw_at or plan is None:
            log("开奖时刻未知,停止闲逛,返回直播间等结果")
            self._wander_back(plan)
            self.sleep(1.5, 2.5)
            self.goto(self.ST_WAIT)
            return
        # 还没到离开时间:在原直播间自然停留,偶尔点赞
        if now < plan["leave_at"]:
            self.sleep(5, 10)
            if self.cfg.get("wander_like", True) and random.random() < 0.5:
                self._like()
            return
        # 到点了:上划去隔壁直播间
        if plan["hopped"] < plan["hops"]:
            log(f"去隔壁直播间逛逛(第 {plan['hopped'] + 1} 家)…")
            self._swipe_up()
            plan["hopped"] += 1
            self.sleep(3.5, 5.5)
            if self.cfg.get("wander_like", True) and random.random() < 0.6:
                self._like()
            return
        # 到返回时刻:下划按原路回原直播间
        if now >= plan["return_at"]:
            log("快开奖了,返回原直播间看结果…")
            self._wander_back(plan)
            self.sleep(2.0, 3.0)
            self.goto(self.ST_WAIT)
            return
        # 还没到返回时间:在当前直播间闲逛
        self.sleep(random.uniform(15, 30))
        if self.cfg.get("wander_like", True) and random.random() < 0.5:
            self._like()

    def shot(self):
        s = self.adb.screenshot()
        if s is None:
            log("    截图失败,2 秒后重试")
            time.sleep(2)
        return s

    # ---- 主循环 ----
    def run(self):
        th = self.cfg["match_threshold"]
        log("=== 开始抢福袋(按 Ctrl+C 停止) ===")
        log(f"阈值={th} 点击抖动=±{self.cfg['tap_jitter']}px 主循环延迟="
            f"{self.cfg['delay_min']}~{self.cfg['delay_max']}s")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"异常:{e},5 秒后继续")
                time.sleep(5)
                if self.state == self.ST_WANDER:
                    # 闲逛中出异常,先回到原直播间再复位
                    self._wander_back(self.wander_plan)
                    self.sleep(1.5, 2.0)
                self.draw_at = None
                self.intensive_until = 0.0
                self.wander_plan = None
                self.goto(self.ST_IDLE)
            # 状态卡死保护(闲逛状态自己管理时序,豁免)
            if (self.state != self.ST_WANDER and
                    time.time() - self.state_since > self.cfg["state_timeout"]):
                log("状态超时,截存档图并重置")
                save_debug(self.shot(), "timeout_" + self.state)
                self.adb.back()  # 返回键兜底,关掉可能卡住的弹窗
                self.draw_at = None
                self.intensive_until = 0.0
                self.wander_plan = None
                self.goto(self.ST_IDLE)

    def tick(self):
        st = self.state
        th = self.cfg["match_threshold"]
        if st == self.ST_IDLE:
            self.tick_idle(th)
        elif st == self.ST_POPUP:
            self.tick_popup(th)
        elif st == self.ST_INPUT:
            self.tick_input(th)
        elif st == self.ST_WAIT:
            self.tick_wait(th)
        elif st == self.ST_WANDER:
            self.tick_wander(th)

    # 蹲福袋:找入口图标
    def tick_idle(self, th):
        shot = self.shot()
        if shot is None:
            return
        pos = find(shot, self.tpls["entry"], th)
        if pos:
            log(f"发现福袋入口 score={pos[2]:.3f},第 {self.round + 1} 轮")
            save_debug(shot, "entry")
            self.tap_pos(pos)
            self.sleep(1.2, 2.0)
            self.goto(self.ST_POPUP)
        else:
            self.sleep()
            # 蹲太久没福袋 → 换个直播间
            mins = self.cfg.get("idle_switch_minutes", 5)
            if mins > 0 and time.time() - self.entered_live_at > mins * 60:
                self.switch_live(f"蹲了超过 {mins} 分钟没发现福袋")

    # 福袋弹窗:找"发弹幕参与"按钮
    def tick_popup(self, th):
        shot = self.shot()
        if shot is None:
            return
        name, pos = find_any(shot, self.tpls, ["join_btn"], th)
        if pos:
            log(f"找到参与按钮 score={pos[2]:.3f},点击参与")
            save_debug(shot, "popup")
            self.tap_pos(pos)
            self.sleep(1.0, 1.8)
            self.goto(self.ST_INPUT)
            return
        # 弹窗里没有参与按钮:可能已参与过,或福袋已结束
        pos = find(shot, self.tpls["close_btn"], th)
        if pos:
            log("弹窗无参与按钮(可能已参与/已结束),关闭")
            self.tap_pos(pos)
            self.round += 1
            self.sleep(1.0, 1.5)
            self.goto(self.ST_WAIT)
        else:
            self.sleep(1.0, 1.5)
            self.goto(self.ST_IDLE)

    # 发弹幕:评论框应已预填口令,找"发送"按钮
    def tick_input(self, th):
        for i in range(6):  # 评论框弹出动画,重试若干次
            shot = self.shot()
            if shot is None:
                continue
            pos = find(shot, self.tpls["send_btn"], th)
            if pos:
                log(f"点击「发送」score={pos[2]:.3f} —— 参与成功!")
                self.tap_pos(pos)
                self.round += 1
                self.stats["participated"] += 1
                self._save_stats()
                self.sleep(1.5, 2.5)
                self.estimate_draw_time()
                if self.wander_plan:
                    self.goto(self.ST_WANDER)
                else:
                    self.goto(self.ST_WAIT)
                return
            time.sleep(1)
        log("没找到「发送」按钮:可能是抖音版本不支持口令预填,")
        log("本轮回弹退避,若每轮都出现请反馈截图到 screenshots/ 目录")
        save_debug(self.shot(), "no_send_btn")
        self.adb.back()          # 收起键盘
        self.sleep(0.8, 1.2)
        self.goto(self.ST_WAIT)

    # 等开奖:OCR 精准等待(未知则盲等)→ 密集检测结果 → 关闭/切换
    def tick_wait(self, th):
        # 1) 已知开奖时刻:睡到开奖前几秒,期间不消耗资源
        if self.draw_at:
            wait = self.draw_at - 4 - time.time()
            if wait > 0:
                log(f"距预计开奖还有 {wait:.0f}s,睡眠等待…")
                time.sleep(min(wait, 300))
                return
            self.draw_at = None      # 到点,转入密集检测

        # 2) 检查间隔:开奖后 3 分钟窗口内密集查,平时盲等随机间隔
        now = time.time()
        if now < self.intensive_until:
            interval = self.cfg.get("draw_poll_seconds", 6)
        else:
            interval = random.uniform(self.cfg["wait_poll_min"],
                                      self.cfg["wait_poll_max"])
        time.sleep(interval)

        shot = self.shot()
        if shot is None:
            return
        # 3) 中奖弹窗:记录+存档,动作由 config.win_action 决定
        if "win_btn" in self.tpls and find(shot, self.tpls["win_btn"], th):
            log("🎉 检测到中奖弹窗!")
            save_debug(shot, "WIN")
            self.record_win()
            if self.cfg.get("win_action", "close") == "claim":
                pos = find(shot, self.tpls["win_btn"], th)
                log("    点击「领取」")
                self.tap_pos(pos)
                self.sleep(1.0, 1.5)
                return
            log("    按配置直接关闭,继续蹲下一轮")
            # 不 return,落到下面的统一关闭逻辑
        # 4) 未中奖 / 通用关闭
        name, pos = find_any(shot, self.tpls, ["lose_btn", "close_btn"], th)
        if pos:
            log(f"开奖结果弹窗({name}),关闭")
            save_debug(shot, "result")
            self.tap_pos(pos)
            self.sleep(1.0, 1.5)
            if self.cfg.get("switch_after_draw", True):
                self.switch_live("本轮开奖结束")
            else:
                self.goto(self.ST_IDLE)
            return
        # 5) 又出现参与按钮(上一轮没参与成功)
        if find(shot, self.tpls["join_btn"], th):
            log("弹窗仍可参与,回去补一刀")
            self.goto(self.ST_POPUP)
            return
        # 6) 什么都没有:弹窗已自行消失,回 IDLE 蹲下一轮
        if find(shot, self.tpls["entry"], th) is None:
            log("弹窗已消失,继续蹲下一轮福袋…")
        self.goto(self.ST_IDLE)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def cmd_test(adb, cfg):
    log("测试 ADB 连接…")
    if not adb.connect():
        sys.exit(1)
    shot = adb.screenshot()
    if shot is None:
        log("连接成功但截图失败!请检查模拟器『开发者选项-USB调试』是否开启")
        sys.exit(2)
    h, w = shot.shape[:2]
    p = SHOT_DIR / "test.png"
    cv2.imwrite(str(p), shot)
    log(f"截图成功:{w}x{h},已保存到 {p}")
    log("一切正常,可以运行 setup 截模板了。")


def cmd_status():
    log("=== 模板状态 ===")
    for name, (desc, required) in TEMPLATES.items():
        p = TPL_DIR / f"{name}.png"
        state = "√ 已有" if p.exists() else ("× 缺失(必需)" if required else "- 无(可选)")
        print(f"  {state:12s} {name:10s} {desc}")


def menu(adb, cfg):
    while True:
        print("\n========== 抖音超级福袋助手 ==========")
        print(" 1. 测试 ADB 连接与截图")
        print(" 2. 截取/更新按钮模板(首次必做)")
        print(" 3. 查看模板状态")
        print(" 4. 开始抢福袋")
        print(" 0. 退出")
        ch = input("请选择: ").strip()
        if ch == "1":
            cmd_test(adb, cfg)
        elif ch == "2":
            if adb.connect():
                setup_mode(adb, cfg)
        elif ch == "3":
            cmd_status()
        elif ch == "4":
            if not adb.connect():
                continue
            bot = FudaiBot(adb, cfg)
            try:
                bot.run()
            except KeyboardInterrupt:
                log("\n已手动停止")
                bot.print_stats()
        elif ch == "0":
            break


def main():
    cfg = load_config()
    adb = ADB(cfg)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "test":
        cmd_test(adb, cfg)
    elif arg == "setup":
        if adb.connect():
            setup_mode(adb, cfg)
    elif arg == "run":
        if not adb.connect():
            sys.exit(1)
        bot = FudaiBot(adb, cfg)
        try:
            bot.run()
        except KeyboardInterrupt:
            log("\n已手动停止")
            bot.print_stats()
    else:
        menu(adb, cfg)


if __name__ == "__main__":
    main()
