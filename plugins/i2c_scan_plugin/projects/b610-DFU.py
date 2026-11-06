#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""B610（DFU 站）—— 一个文件同时覆盖 dut0(7801) 与 dut1(7802)。

核心框架(切/扫/出结果)在 hook_base.make_table_hook，本项目只声明两样就好：
  1) PROJECT   运行信息（IP/切 mux 后稳定延时…）
  2) SLOTS     一条条写下去的扫描表（见文件下半部分）

数据来源：SC8278_DFU_FPGA_System_Configuration_V1.1.xlsx
（“Function Pin / IP Device Name”两列）。DFU 与 FCT 的**逻辑完全一样**，
只是 I2C 编号不同，所以两个文件结构一致、只有总线号不一样。

DFU 的总线分配（配置表原文）：
    ARRAY_I2C1  CH1 -> I2C0   CH2 -> I2C1     (ArrayBoard 扩展 mux, 0x77)
    BASE_I2C_MUX CH1 -> I2C2  CH2 -> I2C3     (BaseBoard  扩展 mux, 0x76)
    TRIGGER_I2C CH1 -> I2C4   CH2 -> I2C5     (Trigger 板, 直连不切 mux)
    WIB_IO_CTL  CH1 -> I2C6   CH2 -> I2C7     (WIB 板,   直连不切 mux)

其中 **CH1 = dut0(7801)、CH2 = dut1(7802)**：同一块板的两个通道对应两个工位。

每个槽位：
    scan     'i2c_N'   要扫的总线（RPC 软件地址遍历，免 SSH）
    port     走哪个 RPC 端点：dut0→7801 / dut1→7802
    mux      是否先切、切哪个通道；没有该键 = 直接扫、不切
    label / expected       展示与人工比对用(可选)

  换/加站：照抄本文件，改 PROJECT + SLOTS 即可，不用改插件。
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
    'title': 'B610-DFU',
    'default_ip': '192.168.99.34',
    'default_port': 7801,          # dut0
    'default_ssh_user': 'mixadmin',
    # SSH 密码：detect_i2c 扫描走 SSH，SSHManager 不接受空密码，必须给。
    # 置空则退回 RPC 扫描（慢但不需要 SSH）。也可用环境变量覆盖：
    #   I2C_SSH_PASSWORD=xxx
    'default_ssh_password': os.environ.get('I2C_SSH_PASSWORD', 'mixadmin'),
    'settle_ms': 250,
}

# 两条 mux 都在同一工位内，只是不同的 RPC 服务名（同一颗 FPGA）：
#   i2c_mux_array = ArrayBoard 扩展 mux，TCA9548 @0x77，上游 ARRAY_I2C1
#   i2c_mux_base  = BaseBoard  扩展 mux，TCA9548 @0x76，上游 BASE_I2C_MUX
MUX_SVC_ARRAY = 'i2c_mux_array'
MUX_SVC_BASE = 'i2c_mux_base'
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

# 工位 → RPC 端口（dut0 / dut1）
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
    # ==================== dut0 @7801（配置表 CH1 列）====================
    # ---- 直接扫（不切 mux）----
    {'key': 'dut0-Trigger', 'label': 'dut0 Trigger板 i2c_4', 'scan': 'i2c_4', 'port': 7801, 'expected': []},
    {'key': 'dut0-WIB', 'label': 'dut0 WIB板 i2c_6', 'scan': 'i2c_6', 'port': 7801, 'expected': []},
    {'key': 'dut0-base-direct', 'label': 'dut0 BaseBoard i2c_2', 'scan': 'i2c_2', 'port': 7801, 'expected': []},
    {'key': 'dut0-array-direct', 'label': 'dut0 ArrayBoard i2c_0', 'scan': 'i2c_0', 'port': 7801, 'expected': []},
    # ---- 先切 array mux(i2c_mux_array @0x77) 再扫其上游总线 ----
    {'key': 'dut0-array-mux-0', 'label': 'dut0 ArrayBoard i2c_0 通道0 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 0)], 'expected': []},
    {'key': 'dut0-array-mux-1', 'label': 'dut0 ArrayBoard i2c_0 通道1 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 1)], 'expected': []},
    {'key': 'dut0-array-mux-2', 'label': 'dut0 ArrayBoard i2c_0 通道2 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 2)], 'expected': []},
    {'key': 'dut0-array-mux-3', 'label': 'dut0 ArrayBoard i2c_0 通道3 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 3)], 'expected': []},
    {'key': 'dut0-array-mux-4', 'label': 'dut0 ArrayBoard i2c_0 通道4 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 4)], 'expected': []},
    {'key': 'dut0-array-mux-5', 'label': 'dut0 ArrayBoard i2c_0 通道5 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 5)], 'expected': []},
    {'key': 'dut0-array-mux-6', 'label': 'dut0 ArrayBoard i2c_0 通道6 (ADG2188)',
     'scan': 'i2c_0', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_ARRAY, 6)], 'expected': []},
    # ---- 先切 base mux(i2c_mux_base @0x76) 再扫 i2c_2 ----
    {'key': 'dut0-base-mux-0', 'label': 'dut0 BaseBoard i2c_2 通道0 (Base CAT9555)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 0)], 'expected': []},
    {'key': 'dut0-base-mux-1', 'label': 'dut0 BaseBoard i2c_2 通道1 (Audio)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 1)], 'expected': []},
    {'key': 'dut0-base-mux-2', 'label': 'dut0 BaseBoard i2c_2 通道2 (Odin POWER)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 2)], 'expected': []},
    {'key': 'dut0-base-mux-3', 'label': 'dut0 BaseBoard i2c_2 通道3 (Odin EEP)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 3)], 'expected': []},
    {'key': 'dut0-base-mux-4', 'label': 'dut0 BaseBoard i2c_2 通道4 (Odin CVS)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 4)], 'expected': []},
    {'key': 'dut0-base-mux-5', 'label': 'dut0 BaseBoard i2c_2 通道5 (DMM)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 5)], 'expected': []},
    {'key': 'dut0-base-mux-6', 'label': 'dut0 BaseBoard i2c_2 通道6 (SIB)',
     'scan': 'i2c_2', 'port': 7801,
     'mux': [_mux_op(MUX_SVC_BASE, 6)], 'expected': []},

    # ==================== dut1 @7802（配置表 CH2 列）====================
    # ---- 直接扫（不切 mux）----
    {'key': 'dut1-Trigger', 'label': 'dut1 Trigger板 i2c_5', 'scan': 'i2c_5', 'port': 7802, 'expected': []},
    {'key': 'dut1-WIB', 'label': 'dut1 WIB板 i2c_7', 'scan': 'i2c_7', 'port': 7802, 'expected': []},
    {'key': 'dut1-base-direct', 'label': 'dut1 BaseBoard i2c_3', 'scan': 'i2c_3', 'port': 7802, 'expected': []},
    {'key': 'dut1-array-direct', 'label': 'dut1 ArrayBoard i2c_1', 'scan': 'i2c_1', 'port': 7802, 'expected': []},
    # ---- 先切 array mux(i2c_mux_array @0x77) 再扫其上游总线 ----
    {'key': 'dut1-array-mux-0', 'label': 'dut1 ArrayBoard i2c_1 通道0 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 0)], 'expected': []},
    {'key': 'dut1-array-mux-1', 'label': 'dut1 ArrayBoard i2c_1 通道1 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 1)], 'expected': []},
    {'key': 'dut1-array-mux-2', 'label': 'dut1 ArrayBoard i2c_1 通道2 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 2)], 'expected': []},
    {'key': 'dut1-array-mux-3', 'label': 'dut1 ArrayBoard i2c_1 通道3 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 3)], 'expected': []},
    {'key': 'dut1-array-mux-4', 'label': 'dut1 ArrayBoard i2c_1 通道4 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 4)], 'expected': []},
    {'key': 'dut1-array-mux-5', 'label': 'dut1 ArrayBoard i2c_1 通道5 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 5)], 'expected': []},
    {'key': 'dut1-array-mux-6', 'label': 'dut1 ArrayBoard i2c_1 通道6 (ADG2188)',
     'scan': 'i2c_1', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_ARRAY, 6)], 'expected': []},
    # ---- 先切 base mux(i2c_mux_base @0x76) 再扫 i2c_3 ----
    {'key': 'dut1-base-mux-0', 'label': 'dut1 BaseBoard i2c_3 通道0 (Base CAT9555)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 0)], 'expected': []},
    {'key': 'dut1-base-mux-1', 'label': 'dut1 BaseBoard i2c_3 通道1 (Audio)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 1)], 'expected': []},
    {'key': 'dut1-base-mux-2', 'label': 'dut1 BaseBoard i2c_3 通道2 (Odin POWER)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 2)], 'expected': []},
    {'key': 'dut1-base-mux-3', 'label': 'dut1 BaseBoard i2c_3 通道3 (Odin EEP)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 3)], 'expected': []},
    {'key': 'dut1-base-mux-4', 'label': 'dut1 BaseBoard i2c_3 通道4 (Odin CVS)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 4)], 'expected': []},
    {'key': 'dut1-base-mux-5', 'label': 'dut1 BaseBoard i2c_3 通道5 (DMM)',
     'scan': 'i2c_3', 'port': 7802,
     'mux': [_mux_op(MUX_SVC_BASE, 5)], 'expected': []},
    {'key': 'dut1-base-mux-6', 'label': 'dut1 BaseBoard i2c_3 通道6 (SIB)',
     'scan': 'i2c_3', 'port': 7802,
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
