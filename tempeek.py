# -*- coding: utf-8 -*-
"""
tempeek.py —— Windows 桌面硬件监控悬浮面板

不绑定具体机型：风扇数量在启动时自动探测（1-6 个），面板高度随风扇数量伸缩；
温度/风扇优先走厂商固件通道（戴尔 AWCC），没有时回退到 LibreHardwareMonitor。

数据源与权限
──────────────────────────────────────────────────────────────
CPU/GPU 温度、风扇转速      WMI root\WMI : AWCCWmiMethodFunction（戴尔固件通道，需管理员）
                            无 AWCC 时回退 LibreHardwareMonitor + PawnIO（通用后端）
                            风扇 ID 由 0x05 探测自动发现，单/双/多风扇都能跑
GPU 占用率 / 显存 / 温度    nvidia-smi（无需管理员）
NVMe 硬盘温度               MSFT_PhysicalDisk.GetStorageReliabilityCounter（需管理员）
主板温度                    LHM Super I/O；回退 ACPI 热区 MSAcpi_ThermalZoneTemperature
CPU/内存/网络/磁盘速率      psutil（无需管理员）

启动时若非管理员，会用 UAC 静默提权重启自身（本机 ConsentPromptBehaviorAdmin=0）。
提权失败也能跑，只是温度、风扇两栏显示 "--"。

交互
──────────────────────────────────────────────────────────────
左键拖动    移动面板
右键        菜单：迷你模式 / 刷新间隔 / 透明度 / 置顶 / CPU 频率上限 / 版本更新 / 退出
双击        切换迷你模式

CPU 频率上限通过 Windows 电源计划 PROCTHROTTLEMAX 实现（需管理员，系统级持久设置，
重启后仍生效）。菜单里随时可恢复 100%。程序不修改最小处理器状态、不写倍频、不改 BIOS。

只读程序：不写风扇转速、不改散热档位，退出时无需复位任何硬件状态。
"""
from __future__ import annotations

import atexit
import collections
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.messagebox as messagebox
import tkinter.simpledialog as simpledialog
import urllib.request

APP_NAME = "TempPeek"
APP_VER = "2.2.0"


def _app_root():
    """源码运行时是脚本目录；PyInstaller 打包后是资源解包目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _app_root()
CFG_PATH = os.path.join(os.path.expanduser("~"), ".tempeek.json")
CREATE_NO_WINDOW = 0x08000000

# 风扇一栏最多显示几行。超过这个数量的接口多半是驱动把同一路信号重复上报，
# 面板不应无限拉长；1-6 已经覆盖笔记本（1-2）到台式机/工作站（3-6）的常见形态。
MAX_FANS = 6

# GitHub 更新：仓库根目录放 version.json（推荐），失败时回退到 Releases API
UPDATE_RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
UPDATE_API = "https://api.github.com/repos/{repo}/releases/latest"
VERSION_FILE = "version.json"
SELF_FILE = "tempeek.py"
DEFAULT_BRANCH = "main"
DEFAULT_REPO = "XiaoShips/tempeek"

# ─────────────────────────── 配色（深蓝灰） ───────────────────────────
C = {
    "bg":      "#0b1018",   # 主背景
    "panel":   "#141c28",   # 菜单背景
    "card":    "#151e2b",   # 卡片
    "card2":   "#121a26",   # 次级卡片
    "line":    "#253043",   # 分隔线
    "text":    "#e9f0fa",
    "dim":     "#8b9ab0",
    "faint":   "#4d5c72",
    "green":   "#3ddc97",
    "yellow":  "#ffc857",
    "orange":  "#ff9f5a",
    "red":     "#ff5f6d",
    "blue":    "#5aa9ff",
    "cyan":    "#4fd6d2",
    "track":   "#222d3e",   # 进度条轨道
}

def temp_color(t):
    """温度 → 颜色（75 起提醒，85 起预警，92 起告警）。"""
    if t is None:
        return C["faint"]
    if t >= 92:
        return C["red"]
    if t >= 85:
        return C["orange"]
    if t >= 75:
        return C["yellow"]
    return C["green"]


def temp_state(t):
    """温度 → (中文状态, 颜色)。"""
    if t is None:
        return "无数据", C["yellow"]
    if t >= 92:
        return "过热", C["red"]
    if t >= 85:
        return "预警", C["orange"]
    if t >= 75:
        return "偏高", C["yellow"]
    return "正常", C["green"]


def fmt_rate(bps):
    if bps is None:
        return "--"
    if bps >= 1048576:
        return "%.1fM" % (bps / 1048576.0)
    if bps >= 1024:
        return "%.0fK" % (bps / 1024.0)
    return "%dB" % int(bps)


def fmt_gb(nbytes):
    return "%.1fG" % (nbytes / 1073741824.0) if nbytes else "--"


# ─────────────────────────── 静默提权 ───────────────────────────
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def windowless_exe():
    """提权时用的解释器：优先 pythonw.exe，避免弹出控制台黑框。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def relaunch_elevated():
    """非管理员时用 runas 重启自身（本机 UAC 为静默提升，不会弹窗）。"""
    if "--elevated" in sys.argv:
        return False
    args = [a for a in sys.argv[1:] if a != "--elevated"]
    params = " ".join('"%s"' % a if " " in a else a for a in args)
    if params:
        params = '"%s" %s --elevated' % (os.path.abspath(__file__), params)
    else:
        params = '"%s" --elevated' % os.path.abspath(__file__)
    try:
        # nShow=0（SW_HIDE）：venv 里的 pythonw.exe 其实是控制台子系统，
        # 用 1 会闪出一个黑框；隐藏后进程照常运行，Tk 窗口自己会显示
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", windowless_exe(), params, HERE, 0)
        return rc > 32
    except Exception:
        return False


# ────────────────────── 单实例：双击两次只开一个面板 ──────────────────────
_MUTEX_NAME = "TempPeek_SingleInstance_v1"
_mutex_handle = None


def _focus_existing_window():
    """把已在运行的面板窗口提到前台（第二次启动时用）。"""
    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    # 64 位句柄必须显式声明签名，否则按 c_int 传会被截断
    user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.AttachThreadInput.argtypes = [
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]

    hits = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lp):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == APP_NAME:
                hits.append(hwnd)
                return False
        return True

    try:
        user32.EnumWindows(proc(_cb), 0)
    except Exception:
        return False
    if not hits:
        return False

    hwnd = hits[0]
    try:
        user32.ShowWindow(hwnd, 9)                   # SW_RESTORE
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = k32.GetCurrentThreadId()
        linked = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if linked:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    except Exception:
        pass
    return True


def acquire_single_instance():
    """抢命名互斥量；已有实例则前置它的窗口并返回 False。"""
    global _mutex_handle
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        h = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return True                              # 创建失败就放行，别把程序卡住
        if k32.GetLastError() == 183:                # ERROR_ALREADY_EXISTS
            k32.CloseHandle(ctypes.c_void_p(h))
            _focus_existing_window()
            return False
        _mutex_handle = h                            # 持有引用，进程退出才释放
        return True
    except Exception:
        return True


# ─────────────────────────── 数据源：AWCC ───────────────────────────
def open_root_wmi():
    """连接 root\\WMI。部分系统会拒绝带机器前缀的 moniker，故优先无前缀写法。"""
    import win32com.client
    last = None
    for mon in ("winmgmts:root/WMI", "winmgmts:\\\\.\\root\\WMI"):
        try:
            return win32com.client.GetObject(mon)
        except Exception as exc:
            last = exc
    raise last


class AwccSource:
    """戴尔 AWCC WMI：CPU/GPU 温度 + 风扇转速。需管理员，且必须在本线程创建 COM。

    风扇数量因机型而异（单风扇轻薄本、双风扇游戏本、部分工作站更多），
    这里不写死 ID 表：启动时用 0x05 逐个探测 0x30-0x3F，
    只有返回 != 0xFFFFFFFF 的 ID 才是真实存在的风扇接口。
    （实测：无效 ID 的 0x05 恒返回 0xFFFFFFFF；0x03 枚举对任何 arg 都回 0x32，
    0x06 百分比恒为 0xFFFFFFFF，都不能用来发现风扇，只有 0x05 可用。）
    """

    OP_INFO, OP_RPM, OP_MAX = 0x04, 0x05, 0x09
    SENSOR_CPU, SENSOR_GPU = 0x101, 0x106
    FAN_ID_RANGE = range(0x30, 0x40)     # AWCC 风扇 ID 的取值区间
    FALLBACK_FAN_IDS = (0x32, 0x33)      # 探测失效时的传统双风扇 ID
    SENTINEL = 0xFFFFFFFF                # “该 ID 不存在”的返回值

    def __init__(self):
        self.ok = False
        self.err = ""
        self.fan_max = 0
        self.fan_ids = list(self.FALLBACK_FAN_IDS)
        self._obj = None
        self._md = {}
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            svc = open_root_wmi()
            for item in svc.ExecQuery("SELECT * FROM AWCCWmiMethodFunction"):
                self._obj = item
                break
            if self._obj is None:
                self.err = "无 AWCC 实例"
                return
            for name in ("Thermal_Information",):
                self._md[name] = self._obj.Methods_(name).InParameters.SpawnInstance_()
            self.fan_ids = self._discover_fans()
            self.fan_max = self._ti(self.OP_MAX, self.fan_ids[0]) or 5600
            if not 0 < self.fan_max < 20000:
                self.fan_max = 5600
            self.ok = True
        except Exception as exc:
            self.err = str(exc)

    def _ti(self, op, idx=0, arg=0):
        params = self._obj.Methods_("Thermal_Information").InParameters.SpawnInstance_()
        params.arg2 = (op | (idx << 8) | (arg << 16)) & 0xFFFFFFFF
        out = self._obj.ExecMethod_("Thermal_Information", params)
        return int(getattr(out, "argr", -1)) & 0xFFFFFFFF

    def _discover_fans(self):
        """探测实际存在的风扇接口，返回 ID 列表（升序）。

        两个保守兜底，避免在非戴尔或固件行为异常的机器上编造风扇：
        - 一个都没探到：退回传统双风扇 ID（让用户至少看到一行 "--"）
        - 探到的比 MAX_FANS 还多：几乎可以确定是固件对所有 ID 都返回 0，
          无法区分真假，同样退回传统 ID
        """
        found = []
        for fid in self.FAN_ID_RANGE:
            try:
                if self._ti(self.OP_RPM, fid) == self.SENTINEL:
                    continue
            except Exception:
                continue
            found.append(fid)
        if not found or len(found) > MAX_FANS:
            return list(self.FALLBACK_FAN_IDS)
        return found

    def read(self, full=True):
        if not self.ok:
            return {}
        d = {}
        try:
            cpu = self._ti(self.OP_INFO, self.SENSOR_CPU)
            d["cpu_temp"] = cpu if 0 < cpu < 120 else None
            gpu = self._ti(self.OP_INFO, self.SENSOR_GPU)
            d["gpu_temp"] = gpu if 0 < gpu < 120 else None
            fans = []
            for fid in self.fan_ids:
                rpm = self._ti(self.OP_RPM, fid)
                # 停转的风扇返回 0，属于有效读数；只有哨兵值才算接口不存在
                fans.append(rpm if rpm != self.SENTINEL and rpm < 20000 else None)
            d["fans"] = fans
        except Exception:
            pass
        return d


# ─────────────────────────── 数据源：nvidia-smi ───────────────────────────
class NvidiaSource:
    """GPU 占用率 / 显存 / 功耗。AWCC 只给温度，这几项从 nvidia-smi 取。"""

    def __init__(self):
        self.exe = None
        for cand in ("nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe",
                     r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if cand == "nvidia-smi":
                import shutil
                p = shutil.which("nvidia-smi")
                if p:
                    self.exe = p
                    break
            elif os.path.exists(cand):
                self.exe = cand
                break
        self.name = ""

    def read(self):
        if not self.exe:
            return {}
        try:
            r = subprocess.run(
                [self.exe,
                 "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,"
                 "memory.total,power.draw,clocks.gr",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=6,
                creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0 or not r.stdout.strip():
                return {}
            parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
            if len(parts) < 5:
                return {}
            self.name = parts[0]
            out = {"gpu_temp": int(float(parts[1]))
                   if parts[1] not in ("[N/A]", "") else None,
                   "gpu_util": int(float(parts[2])) if parts[2] else None,
                   "gpu_mem": (int(float(parts[3])), int(float(parts[4])))}
            try:
                out["gpu_power"] = float(parts[5]) if parts[5] not in ("[N/A]", "") else None
            except Exception:
                out["gpu_power"] = None
            try:
                out["gpu_clock"] = int(float(parts[6])) if parts[6] not in ("[N/A]", "") else None
            except Exception:
                out["gpu_clock"] = None
            return out
        except Exception:
            return {}


# ─────────────────────────── 数据源：硬盘温度 ───────────────────────────
class DiskTempSource:
    """通过 Get-StorageReliabilityCounter 读 NVMe 温度。

    Windows 对 SMART 有频率限制，且该 CIM 查询开销约 0.5-1s，故低频刷新。
    SATA 盘在 Intel RST 驱动下不上报温度，会返回 0，这里直接过滤掉。
    """

    PS = ("$ErrorActionPreference='SilentlyContinue';"
          "Get-PhysicalDisk | ForEach-Object { $r = $_ | Get-StorageReliabilityCounter;"
          "if($r -and $r.Temperature -gt 0){ '{0}|{1}' -f $_.FriendlyName, $r.Temperature } }")

    def read(self):
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", self.PS],
                capture_output=True, text=True, timeout=25,
                creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0:
                return []
            rows = []
            for line in (r.stdout or "").strip().splitlines():
                if "|" in line:
                    name, _, temp = line.rpartition("|")
                    try:
                        rows.append((name.strip(), int(float(temp))))
                    except ValueError:
                        pass
            return rows
        except Exception:
            return []


# ─────────────────────────── 数据源：ACPI 热区 ───────────────────────────
class AcpiSource:
    """主板/机身热区。笔记本通常只有一个 zone，值偏低，仅作参考。"""

    def __init__(self):
        self.ok = False
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            self._svc = win32com.client.GetObject("winmgmts:\\\\.\\root\\WMI")
            self.ok = True
        except Exception:
            self.ok = False

    def read(self):
        if not self.ok:
            return {}
        try:
            vals = []
            for it in self._svc.ExecQuery(
                    "SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature"):
                raw = it.CurrentTemperature
                if isinstance(raw, int) and raw > 0:
                    vals.append(round(raw / 10.0 - 273.15, 1))
            return {"board_temp": max(vals) if vals else None}
        except Exception:
            return {}


# ─────────────────── 数据源：LibreHardwareMonitor（通用后端） ───────────────────
class LhmSource:
    """驱动级传感器后端：CPU 温度、风扇转速、主板与硬盘温度。

    戴尔笔记本有 AWCC 就够用；其他机型（尤其台式机）这些值只能走
    LibreHardwareMonitor + PawnIO。helper 每秒在 stdout 输出一行 JSON，
    由本线程读取，避免面板主线程被 COM/子进程阻塞。
    """

    EXE = os.path.join(HERE, "tools", "lhm", "sensors.exe")

    def __init__(self):
        self.ok = False
        self.err = ""
        self._proc = None
        self._data = {}
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        if self._proc and self._proc.poll() is None:
            return
        if not os.path.exists(self.EXE):
            self.err = "缺少 tools/lhm/sensors.exe"
            return
        try:
            self._proc = subprocess.Popen(
                [self.EXE, "--parent", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(self.EXE),
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            self.err = str(exc)
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        try:
            for raw in self._proc.stdout:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if d.get("fatal"):
                    self.err = d["fatal"]
                    continue
                with self._lock:
                    self._data = d
                self.ok = True
        except Exception as exc:
            self.err = str(exc)

    def snapshot(self):
        """helper 已退出时返回空，避免面板继续显示过期值。"""
        if self._proc is None or self._proc.poll() is not None:
            return {}
        with self._lock:
            return dict(self._data)

    def stop(self):
        p = self._proc
        self._proc = None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


# ─────────────────────────── 数据源：系统 ───────────────────────────
class SystemSource:
    def __init__(self):
        import psutil
        self.ps = psutil
        psutil.cpu_percent(interval=None)
        self._net = psutil.net_io_counters()
        self._disk = psutil.disk_io_counters()
        self._t = time.time()
        self._base_mhz = self._read_base_mhz()
        # psutil.cpu_freq() 在 Windows 上常返回标称基频、看不出睿频，
        # 改用性能计数器 % Processor Performance × 基频，能真实反映 Turbo。
        self._pdh_ok = False
        try:
            import win32pdh
            self._pdh = win32pdh
            self._q = win32pdh.OpenQuery()
            self._h = win32pdh.AddCounter(
                self._q, r"\Processor Information(_Total)\% Processor Performance")
            win32pdh.CollectQueryData(self._q)
            self._pdh_ok = True
        except Exception:
            self._pdh_ok = False

    @staticmethod
    def _read_base_mhz():
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                v, _ = winreg.QueryValueEx(k, "~MHz")
                if v:
                    return float(v)
        except Exception:
            pass
        return 2600.0

    def read(self):
        ps = self.ps
        now = time.time()
        dt = max(0.2, now - self._t)
        out = {}
        try:
            out["cpu_load"] = ps.cpu_percent(interval=None)

            freq = None
            if self._pdh_ok:
                try:
                    self._pdh.CollectQueryData(self._q)
                    _, v = self._pdh.GetFormattedCounterValue(
                        self._h, self._pdh.PDH_FMT_DOUBLE)
                    if v and v > 0:
                        freq = self._base_mhz * v / 100.0
                except Exception:
                    pass
            if freq is None:
                f = ps.cpu_freq()
                freq = f.current if f and f.current else None
            out["cpu_freq"] = freq

            vm = ps.virtual_memory()
            out["mem_pct"] = vm.percent
            out["mem_used"] = vm.used
            out["mem_total"] = vm.total
            net = ps.net_io_counters()
            out["net_down"] = max(0, (net.bytes_recv - self._net.bytes_recv) / dt)
            out["net_up"] = max(0, (net.bytes_sent - self._net.bytes_sent) / dt)
            self._net = net
            dk = ps.disk_io_counters()
            if dk and self._disk:
                out["disk_read"] = max(0, (dk.read_bytes - self._disk.read_bytes) / dt)
                out["disk_write"] = max(0, (dk.write_bytes - self._disk.write_bytes) / dt)
            self._disk = dk
            self._t = now
            b = ps.sensors_battery()
            if b:
                out["battery"] = b.percent
                out["on_ac"] = bool(b.power_plugged)
        except Exception:
            pass
        return out


# ─────────────────────────── 采集线程 ───────────────────────────
class Sampler(threading.Thread):
    """后台采集。Tk 非线程安全，所以只写共享 dict，由主线程轮询。"""

    def __init__(self, interval=1.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.data = {}
        self._stop = threading.Event()
        self._sys = SystemSource()
        self._nvs = NvidiaSource()
        self._disk = DiskTempSource()
        self._lhm = LhmSource()
        self._awcc = None
        self._acpi = None
        self._last_gpu = 0.0
        self._last_disk = 0.0
        self._last_acpi = 0.0
        self._fan_seen = set()          # 本次运行中转起来过的风扇名
        self.status = "启动中"
        self.status_detail = ""

    def stop(self):
        self._stop.set()
        self._lhm.stop()

    def run(self):
        # COM 对象必须在本线程创建；LHM helper 也由本线程拉起
        self._awcc = AwccSource()
        self._acpi = AcpiSource()
        self._lhm.start()
        if self._awcc.ok:
            self.status = "OK"
        elif self._lhm.ok:
            self.status = "OK"
        else:
            self.status = "降级(%s)" % (self._awcc.err or "无管理员")

        while not self._stop.is_set():
            t0 = time.time()
            d = {}
            try:
                d.update(self._sys.read())
            except Exception:
                pass
            try:
                if self._awcc:
                    d.update(self._awcc.read())
                    d["fan_max"] = self._awcc.fan_max
                    d["awcc_ok"] = self._awcc.ok
            except Exception:
                pass

            now = time.time()
            # GPU 细节 3 秒一次（子进程开销 60-80ms）
            if now - self._last_gpu >= 3.0:
                try:
                    d.update(self._nvs.read())
                    self._last_gpu = now
                except Exception:
                    pass
            # ACPI 5 秒一次
            if now - self._last_acpi >= 5.0:
                try:
                    if self._acpi:
                        d.update(self._acpi.read())
                    self._last_acpi = now
                except Exception:
                    pass

            lhm = self._lhm.snapshot()
            self._merge_lhm(d, lhm)
            self._update_status(d)
            self._store(d)

            # 硬盘温度放在本轮发布之后：没有 LHM 的机器只能走 PowerShell
            # （Get-StorageReliabilityCounter，实测 0.5-6s），若压在发布之前，
            # 首屏会一直停在 "--"；单独补一轮只影响硬盘这一格。
            if not lhm.get("disks") and now - self._last_disk >= 45.0:
                try:
                    rows = self._disk.read()
                    self._last_disk = now
                    if rows:
                        self._store({"disks": rows})
                except Exception:
                    pass

            elapsed = time.time() - t0
            self._stop.wait(max(0.2, self.interval - elapsed))

    def _store(self, d):
        """把本轮结果合并进共享快照，面板线程只读这个 dict。"""
        if not d:
            return
        with self.lock:
            merged = dict(self.data)
            merged.update(d)
            self.data = merged

    def _merge_lhm(self, d, lhm):
        """AWCC 优先；缺项用 LHM 补齐。"""
        if not lhm:
            return
        if not d.get("cpu_temp") and lhm.get("cpu_temp"):
            d["cpu_temp"] = lhm["cpu_temp"]
        if not d.get("gpu_temp") and lhm.get("gpu_temp"):
            d["gpu_temp"] = lhm["gpu_temp"]
        # Super I/O 的值比 ACPI 热区（常为固定环境温度）更有参考意义
        if lhm.get("board_temp"):
            d["board_temp"] = lhm["board_temp"]
        if not d.get("fans"):
            self._pick_lhm_fans(d, lhm.get("fans") or [])
        if not d.get("disks"):
            disks = [(n, int(round(t))) for n, t in (lhm.get("disks") or [])]
            if disks:
                d["disks"] = disks

    def _pick_lhm_fans(self, d, fans):
        """主板 Super I/O 的转速输入口多数没接线，恒为 0。
        只保留当前有转速或本次运行中转过的接口，避免把空接口当成风扇。"""
        picked = []
        for name, value in fans:
            rpm = int(round(value))
            if rpm > 0:
                self._fan_seen.add(name)
            elif name not in self._fan_seen:
                continue
            picked.append(rpm)
            if len(picked) >= MAX_FANS:
                break
        if picked:
            d["fans"] = picked

    def _update_status(self, d):
        """CPU 温度是核心项：拿不到才算不可用。
        核显机器没有 GPU 温度、无风扇机器没有转速都属于硬件本身不上报。"""
        if d.get("cpu_temp"):
            self.status = "OK"
        elif not self.status.startswith("降级"):
            self.status = "部分数据不可用"
        missing = []
        if not d.get("cpu_temp"):
            missing.append("CPU 温度")
        if not d.get("gpu_temp"):
            missing.append("GPU 温度")
        if not d.get("fans"):
            missing.append("风扇")
        self.status_detail = ("缺少 " + "、".join(missing)) if missing else ""


# ────────────────────── CPU 频率上限（电源计划） ──────────────────────
class CpuPowerCtl:
    """通过 Windows 电源计划的「最大处理器状态」限制 CPU 睿频上限。

    Windows 没有公开的写倍频接口（本机 BIOS 也锁了 undervolt），唯一稳妥、
    可逆的官方通道是 powercfg 的 PROCTHROTTLEMAX 百分比。这是系统级持久设置，
    重启后仍生效；面板菜单里随时可以恢复 100%。需管理员权限。
    """
    SUB = "SUB_PROCESSOR"
    KEY = "PROCTHROTTLEMAX"
    MIN_PCT = 50          # 低于 50% 会显著卡顿，不开放
    MAX_PCT = 100

    def _run(self, *args):
        try:
            r = subprocess.run(
                ["powercfg", *args], capture_output=True, text=True,
                timeout=12, creationflags=CREATE_NO_WINDOW,
                errors="replace")
            return (r.stdout or "") + (r.stderr or "")
        except Exception as exc:
            return "ERR:" + str(exc)

    def _query_values(self):
        """解析 powercfg 输出里所有 0x 值。

        固定结构：最小 / 最大 / 递增 / 当前交流 / 当前直流，最后两个即当前值。
        不依赖中文或英文关键词，避免中文系统用 utf-8 解码后关键词变乱码。
        """
        txt = self._run("/query", "SCHEME_CURRENT", self.SUB, self.KEY)
        if txt.startswith("ERR:"):
            return []
        vals = []
        for line in txt.splitlines():
            m = re.search(r"0x([0-9a-fA-F]+)", line)
            if m:
                vals.append(int(m.group(1), 16))
        return vals

    def _value(self, ac=True):
        vals = self._query_values()
        if len(vals) >= 4:
            return vals[-2] if ac else vals[-1]
        return None

    def get_max(self):
        """读取当前 AC（接电）上限百分比。"""
        return self._value(True)

    def get_max_dc(self):
        """读取当前 DC（电池）上限百分比。"""
        return self._value(False)

    def set_max(self, pct):
        """写入 AC + DC 并回读校验，返回 (是否成功, 说明文字)。"""
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            return False, "百分比无效"
        if not (self.MIN_PCT <= pct <= self.MAX_PCT):
            return False, "只允许 50–100"
        hx = "0x%02X" % pct
        for verb in ("/setacvalueindex", "/setdcvalueindex"):
            out = self._run(verb, "SCHEME_CURRENT", self.SUB, self.KEY, hx)
            if out.startswith("ERR:"):
                return False, out[4:]
        self._run("/setactive", "SCHEME_CURRENT")
        got = self.get_max()
        if got != pct:
            return False, "写入未生效（回读=" + str(got) + "），可能需要管理员权限"
        return True, "已设为 " + str(pct) + "%"


# ─────────────────────────── GitHub 版本更新 ───────────────────────────
def parse_ver(text):
    """把 v2.2.0 / 2.2 / 2.2.0-beta 解析成可比较的元组。"""
    nums = re.findall(r"\d+", (text or "").strip().lstrip("vV"))
    return tuple(int(n) for n in nums[:3]) or (0,)


def ver_newer(remote, local):
    a, b = parse_ver(remote), parse_ver(local)
    n = max(len(a), len(b))
    return (a + (0,) * (n - len(a))) > (b + (0,) * (n - len(b)))


def detect_repo(here):
    """从本地 .git/config 的 remote origin 自动探测 owner/repo。"""
    cfg = os.path.join(here, ".git", "config")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        m = re.search(
            r'url\s*=\s*(?:https?://github\.com/|git@github\.com:)([^\s]+)', text)
        if not m:
            return ""
        path = m.group(1).strip().rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = [p for p in path.split("/") if p]
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    except Exception:
        return ""


class UpdateChecker:
    """检查 GitHub 版本清单，并在用户确认后安全替换主程序文件。

    通道 1：仓库根目录 version.json（推荐，无 API 限流）
    通道 2：GitHub Releases 最新 tag（备用）
    只替换 tempeek.py，绝不触碰 ~/.tempeek.json、启动器、图标等用户数据。
    """
    TIMEOUT = 6

    def __init__(self, cfg, here):
        detected = detect_repo(here)
        self.repo = (cfg.get("repo") or detected or DEFAULT_REPO or "").strip()
        self.branch = (cfg.get("branch") or DEFAULT_BRANCH).strip()
        self.auto = bool(cfg.get("auto_check_update", True))
        self.info = None
        self.error = ""

    def _http(self, url, timeout=None):
        req = urllib.request.Request(
            url, headers={"User-Agent": "tempeek-updater",
                          "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout or self.TIMEOUT) as resp:
            return resp.read()

    def check(self):
        """返回 (状态, 说明)：new / same / norepo / error。"""
        if not self.repo:
            return "norepo", "未设置更新源"
        self.error = ""
        # 默认分支可能是 main 也可能是 master，两个都试；成功后记住可用分支
        last = None
        branches = [self.branch]
        alt = "master" if self.branch == "main" else "main"
        if alt not in branches:
            branches.append(alt)
        for br in branches:
            try:
                url = UPDATE_RAW.format(repo=self.repo, branch=br,
                                        path=VERSION_FILE)
                data = json.loads(self._http(url).decode("utf-8"))
                ver = str(data.get("version", "")).strip()
                if not ver:
                    raise ValueError("version.json 缺少 version 字段")
                self.branch = br
                self.info = {
                    "version": ver,
                    "date": str(data.get("date", "")),
                    "changelog": str(data.get("changelog", "")),
                    "file": str(data.get("file", SELF_FILE)),
                }
                return ("new" if ver_newer(ver, APP_VER) else "same"), ver
            except Exception as exc:
                last = exc
        try:
            d = json.loads(self._http(
                UPDATE_API.format(repo=self.repo)).decode("utf-8"))
            ver = str(d.get("tag_name") or "").strip()
            if not ver:
                raise ValueError("无 release")
            self.info = {"version": ver, "date": "",
                         "changelog": str(d.get("name") or ""),
                         "file": SELF_FILE}
            return ("new" if ver_newer(ver, APP_VER) else "same"), ver
        except Exception as exc2:
            self.error = "清单:%s / Release:%s" % (last, exc2)
            return "error", self.error

    def download(self, target=None):
        """下载 → 内容校验 → 语法编译 → 备份 → 原子替换。失败时不动原文件。"""
        if not self.repo or not self.info:
            raise RuntimeError("没有可用的更新信息")
        path = self.info.get("file") or SELF_FILE
        if path != os.path.basename(path) or "/" in path or "\\" in path:
            raise RuntimeError("更新目标不合法：" + path)
        url = UPDATE_RAW.format(repo=self.repo, branch=self.branch, path=path)
        raw = self._http(url, timeout=20)
        if len(raw) < 5000:
            raise RuntimeError("下载内容过小，可能不是完整脚本")
        text = raw.decode("utf-8")
        if APP_NAME not in text and SELF_FILE not in text:
            raise RuntimeError("下载内容不是本程序")
        compile(text, path, "exec")          # 只编译，不执行

        target = os.path.abspath(target or __file__)
        bak = target + ".bak"
        tmp = target + ".new"
        with open(target, "rb") as fh:
            old = fh.read()
        with open(bak, "wb") as fh:
            fh.write(old)
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, target)
        return target, bak


# ─────────────────────────── 悬浮窗 ───────────────────────────
class Panel:
    """悬浮面板：卡片分区式布局，Canvas 静态图元 + itemconfigure 增量更新。"""
    W = 296
    HMINI = 156
    P = 13                      # 左右外边距

    # 风扇卡片纵向参数：卡片高度 = FAN_HEAD + n * FAN_ROW，
    # 行文字自卡片底边向上排，n 变化时下方的系统卡与趋势区整体跟着平移。
    FAN_TOP = 210
    FAN_HEAD = 26
    FAN_ROW = 16
    FAN_PAD = 10                # 末行文字到卡片底边的距离

    # 字体集中定义：中文用雅黑，数字用等宽 Consolas
    F_TITLE = ("Microsoft YaHei UI", 10, "bold")
    F_LABEL = ("Microsoft YaHei UI", 8, "bold")
    F_SMALL = ("Microsoft YaHei UI", 8)
    F_TINY = ("Microsoft YaHei UI", 7)
    F_BIG = ("Consolas", 22, "bold")
    F_MID = ("Consolas", 12, "bold")
    F_NUM = ("Consolas", 9, "bold")
    F_TXT = ("Consolas", 8)

    def __init__(self, sampler):
        self.s = sampler
        self.cfg = self._load_cfg()
        self.interval = float(self.cfg.get("interval", 1.5))
        self.alpha = float(self.cfg.get("alpha", 0.97))
        self.mini = bool(self.cfg.get("mini", False))
        self.topmost = bool(self.cfg.get("topmost", True))
        self.cpu_hist = collections.deque(maxlen=48)
        # CPU die 热容极小，睿频爆发时 1 秒内可跳 20°C，默认做滑动平均再显示
        self.smooth = bool(self.cfg.get("smooth", True))
        self.sm_cpu = collections.deque(maxlen=4)
        self.sm_gpu = collections.deque(maxlen=4)
        self.fan_scale = 2000          # 没有标称上限时用来归一化风扇进度条
        # CPU 频率上限：以系统电源计划为准，读取失败时回退到配置里的值
        self.power = CpuPowerCtl()
        _cur = self.power.get_max()
        self.max_pct = _cur if _cur else int(self.cfg.get("max_pct", 100))
        self._toast_until = 0.0
        self._tick_timer = None        # 保证始终只有一条刷新链
        self._last_max_read = 0.0     # 右键菜单读取电源设置的节流时间戳
        self.updater = UpdateChecker(self.cfg, HERE)
        self.update_state = None       # ("new", 远端版本号) / None
        self._ui_queue = queue.Queue()  # 后台线程 → 主线程的安全通道
        # 先按上次运行的风扇数排版，采到数据后再校正，避免开面板时闪一下
        self.L = self._layout(self.cfg.get("fan_n", 2))

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-alpha", self.alpha)
        self.root.attributes("-topmost", self.topmost)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = int(self.cfg.get("x", sw - self.W - 28))
        y = int(self.cfg.get("y", 28))
        x = max(0, min(x, sw - self.W))
        y = max(0, min(y, sh - 120))
        self.root.geometry("%dx%d+%d+%d"
                           % (self.W, self.HMINI if self.mini else self.L["h"],
                              x, y))

        self.cv = tk.Canvas(self.root, width=self.W, height=self.L["h"],
                            bg=C["bg"], highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.it = {}
        self._build()
        self._bind()
        self._apply_round_corners()
        self._tick()
        atexit.register(self._save_cfg)
        if self.updater.auto and self.updater.repo:
            self.root.after(3500, lambda: self._bg_check(manual=False))

    # ---------- 布局 ----------
    def _layout(self, n):
        """按风扇数量算出各区纵向坐标。

        n=2 时结果与固定布局逐像素一致（风扇卡 210-268、系统卡 274-330、
        趋势区 348-368、总高 376），常见双风扇机型外观不变；
        1 个风扇时面板矮 16px，3 个以上时等高增高。
        """
        n = max(1, min(MAX_FANS, int(n or 1)))
        fan_bot = self.FAN_TOP + self.FAN_HEAD + n * self.FAN_ROW
        sys_top = fan_bot + 6
        sys_bot = sys_top + 56
        label_y = sys_bot + 10
        return {
            "n": n,
            "fan_top": self.FAN_TOP,
            "fan_bot": fan_bot,
            # 行自底向上排，保证卡片底部留白固定
            "fan_rows": [fan_bot - self.FAN_PAD - (n - 1 - i) * self.FAN_ROW
                         for i in range(n)],
            "sys_top": sys_top,
            "sys_bot": sys_bot,
            "spark_label": label_y,
            "spark_top": label_y + 8,
            "spark_bot": label_y + 28,
            "h": sys_bot + 46,
        }

    def _relayout(self, n):
        """风扇数量变化时重建画布并重设窗口高度（保持左上角位置不动）。"""
        n = max(1, min(MAX_FANS, int(n or 1)))
        if n == self.L["n"]:
            return
        self.L = self._layout(n)
        h = self.L["h"]
        try:
            self.cv.configure(height=h)
            self.root.geometry("%dx%d" % (self.W, h))
        except Exception:
            pass
        self._build()
        self._apply_round_corners()

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            d = {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                 "interval": self.interval, "alpha": self.alpha,
                 "mini": self.mini, "topmost": self.topmost,
                 "smooth": self.smooth, "max_pct": self.max_pct,
                 "repo": self.updater.repo, "branch": self.updater.branch,
                 "auto_check_update": self.updater.auto,
                 "fan_n": self.L["n"]}
            with open(CFG_PATH, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
        except Exception:
            pass

    # ---------- Win11 圆角窗口 ----------
    def _apply_round_corners(self):
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = wt.DWORD(2)   # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    # ---------- 绘图辅助 ----------
    def _round_rect(self, x0, y0, x1, y1, r=9, fill=None):
        """Canvas 没有原生圆角矩形，用四段圆弧折线近似。"""
        pts = []
        for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                           (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
            for i in range(6):
                a = math.radians(a0 + 90.0 * i / 5.0)
                pts.extend([cx + r * math.cos(a), cy + r * math.sin(a)])
        return self.cv.create_polygon(pts, fill=fill, outline="")

    def _rule(self, y):
        self.cv.create_line(self.P, y, self.W - self.P, y, fill=C["line"])

    def _track(self, x0, x1, y, width=3):
        """圆头轨道线，比矩形条更轻。"""
        return self.cv.create_line(x0, y, x1, y, fill=C["track"],
                                   width=width, capstyle="round")

    def _bar(self, key, x0, x1, y, color, width=3):
        self.it[key + "_bar"] = self.cv.create_line(
            x0, y, x0 + 1, y, fill=color, width=width, capstyle="round")

    def _text(self, x, y, value, anchor="w", fill=None, font=None):
        return self.cv.create_text(x, y, text=value, anchor=anchor,
                                   fill=fill or C["dim"], font=font or self.F_SMALL)

    # ---------- 构建静态图元 ----------
    def _build(self):
        cv = self.cv
        cv.delete("all")
        self.it.clear()
        P, W = self.P, self.W

        # 标题栏：状态点 / 标题 / 副标题 / 12 小时制时钟
        self.it["dot"] = cv.create_oval(P - 8, 12, P - 3, 17,
                                        fill=C["green"], outline="")
        self._text(P, 14, "TempPeek", fill=C["text"], font=self.F_TITLE)
        self.it["hdr_state"] = self._text(
            P + 92, 15, "", fill=C["faint"], font=self.F_TINY)
        self.it["clock"] = cv.create_text(
            W - P, 15, text="", anchor="e", fill=C["dim"], font=("Consolas", 9))
        self._rule(30)

        if self.mini:
            self._build_mini()
        else:
            self._build_full()

    def _build_full(self):
        cv, P, W = self.cv, self.P, self.W
        IX = P + 9                  # 卡片内左边距
        IR = W - P - 9              # 卡片内右边距

        # ── 处理器 ──
        self._round_rect(P, 36, W - P, 96, fill=C["card"])
        self._text(IX, 48, "处理器", fill=C["dim"], font=self.F_LABEL)
        self.it["cpu_state"] = self._text(IR, 48, "--", anchor="e",
                                          fill=C["faint"], font=self.F_SMALL)
        self.it["cpu_val"] = cv.create_text(
            IX, 74, text="--°C", anchor="w", fill=C["faint"], font=self.F_BIG)
        self.it["cpu_f"] = self._text(IR, 66, "", anchor="e",
                                      fill=C["text"], font=self.F_NUM)
        self.it["cpu_l"] = self._text(IR, 82, "", anchor="e",
                                      fill=C["dim"], font=self.F_SMALL)
        self._track(IX + 82, IR - 6, 93, 2)
        self._bar("cpu", IX + 82, IR - 6, 93, C["green"], 2)

        # ── 显卡 ──
        self._round_rect(P, 102, W - P, 162, fill=C["card"])
        self._text(IX, 114, "显卡", fill=C["dim"], font=self.F_LABEL)
        self.it["gpu_state"] = self._text(IR, 114, "--", anchor="e",
                                          fill=C["faint"], font=self.F_SMALL)
        self.it["gpu_val"] = cv.create_text(
            IX, 140, text="--°C", anchor="w", fill=C["faint"], font=self.F_BIG)
        self.it["gpu_u"] = self._text(IR, 132, "", anchor="e",
                                      fill=C["text"], font=self.F_NUM)
        self.it["gpu_m"] = self._text(IR, 148, "", anchor="e",
                                      fill=C["dim"], font=self.F_SMALL)
        self._track(IX + 82, IR - 6, 160, 2)
        self._bar("gpu", IX + 82, IR - 6, 160, C["green"], 2)

        # ── 主板 / 硬盘：双小卡并排 ──
        gap = 8
        w2 = (W - 2 * P - gap) / 2.0
        self._round_rect(P, 168, P + w2, 204, r=8, fill=C["card2"])
        self._round_rect(P + w2 + gap, 168, W - P, 204, r=8, fill=C["card2"])
        self._text(P + 9, 178, "主板", fill=C["dim"], font=self.F_TINY)
        self.it["mb_val"] = cv.create_text(
            P + 9, 194, text="--°C", anchor="w", fill=C["faint"], font=self.F_MID)
        self._text(P + w2 + gap + 9, 178, "硬盘", fill=C["dim"], font=self.F_TINY)
        self.it["ssd_val"] = cv.create_text(
            P + w2 + gap + 9, 194, text="--°C", anchor="w",
            fill=C["faint"], font=self.F_MID)

        # ── 散热风扇：行数 = 实际风扇数量（1-6），面板高度随之伸缩 ──
        L = self.L
        self._round_rect(P, L["fan_top"], W - P, L["fan_bot"], fill=C["card"])
        self._text(IX, L["fan_top"] + 12, "散热风扇", fill=C["dim"], font=self.F_LABEL)
        self.it["fan_max"] = self._text(IR, L["fan_top"] + 12, "", anchor="e",
                                        fill=C["faint"], font=self.F_TINY)
        self.fan_rows = []
        for i, y in enumerate(L["fan_rows"]):
            self._text(IX, y, "风扇 %d" % (i + 1), fill=C["dim"], font=self.F_SMALL)
            self._track(IX + 46, IR - 96, y)
            self._bar("fan%d" % i, IX + 46, IR - 96, y, C["blue"])
            self.it["fan%d_val" % i] = self._text(
                IR, y, "--", anchor="e", fill=C["faint"], font=self.F_NUM)
            self.fan_rows.append((self.it["fan%d_bar" % i],
                                  self.it["fan%d_val" % i]))

        # ── 系统：内存 / 网络 / 磁盘 ──
        self._round_rect(P, L["sys_top"], W - P, L["sys_bot"], fill=C["card"])
        for dy, label in ((13, "内存"), (31, "网络"), (47, "磁盘")):
            self._text(IX, L["sys_top"] + dy, label, fill=C["dim"], font=self.F_SMALL)
        self._track(IX + 34, IR - 132, L["sys_top"] + 13)
        self._bar("ram", IX + 34, IR - 132, L["sys_top"] + 13, C["cyan"])
        self.it["ram_val"] = self._text(IR, L["sys_top"] + 13, "", anchor="e",
                                        fill=C["text"], font=self.F_TXT)
        self.it["net_val"] = self._text(IR, L["sys_top"] + 31, "", anchor="e",
                                        fill=C["text"], font=self.F_TXT)
        self.it["disk_val"] = self._text(IR, L["sys_top"] + 47, "", anchor="e",
                                         fill=C["text"], font=self.F_TXT)

        # ── 处理器温度趋势 ──
        self._text(P, L["spark_label"], "处理器温度趋势",
                   fill=C["dim"], font=self.F_TINY)
        self.it["spark_hi"] = self._text(W - P, L["spark_label"], "", anchor="e",
                                         fill=C["faint"], font=self.F_TINY)
        self.it["spark_fill"] = cv.create_polygon(
            P, L["spark_bot"], W - P, L["spark_bot"], W - P, L["spark_bot"],
            fill="#1e3a46", outline="")
        self.it["spark"] = cv.create_line(
            P, L["spark_bot"] - 10, W - P, L["spark_bot"] - 10, fill=C["green"],
            width=1.8, smooth=True, capstyle="round")
        self.it["spark_dot"] = cv.create_oval(
            P - 2, L["spark_bot"] - 12, P + 2, L["spark_bot"] - 8,
            fill=C["green"], outline="")

    def _build_mini(self):
        P, W = self.P, self.W
        self._round_rect(P, 36, W - P, 74, r=8, fill=C["card2"])
        self._text(P + 9, 55, "处理器", fill=C["dim"], font=self.F_LABEL)
        self.it["cpu_val"] = self.cv.create_text(
            W - P - 9, 55, text="--°C", anchor="e", fill=C["faint"],
            font=("Consolas", 16, "bold"))
        self._round_rect(P, 78, W - P, 116, r=8, fill=C["card2"])
        self._text(P + 9, 97, "显卡", fill=C["dim"], font=self.F_LABEL)
        self.it["gpu_val"] = self.cv.create_text(
            W - P - 9, 97, text="--°C", anchor="e", fill=C["faint"],
            font=("Consolas", 16, "bold"))
        self.it["cpu_sub"] = self._text(P, 130, "", fill=C["dim"], font=self.F_TINY)
        self.it["fan_val"] = self._text(W - P, 130, "", anchor="e",
                                        fill=C["blue"], font=self.F_TINY)

    # ---------- 交互 ----------
    def _bind(self):
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<Button-3>", self._popup)
        self.cv.bind("<Double-Button-1>", lambda e: self.toggle_mini())
        self.root.bind("<Escape>", lambda e: self.quit())

    def _press(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def _popup(self, e):
        # 用户可能在别处改了电源计划；powercfg 子进程约 50~100ms，做节流
        now = time.time()
        if now - self._last_max_read > 5.0:
            cur = self.power.get_max()
            if cur:
                self.max_pct = cur
            self._last_max_read = now
        try:
            self.root.focus_force()
        except Exception:
            pass
        m = tk.Menu(self.root, tearoff=0, bg=C["panel"], fg=C["text"],
                    activebackground=C["blue"], activeforeground="#ffffff",
                    bd=0, relief="flat", font=self.F_SMALL)
        m.add_command(label="返回完整模式" if self.mini else "切换迷你模式",
                      command=self.toggle_mini)

        sub_i = tk.Menu(m, tearoff=0, bg=C["panel"], fg=C["text"],
                        activebackground=C["blue"], activeforeground="#ffffff",
                        bd=0, font=self.F_SMALL)
        for v in (1.0, 1.5, 2.0, 3.0, 5.0):
            mark = "● " if abs(v - self.interval) < 0.01 else "   "
            sub_i.add_command(label="%s%.1f 秒" % (mark, v),
                              command=lambda v=v: self.set_interval(v))
        m.add_cascade(label="刷新间隔", menu=sub_i)

        sub_a = tk.Menu(m, tearoff=0, bg=C["panel"], fg=C["text"],
                        activebackground=C["blue"], activeforeground="#ffffff",
                        bd=0, font=self.F_SMALL)
        for v in (0.99, 0.97, 0.94, 0.88, 0.80):
            mark = "● " if abs(v - self.alpha) < 0.01 else "   "
            sub_a.add_command(label="%s%d%%" % (mark, int(v * 100)),
                              command=lambda v=v: self.set_alpha(v))
        m.add_cascade(label="不透明度", menu=sub_a)

        m.add_command(label="取消置顶" if self.topmost else "窗口置顶",
                      command=self.toggle_topmost)
        m.add_command(label="温度平滑：开" if self.smooth else "温度平滑：关",
                      command=self.toggle_smooth)
        sub_c = tk.Menu(m, tearoff=0, bg=C["panel"], fg=C["text"],
                        activebackground=C["blue"], activeforeground="#ffffff",
                        bd=0, font=self.F_SMALL)
        for v in (100, 95, 90, 85, 80, 70, 60, 50):
            mark = "● " if v == self.max_pct else "   "
            tail = "  全速睿频" if v == 100 else ""
            sub_c.add_command(label=mark + str(v) + "%" + tail,
                              command=lambda v=v: self.set_max_pct(v))
        sub_c.add_separator()
        sub_c.add_command(label="自定义百分比…", command=self.custom_max_pct)
        sub_c.add_command(label="恢复默认（100%）", command=lambda: self.set_max_pct(100))
        m.add_cascade(label="CPU 频率上限 · " + str(self.max_pct) + "%", menu=sub_c)
        m.add_separator()
        sub_u = tk.Menu(m, tearoff=0, bg=C["panel"], fg=C["text"],
                        activebackground=C["blue"], activeforeground="#ffffff",
                        bd=0, font=self.F_SMALL)
        upd = "立即检查更新…"
        if self.update_state and self.update_state[0] == "new":
            upd = "发现新版本 " + self.update_state[1] + " · 立即更新"
        sub_u.add_command(label=upd, command=lambda: self._bg_check(manual=True))
        sub_u.add_command(
            label="自动检查：开" if self.updater.auto else "自动检查：关",
            command=self._toggle_auto_update)
        sub_u.add_separator()
        sub_u.add_command(label="设置更新源…", command=self._set_update_source)
        sub_u.add_command(label="重启面板（更新后生效）", command=self._restart)
        sub_u.add_command(label="关于 · 版本 " + APP_VER, command=self._about)
        m.add_cascade(label="版本更新" + ("  ● 有新版本" if self.update_state else ""),
                      menu=sub_u)
        m.add_separator()
        m.add_command(label="立即刷新", command=lambda: self._tick(force=True))
        m.add_command(label="左键拖动 · 双击缩放", state="disabled")
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
            self.root.after(200, m.destroy)

    def toggle_mini(self):
        self.mini = not self.mini
        h = self.HMINI if self.mini else self.L["h"]
        self.root.geometry("%dx%d" % (self.W, h))
        self.cv.configure(height=h)
        self._build()
        self._tick(force=True)

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)

    def toggle_smooth(self):
        """关闭后显示固件原始瞬时值（会随睿频剧烈跳动，用于观察尖峰）。"""
        self.smooth = not self.smooth
        self._tick(force=True)

    def set_max_pct(self, pct):
        """应用频率上限并给出界面反馈。"""
        ok, msg = self.power.set_max(pct)
        if ok:
            self.max_pct = int(pct)
            self._toast("频率上限 " + str(self.max_pct) + "%", C["green"])
        else:
            self._toast("设置失败：" + msg, C["red"])
            _now = self.power.get_max()
            if _now:
                self.max_pct = _now
        self._tick(force=True)

    def custom_max_pct(self):
        """弹出输入框，允许 50–100 之间的任意整数百分比。"""
        was_top = self.topmost
        if was_top:
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass
        try:
            v = simpledialog.askinteger(
                "CPU 频率上限",
                "输入 50–100 的百分比\n100 = 全速睿频，数值越低越凉快也越慢",
                parent=self.root, minvalue=CpuPowerCtl.MIN_PCT,
                maxvalue=CpuPowerCtl.MAX_PCT, initialvalue=self.max_pct)
        except Exception:
            v = None
        finally:
            if was_top:
                try:
                    self.root.attributes("-topmost", True)
                except Exception:
                    pass
        if v:
            self.set_max_pct(v)

    def _toast(self, msg, color):
        """借用标题栏状态文字短暂显示提示，2.6 秒后由 _apply 自动恢复。"""
        self._toast_until = time.time() + 2.6
        try:
            self.cv.itemconfigure(self.it["hdr_state"], text=msg, fill=color)
        except Exception:
            pass

    # ---------- 版本更新 ----------
    def _post(self, fn, *args):
        """后台线程调用：只投递，不碰 Tk。"""
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        """主线程调用：执行所有后台投递的回调。"""
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                pass

    def _bg_check(self, manual=False):
        """检查放后台线程，结果用 after(0) 回主线程，避免卡住界面。"""
        if manual:
            self._toast("正在检查更新…", C["blue"])

        def work():
            try:
                status, info = self.updater.check()
            except Exception as exc:
                status, info = "error", str(exc)
            self._post(self._on_check, status, info, manual)

        threading.Thread(target=work, daemon=True).start()

    def _on_check(self, status, info, manual):
        if status == "new":
            self.update_state = ("new", str(info))
            detail = self.updater.info or {}
            msg = ("当前版本 v%s\n最新版本 v%s\n\n%s\n\n"
                   "是否立即下载并更新？\n"
                   "更新前会备份为 tempeek.py.bak，完成后需重启面板。"
                   % (APP_VER, info, detail.get("changelog") or "无更新说明"))
            if messagebox.askyesno("发现新版本", msg, parent=self.root):
                self._do_update()
            else:
                self._toast("发现新版本 " + str(info), C["yellow"])
            return
        self.update_state = None
        if status == "same":
            self._toast("已是最新版本 v" + APP_VER, C["green"])
        elif status == "norepo":
            if manual:
                self._set_update_source()
            else:
                self._toast("未设置更新源", C["faint"])
        else:
            self._toast("检查失败：" + str(info)[:36], C["red"])

    def _do_update(self):
        self._toast("正在下载更新…", C["blue"])

        def work():
            try:
                target, bak = self.updater.download()
                self._post(self._on_updated, target, bak)
            except Exception as exc:
                self._post(self._toast, "更新失败：" + str(exc)[:36], C["red"])

        threading.Thread(target=work, daemon=True).start()

    def _on_updated(self, target, bak):
        self._toast("更新完成 · 请重启面板", C["green"])
        messagebox.showinfo(
            "更新完成",
            "已更新主程序：\n%s\n\n原文件已备份为：\n%s\n\n"
            "请点右键「重启面板」或重新双击启动器使新版本生效。"
            % (target, bak), parent=self.root)

    def _set_update_source(self):
        was_top = self.topmost
        if was_top:
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass
        try:
            v = simpledialog.askstring(
                "设置更新源",
                "输入 GitHub 仓库，格式 owner/repo\n"
                "仓库根目录需放 version.json\n"
                "当前：" + (self.updater.repo or "未设置"),
                parent=self.root, initialvalue=self.updater.repo)
        except Exception:
            v = None
        finally:
            if was_top:
                try:
                    self.root.attributes("-topmost", True)
                except Exception:
                    pass
        if v is None:
            return
        v = v.strip().strip("/")
        if v and not re.match(r"^[\w.\-]+/[\w.\-]+$", v):
            self._toast("格式应为 owner/repo", C["red"])
            return
        self.updater.repo = v
        self._toast("更新源已设为 " + (v or "未设置"), C["green"])
        if v:
            self._bg_check(manual=True)

    def _toggle_auto_update(self):
        self.updater.auto = not self.updater.auto
        self._toast("自动检查更新：" + ("开" if self.updater.auto else "关"),
                    C["green"] if self.updater.auto else C["dim"])

    def _restart(self):
        """execv 原地重启：保持提权令牌与互斥量语义，不产生第二个实例。"""
        self._save_cfg()
        try:
            os.execv(sys.executable,
                     [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        except Exception as exc:
            self._toast("重启失败：" + str(exc)[:30], C["red"])

    def _about(self):
        s = self.s
        fans = getattr(s, "_awcc", None)
        n_fan = len(getattr(fans, "fan_ids", []) or [])
        src = []
        if getattr(fans, "ok", False):
            src.append("AWCC WMI")
        if getattr(getattr(s, "_lhm", None), "ok", False):
            src.append("LibreHardwareMonitor")
        if getattr(getattr(s, "_nvs", None), "exe", None):
            src.append("nvidia-smi")
        src.append("psutil")
        messagebox.showinfo(
            "关于",
            "TempPeek  v%s\n\n更新源：%s\n自动检查：%s\n"
            "风扇通道：%s\n\n数据源：%s"
            % (APP_VER, self.updater.repo or "未设置",
               "开" if self.updater.auto else "关",
               ("%d 路" % n_fan) if n_fan else "未探到",
               " / ".join(src)),
            parent=self.root)

    def set_interval(self, v):
        self.interval = v
        self.s.interval = v

    def set_alpha(self, v):
        self.alpha = v
        self.root.attributes("-alpha", v)

    def quit(self):
        self._save_cfg()
        self.s.stop()
        timer = getattr(self, "_tick_timer", None)
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 刷新 ----------
    def _tick(self, force=False):
        self._drain_ui_queue()
        # toggle_mini / 立即刷新 / set_max_pct 都会调本方法；不去重会叠加
        # 定时器，点几次就多几条刷新链，刷新越来越频繁。
        timer = getattr(self, "_tick_timer", None)
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
        with self.s.lock:
            d = dict(self.s.data)
        try:
            self._apply(d)
        except Exception:
            pass
        self._tick_timer = self.root.after(
            int(self.interval * 1000), self._tick)

    def _apply(self, d):
        cv = self.cv
        cpu_raw = d.get("cpu_temp")
        gpu_raw = d.get("gpu_temp")
        if self.smooth:
            if cpu_raw:
                self.sm_cpu.append(cpu_raw)
            if gpu_raw:
                self.sm_gpu.append(gpu_raw)
            cpu_t = (sum(self.sm_cpu) / float(len(self.sm_cpu))) if self.sm_cpu else cpu_raw
            gpu_t = (sum(self.sm_gpu) / float(len(self.sm_gpu))) if self.sm_gpu else gpu_raw
        else:
            self.sm_cpu.clear()
            self.sm_gpu.clear()
            cpu_t, gpu_t = cpu_raw, gpu_raw

        # 公共部分（完整/迷你模式都存在）
        # 中文系统下 strftime("%p") 会输出"上午/下午"，这里手动拼成 12 小时制 AM/PM
        lt = time.localtime()
        ap = "AM" if lt.tm_hour < 12 else "PM"
        hh = lt.tm_hour % 12 or 12
        cv.itemconfigure(self.it["clock"],
                         text="%d:%02d %s" % (hh, lt.tm_min, ap))
        cv.itemconfigure(self.it["dot"],
                         fill=C["green"] if self.s.status == "OK" else C["yellow"])
        ok = self.s.status == "OK"
        if time.time() >= self._toast_until:
            if self.update_state and self.update_state[0] == "new":
                cv.itemconfigure(self.it["hdr_state"],
                                 text="有新版本 " + self.update_state[1],
                                 fill=C["blue"])
            else:
                cv.itemconfigure(self.it["hdr_state"],
                                 text="运行中" if ok else "部分数据不可用",
                                 fill=C["green"] if ok else C["yellow"])
        cv.itemconfigure(self.it["cpu_val"],
                         text=("%.0f°C" % cpu_t) if cpu_t else "--°C",
                         fill=temp_color(cpu_t))
        cv.itemconfigure(self.it["gpu_val"],
                         text=("%.0f°C" % gpu_t) if gpu_t else "--°C",
                         fill=temp_color(gpu_t))

        fans = list(d.get("fans") or [])
        # 首次采到风扇数、或数量跟上次运行不同（换机、外接散热底座等）时重排
        if fans and not self.mini and len(fans) != self.L["n"]:
            self._relayout(len(fans))

        if self.mini:
            f = d.get("cpu_freq")
            parts = []
            if f:
                parts.append("%.2f GHz" % (f / 1000.0))
            if d.get("cpu_load") is not None:
                parts.append("占用 %.0f%%" % d["cpu_load"])
            cv.itemconfigure(self.it["cpu_sub"], text=" · ".join(parts))
            # 迷你模式只有一行放风扇，多于两个时只报数量与最高转速
            vals = [v for v in fans if v is not None]
            if not vals:
                txt, col = "风扇 --", C["faint"]
            elif len(vals) == 1:
                txt, col = "风扇 %d 转/分" % vals[0], C["blue"]
            elif len(vals) == 2:
                txt, col = "风扇 %d / %d" % (vals[0], vals[1]), C["blue"]
            else:
                txt, col = ("风扇 %d 个 · 最高 %d" % (len(vals), max(vals)),
                            C["blue"])
            cv.itemconfigure(self.it["fan_val"], text=txt, fill=col)
            return

        IX, IR = self.P + 9, self.W - self.P - 9

        # 处理器
        st, col = temp_state(cpu_t)
        badge = st if self.max_pct >= 100 else st + " · 限" + str(self.max_pct) + "%"
        cv.itemconfigure(self.it["cpu_state"], text=badge, fill=col)
        self._fill_bar("cpu", (cpu_t - 20) / 80.0 if cpu_t else 0, col, IX + 82, IR - 6)
        f = d.get("cpu_freq")
        cv.itemconfigure(self.it["cpu_f"],
                         text=("%.2f GHz" % (f / 1000.0)) if f else "-- GHz")
        cv.itemconfigure(self.it["cpu_l"],
                         text=("占用 %.0f%%" % d["cpu_load"])
                         if d.get("cpu_load") is not None else "占用 --")

        # 显卡
        st, col = temp_state(gpu_t)
        cv.itemconfigure(self.it["gpu_state"], text=st, fill=col)
        self._fill_bar("gpu", (gpu_t - 20) / 80.0 if gpu_t else 0, col, IX + 82, IR - 6)
        cv.itemconfigure(self.it["gpu_u"],
                         text=("占用 %d%%" % d["gpu_util"])
                         if d.get("gpu_util") is not None else "占用 --")
        gm = d.get("gpu_mem")
        cv.itemconfigure(self.it["gpu_m"],
                         text=("显存 %d/%dM" % gm) if gm else "显存 --")

        # 主板 / 硬盘
        mb = d.get("board_temp")
        cv.itemconfigure(self.it["mb_val"],
                         text=("%.0f°C" % mb) if mb else "--°C",
                         fill=temp_color(mb))
        disks = d.get("disks") or []
        ssd = max((t for _, t in disks), default=None)
        cv.itemconfigure(self.it["ssd_val"],
                         text=("%.0f°C" % ssd) if ssd else "--°C",
                         fill=temp_color(ssd))

        # 风扇：行数由布局决定，逐行对应 fans 里的读数，缺位的行显示 "--"
        fmax = d.get("fan_max")
        cv.itemconfigure(self.it["fan_max"],
                         text=("上限 %d 转/分" % fmax) if fmax else "")
        peak = max([v for v in fans if v] or [0])
        self.fan_scale = max(self.fan_scale, peak)
        scale = float(fmax or self.fan_scale)
        for i, (_, val) in enumerate(self.fan_rows):
            rpm = fans[i] if i < len(fans) else None
            cv.itemconfigure(val,
                             text=("%d 转/分" % rpm) if rpm is not None
                             else "-- 转/分",
                             fill=C["blue"] if rpm is not None else C["faint"])
            self._fill_bar("fan%d" % i, (rpm / scale) if rpm else 0,
                           C["blue"], IX + 46, IR - 96)

        # 系统
        mp = d.get("mem_pct") or 0
        cv.itemconfigure(self.it["ram_val"],
                         text="%d%%  %s/%s" % (mp, fmt_gb(d.get("mem_used")),
                                               fmt_gb(d.get("mem_total"))))
        self._fill_bar("ram", mp / 100.0, C["cyan"], IX + 34, IR - 132)
        cv.itemconfigure(self.it["net_val"],
                         text="↓%s/s  ↑%s/s" % (fmt_rate(d.get("net_down")),
                                                fmt_rate(d.get("net_up"))))
        cv.itemconfigure(self.it["disk_val"],
                         text="读 %s/s  写 %s/s" % (fmt_rate(d.get("disk_read")),
                                                    fmt_rate(d.get("disk_write"))))

        # 趋势
        if cpu_t:
            self.cpu_hist.append(cpu_t)
        self._draw_spark()

    def _fill_bar(self, key, pct, color, x0, x1):
        item = self.it.get(key + "_bar")
        if not item:
            return
        pct = max(0.0, min(1.0, float(pct or 0)))
        if pct <= 0.001:
            # 长度为 0 的圆头线仍会画出一个小圆点（降级/无数据时像噪点）
            self.cv.itemconfigure(item, state="hidden")
            return
        self.cv.itemconfigure(item, state="normal")
        coords = self.cv.coords(item)
        if len(coords) != 4:
            return
        _, y0, _, y1 = coords
        self.cv.coords(item, x0, y0, x0 + (x1 - x0) * pct, y1)
        self.cv.itemconfigure(item, fill=color)

    def _draw_spark(self):
        vals = list(self.cpu_hist)
        if len(vals) < 2:
            # 样本不足时不画那条初始水平直线，避免看起来像"温度恒定"
            for key in ("spark", "spark_fill", "spark_dot"):
                item = self.it.get(key)
                if item:
                    self.cv.itemconfigure(item, state="hidden")
            if self.it.get("spark_hi"):
                self.cv.itemconfigure(self.it["spark_hi"],
                                     text="采集中" if vals else "暂无数据",
                                     fill=C["faint"])
            return
        for key in ("spark", "spark_fill", "spark_dot"):
            item = self.it.get(key)
            if item:
                self.cv.itemconfigure(item, state="normal")
        W, P = self.W, self.P
        x0, x1 = P, W - P
        top, bot = self.L["spark_top"], self.L["spark_bot"]
        lo, hi = min(vals) - 2.0, max(vals) + 2.0
        if hi - lo < 8.0:
            mid = (lo + hi) / 2.0
            lo, hi = mid - 4.0, mid + 4.0
        span = float(max(1, len(vals) - 1))
        pts = []
        for i, t in enumerate(vals):
            x = x0 + (x1 - x0) * (i / span)
            ratio = (max(lo, min(hi, t)) - lo) / (hi - lo)
            y = bot - ratio * (bot - top)
            pts.extend([x, y])
        if len(pts) >= 4:
            col = temp_color(vals[-1])
            self.cv.coords(self.it["spark"], *pts)
            self.cv.itemconfigure(self.it["spark"], fill=col)
            poly = list(pts) + [pts[-2], bot, pts[0], bot]
            self.cv.coords(self.it["spark_fill"], *poly)
            lx, ly = pts[-2], pts[-1]
            self.cv.coords(self.it["spark_dot"],
                           lx - 2.5, ly - 2.5, lx + 2.5, ly + 2.5)
            self.cv.itemconfigure(self.it["spark_dot"], fill=col)
        self.cv.itemconfigure(self.it["spark_hi"],
                              text="最高 %.0f°C" % max(vals), fill=C["dim"])


def main():
    # 开启 DPI 感知：否则在 125%/150% 缩放下窗口会被系统位图拉伸而变模糊
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # 非管理员则静默提权重启；失败也继续（降级模式）
    if not is_admin() and "--no-elevate" not in sys.argv:
        if relaunch_elevated():
            return 0

    # 单实例：提权之后再抢互斥量，避免父进程与提权子进程互相顶掉
    if not acquire_single_instance():
        return 0

    sampler = Sampler(interval=1.5)
    sampler.start()

    panel = Panel(sampler)
    try:
        panel.root.mainloop()
    except KeyboardInterrupt:
        panel.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
