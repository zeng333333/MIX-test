#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""B610（FCT 站）—— 一个文件同时覆盖 dut0(7801) 与 dut1(7802)。

核心框架(切/扫/出结果)在 hook_base.make_table_hook，本项目只声明两样就好：
  1) PROJECT   运行信息（IP/切 mux 后稳定延时…）
  2) DUTS      “每个工位一套表”：dut0/dut1 各一份总线与端口，端口是参数
  SLOTS 由 DUTS 自动展开，不用手抄两遍。

为什么要合并：`sw_profile.mixconf` 里 dut0=tcp://…:7801、dut1=tcp://…:7802，
是**两个工位(DUT)**；`hw_profile.mixconf` 里 **dut0 和 dut1 各自都带**
i2c_mux_array 与 i2c_mux_base：

    dut0: i2c_mux_array @i2c_0(0x77)   i2c_mux_base @i2c_8(0x76)
    dut1: i2c_mux_array @i2c_4(0x77)   i2c_mux_base @i2c_9(0x76)

所以**端口区分的是“哪个工位”，不是“哪条 mux”**：同一工位的两条 mux
都走同一个端口，靠 RPC 服务名(i2c_mux_array / i2c_mux_base)区分。

每个槽位：
    scan     'i2c_N'   要扫的总线（RPC 软件地址遍历，免 SSH）
    port     本槽位走哪个 RPC 端点(7801/7802)——由工位决定，见 DUTS
    mux      是否先切、切哪个通道：
             - 不带 mux = 直接扫、不需要切
             - [_mux_op(MUX_SVC_ARRAY, ch)] / [_mux_op(MUX_SVC_BASE, ch)]
    label / expected       展示与人工比对用(可选)

  换/加站：照抄本文件，改 DUTS + CHANNELS 即可，不用改插件。
  通道与物理设备的严格对应、expected 请按你实机检校后填。
"""

import os
import sys

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
from hook_base import make_table_hook  # noqa: E402

PROJECT = {
    'name': 'b610',
    'title': 'B610-FCT',
    'default_ip': '169.254.1.34',
    'default_port': 7801,          # dut0；dut1 见 DUTS
    'default_ssh_user': 'mixadmin',
    'default_ssh_password': os.environ.get('I2C_SSH_PASSWORD', 'mixadmin'),
    'settle_ms': 250,
}

# —— 工位 → RPC 端口 / 总线（依据 hw_profile.mixconf + sw_profile.mixconf）——
# 两条 mux 都在同一工位内，只是不同的 RPC 服务名，所以端口=工位，不是 mux。
MUX_SVC_ARRAY = 'i2c_mux_array'    # TCA9548 @0x77
MUX_SVC_BASE = 'i2c_mux_base'      # TCA9548 @0x76
MUX_METHOD = 'set_channel_state_doe'


def _mux_op(service, channel, port=None):
    """切 mux 的动作 op（rpc-call 原样下发 int）。

    port 为可选参数：给 None 时由所在行的 'port' 决定（hook_base 会把行的
    port 落到动作上）；显式传入则以此为准。
    """
    op = {'driver': 'rpc-call', 'service': service,
          'method': MUX_METHOD, 'args': [int(channel)]}
    if port is not None:
        op['port'] = int(port)
    return op


MUX_PROTO = _mux_op(MUX_SVC_BASE, 0)  # 单级 mux 项目的默认模板（本项目不依赖）

# 工位 → RPC 端口（依据 sw_profile.mixconf：dut0=7801 / dut1=7802）
PORT_DUT0 = 7801
PORT_DUT1 = 7802

# —— 扫法（只有一种）——
# 在**下位机上**跑 detect_i2c 扫总线：一条 SSH 命令扫完一条总线，
# 网络只走 1 个来回，快。不提供 RPC 逐个地址扫的退路。
#
# 为什么要 sudo -S：
#   /dev/i2c-* 只有 root 能开，普通用户直接跑会报
#     “Could not open file `/dev/i2c-4': Permission denied”。
#   而 SSH 是非交互的、没有 tty，普通 `sudo` 会报
#     “sudo: no tty present and no askpass program specified”。
#   所以用 `sudo -n`（non-interactive）：设备上需给该用户 NOPASSWD，
#   即 /etc/sudoers.d/ 里加一行（在设备上执行一次即可）：
#       mixadmin ALL=(ALL) NOPASSWD: /tmp/detect_i2c
#   若设备不方便配 sudoers，也可以改用 i2c 组权限后把 'sudo -n ' 去掉。
DETECT_I2C = os.environ.get('I2C_DETECT_BIN', '/tmp/detect_i2c')
SUDO = os.environ.get('I2C_SUDO', 'sudo -S ')   # -S 从 stdin 读密码(无 tty)；置空则不经 sudo
SSH_TPL = '%s%s -y {bus}' % (SUDO, DETECT_I2C)

# ==========================================================================
# 扫描表：一条条写下去，想加/删/改哪行直接改这一行即可
#   key      唯一名（下拉/表格用；dut0-/dut1- 前缀区分工位）
#   label    表格“扫描对象”列显示的文字
#   scan     要扫的总线
#   port     走哪个 RPC 端点（=工位）：dut0→7801 / dut1→7802
#   mux      先切哪条 mux、切到哪个通道；没有该键 = 直接扫、不切
#   expected 预期地址（留空=只报不判）
# ==========================================================================
SLOTS = [
    # ---------------- dut0 @7801 ----------------
    # ---- 直接扫（不切 mux）----
    {'key': 'dut0-TRIG',         'label': 'dut0 Trigger板 i2c_10',  'scan': 'i2c_10', 'port': 7801, 'expected': []},
    {'key': 'dut0-WIB',          'label': 'dut0 WIB板 i2c_12',      'scan': 'i2c_12', 'port': 7801, 'expected': []},
    {'key': 'dut0-base-direct',  'label': 'dut0 Base i2c_8',        'scan': 'i2c_8',  'port': 7801, 'expected': []},
    {'key': 'dut0-array-direct', 'label': 'dut0 Array i2c_0',       'scan': 'i2c_0',  'port': 7801, 'expected': []},
    # ---- 先切 array mux(i2c_mux_array @0x77) 再扫 i2c_0 ----
    {'key': 'dut0-array-mux-0',  'label': 'dut0 i2c_0 通道0', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 0)], 'expected': []},
    {'key': 'dut0-array-mux-1',  'label': 'dut0 i2c_0 通道1', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 1)], 'expected': []},
    {'key': 'dut0-array-mux-2',  'label': 'dut0 i2c_0 通道2', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 2)], 'expected': []},
    {'key': 'dut0-array-mux-3',  'label': 'dut0 i2c_0 通道3', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 3)], 'expected': []},
    {'key': 'dut0-array-mux-4',  'label': 'dut0 i2c_0 通道4', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 4)], 'expected': []},
    {'key': 'dut0-array-mux-5',  'label': 'dut0 i2c_0 通道5', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 5)], 'expected': []},
    {'key': 'dut0-array-mux-6',  'label': 'dut0 i2c_0 通道6', 'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 6)], 'expected': []},
    # ---- 先切 base mux(i2c_mux_base @0x76) 再扫 i2c_8 ----
    {'key': 'dut0-base-mux-0',   'label': 'dut0 i2c_8 通道0', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 0)], 'expected': []},
    {'key': 'dut0-base-mux-1',   'label': 'dut0 i2c_8 通道1', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 1)], 'expected': []},
    {'key': 'dut0-base-mux-2',   'label': 'dut0 i2c_8 通道2', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 2)], 'expected': []},
    {'key': 'dut0-base-mux-3',   'label': 'dut0 i2c_8 通道3', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 3)], 'expected': []},
    {'key': 'dut0-base-mux-4',   'label': 'dut0 i2c_8 通道4', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 4)], 'expected': []},
    {'key': 'dut0-base-mux-5',   'label': 'dut0 i2c_8 通道5', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 5)], 'expected': []},
    {'key': 'dut0-base-mux-6',   'label': 'dut0 i2c_8 通道6', 'scan': 'i2c_8', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 6)], 'expected': []},

    # ---------------- dut1 @7802 ----------------
    # ---- 直接扫（不切 mux）----
    {'key': 'dut1-TRIG',         'label': 'dut1 Trigger板 i2c_11',  'scan': 'i2c_11', 'port': 7802, 'expected': []},
    {'key': 'dut1-WIB',          'label': 'dut1 WIB板 i2c_13',      'scan': 'i2c_13', 'port': 7802, 'expected': []},
    {'key': 'dut1-base-direct',  'label': 'dut1 Base i2c_9',        'scan': 'i2c_9',  'port': 7802, 'expected': []},
    {'key': 'dut1-array-direct', 'label': 'dut1 Array i2c_4',       'scan': 'i2c_4',  'port': 7802, 'expected': []},
    # ---- 先切 array mux(i2c_mux_array @0x77) 再扫 i2c_4 ----
    {'key': 'dut1-array-mux-0',  'label': 'dut1 i2c_4 通道0', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 0)], 'expected': []},
    {'key': 'dut1-array-mux-1',  'label': 'dut1 i2c_4 通道1', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 1)], 'expected': []},
    {'key': 'dut1-array-mux-2',  'label': 'dut1 i2c_4 通道2', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 2)], 'expected': []},
    {'key': 'dut1-array-mux-3',  'label': 'dut1 i2c_4 通道3', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 3)], 'expected': []},
    {'key': 'dut1-array-mux-4',  'label': 'dut1 i2c_4 通道4', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 4)], 'expected': []},
    {'key': 'dut1-array-mux-5',  'label': 'dut1 i2c_4 通道5', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 5)], 'expected': []},
    {'key': 'dut1-array-mux-6',  'label': 'dut1 i2c_4 通道6', 'scan': 'i2c_4', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 6)], 'expected': []},
    # ---- 先切 base mux(i2c_mux_base @0x76) 再扫 i2c_9 ----
    {'key': 'dut1-base-mux-0',   'label': 'dut1 i2c_9 通道0', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 0)], 'expected': []},
    {'key': 'dut1-base-mux-1',   'label': 'dut1 i2c_9 通道1', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 1)], 'expected': []},
    {'key': 'dut1-base-mux-2',   'label': 'dut1 i2c_9 通道2', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 2)], 'expected': []},
    {'key': 'dut1-base-mux-3',   'label': 'dut1 i2c_9 通道3', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 3)], 'expected': []},
    {'key': 'dut1-base-mux-4',   'label': 'dut1 i2c_9 通道4', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 4)], 'expected': []},
    {'key': 'dut1-base-mux-5',   'label': 'dut1 i2c_9 通道5', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 5)], 'expected': []},
    {'key': 'dut1-base-mux-6',   'label': 'dut1 i2c_9 通道6', 'scan': 'i2c_9', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 6)], 'expected': []},
]


# 把每行的 'i2c_N' 翻译成板端 detect_i2c 命令；mux 前置已在各行显式给出。
def _hook_rows():
    out = []
    for r in SLOTS:
        rr = dict(r)
        num = str(r.get('scan') or '').rsplit('_', 1)[-1]
        try:
            bus = int(num)
        except Exception:
            bus = 0
        rr['scan'] = {'kind': 'ssh-scan', 'cmd': SSH_TPL.format(bus=bus)}
        out.append(rr)
    return out


_hook = make_table_hook(PROJECT, _hook_rows(),
                        settle_ms=PROJECT.get('settle_ms', 250),
                        mux_proto=MUX_PROTO)


# —— 只做转发，让加载器/插件用的是同一份“表驱动结构” ——
PROJECT = _hook.PROJECT


def slots():
    return _hook.slots()


def mux_ops(slot):
    return _hook.mux_ops(slot)


def probe(slot):
    return _hook.probe(slot)


def release_ops(slot):
    return []


def settle_ms():
    return PROJECT.get('settle_ms', 250)


def label_for(key):
    return _hook.label_for(key)


def expected_for(key):
    return _hook.expected_for(key)


def needs_mux(key):
    return _hook.needs_mux(key)


def scan_hint(key):
    return _hook.scan_hint(key)
