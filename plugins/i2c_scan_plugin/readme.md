# I2C-Scan 插件 — 板卡 I2C 扫描 / 插卡自检

**它解决什么问题**：板卡插上后程序起不来，多半是某条 I2C 总线（尤其是 mux 门控后的那几条）
上的器件没到位。本插件把「选一个要扫的目标 → 需要的话先切 mux → 在下位机上扫这条总线 →
把探测到的地址列出来」做成一个通用 Qt 工具。

**设计要点**：插件代码只做「界面 + 后台线程 + 执行动作」，**扫什么、怎么切 mux 全部写在一张数据表里**
（`projects/*.py`）。换项目 = 换一张表，不改插件代码。

- 插件目录：`plugins/i2c_scan_plugin/`，入口类 `I2cScanPlugin`，标签页名 `I2C-Scan v1.0`
- 已在 `plugins/plugins.json` 注册为 `"i2c_scan"`，随主程序自动加载

---

## 一、怎么用（界面）

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ [目标与扫描驱动]  项目▾  IP[____]  端口:7801/7802  SSH:mixadmin/已配置  [连接测试] │
├───────────────────────────────────────────────────────────────────────────────┤
│ [按槽位扫描]  [扫描全部槽位] [停止]   当前扫描命令: sudo -S /tmp/detect_i2c -y 0 │
│  ┌────────────────────┬──────────────┬──────┬──────────────────────┐          │
│  │ 扫描对象            │ 切 mux        │ 扫描 │ 探测到地址            │          │
│  ├────────────────────┼──────────────┼──────┼──────────────────────┤          │
│  │ dut0 Trigger板 ...  │ 不需要切 mux  │[扫描]│ 0x20, 0x50           │          │
│  │ dut0 Array ... 通道1│ [切 mux]      │[扫描]│ 0x70, 0x71           │ ← 需先切  │
│  └────────────────────┴──────────────┴──────┴──────────────────────┘          │
├───────────────────────────────────────────────────────────────────────────────┤
│ [调试日志]  ping 核验 / 每步动作 / 返回结果 / 失败原因（只读）                     │
└───────────────────────────────────────────────────────────────────────────────┘
```

操作顺序：

1. 选**项目**（下拉里是 `projects/*.py` 发现的全部项目文件），选完 IP 会带出该项目的默认值
2. 点**连接测试**：对 IP 做一次裸 TCP 探测（本项目用到的 RPC 端口 + SSH 22），先确认网络通
3. 单行点**扫描**，或点**扫描全部槽位**批量扫（可点**停止**中断，见下）
4. 按钮**切 mux** 只出现在「需要先切」的行上，作用是**只切不扫**（用于手动接好线再确认）；
   不需要切的行显示灰字 `不需要切 mux`，避免误点
5. 结果写回「探测到地址」列（逗号分隔，鼠标悬停有完整内容），细节全程打在日志里

界面上的「端口」「SSH」是**只读展示**，不给改：

| 展示项 | 真实来源 | 说明 |
|---|---|---|
| 端口 | 各行 `SLOTS[*]['port']` | 显示本项目用到的全部端口（dut0=7801 / dut1=7802） |
| SSH | `PROJECT['default_ssh_user']` / `default_ssh_password` | 只显示“已配置/未配置”，改凭据请改项目文件 |

---

## 二、代码结构与职责

| 文件 | 职责 | 关键入口 |
|---|---|---|
| `i2c_scan_plugin.py` | Qt 界面、后台线程、拼 ctx、把结果写回表格 | `I2cScanPlugin`、`_Worker`、`_make_ctx()` |
| `hook_base.py` | 加载项目文件；跑「一个槽位」的完整流程；表驱动工厂 | `load_project_module()`、`run_slot_workflow()`、`make_table_hook()` |
| `transports.py` | 按 `driver` 真正下发动作；把扫描输出解析成地址列表 | `default_registry()`、`parse_found_addresses()` |
| `projects/*.py` | **每个项目一张数据表**（要扫的槽位 + 怎么切 mux） | `PROJECT`、`SLOTS`、`make_table_hook(...)` |
| `detect_i2c` | 板端扫描器的本地副本（ARM 版 i2cdetect）；板端缺失时自动下发 | `ensure_detect_i2c()` |

调用关系：`I2cScanPlugin` → `hook_base`（问“这一行该干什么”）→ `transports`（真正去 RPC/SSH 干活）。

---

## 三、一次「扫描」到底发生了什么

```
点「扫描」/「扫描全部槽位」
        │
        ▼
  ① worker 后台线程          UI 不卡；界面更新全部走 Qt 信号回主线程
        │
        ▼
  ② 可达性核验（ping）        该行要用 RPC 就探 RPC 端口，只走 SSH 就探 22
        │                    不通 → 报错；批量扫描时**整批中止**（后面再做也没意义）
        ▼
  ③ 切 mux（只有需要切的行）   执行 mux_ops：RPC 下发通道号，每条切动作后自动带 settle 延时
        │
        ▼
  ④ 扫地址                   执行 probe：板端 `detect_i2c` 扫这条总线（ssh-scan）
        │                    输出交给 parse_found_addresses 归一成 ['0x20','0x50',...]
        ▼
  ⑤ 收尾                     release_ops（表驱动项目返回空）
        │
        ▼
  ⑥ 结果回填                  result 信号 → 主线程写「探测到地址」列；日志同步打印耗时
```

几个容易踩的实现细节（改代码前先看这几条）：

- **可达性结果按 ctx 缓存**：同一批扫描里 `(ip, 端口)` 只探一次，不会每行都 ping。
- **延时不会重复**：`mux_ops()` 已经在切动作后带了 `settle_ms`（默认 250ms），
  所以 `run_slot_workflow()` 只对「不切 mux 的行」补一次延时。
- **子线程绝不碰 Qt 控件**：SSH 的调试信息写进 `ctx['_dbg']`（纯 Python list），
  由界面上的 200ms `QTimer` 在主线程 drain 进日志框。子线程里直接 `appendPlainText`
  会触发 Qt 跨线程排版，实测直接 SIGSEGV。
- **停止是“行间生效”**：点停止只置一个标志，当前这一行跑完才退出，不会打断正在跑的
  一次 SSH/RPC 调用（也意味着不会把设备留在半切状态）。

---

## 四、三个必须搞清的概念

**1. 槽位（slot）**：表格里的一行 = 一个要扫的 I2C 目标。
`key` 是唯一标识（内部用），`label` 是给人看的显示名。

**2. 动作（op）**：一个可序列化的 dict，`driver` 决定谁来执行。

| driver | 谁执行 | 用途 |
|---|---|---|
| `rpc` | `JsonRpcClient.stub()` | 一般设备方法调用（会做数值转换） |
| `rpc-call` | `JsonRpcClient._send_request()` | 原样下发 int，**切 mux 用这个** |
| `ssh` | `SSHManager`（expect 包 ssh） | 板壳执行命令，返回原始输出 |
| `ssh-scan` | 同上 + 地址解析 | 板壳执行扫描命令，返回 `['0x..', ...]` |
| `delay` | `execute_ops` 自己处理 | 步间等待（**不经过** registry） |

**3. port = 工位，不是 mux**（最容易搞混的一条）：

- `dut0` 对应 `7801`、`dut1` 对应 `7802`，**端口区分的是“哪个工位”**；
- 同一工位内有**两条 mux**，它们是同一颗 FPGA 里的两个 RPC 服务，靠**服务名**区分，
  与端口无关：

| 服务名 | 物理器件 | 上游总线 |
|---|---|---|
| `i2c_mux_array` | TCA9548 @0x77 | dut0: i2c_0 / dut1: i2c_4 |
| `i2c_mux_base` | TCA9548 @0x76 | dut0: i2c_8 / dut1: i2c_9 |

所以切 mux 时如果连错端口，就会去切**另一个工位**的板子。动作上的端口优先级：
`op['port']` > `ctx['rpc_port']` > `ctx['port']` > 7801。

---

## 五、扫描表怎么写（加 / 改一个项目）

### 5.1 最小项目文件

在 `projects/` 下新建 `xxx.py`：

```python
import os
import sys

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
from hook_base import make_table_hook

PROJECT = {
    'name': 'xxx',
    'title': 'XXX站',              # 下拉里显示的名字
    'default_ip': '192.168.99.34',
    'default_port': 7801,
    'default_ssh_user': 'mixadmin',
    'default_ssh_password': os.environ.get('I2C_SSH_PASSWORD', 'mixadmin'),
    'settle_ms': 250,              # 切 mux 后等器件稳定的时间
}

def _mux(service, channel):
    return {'driver': 'rpc-call', 'service': service,
            'method': 'set_channel_state_doe', 'args': [int(channel)]}

SLOTS = [
    # 直接扫：scan 直接给完整的 probe dict
    {'key': 'dut0-WIB', 'label': 'dut0 WIB板 i2c_6', 'port': 7801,
     'scan': {'kind': 'ssh-scan', 'cmd': 'sudo -S /tmp/detect_i2c -y 6'}},

    # 先切 array mux 的通道 1，再扫
    {'key': 'dut0-array-ch1', 'label': 'dut0 Array i2c_0 通道1', 'port': 7801,
     'scan': {'kind': 'ssh-scan', 'cmd': 'sudo -S /tmp/detect_i2c -y 0'},
     'mux': [_mux('i2c_mux_array', 1)]},
]

_hook = make_table_hook(PROJECT, SLOTS, settle_ms=PROJECT['settle_ms'])

# 下面这组转发是给加载器用的（加载器只认模块级函数）
PROJECT = _hook.PROJECT
def slots():        return _hook.slots()
def mux_ops(key):   return _hook.mux_ops(key)
def probe(key):     return _hook.probe(key)
def release_ops(key): return []
def settle_ms():    return PROJECT.get('settle_ms', 250)
def label_for(key): return _hook.label_for(key)
def needs_mux(key): return _hook.needs_mux(key)
```

`projects/b610-DFU.py`、`projects/b610-FCT.py` 就是按这个套路写的模板，照抄改表即可。

### 5.2 表里的字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `key` | 是 | 唯一标识 |
| `label` | 否 | 「扫描对象」列显示的文字（不写就显示 key） |
| `scan` | 是 | **必须是 probe dict**：`{'kind':'ssh-scan','cmd':...}`；也支持 `{'kind':'rpc',...}`、`{'kind':'none'}`（只切不扫） |
| `port` | 否 | 该行走哪个 RPC 端点（=工位）。不写则用 ctx 的默认端口 |
| `mux` | 否 | 不写 = 直接扫；写 `[op, ...]` = 先执行这些动作再扫 |
| `expected` | 否 | 预期地址，仅供外部比对；**当前界面不展示** |

`scan` 传字符串会直接抛 `TypeError` —— 这是**有意为之**：早期的「上位机逐个地址发 RPC」扫法
已删除，留字符串入口只会静默失效。

### 5.3 b610 这两个文件多做的事

它们把表里的 `'i2c_8'` 写法翻译成板端命令（`_hook_rows()` + `SSH_TPL`），
所以表里只需要写总线号，不用手抄 `sudo -S /tmp/detect_i2c -y 8`：

```python
SSH_TPL = '%s%s -y {bus}' % (SUDO, DETECT_I2C)   # SUDO 默认 'sudo -S '
```

想换扫描器或改 sudo 行为，只需改环境变量，见下一节。

---

## 六、设备侧前提（SSH / sudo）

### 6.1 只有一种扫法

在**下位机上**执行板端扫描器扫这条总线（一条 SSH 命令 = 一条总线，网络只走一个来回）。
不再有「上位机逐个地址发 RPC read」的退路。

### 6.2 为什么是 `sudo -S`

`/dev/i2c-*` 默认只有 root 能打开，普通用户跑会报：

```
Error: Could not open file `/dev/i2c-4': Permission denied ... Run as root?
```

而 SSH 是非交互的、没有 tty，直接 `sudo` 会报 `sudo: no tty present and no askpass program specified`。

本插件的做法：命令走 **`sudo -S`**（从 **stdin** 读密码），密码用 SSH 密码通过管道喂给它
（`SSHManager.execute_command(..., stdin_password=True)`），并且提示符被替换成 `MIXSUDO:`
以免混进输出干扰地址解析。**因此设备侧不需要配置 NOPASSWD sudoers。**

### 6.3 环境变量与凭据

| 变量 | 默认 | 作用 |
|---|---|---|
| `I2C_SUDO` | `sudo -S ` | sudo 前缀；**置空则不加 sudo**（设备已放开 `/dev/i2c-*` 权限时用） |
| `I2C_DETECT_BIN` | `/tmp/detect_i2c` | 板端扫描器路径；换 `i2cdetect` 需其输出能被解析 |
| `I2C_DETECT_LOCAL_BIN` | 插件目录下的 `detect_i2c` | 自动下发时使用的**本地副本**路径（换一份编译产物时用） |
| `I2C_SSH_PASSWORD` | 见项目文件 | 覆盖项目里的 SSH 密码 |

SSH 账号/密码写在项目文件的 `default_ssh_user` / `default_ssh_password`
（`SSHManager` 不接受空密码）；界面上不提供修改。SSH 底层是
`plugins/libs/ssh_manager.py`（expect 包装 ssh），所以**系统需要 expect**。

### 6.4 替代方案（不想用 sudo）

设备上给用户放开 i2c 权限（加 i2c 组 / udev 规则）后，置空 `I2C_SUDO`：

```bash
I2C_SUDO= python3 main_application.py
```

### 6.5 扫描器缺失时自动下发

每条 `ssh-scan` 动作执行前会先自检板端扫描器路径是否可执行
（`transports.ensure_detect_i2c()`）：

- **已存在** → 直接扫，不做任何上传；
- **缺失** → 从插件目录的 `detect_i2c` 副本下发（可用 `I2C_DETECT_LOCAL_BIN` 换本地文件）：
  1. `scp` 传到 `<路径>.mixup`（复用 SSHManager 的 ControlMaster 连接，不必再认证一次）；
  2. 板上没有 scp/sftp 时退回 base64 分块写入（只依赖板端 `base64`）；
  3. `chmod +x` + `mv -f` 落位，再 `test -x` 复核。
- 命令里用的是**别的扫描器**（如 `/usr/bin/i2cdetect`）→ 不补，按原命令执行。

自检结果按 `(ip, 扫描器路径)` 缓存在 `ctx['_i2c_tool_ready']`，一轮扫描只查/传一次；
下发失败会直接抛错并显示在槽位日志里（`自动下发 /tmp/detect_i2c 失败: ...`），不会静默跳过。
板端路径需可写（默认 `/tmp` 可以）；若改成 `/usr/local/bin` 这类目录，需要额外 sudo，
建议保持默认。

---

## 七、超时与排错

| 项 | 值 | 说明 |
|---|---|---|
| `rpc_timeout` | 5s | 切 mux 是本地 RPC，正常几十 ms |
| `ssh_timeout` | 3s | 单条 `detect_i2c` 上限；正常 0.05~0.5s |
| `settle_ms` | 250ms（项目里配） | 切 mux 后等器件稳定 |
| TCP 可达探测 | 1.5s / 2s | 前台提示 + worker 内 ping 核验 |

> 备注：`ctx['scan_deadline']` 字段目前**没有实际生效**（是早期逐地址扫描的遗留），不要依赖它。

常见日志与含义：

| 日志 | 原因 / 处理 |
|---|---|
| `⚠ 无法连接 ip:7801 → 网络不通或平台未启动。已停止后续扫描。` | 网线/网关/IP 不对，或下位机平台进程没起 |
| `RPC 无法连接 ip:port → ...` | 同上（由建立连接时的 TCP ping 抛出） |
| `创建 SSH 管理器失败(...)` | SSH 账号/密码没配（改项目文件的 `default_ssh_*`） |
| `ssh 执行失败(rc=..., 耗时) cmd=... :: stderr` | 会把 rc、耗时、stderr 原文带出来：常见是密码不对（`sudo: a password is required`）、扫描器路径不存在（用 `I2C_DETECT_BIN` 改） |
| `自动下发 /tmp/detect_i2c 失败: ...` | 板端缺扫描器且自动补齐失败：多为板端不可写、无 scp/base64，或本地副本不存在（用 `I2C_DETECT_LOCAL_BIN` 指定） |
| `✓ dut0-WIB: (空) (0.12s)` | 扫**成功了**但这条总线上确实没器件；与“没扫成功”（会带错误信息）区分开 |
| `首次错误完整堆栈` | 批量扫描里第一处失败的完整 traceback，用于定位 |

---

## 八、接口速查（改代码/写单测时看）

### 8.1 项目钩子的最小接口

加载器（`load_project_module`）会校验模块里必须有 `PROJECT`、`mux_ops`、`probe`；
其余是可选增强：

| 成员 | 签名 | 作用 |
|---|---|---|
| `PROJECT` | dict | `name/title/default_ip/default_port/default_ssh_user/default_ssh_password/settle_ms` |
| `mux_ops(key)` | → `list[op]` | 切到该槽位需要的动作（可含 delay） |
| `probe(key)` | → probe dict | 该槽位去读哪些地址 |
| `slots()` | → `list[key]` | 可留空，留空则用 `PROJECT['slots']` |
| `release_ops(key)` | → `list[op]` | 扫完收尾，可为空 |
| `settle_ms()` | → int | 不切 mux 的行补一次延时用 |
| `needs_mux(key)` | → bool | 界面决定该行放「切 mux」按钮还是灰字 |
| `label_for(key)` | → str | 界面「扫描对象」列显示名 |
| `expected_for(key)` / `scan_hint(key)` | → str | 目前界面未使用，供外部/调试取用 |

> 注意：钩子模块**不应 import 本插件的 Qt 类**，保持纯函数便于离屏测试。

### 8.2 probe 格式

```python
{'kind': 'ssh-scan', 'cmd': 'sudo -S /tmp/detect_i2c -y 8'}  # 板端扫，结果自动解析
{'kind': 'rpc', 'service': 'xxx', 'method': 'yyy', 'args': []}     # 走 RPC 取一组地址
{'kind': 'none'}                                                    # 只切不扫（手动接线场景）
```

probe 里的 `port` 会被带到对应动作上，避免丢端口连错工位。

### 8.3 地址解析规则（`parse_found_addresses`）

同时兼容：

1. `detect_i2c` / `i2cdetect` 的表格输出（行首 `NN:` 前缀、格子里的 `XX/--/UU`）
2. 纯地址列表（一行一个、逗号或空格分隔、带不带 `0x` 都行）

统一过滤成 **0x03 ~ 0x77** 的 7bit 地址，去重后按升序返回小写字符串，如 `['0x20', '0x50', '0x77']`。

### 8.4 无板卡/单测

`transports.default_registry(fake={...})` 可注入 `_FakeRpc` 桩，
键为 `'service.method'`，值可以是常量或函数，用来在没有下位机时验证动作编派逻辑。
