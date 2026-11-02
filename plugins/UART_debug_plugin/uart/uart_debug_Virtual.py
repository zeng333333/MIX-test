#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
虚拟电控板 (Virtual ECB) —— 模拟 ECB 电控板 MCU 串口行为

在没有真实电控板的情况下，用本脚本创建一个虚拟串口（pty），
它会像真实电控板一样响应电控板指令.csv》里定义的指令：

  治具动作  : press(e.g. in+down) / release(up+out) / in / out / up / down
  传感器查询 : in? / out? / down? / up? / frontsensor? / rearsensor?
  LED 控制  : LED001,R~LED008,Y (R/G/B/Y) / reset
  其他      : help / version

用法:
  python3 uart_debug_Virtual.py
  python3 uart_debug_Virtual.py --dev /dev/pts/5        # 指定端口名(可空，自动创建)

完成后会打印 "测试用串口路径: /dev/ttysXXX"，用真正的串口调试工具
(如 ATLAS fixture 插件 / minicom / 自写工具) 连接该路径即可。

交互控制台(在运行本脚本的终端输入):
  以 ! 开头的是控制台指令，其余输入当作真实指令注入串口:
    !front [on|off]  模拟遮挡/移开前对射传感器(frontsensor?)
    !rear  [on|off]  模拟遮挡/移开后对射传感器(rearsensor?)
    !startbtn        模拟同时按下 START1&2 按钮  -> in and down
    !resetbtn        模拟按下 RESET 按钮         -> up and out
    !state           打印当前治具/LED/传感器状态
    !help            打印控制台帮助

典型调试流程(FixtureControl 插件侧):
  fc.fixture_init()
  fc.fixture_startbutton_check()      # 等待 down? -> ON (此时在控制台敲 !startbtn)
  fc.fixture_in()                     # 发送 press, 轮询 down? -> ON
  fc.fixture_set_led(1, "G")
  fc.fixture_send("frontsensor?")     # 返回 ON/OFF, 可敲 !front 切换
  fc.fixture_out()                    # 发送 release, 轮询 out? -> ON
  fc.fixture_led_reset()
  fc.close()
"""

import os
import pty
import select
import threading
import time
import argparse
import re
import sys
import termios
import tty


# ============================================================================
# 虚拟电控板状态机
# ============================================================================

class VirtualECB:
    FW_VERSION = "B610_B611_ECB_v2.1.0"

    # 4 个治具传感器: 位置(in/out) 与 高度(up/down)
    SENSORS = ("in", "out", "up", "down")
    BEAMS = ("frontsensor", "rearsensor")

    # 所有可识别的完整指令(用于判断“输入是否还没输完”)
    KNOWN_COMMANDS = (
        "press", "release", "in", "out", "up", "down",
        "in?", "out?", "down?", "up?", "frontsensor?", "rearsensor?",
        "reset", "version", "help",
    )
    _LED_PARTIAL_RE = re.compile(r"^led(\d{0,3})?(\s*,\s*[rgby]?)?$")
    _LED_FULL_RE = re.compile(r"^led\d{1,3}\s*,\s*[rgby]$")

    def __init__(self):
        # 物理到位开关: True = sensor 触发(查询返回 ON)
        self.pos = {"in": False, "out": False, "up": False, "down": False}
        # 对射传感器: True = 有遮挡(返回 ON)
        self.beam = {"frontsensor": False, "rearsensor": False}
        # LED 状态: slot(1-8) -> color(R/G/B/Y)
        self.led = {slot: None for slot in range(1, 9)}
        self._lock = threading.Lock()

    # ---------------- 治具动作 ----------------
    def _set_pos(self, vals):
        with self._lock:
            self.pos.update(vals)

    def press(self):
        """press: 治具进入并下压 (in and down)"""
        self._set_pos({"in": True, "down": True, "out": False, "up": False})
        return "OK"

    def release(self):
        """release: 治具上升并推出 (up and out)"""
        self._set_pos({"out": True, "up": True, "in": False, "down": False})
        return "OK"

    def step_in(self):
        self._set_pos({"in": True, "out": False})
        return "OK"

    def step_out(self):
        self._set_pos({"out": True, "in": False})
        return "OK"

    def step_up(self):
        self._set_pos({"up": True, "down": False})
        return "OK"

    def step_down(self):
        self._set_pos({"down": True, "up": False})
        return "OK"

    # ---------------- 传感器查询 ----------------
    def query_pos(self, name):
        return "ON" if self.pos.get(name) else "OFF"

    def query_beam(self, name):
        return "ON" if self.beam.get(name) else "OFF"

    # ---------------- LED ----------------
    def set_led(self, slot, color):
        with self._lock:
            color = color.upper()
            if color not in ("R", "G", "B", "Y"):
                return "ERR:BAD_COLOR"
            self.led[slot] = color
            return f"OK LED{slot:03d},{color}"

    def reset_led(self):
        with self._lock:
            for s in self.led:
                self.led[s] = None
        return "OK ALL_LED_OFF"

    # ---------------- 其他 ----------------
    def version(self):
        return self.FW_VERSION

    def help(self):
        msg = [
            "Commands:",
            "  press | release | in | out | up | down",
            "  in? | out? | down? | up? | frontsensor? | rearsensor?",
            "  LED001,R .. LED008,Y  (R/G/B/Y)",
            "  reset | version | help",
        ]
        return "\r\n".join(msg)

    # ---------------- 模拟外部按钮/传感器 ----------------
    def press_start_button(self):
        """START1&2 同时按下 -> in and down"""
        return self.press()

    def press_reset_button(self):
        """RESET 按下 -> up and out"""
        return self.release()

    def set_beam(self, name, value):
        with self._lock:
            self.beam[name] = bool(value)
        return f"{name}={'ON' if value else 'OFF'}"

    # ---------------- 统一分发 ----------------
    def handle(self, line):
        """解析一行串口指令，返回响应文本。未知指令返回空字符串。"""
        cmd = line.strip().rstrip("\r\n")
        if not cmd:
            return ""
        c = cmd.lower()

        if c == "press":
            return self.press()
        if c == "release":
            return self.release()
        if c == "in":
            return self.step_in()
        if c == "out":
            return self.step_out()
        if c == "up":
            return self.step_up()
        if c == "down":
            return self.step_down()
        if c == "in?":
            return self.query_pos("in")
        if c == "out?":
            return self.query_pos("out")
        if c == "down?":
            return self.query_pos("down")
        if c == "up?":
            return self.query_pos("up")
        if c == "frontsensor?":
            return self.query_beam("frontsensor")
        if c == "rearsensor?":
            return self.query_beam("rearsensor")
        if c == "reset":
            return self.reset_led()
        if c == "version":
            return self.version()
        if c == "help":
            return self.help()

        # LED 指令: LED001,R .. LED008,Y
        m = re.match(r"^led(\d{1,3})\s*,\s*([rgby])$", c)
        if m:
            slot = int(m.group(1))
            if 1 <= slot <= 8:
                return self.set_led(slot, m.group(2).upper())
            return "ERR:SLOT_OUT_OF_RANGE"

        return "ERR:UNKNOWN"
    # ------------------------------------------------------------------------

    # ---------------- 断句辅助 ----------------
    @classmethod
    def is_partial_command(cls, text):
        """判断一段串口输入是否仍是某条指令的前半部分。

        串口工具经常逐字符发送且不带回车，需要靠“串口静默超时”断句；
        若已收到的内容还是某条已知指令的前缀，应继续等待后续字符，
        避免把 "hel" 当成一条完整指令而提前返回 ERR:UNKNOWN。

        Args:
            text (str): 已接收但尚未断句的输入文本。

        Returns:
            bool: True 表示仍可能是某条完整指令的前缀，需要继续等待；
                False 表示已可当作一条完整指令处理。

        Example:
            >>> VirtualECB.is_partial_command("hel")
            True
            >>> VirtualECB.is_partial_command("help")
            False
        """
        t = text.strip().lower()
        if not t:
            return True
        if any(cmd.startswith(t) and cmd != t for cmd in cls.KNOWN_COMMANDS):
            return True
        if "led".startswith(t):
            return True
        if cls._LED_PARTIAL_RE.match(t) and not cls._LED_FULL_RE.match(t):
            return True
        return False

    # ---------------- 状态打印 ----------------
    def state_lines(self):
        with self._lock:
            pos = ", ".join(f"{k}: {self.pos[k]}" for k in self.SENSORS)
            beam = ", ".join(f"{k}: {self.beam[k]}" for k in self.BEAMS)
            led = ", ".join(
                f"{s:03d}:" + (self.led[s] or "OFF") for s in sorted(self.led)
            )
            return f"治具: {pos}\n传感器: {beam}\nLED : {led}"


# ============================================================================
# 串口服务 (pty 虚拟串口)
# ============================================================================

def serial_service(master_fd, slave_name, ecb):
    """后台线程: 从虚拟串口读取指令 -> 交给 VirtualECB 处理 -> 写回响应。

    宿主写入的字节可能被拆成多次到达(甚至一次只到一个字符)，因此这里
    先累积到缓冲区，再按两种方式断句：

    1. 收到换行/回车(\\n、\\r)：按行逐条处理；
    2. 串口静默超过 IDLE_FLUSH：把缓冲区内容当作一条完整指令处理，
       以兼容不带回车的手动输入；若内容仍是某条指令的前缀，则再宽限
       到 PREFIX_GRACE 秒。

    Args:
        master_fd (int): pty 主端文件描述符，用于读写虚拟串口。
        slave_name (str): 虚拟串口从端路径，仅用于日志打印。
        ecb (VirtualECB): 虚拟电控板状态机实例。
    """
    print(f"✅ 虚拟电控板已启动, 串口路径: {slave_name}", flush=True)
    print("提示: 让 ATLAS fixture 插件 / 自写工具连接以上串口路径即可调试。\n", flush=True)

    IDLE_FLUSH = 0.30      # 静默超过该时长(秒)视为一条指令结束
    PREFIX_GRACE = 0.80    # 若内容仍是某条指令的前缀, 再宽限到该时长(秒)

    buf = ""
    last_rx = time.monotonic()

    def reply(line):
        """处理一条指令并把响应文本写回虚拟串口。"""
        resp = ecb.handle(line)
        if not resp:
            return
        try:
            os.write(master_fd, (resp + "\r\n").encode("utf-8"))
            print(f"📤 回: {resp}", flush=True)
        except OSError:
            pass

    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.05)
            if master_fd in r:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    break
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace")
                print(f"📥 收: {text!r}", flush=True)
                # 统一换行符, 便于按行切分(也处理 \r\n 被拆成两次读取的情况)
                buf += text.replace("\r\n", "\n").replace("\r", "\n")
                last_rx = time.monotonic()

                if "\n" in buf:
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        line = line.strip()
                        if line:
                            reply(line)
                continue

            # 串口静默: 把残留缓冲当作一条完整指令
            if not buf:
                continue
            idle = time.monotonic() - last_rx
            if idle < IDLE_FLUSH:
                continue
            cmd = buf.strip()
            if cmd and ecb.is_partial_command(cmd) and idle < PREFIX_GRACE:
                continue
            buf = ""
            if cmd:
                print(f"📨 静默断句: {cmd}", flush=True)
                reply(cmd)
    except Exception as e:
        print(f"❌ 串口服务出错: {e}", flush=True)
    finally:
        os.close(master_fd)


# ============================================================================
# 交互控制台 (模拟物理按钮/传感器)
# ============================================================================

def console_help():
    print(
        "控制台指令(以 ! 开头; 其他输入将作为真实指令注入串口):\n"
        "  !front [on|off]   模拟遮挡/移开前对射传感器\n"
        "  !rear  [on|off]   模拟遮挡/移开后对射传感器\n"
        "  !startbtn         模拟按下 START1&2 (治具进去并下压)\n"
        "  !resetbtn         模拟按下 RESET     (治具上升并推出)\n"
        "  !state            打印当前状态\n"
        "  !help             打印本帮助\n",
        flush=True,
    )


def console_service(master_fd, ecb):
    """从 stdin 读取管理命令，并作为虚拟电控板的“物理世界”输入。"""
    console_help()
    try:
        while True:
            # 兼容管道(非 tty)读取
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            if line.startswith("!"):
                args = line[1:].split()
                act = args[0].lower() if args else ""
                if act in ("front", "rear"):
                    name = "frontsensor" if act == "front" else "rearsensor"
                    val = args[1].lower() in ("on", "1") if len(args) > 1 else not ecb.beam[name]
                    print(ecb.set_beam(name, val), flush=True)
                elif act == "startbtn":
                    print("🔘 模拟 START1&2 按下", flush=True)
                    print(ecb.press_start_button(), flush=True)
                elif act == "resetbtn":
                    print("🔘 模拟 RESET 按下", flush=True)
                    print(ecb.press_reset_button(), flush=True)
                elif act == "state":
                    print(ecb.state_lines(), flush=True)
                elif act == "help":
                    console_help()
                else:
                    print(f"❓ 未知控制台指令: {line} (输入 !help 查看)", flush=True)
            else:
                # 其它输入当作真实指令注入串口(方便手动测试)
                os.write(master_fd, (line + "\n").encode("utf-8"))
                print(f"📤 注入串口: {line}", flush=True)
    except (KeyboardInterrupt, EOFError):
        pass


# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="虚拟电控板 (Virtual ECB) — Linux/macOS pty 模拟")
    parser.add_argument("--dev", default=None, help="若指定已存在的 pty 从端路径则复用，否则自动创建")
    args = parser.parse_args()

    ecb = VirtualECB()

    print("===== 虚拟电控板 (SC8278_FCT_ECB) =====")
    print(f"FW 版本: {ecb.FW_VERSION}\n")

    # 1. 创建/复用虚拟串口
    if args.dev and os.path.exists(args.dev):
        slave_name = args.dev
        print(f"复用已存在串口: {slave_name}")
        # 复用模式下无法拿到 master_fd 做应答写入，提示不适用
        print("⚠ 复用模式主要用于只读监视，主模式请不传 --dev 让其自动创建。")
    else:
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)

        # 关闭从端的回显/行缓冲: 否则宿主写入的每个字符会被回显回主端,
        # 虚拟电控板会把回显内容再当成一条指令重复解析。
        try:
            tty.setraw(slave_fd)
        except (OSError, termios.error):
            pass

        print("=== 虚拟串口创建成功 ===")
        print(f"测试用串口路径: {slave_name}")
        print("-" * 50)

        # 2. 启动串口应答服务线程 (关键: 真正模拟电控板)
        thread = threading.Thread(
            target=serial_service, args=(master_fd, slave_name, ecb), daemon=True
        )
        thread.start()

        # 3. 启动交互控制台线程
        console_thread = threading.Thread(
            target=console_service, args=(master_fd, ecb), daemon=True
        )
        console_thread.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n✅ 虚拟电控板已关闭")
            try:
                os.close(slave_fd)
            except OSError:
                pass


if __name__ == "__main__":
    main()
