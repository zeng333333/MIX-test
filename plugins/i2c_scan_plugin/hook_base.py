#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""项目钩子加载器与单槽位工作流执行。

项目钩子是一个**纯函数模块**（不 import 本插件的 Qt/传输类），只负责依照
“给定槽位，返回要执行的动作与探测方式”，约定了如下最小接口：

    PROJECT   dict   {name, full_name?, slots?: [...], metas: {...}}
    def slots():                      # 可留空 -> 用 PROJECT['slots']
    def mux_ops(slot):    -> list[op]# 切到某槽所需 RPC/SSH 动作
    def release_ops(slot):-> list[op]# 扫完收尾（可为空）
    def probe(slot):      -> probe   # 探测描述（见下）

动作 op:   {'driver': 'rpc', 'service','method','args'}
       或  {'driver': 'ssh'|'ssh-scan', 'cmd', ...}
       或  {'driver': 'delay', 'ms': 100}         # 槽间/等待，无传输

探测 probe（决定“这个槽去读哪些地址”）：
    {'kind': 'ssh-scan', 'cmd': 'sudo detect_i2c -y 8'}
    {'kind': 'rpc',      'service','method','args'}   # 结果交给地址规范化
    {'kind': 'none'}                                  # 只切不探，用于手动接线

“远程不一定有 detect_i2c”的兜底：钩子可另给出一个原始探测 cmd 模板
（见 projects/b610.py 的 _raw_probe_cmd，用 shell 读 /dev/i2c-N），并可在
界面里选择 detect_i2c / raw 两种。

本模块不含 Qt 依赖，便于离屏/单测。
"""

# 为保证本文件既可被插件顶层模块以绝对路径 import，又可单独跑，这里把
# 插件目录加进 sys.path，统一用绝对导入 transports。
import importlib.util
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transports  # noqa: E402
from transports import parse_found_addresses  # noqa: E402,F401


# --------------------------------------------------------------------------
# 钩子加载
# --------------------------------------------------------------------------
PROJECT_DEFAULT_IP = None
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def discover_project_files():
    """返回按文件名排序的钩子模块路径列表（projects/*.py，排除 __init__）。"""
    proj_dir = os.path.join(HOOK_DIR, 'projects')
    out = []
    if os.path.isdir(proj_dir):
        for fn in sorted(os.listdir(proj_dir)):
            if fn.startswith('_'):
                continue
            if fn.endswith('.py'):
                out.append(os.path.join(proj_dir, fn))
    return out


def load_project_module(name):
    """按钩子文件/模块名加载项目钩子并校验最小接口。"""
    name = name.replace('.py', '')
    path = os.path.join(HOOK_DIR, 'projects', name + '.py')
    if not os.path.isfile(path):
        # 也允许纯模块名（同一目录）
        path = None
        for p in discover_project_files():
            if os.path.splitext(os.path.basename(p))[0] == name:
                path = p
                break
    if not path:
        raise FileNotFoundError('未找到项目钩子: %s' % name)

    spec = importlib.util.spec_from_file_location('i2cproj_' + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for required in ('PROJECT', 'mux_ops', 'probe'):
        if not hasattr(mod, required):
            raise TypeError('项目钩子 %s 缺少必要成员: %s' % (name, required))
    return mod


def project_list():
    """返回 [(display_name, module_name)] 供下拉使用。"""
    names = []
    for p in discover_project_files():
        base = os.path.splitext(os.path.basename(p))[0]
        try:
            mod = load_project_module(base)
            title = mod.PROJECT.get('title') or mod.PROJECT.get('name') or base
        except Exception:
            title = base
        names.append((f'{title}', base))
    return names


# --------------------------------------------------------------------------
# 动作执行 + 探测
# --------------------------------------------------------------------------
def slot_list(hook):
    """返回钩子声明的槽位名列表（PROJECT['slots'] 或 slots()）。"""
    if callable(getattr(hook, 'slots', None)):
        try:
            s = hook.slots()
        except Exception:
            s = None
        if s:
            return list(s)
    if hook.PROJECT.get('slots'):
        return list(hook.PROJECT['slots'])
    return []


def execute_ops(reg, ctx, ops, log=None):
    """依次执行动作列表 op（支持 delay）。返回 ((driver, result), ...) 或抛错。"""
    raw = []
    for op in (ops or []):
        drv = op.get('driver')
        if drv == 'delay':
            ms = int(op.get('ms', 100))
            if log:
                log('  等待 %d ms' % ms)
            time.sleep(ms / 1000.0)
            raw.append(('delay', '%dms' % ms))
            continue
        try:
            res = reg.run(ctx, op)
        except Exception as e:  # noqa: BLE001 - 单个动作失败即把 mux 语义报出
            if log:
                log('  动作失败: %s' % (e,))
            raise RuntimeError('动作 %s 失败: %s' % (op, e))
        raw.append((drv, res))
    return raw


def run_slot_workflow(reg, ctx, hook, slot, log=None):
    """切 mux -> 探测 -> 收尾，返回归一化后的地址列表。

    第 0 步先做可达性核验：ping 不通直接抛错（见
    ``preflight_reachable``），避免“目标不在线还去切 mux / 扫一遍也无意义”。
    同一 ctx 校验过一次则复用结果（ctx['_reach_ok']）。

    Returns:
        (addresses, events): 探测到的地址列表；events 为过程日志。
    """
    def _log(msg):
        if log:
            log(msg)

    # --- 0) 可达性（最先，ping 不通后面全略）---
    try:
        used_rpc = _uses_rpc(hook, slot)
        ok, allmsg = preflight_reachable(ctx, used_rpc, log=_log)
        if not ok:
            raise ConnectionError(allmsg)
    except ConnectionError:
        raise
    except Exception:
        pass

    # --- 1) mux 切到 slot ---
    _log('切 mux -> %s' % slot)
    mux_ops = hook.mux_ops(slot)
    execute_ops(reg, ctx, mux_ops, log=_log)

    # --- 2) 稳定延时 ---
    # 注意：mux_ops() 已经在本行末尾带过 delay(settle_ms)（见 make_table_hook），
    # 所以“有 mux 的行”这里不能再补睡一次，否则每行白等两个 settle。
    # 只有“不切 mux 的行”才需要在这补一次延时（切 mux 后要等器件稳定）。
    if not [o for o in (mux_ops or []) if o.get('driver') != 'delay']:
        settle = getattr(hook, 'settle_ms', None)
        if callable(settle):
            try:
                ms = int(settle() or 0)
            except Exception:
                ms = 0
        else:
            ms = 0
        if ms:
            time.sleep(ms / 1000.0)

    # --- 3) 探测地址 ---
    probe = hook.probe(slot)
    addresses = _exec_probe(reg, ctx, probe, log=_log)

    # --- 4) 收尾 ---
    rel = []
    if callable(getattr(hook, 'release_ops', None)):
        rel = hook.release_ops(slot) or []
    if rel:
        execute_ops(reg, ctx, rel, log=_log)

    return addresses, events_placeholder()


def _uses_rpc(hook, slot):
    """判断该 slot 的动作是否走 RPC（用于选 ping 的端口）。

    有 mux 动作 = 一定走 RPC（切 mux 是 RPC 调用）；
    没有 mux 时看 probe：ssh-scan → 探 SSH(22)，其余按 RPC。
    """
    try:
        for op in (hook.mux_ops(slot) or []):
            if op.get('driver') in ('rpc', 'rpc-call'):
                return True
    except Exception:
        return True  # 探测失败保守按 RPC 判
    try:
        p = hook.probe(slot) or {}
        if p.get('kind') in ('ssh', 'ssh-scan'):
            return False
        if p.get('kind') == 'rpc':
            return True
    except Exception:
        pass
    return True


def events_placeholder():
    return []


def preflight_reachable(ctx, needs_rpc=True, log=None, tcp_timeout=1.5):
    """对 ctx 的目标做一次轻量可达性探测（裸 TCP，不需 PTY）。

    - needs_rpc=True  → 探 ctx 的 RPC 端口（rpc_port/port，默认 7801）
    - needs_rpc=False → 探 SSH 端口 22（或 ctx['ssh_port']）
    同一 ctx 探测过即缓存到 ctx['_reach_ok']（True/False），避免多行重复 ping。
    返回 (ok, 消息)。
    """
    def _tcp(ip, port):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(tcp_timeout)
        try:
            s.connect((ip, int(port)))
            return True
        except Exception:
            return False
        finally:
            s.close()

    key = '_reach_rpc' if needs_rpc else '_reach_ssh'
    if key in ctx:
        return ctx[key], ctx.get(key + '_msg', '')

    ip = ctx.get('ip')
    port = int(ctx.get('rpc_port') or ctx.get('port') or 7801) if needs_rpc \
        else int(ctx.get('ssh_port') or 22)
    name = 'RPC' if needs_rpc else 'SSH'
    ok = False
    if ip:
        ok = _tcp(ip, port)
    msg = ('%s 可达 %s:%s' % (name, ip, port)) if ok else \
        ('无法连接 %s:%s → 网络不通或平台未启动。已停止后续扫描。' % (ip, port))
    if log:
        log(('ping 核验: ' + msg) if ok else ('⚠ ' + msg))
    ctx[key] = ok
    ctx[key + '_msg'] = msg
    if not ok:
        raise ConnectionError(msg)
    return ok, msg





def _exec_probe(reg, ctx, probe, log=None):
    """按 probe 种类读取并归一化地址列表。"""
    if not probe:
        if log:
            log('  无探测动作 (probe 为空)')
        return []
    kind = probe.get('kind', 'ssh-scan')

    def _op(driver, **kw):
        """构造动作时带上 probe 的 port —— 端口是“设备的哪个 RPC 端点”，
        丢了 port 就可能拿默认端口去连另一台设备/工位。"""
        op = {'driver': driver}
        op.update(kw)
        p = probe.get('port')
        if p is not None:
            op['port'] = int(p)
        return op

    if kind in ('ssh-scan', 'ssh'):
        cmd = probe['cmd']
        if log:
            log('  探测 ssh: %s' % cmd)
        # 只有一种扫法：板端 detect_i2c。失败就直接报错，不做 RPC 回退，
        # 免得“悄悄换成慢路径”掩盖真实问题（缺权限/路径不对等）。
        res = reg.run(ctx, _op('ssh-scan', cmd=cmd))
        return transports.parse_found_addresses(res)
    if kind == 'rpc':
        if log:
            log('  探测 rpc: %s.%s' % (probe.get('service'), probe.get('method')))
        res = reg.run(ctx, _op('rpc',
                               service=probe['service'],
                               method=probe['method'],
                               args=probe.get('args') or []))
        return transports.parse_found_addresses(res) if not isinstance(res, list) \
            else transports._norm([_hexstr(x) for x in res])
    if kind == 'none':
        if log:
            log('  仅切 mux，不做自动扫描')
        return []
    raise ValueError('未知 probe kind: %s' % kind)


def _hexstr(v):
    if isinstance(v, str):
        return v
    return '0x%02x' % int(v)


def fake_registry(fake_table=None):
    """供离屏/单测：给 rpc driver 用 FakeRpc 桩的注册表。"""
    return transports.default_registry(fake=fake_table)


# --------------------------------------------------------------------------
# 通用表驱动的项目钩子工厂
# --------------------------------------------------------------------------
_DEFAULT_SETTLE_MS = 200


def make_table_hook(project, slots, settle_ms=_DEFAULT_SETTLE_MS, mux_proto=None):
    """由一个“声明式表”快速构造一个项目钩子对象。

    新项目 = 一份 数据(PROJECT/SLOTS) + 一份“切法原型(MUX_PROTO)”，无需写任何
    mux_ops/probe 之类的分散函数；切法与“多少条 I2C、要不要切”都集中在一张表。

    PROJECT : dict  {name,title,default_ip,default_port,...}
    slots    : list[dict]，每行= 一个要扫的 I2C 目标：
         scan     : 扫哪条总线，'i2c_3' → RPC 软件扫；也可给完整 probe dict
         mux      : None(直接扫) |
                    'channel'            数字/文本，代表要对 MUX_PROTO 切一次；
                    list[op]             高级：直接给“切”的动作 op 列表
         label, expected                 可选展示/比对用
    MUX_PROTO: 该“怎么切”的模板（不同项目不同）：
         None  -> 不用切（各行 mux 只能留空）
         dict  {service, method, driver:'rpc-call'} -> 一行切一次
         或 callback(ch)->list[op]  -> 真需要自定义多步切法时才写函数

    返回一个结构统一、插件/执行器可直接处理的钩子对象：
      .PROJECT/.SLOTS/.slots()/.mux_ops(key)/.probe(key)
      .label_for(key)/.expected_for(key)/.needs_mux(key)/.scan_hint(key)
    """
    if settle_ms is None:
        settle_ms = _DEFAULT_SETTLE_MS
    proto_driver = mux_proto  # 保持原型（dict=一型切法 / callable=自定义 / None=不用切）

    def _expand_mux(mux):
        if mux is None or mux == '' or mux == []:
            return []
        if isinstance(mux, list):
            return [dict(o) for o in mux]
        # mux_proto driven single switch
        if isinstance(proto_driver, dict):
            d = dict(proto_driver)
            d['args'] = [int(mux)]
            return [d]
        if callable(proto_driver):
            return [dict(o) for o in proto_driver(mux)]
        raise TypeError('表项 mux=%r 需要 MUX_PROTO 才能切。' % (mux,))

    def _expand_scan(scan):
        # 只支持显式 probe dict（板端 ssh-scan）。留字符串入口会生成已删除的
        # rpc-scan 驱动，属于静默失效，这里直接报错更清楚。
        if isinstance(scan, str):
            raise TypeError(
                'scan=%r 需要写成 probe dict，例如 '
                "{'kind':'ssh-scan','cmd':'/tmp/detect_i2c -y 8'}"
                '（已不再支持 rpc 逐个地址扫）' % (scan,))
        return dict(scan)

    _s = {}

    def _get(key):
        if key in _s:
            return _s[key]
        for r in slots:
            if r.get('key') == key or (r.get('scan') == key and not r.get('key')):
                _s[key] = r
                return r
        return None

    def _apply_port(op, row):
        """把该行声明的 port 落到动作上（op 自带 port 时以 op 为准）。

        一台设备 = 一个 IP，但对外可能有多个 RPC 端点（7801/7802，按工位/设备
        区分，见系统配置表）。所以该行用哪个端点必须传到 mux 动作与扫描动作上。
        注意：i2c_mux_base 与 i2c_mux_array 是同一颗 FPGA 里的两个服务，
        靠“服务名”区分，与端口无关。
        """
        p = (row or {}).get('port')
        if p is None or 'port' in op:
            return op
        op['port'] = int(p)
        return op

    def mux_ops(key):
        r = _get(key)
        if r is None:
            raise KeyError('表驱动项目无此目标: %s' % key)
        ops = [_apply_port(dict(o), r) for o in _expand_mux(r.get('mux'))]
        if not ops:
            return []
        # 每条切动作后带一个稳定延时
        out = []
        for o in ops:
            out.append(o)
            if o.get('driver') != 'delay':
                out.append({'driver': 'delay', 'ms': settle_ms})
        return out

    def probe(key):
        r = _get(key)
        if r is None:
            raise KeyError('表驱动项目无此目标: %s' % key)
        return _apply_port(_expand_scan(r.get('scan', '')), r)

    def _keys():
        keys = []
        for r in slots:
            keys.append(r.get('key') or r.get('scan') or str(len(keys) + 1))
        return keys

    class _TableHook:
        def __init__(self, _project, _rows):
            self.PROJECT = _project
            self.SLOTS = _rows

        def slots(self):
            return _keys()

        def mux_ops(self, key):
            return mux_ops(key)

        def probe(self, key):
            return probe(key)

        def release_ops(self, key):
            return []

        def settle_ms(self):
            return settle_ms

        def needs_mux(self, key):
            r = _get(key)
            return bool(r is not None and 'mux' in r and r.get('mux') not in (None, '', []))

        def label_for(self, key):
            r = _get(key)
            return (r or {}).get('label') or key

        def expected_for(self, key):
            r = _get(key)
            exp = (r or {}).get('expected') or []
            return ','.join(exp) if isinstance(exp, list) else str(exp)

        def scan_hint(self, key):
            r = _get(key)
            if r is None:
                return key
            sc = _expand_scan(r.get('scan', ''))
            if isinstance(sc, dict) and sc.get('kind') in ('ssh-scan', 'ssh'):
                return sc.get('cmd', '')
            return str(r.get('scan', key))

    return _TableHook(dict(project), list(slots))

