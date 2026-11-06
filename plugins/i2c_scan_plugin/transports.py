#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""I2C 扫描插件的传输层。

项目钩子只负责“声明按槽位要做的动作”，实际向板卡下发/读取的动作统一在
这里按 ``driver`` 解释执行：
- ``rpc`` : 通过 MIX JSON-RPC 调用设备端方法（如切 mux、读 i2c 总线）。
- ``ssh`` : 通过 expect-ssh 在板卡 shell 上执行命令（如 detect_i2c / 原始探测）。

动作（op）由项目钩子返回，是一个 dict，即“函数式接口”所返回的可序列化动作清单。
本模块刻意不 import 具体设备协议；RPC/SSH 底层均惰性导入。
在离屏 / 无板卡环境下插件仍可加载，钩子与动作编派逻辑可单测（见 _FakeRpc）。
"""

import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor


# --------------------------------------------------------------------------
# I2C 地址解析（通用）
# --------------------------------------------------------------------------
def parse_found_addresses(text):
    """从各种来源的扫描文本中提取“探测到的 I2C 7bit 地址”。

    兼容已知两类输出：
    1) detect_i2c / i2cdetect 风格表格（行首总线号 + 每格 “XX/--/UU”）。
    2) 纯地址列表：一行一个、逗号/空格分隔、带或不带 0x 前缀。
    返回排序去重后的小写 0x 字符串列表。

    Args:
        text: 扫描输出文本。

    Returns:
        list[str]: 如 ['0x20', '0x50', '0x77']。
    """
    if not text:
        return []
    if isinstance(text, (list, tuple, set)):
        merged = []
        for t in text:
            # 列表里可能是 str(['0x20']) 也可能是 int([32])：统一成字符串再解析，
            # 否则 int 会走到 text.splitlines() 崩掉。
            if isinstance(t, str):
                merged.extend(parse_found_addresses(t))
            elif isinstance(t, int):
                merged.append('0x%02x' % t)
            else:
                merged.extend(parse_found_addresses(str(t)))
        return _norm(merged)

    found = []
    for raw_line in text.splitlines():
        # 去掉 sudo -S 的密码提示标记（我们传了 -p MIXSUDO:），避免它被
        # 当成地址内容；也顺手去掉行首表格前缀“0xNN:”或“NN:”。
        raw_line = re.sub(r'MIXSUDO:\s*', '', raw_line)
        line = re.sub(r'^\s*(?:0x)?[0-9a-fA-F]{2}\s*:\s*', '', raw_line).rstrip()
        for tok in re.split(r'\s+', line):
            tok = tok.strip()
            if tok in ('', '--', 'xx', 'x') or tok.lower() in ('uu', 'dd'):
                continue
            m = re.fullmatch(r'(?:0[xX])?([0-9a-fA-F]{2})', tok)
            if not m:
                continue
            val = int(m.group(1), 16)
            # 极低的“保留区”地址多为表格空格误读，排除 < 0x03
            if val < 0x03 or val > 0x77:
                continue
            found.append(f'0x{val:02x}')
    return _norm(found)


def _norm(items):
    seen, out = set(), []
    for it in items:
        if not isinstance(it, str):
            continue
        low = it.strip().lower()
        if low.startswith('0x'):
            low = low[2:]
        if not low:
            continue
        try:
            v = int(low, 16)
        except ValueError:
            continue
        key = f'{v:02x}'
        if key in seen:
            continue
        seen.add(key)
        out.append(f'0x{key}')
    return sorted(out)


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------
class TransportRegistry:
    """动作（op）执行器注册与派发。"""

    def __init__(self):
        self._runners = {}

    def register(self, name, runner):
        self._runners[name] = runner

    def run(self, ctx, op):
        driver = (op or {}).get('driver')
        if not driver:
            raise ValueError('动作缺少 driver 字段: %r' % (op,))
        runner = self._runners.get(driver)
        if runner is None:
            raise ValueError('不支持的 driver: %r，已注册 %s' % (driver, sorted(self._runners)))
        return runner(ctx, op)


class _FakeRpc:
    """无服务器测试桩：按 key 返回预设结果，便于离屏/单测。"""

    def __init__(self, table):
        self._table = table or {}

    def stub(self, service, method, *args, rpc_timeout=None, **kwargs):
        key = f'{service}.{method}'
        entry = self._table.get(key)
        if entry is None:
            raise RuntimeError(f'fake rpc 未定义 key: {key}')
        if callable(entry):
            return entry(*args, **kwargs)
        return entry

    def close(self):
        pass


def make_rpc_runner(max_rpc_timeout=30, fake=None):
    """构造 RPC 动作执行器。

    op 形如:
        {'driver': 'rpc', 'service': 'dut0.i2c_mux_base',
         'method': 'set_channel_state_doe', 'args': [1]}

    底层使用 MIX_debug_plugin/mix 目录下的 JsonRpcClient（自动注册 + stub）。
    fake 非空（dict）时改走 _FakeRpc 桩，便于无服务器环境测试编派逻辑。
    """

    def run(ctx, op):
        if not isinstance(ctx, dict):
            raise TypeError('ctx 需为 dict（传输缓存存于 ctx 内）')
        if fake is not None:
            key = (ctx.get('ip'), _resolve_port(ctx, op))
            pool = ctx.setdefault('_fake_clients', {})
            client = pool.get(key)
            if client is None:
                client = _FakeRpc(fake)
                pool[key] = client
        else:
            client = _ensure_client(ctx, op)

        service = op['service']
        method = op['method']
        args = list(op.get('args') or [])
        timeout = op.get('rpc_timeout') or ctx.get('rpc_timeout') or max_rpc_timeout
        return client.stub(service, method, *args, rpc_timeout=int(timeout))

    return run


def _import_and_add_mix_path():
    """确保可 `from mix8_rpc_client import JsonRpcClient`。

    mix8_rpc_client 位于 MIX_debug_plugin/mix，且依赖 plugins/utils（logger）。
    main_application 已把 plugins 目录加入 sys.path，这里只需补 mix 子目录。
    """
    import sys
    if any(m for m in sys.modules if m == 'mix8_rpc_client'):
        return
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, 'MIX_debug_plugin', 'mix')
    candidate = os.path.abspath(candidate)
    if candidate not in sys.path and os.path.isdir(candidate):
        sys.path.insert(0, candidate)


# --------------------------------------------------------------------------
# 板端扫描器自动补齐（detect_i2c）
#
# 规则：ssh 命令里出现 detect_i2c 时，先查板端该路径是否可执行；缺了就从插件
# 目录下的同名副本下发过去，避免换一台设备就得手工拷工具。
# 下发优先用 scp（复用 SSHManager 的 ControlMaster 会话，不必再认证一次）；
# 板上没有 scp/sftp 时退回 base64 分块写入（依赖板端 base64）。
# 命令里用的是别的扫描器（如 /usr/bin/i2cdetect）时不做补齐——不能把 ARM 版
# detect_i2c 冒充成别家的工具。
# --------------------------------------------------------------------------

TOOL_NAME = 'detect_i2c'
LOCAL_TOOL_ENV = 'I2C_DETECT_LOCAL_BIN'   # 本地副本路径覆盖（默认与插件同目录）


def _shq(text):
    """把字符串转成远端 shell 安全的单引号字面量。

    Args:
        text (str): 原始字符串。

    Returns:
        str: 单引号包裹后的串；内部的单引号替换成 '"'"' 拼接。
    """
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def _local_tool_path():
    """返回本地 detect_i2c 副本路径。

    Returns:
        str: 优先取环境变量 ``I2C_DETECT_LOCAL_BIN``，否则用插件目录下的
            ``detect_i2c``（与 transports.py 同级）。
    """
    env = os.environ.get(LOCAL_TOOL_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TOOL_NAME)


def _tool_path_in_cmd(cmd):
    """从待执行的 ssh 命令里取出 detect_i2c 的远端路径。

    只按“空白切分后的某个参数 basename 等于 detect_i2c”判定，这样
    ``sudo -S /tmp/detect_i2c -y 8``、``sudo -S -p MIXSUDO: /tmp/detect_i2c -y 8``
    都能取到；命令里若没有该工具则返回 None。

    Args:
        cmd (str): 下发给板端的完整 shell 命令。

    Returns:
        str | None: 远端工具路径；未使用 detect_i2c 时为 None。
    """
    for tok in str(cmd or '').split():
        t = tok.strip('\'"')
        if os.path.basename(t) == TOOL_NAME:
            return t
    return None


def _remote_exec(mgr, ip, cmd, timeout=10):
    """执行一条板端命令并把返回值统一成 ``(rc, stdout, stderr)``。

    Args:
        mgr: SSHManager（expect 包装 ssh）实例。
        ip (str): 目标板卡 IP。
        cmd (str): 远端命令。
        timeout (int, optional): 超时秒数。

    Returns:
        tuple: ``(rc, stdout, stderr)``；执行器抛异常时返回 rc=1 与异常文本。
    """
    try:
        res = mgr.execute_command(ip, cmd, timeout=timeout)
    except Exception as e:
        return 1, '', '%s: %s' % (type(e).__name__, e)
    if isinstance(res, tuple) and len(res) >= 3:
        return res[0], res[1] or '', res[2] or ''
    if isinstance(res, tuple) and len(res) == 2:   # 兼容旧/替身实现 (ok, out)
        ok, out = res
        return (0 if ok else 1), out or '', ''
    return 1, '', 'execute_command 返回格式无法识别: %r' % (res,)


def _scp_upload(mgr, ip, local, remote, timeout=20):
    """用 scp 把本地文件传到板端（复用 SSHManager 的 ControlMaster 连接）。

    说明：SSHManager 每次 execute_command 都会建立 ``ControlMaster=auto`` 且
    ``ControlPersist=30`` 的主连接（socket 路径见 ssh_manager 的
    ``/tmp/mix_ssh_exec_<user>_<ip>_<port>.sock``）。刚做过自检命令时该 socket
    仍然有效，scp 只需 ``ControlPath=`` 复用，不必重新输密码。

    Args:
        mgr: SSHManager 实例（提供 username / port）。
        ip (str): 目标板卡 IP。
        local (str): 本地文件路径。
        remote (str): 板端目标路径。
        timeout (int, optional): scp 超时秒数。

    Returns:
        tuple[bool, str]: ``(是否成功, 失败原因)``。
    """
    port = int(getattr(mgr, 'port', 22) or 22)
    user = getattr(mgr, 'username', '') or ''
    mux = '/tmp/mix_ssh_exec_%s_%s_%d.sock' % (user, ip, port)
    cmd = [
        'scp', '-q',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'BatchMode=yes',
        '-o', 'ControlMaster=no',
        '-o', 'ControlPath=' + mux,
        '-P', str(port),
        local, '%s@%s:%s' % (user, ip, remote),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, e)
    if proc.returncode == 0:
        return True, ''
    detail = (proc.stderr or proc.stdout or '').strip().replace('\n', ' | ')
    return False, detail or ('scp rc=%s' % proc.returncode)


def _b64_upload(mgr, ip, local, remote, timeout=10, chunk=1200):
    """退回方案：把文件 base64 分块写入板端（只依赖板端 ``base64`` 命令）。

    Args:
        mgr: SSHManager 实例。
        ip (str): 目标板卡 IP。
        local (str): 本地文件路径。
        remote (str): 板端目标路径（先写 ``<remote>.b64``，解码后再落位）。
        timeout (int, optional): 单条命令超时秒数。
        chunk (int, optional): 每个分块的 base64 字符数，避免单条命令过长
            被 PTY/expect 截断。

    Returns:
        tuple[bool, str]: ``(是否成功, 失败原因)``。
    """
    import base64

    with open(local, 'rb') as f:
        blob = base64.b64encode(f.read()).decode('ascii')

    b64_path = remote + '.b64'
    rc, out, err = _remote_exec(mgr, ip, 'rm -f %s' % _shq(b64_path), timeout)
    if rc not in (0, '0', None):
        return False, '清理临时文件失败(rc=%s): %s' % (rc, (err or out).strip()[:200])

    for i in range(0, len(blob), chunk):
        piece = blob[i:i + chunk]
        rc, out, err = _remote_exec(
            mgr, ip, "printf '%%s' %s >> %s" % (_shq(piece), _shq(b64_path)), timeout)
        if rc not in (0, '0', None):
            return False, '分块写入失败(rc=%s): %s' % (rc, (err or out).strip()[:200])

    rc, out, err = _remote_exec(
        mgr, ip, 'base64 -d < %s > %s && chmod +x %s && rm -f %s'
        % (_shq(b64_path), _shq(remote), _shq(remote), _shq(b64_path)), timeout)
    if rc not in (0, '0', None):
        return False, 'base64 解码落位失败(rc=%s): %s' % (rc, (err or out).strip()[:200])
    return True, ''


def ensure_detect_i2c(ctx, mgr, ip, cmd, log=None):
    """确保板端有可执行的 detect_i2c；缺失时从插件目录自动下发。

    检查结果按 ``(ip, 远端路径)`` 缓存在 ``ctx['_i2c_tool_ready']``，一轮扫描里
    只查/传一次。只在命令确实引用 detect_i2c 时才动手，自定义扫描器不受影响。

    Args:
        ctx (dict): 扫描上下文（由插件工作线程构造）。
        mgr: SSHManager 实例，用于执行板端命令。
        ip (str): 目标板卡 IP。
        cmd (str): 即将下发的 ssh 命令。
        log (callable, optional): 调试日志回调，形如 ``log(str)``。

    Raises:
        RuntimeError: 自检失败、本地副本缺失，或下发/安装失败。
    """
    remote = _tool_path_in_cmd(cmd)
    if not remote:
        return
    key = (ip, remote)
    ready = ctx.setdefault('_i2c_tool_ready', set())
    if key in ready:
        return

    def _log(msg):
        if log:
            log(msg)

    check = 'if test -x %s; then echo MIX_TOOL_OK; else echo MIX_TOOL_MISSING; fi' % _shq(remote)
    rc, out, err = _remote_exec(mgr, ip, check, 10)
    if rc not in (0, '0', None):
        raise RuntimeError('检查板端 %s 失败(rc=%s): %s'
                           % (remote, rc, (err or out).strip()[:200]))
    if 'MIX_TOOL_OK' in (out or ''):
        ready.add(key)
        _log('板端已存在 %s，无需下发' % remote)
        return

    local = _local_tool_path()
    if not os.path.isfile(local):
        raise RuntimeError('板端缺少 %s，且本地副本不存在: %s（可用环境变量 %s 指定本地路径）'
                           % (remote, local, LOCAL_TOOL_ENV))
    size = os.path.getsize(local)
    _log('板端缺少 %s，开始自动下发（本地 %s，%d bytes）' % (remote, local, size))

    # 先传临时文件，再 chmod + mv，避免把没传完的文件当成“已存在”。
    staging = remote + '.mixup'
    ok, detail = _scp_upload(mgr, ip, local, staging)
    if not ok:
        _log('scp 下发不可用(%s)，改用 base64 分块下发' % (detail or '未知原因'))
        ok, detail = _b64_upload(mgr, ip, local, staging, timeout=10)
    if not ok:
        raise RuntimeError('自动下发 %s 失败: %s' % (remote, detail))

    rc, out, err = _remote_exec(
        mgr, ip, 'chmod +x %s && mv -f %s %s && test -x %s && echo MIX_TOOL_OK'
        % (_shq(staging), _shq(staging), _shq(remote), _shq(remote)), 10)
    if rc not in (0, '0', None) or 'MIX_TOOL_OK' not in (out or ''):
        raise RuntimeError('安装 %s 失败(rc=%s): %s'
                           % (remote, rc, (err or out).strip()[:200]))
    ready.add(key)
    _log('已下发 %s（%d bytes）并赋可执行权限' % (remote, size))


def make_ssh_runner(default_timeout=30):
    """构造 SSH 动作执行器。

    op 形如:
        {'driver': 'ssh',       'cmd': 'echo hi'}
        {'driver': 'ssh-scan',  'cmd': 'sudo detect_i2c -y 8'}   # 自动解析地址

    底层使用公共模块 plugins/libs 的 SSHManager（expect 包装 ssh，密码经环境变量传）。
    ctx 需含 ssh_username/ssh_password/ssh_ip/ssh_port（或 ip/port/username 兜底）。
    """

    def _manager(ctx):
        if not isinstance(ctx, dict):
            raise TypeError('ctx 需为 dict')
        mgr = ctx.get('_ssh_manager')
        addr = ctx.get('_ssh_addr')
        ip = ctx.get('ssh_ip') or ctx.get('ip')
        key = (ip, ctx.get('ssh_port') or 22)
        if mgr is None or addr != key:
            # ssh_manager 是公共模块（plugins/libs），与 rsync_plugin 互不引用。
            # main_application 已把 libs 插到 sys.path 最前；这里兜底，保证
            # i2c_scan 插件独立加载时也能解析（不依赖 rsync_plugin 先加载）。
            import sys
            _libs = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), os.pardir, 'libs'))
            if os.path.isdir(_libs) and _libs not in sys.path:
                sys.path.insert(0, _libs)
            from ssh_manager import SSHManager
            username = ctx.get('ssh_username') or ctx.get('username') or 'mixadmin'
            password = ctx.get('ssh_password') or ctx.get('password') or ''
            port = int(ctx.get('ssh_port') or 22)
            mgr = SSHManager(username, password, port=port)
            ctx['_ssh_manager'] = mgr
            ctx['_ssh_addr'] = key
        return mgr

    def run(ctx, op):
        t0 = time.time()
        # 诊断日志：写入 ctx['_dbg'] 这个纯 Python 列表（线程安全，由调用方
        # 在主线程 drain 到界面）。**不要**在这里直接碰 Qt —— 本函数运行在
        # 工作线程，直接 appendPlainText 会让 Qt 文本排版跨线程访问而 SIGSEGV。
        dbg = ctx.get('ssh_debug')
        _buf = ctx.get('_dbg')

        def _d(msg):
            if dbg and isinstance(_buf, list):
                _buf.append('    [ssh] ' + str(msg))
        try:
            mgr = _manager(ctx)
        except Exception as e:
            raise RuntimeError('创建 SSH 管理器失败(%s: %s)；'
                               '请检查 SSH 账号/密码（项目里的 '
                               'default_ssh_user / default_ssh_password）'
                               % (type(e).__name__, e)) from e
        ip = ctx.get('ssh_ip') or ctx.get('ip')
        cmd = str(op['cmd'])
        timeout = int(op.get('timeout') or ctx.get('ssh_timeout') or default_timeout)
        # sudo 需要密码时：SSH 是非交互的、没有 tty，`sudo -n` 会报
        #   “sudo: a password is required”。
        # 但 `sudo -S` 会从 **stdin** 读密码，所以把 SSH 密码用管道喂给它即可，
        # 既不需要 tty，也不需要设备侧配 NOPASSWD。
        # 注意：密码通过 shell 变量传入（不是写死在命令串里），日志里看不到明文。
        use_sudo_pw = False
        if 'sudo' in cmd:
            use_sudo_pw = True
            # 无 tty 时 sudo 无法自己提示密码：用 -S 从 stdin 读。
            if 'sudo -S' not in cmd and 'sudo -n' not in cmd:
                cmd = cmd.replace('sudo', 'sudo -S', 1)
            # -p '' 会打提示符到 stderr，混进输出干扰地址解析；但空串引号在
            # Tcl 的 "..." 里会破坏引号配对（extra characters after close-quote），
            # 所以用 -p "" 之外的等价写法：-p 后跟一个空格分隔的空参数不行，
            # 改为让 sudo 用 -p 指定一个不会出现的提示串。
            if 'sudo -S' in cmd and '-p' not in cmd:
                cmd = cmd.replace('sudo -S', 'sudo -S -p MIXSUDO:', 1)
        # 板端缺 detect_i2c 时自动下发（只对引用该工具的命令生效，按 ip+路径缓存）
        ensure_detect_i2c(ctx, mgr, ip, cmd, _d)
        _d('ip=%s user=%s port=%s timeout=%ss' % (
            ip, ctx.get('ssh_username'), ctx.get('ssh_port') or 22, timeout))
        _d('cmd=%s' % cmd)
        try:
            # SSHManager.execute_command 返回 (returncode, stdout, stderr) 三元组；
            # returncode==0 才是成功（别用真值判断，0 是 falsy）。
            res = mgr.execute_command(ip, cmd, timeout=timeout,
                                      stdin_password=use_sudo_pw)
        except Exception as e:
            raise RuntimeError('SSH 执行异常(%s: %s) cmd=%s' %
                               (type(e).__name__, e, cmd)) from e
        dt = time.time() - t0
        if isinstance(res, tuple) and len(res) >= 3:
            rc, out, err = res[0], res[1], res[2]
        elif isinstance(res, tuple) and len(res) == 2:
            ok, out = res          # 兼容旧/替身实现 (ok, out)
            rc, err = (0 if ok else 1), ''
        else:
            rc, out, err = 1, '', 'execute_command 返回格式无法识别: %r' % (res,)
        _d('rc=%s 用时=%.2fs  stdout=%dB stderr=%dB' % (rc, dt, len(out or ''), len(err or '')))
        if (out or '').strip():
            _d('stdout 首行: %s' % (out.strip().splitlines()[0][:120],))
        if rc not in (0, '0', None):
            # 把 stderr 的每一行都带出来，否则用户只看到“失败”不知道为什么
            detail = (err or out or '').strip().replace('\n', ' | ')
            raise RuntimeError('ssh 执行失败(rc=%s, %.2fs) cmd=%s :: %s'
                               % (rc, dt, cmd, detail[:300]))
        if op.get('driver') == 'ssh-scan':
            return parse_found_addresses(out)
        return out

    return run



def _resolve_port(ctx, op=None):
    """决定本次动作该连哪个 RPC 端口。

    一台设备 = 一个 IP，但对外可能有多个 RPC 端点（如 7801/7802，按工位/设备
    区分）。端口因此按“动作/槽位”走；它只表示连哪个端点，
    与选 i2c_mux_base 还是 i2c_mux_array（同一颗 FPGA 的两个服务）无关：
        op['port'] > ctx['rpc_port'] > ctx['port'] > 7801
    这样同一张表里可以有的行切 7801 的 mux、有的行切 7802 的。
    """
    for src in (op or {}), ctx:
        if not isinstance(src, dict):
            continue
        for k in ('port', 'rpc_port'):
            v = src.get(k)
            if v not in (None, ''):
                try:
                    return int(v)
                except Exception:
                    pass
    return 7801


def _ensure_client(ctx, op=None, _deadline=5):
    """获取/建立（并按 ip:port 缓存）一个 JsonRpcClient。

    新建立连接时会做一次轻量可达性核验（client.ping = 裸 TCP 连接，不需 PTY）。
    ip:port 不可达直接抛 ConnectionError，并把错误信息带到上层日志，避免下游把
    “网络不通”误判成“总线上没设备”。

    连接按 (ip, port) 缓存在 ctx['_rpc_clients'] 里：因为一次扫描可能先后用到
    多个端点的连接，不能只留一个 client，否则每换一个端点都要重连。
    """
    if not isinstance(ctx, dict):
        raise TypeError('ctx 需为 dict')
    ip = ctx.get('ip')
    port = _resolve_port(ctx, op)
    key = (ip, port)

    pool = ctx.get('_rpc_clients')
    if not isinstance(pool, dict):
        pool = {}
        ctx['_rpc_clients'] = pool
    # 兼容旧字段：早期版本只缓存单个 client，这里收编进池子
    old_c, old_k = ctx.get('_rpc_client'), ctx.get('_rpc_key')
    if old_c is not None and old_k is not None and old_k not in pool:
        pool[old_k] = old_c

    client = pool.get(key)
    if client is None:
        _import_and_add_mix_path()
        from mix8_rpc_client import JsonRpcClient
        client = JsonRpcClient(ip, port)
        # 轻量可达：ping = TCP connect。不通立刻给出明确诊断。
        try:
            reach = bool(client.ping(ip, port))
        except Exception:
            reach = False
        if not reach:
            try:
                client.close()
            except Exception:
                pass
            raise ConnectionError(
                'RPC 无法连接 %s:%s → 网络不通或下位机平台未启动。' % (ip, port))
        # 走一次注册握手确认（部分固件要 hello/register 后才真正可用）
        if hasattr(client, 'connect'):
            try:
                if callable(getattr(client, 'connect', None)):
                    client.connect()
            except Exception:
                pass
        pool[key] = client
        # 保留旧字段语义（=最近一次用到的 client），不破坏既有读取方
        ctx['_rpc_client'] = client
        ctx['_rpc_key'] = key
    return client


def make_rpc_call_runner():
    """执行一个“原样 int 参数”的 RPC 方法调用（绕过 stub 的 to_float 强转）。

    用于 mux 类方法（如 i2c_mux_base.set_channel_state_doe(ch)）等需要把 int 原样
    下发到驱动层的场景。op:
        {'driver':'rpc-call','service':'i2c_mux_base',
         'method':'set_channel_state_doe','args':[1]}
    可选 op['port']：本动作要连哪个 RPC 端点；给了就用它，否则用 ctx 的端口。
    末尾以 delay 拆分回 main：由调用方决定（此处仅返回结果）。
    """

    def run(ctx, op):
        client = _ensure_client(ctx, op)
        service = op['service']
        method = op['method']
        args = list(op.get('args') or [])
        timeout = int(op.get('rpc_timeout') or ctx.get('rpc_timeout') or 15)
        # service 可能带 dut0./mixdevice. 等宿主前缀，底层 RPC 服务名不带该前缀
        svc = service
        for prefix in ('dut0.', 'dut1.', 'mixdevice.'):
            if svc.startswith(prefix):
                svc = svc[len(prefix):]
                break
        return client._send_request(svc, method, args, rpc_timeout=timeout)

    return run


def default_registry(fake=None):
    """构造含 rpc / rpc-call / ssh / ssh-scan 的默认注册表。

    扫描只有一种方式：ssh-scan（板端 detect_i2c）。原来的 rpc-scan
    （上位机逐个地址 read）已删除。
    """
    reg = TransportRegistry()
    reg.register('rpc', make_rpc_runner(fake=fake))
    reg.register('rpc-call', make_rpc_call_runner())
    reg.register('ssh', make_ssh_runner())
    reg.register('ssh-scan', make_ssh_runner())
    return reg


def fmt_result(res):
    """把动作结果格式化为单行可读文本（用于日志/界面）。"""
    if isinstance(res, (list, tuple, set)):
        return ', '.join(str(x) for x in res)
    if isinstance(res, dict):
        try:
            import json
            return json.dumps(res, ensure_ascii=False)
        except Exception:
            return str(res)
    return str(res)
