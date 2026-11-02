# -*- coding: utf-8 -*-
"""
hwpanel.py —— Windows 桌面硬件监控悬浮面板

针对本机 Dell G3 3590 (i7-9750H + GTX 1660 Ti Max-Q) 实测适配，其他机型可降级运行。

数据源与权限
──────────────────────────────────────────────────────────────
CPU/GPU 温度、风扇转速      LibreHardwareMonitor + PawnIO（通用后端，需管理员）
                            tools/lhm/sensors.exe 每秒输出一行 JSON；PawnIO 未装则自动跳过
戴尔机型温度/风扇           AWCC WMI（Thermal_Information），存在时优先级高于 LHM
GPU 占用率 / 显存 / 温度    nvidia-smi（无需管理员）
NVMe 硬盘温度               LHM；没有 LHM 时回退 Get-StorageReliabilityCounter
主板温度                    LHM Super I/O；回退 ACPI 热区
CPU/内存/网络/磁盘速率      psutil（无需管理员）

启动时若非管理员，会用 UAC 静默提权重启自身（本机 ConsentPromptBehaviorAdmin=0）。
提权失败也能跑，只是温度、风扇两栏显示 "--"。
PawnIO 安装包：https://pawnio.eu（签名驱动，可卸载）。

交互
──────────────────────────────────────────────────────────────
左键拖动    移动面板
右键        菜单：迷你模式 / 刷新间隔 / 透明度 / 置顶 / 退出
双击        切换迷你模式

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
import subprocess
import sys
import threading
import time
import tkinter as tk

APP_NAME = "硬件监控面板"
APP_VER = "2.1"
CFG_PATH = os.path.join(os.path.expanduser("~"), ".hwpanel.json")
CREATE_NO_WINDOW = 0x08000000


def _app_root():
    """源码运行时是脚本目录；PyInstaller 打包后是资源解包目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _app_root()

# ─────────────────────────── 配色（GitHub Dark） ───────────────────────────
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
        return "无数据", C["faint"]
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
    extra = " ".join('"%s"' % a if " " in a else a for a in args)
    if getattr(sys, "frozen", False):
        # 冻结版的 lpFile 就是 exe 自身，参数中不能再重复一次路径
        params = (extra + " --elevated").strip()
    else:
        params = '"%s"' % os.path.abspath(__file__)
        if extra:
            params += " " + extra
        params += " --elevated"
    try:
        # nShow=0（SW_HIDE）：venv 里的 pythonw.exe 其实是控制台子系统，
        # 用 1 会闪出一个黑框；隐藏后进程照常运行，Tk 窗口自己会显示
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", windowless_exe(), params, HERE, 0)
        return rc > 32
    except Exception:
        return False


# ────────────────────── 单实例：双击两次只开一个面板 ──────────────────────
_MUTEX_NAME = "HWPanel_SingleInstance_v1"
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
    """戴尔 AWCC WMI：CPU/GPU 温度 + 双风扇转速。需管理员，且必须在本线程创建 COM。"""

    OP_INFO, OP_RPM, OP_MAX = 0x04, 0x05, 0x09
    SENSOR_CPU, SENSOR_GPU = 0x101, 0x106
    FAN_IDS = (0x32, 0x33)

    def __init__(self):
        self.ok = False
        self.err = ""
        self.fan_max = 0
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
            self.fan_max = self._ti(self.OP_MAX, self.FAN_IDS[0]) or 5600
            self.ok = True
        except Exception as exc:
            self.err = str(exc)

    def _ti(self, op, idx=0, arg=0):
        params = self._obj.Methods_("Thermal_Information").InParameters.SpawnInstance_()
        params.arg2 = (op | (idx << 8) | (arg << 16)) & 0xFFFFFFFF
        out = self._obj.ExecMethod_("Thermal_Information", params)
        return int(getattr(out, "argr", -1)) & 0xFFFFFFFF

    def read(self, full=True):
        if not self.ok:
            return {}
        d = {}
        try:
            cpu = self._ti(self.OP_INFO, self.SENSOR_CPU)
            d["cpu_temp"] = cpu if 0 < cpu < 120 else None
            gpu = self._ti(self.OP_INFO, self.SENSOR_GPU)
            d["gpu_temp"] = gpu if 0 < gpu < 120 else None
            for i, fid in enumerate(self.FAN_IDS):
                rpm = self._ti(self.OP_RPM, fid)
                d["fan%d" % (i + 1)] = rpm if 0 <= rpm < 20000 else None
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
            out = {"gpu_temp": int(float(parts[1])) if parts[1] not in ("[N/A]", "") else None,
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
            pythoncom.CoInitialize()
            self._svc = open_root_wmi()
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

        while not self._stop.is_set():
            t0 = time.time()
            d = {}
            try:
                d.update(self._sys.read())
            except Exception:
                pass
            try:
                if self._awcc and self._awcc.ok:
                    d.update(self._awcc.read())
                    d["fan_max"] = self._awcc.fan_max
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
            # 硬盘温度：有 LHM 就不用再跑 PowerShell；SMART 有限流，低频兜底
            if not lhm.get("disks") and now - self._last_disk >= 45.0:
                try:
                    rows = self._disk.read()
                    if rows:
                        d["disks"] = rows
                    self._last_disk = now
                except Exception:
                    pass

            self._merge_lhm(d, lhm)
            self._update_status(d)

            with self.lock:
                merged = dict(self.data)
                merged.update(d)
                self.data = merged

            elapsed = time.time() - t0
            self._stop.wait(max(0.2, self.interval - elapsed))

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
        if d.get("fan1") is None and d.get("fan2") is None:
            self._pick_lhm_fans(d, lhm.get("fans") or [])
        if not d.get("disks"):
            disks = [(n, int(round(t))) for n, t in (lhm.get("disks") or [])]
            if disks:
                d["disks"] = disks

    def _pick_lhm_fans(self, d, fans):
        """主板 Super I/O 的转速输入口多数没接线，恒为 0。
        只保留当前有转速或本次运行中转过的接口，避免把空接口当成第二个风扇。"""
        picked = []
        for name, value in fans:
            rpm = int(round(value))
            if rpm > 0:
                self._fan_seen.add(name)
            elif name not in self._fan_seen:
                continue
            picked.append(rpm)
        if picked:
            d["fan1"] = picked[0]
            d["fan2"] = picked[1] if len(picked) > 1 else None

    def _update_status(self, d):
        missing = []
        if not d.get("cpu_temp"):
            missing.append("CPU 温度")
        if not d.get("gpu_temp"):
            missing.append("GPU 温度")
        if d.get("fan1") is None and d.get("fan2") is None:
            missing.append("风扇")
        # CPU 温度需要驱动与管理员权限，是核心项；核显机器没有 GPU 温度、
        # 无风扇机器没有转速都属于硬件本身不上报，不应误报“部分数据不可用”。
        self.status = "OK" if d.get("cpu_temp") else "部分数据不可用"
        self.status_detail = ("缺少 " + "、".join(missing)) if missing else ""


# ─────────────────────────── 悬浮窗 ───────────────────────────
class Panel:
    """悬浮面板：卡片分区式布局，Canvas 静态图元 + itemconfigure 增量更新。"""
    W = 296
    H = 376
    HMINI = 152
    P = 13                      # 左右外边距

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
        self.fan_scale = 2000

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
        # 多显示器下按虚拟桌面范围收敛，避免窗口被拉回主屏
        try:
            u = ctypes.windll.user32
            vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
            if vw <= 0 or vh <= 0:
                raise ValueError
            x = max(vx, min(x, vx + vw - self.W))
            y = max(vy, min(y, vy + vh - 120))
        except Exception:
            x = max(0, min(x, sw - self.W))
            y = max(0, min(y, sh - 120))
        self.root.geometry("%dx%d+%d+%d" % (self.W, self.HMINI if self.mini else self.H, x, y))

        self.cv = tk.Canvas(self.root, width=self.W, height=self.H,
                            bg=C["bg"], highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.it = {}
        self._build()
        self._bind()
        self._apply_round_corners()
        self._tick()
        atexit.register(self._save_cfg)

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
                 "smooth": self.smooth}
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

    def _track(self, x0, x1, y):
        """圆头轨道线，比矩形条更轻。"""
        return self.cv.create_line(x0, y, x1, y, fill=C["track"],
                                   width=3, capstyle="round")

    def _bar(self, key, x0, x1, y, color):
        self.it[key + "_bar"] = self.cv.create_line(
            x0, y, x0 + 1, y, fill=color, width=3, capstyle="round")

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
        self._text(P, 14, "硬件监控", fill=C["text"], font=self.F_TITLE)
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
        self._track(IX, IR, 88)
        self._bar("cpu", IX, IR, 88, C["green"])

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
        self._track(IX, IR, 154)
        self._bar("gpu", IX, IR, 154, C["green"])

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

        # ── 散热风扇 ──
        self._round_rect(P, 210, W - P, 268, fill=C["card"])
        self._text(IX, 222, "散热风扇", fill=C["dim"], font=self.F_LABEL)
        self.it["fan_max"] = self._text(IR, 222, "", anchor="e",
                                        fill=C["faint"], font=self.F_TINY)
        self.fan_rows = {}
        for key, y, label in (("fan1", 242, "风扇 1"), ("fan2", 258, "风扇 2")):
            row = [self._text(IX, y, label, fill=C["dim"], font=self.F_SMALL),
                   self._track(IX + 46, IR - 66, y)]
            self._bar(key, IX + 46, IR - 66, y, C["blue"])
            self.it[key + "_val"] = self._text(
                IR, y, "--", anchor="e", fill=C["faint"], font=self.F_NUM)
            self.fan_rows[key] = row + [self.it[key + "_bar"],
                                        self.it[key + "_val"]]

        # ── 系统：内存 / 网络 / 磁盘 ──
        self._round_rect(P, 274, W - P, 330, fill=C["card"])
        self._text(IX, 287, "内存", fill=C["dim"], font=self.F_SMALL)
        self._track(IX + 34, IR - 92, 287)
        self._bar("ram", IX + 34, IR - 92, 287, C["cyan"])
        self.it["ram_val"] = self._text(IR, 287, "", anchor="e",
                                        fill=C["text"], font=self.F_TXT)
        self._text(IX, 305, "网络", fill=C["dim"], font=self.F_SMALL)
        self.it["net_val"] = self._text(IR, 305, "", anchor="e",
                                        fill=C["text"], font=self.F_TXT)
        self._text(IX, 321, "磁盘", fill=C["dim"], font=self.F_SMALL)
        self.it["disk_val"] = self._text(IR, 321, "", anchor="e",
                                         fill=C["text"], font=self.F_TXT)

        # ── 处理器温度趋势 ──
        self._text(P, 336, "处理器温度趋势", fill=C["dim"], font=self.F_TINY)
        self.it["spark_hi"] = self._text(W - P, 336, "", anchor="e",
                                         fill=C["faint"], font=self.F_TINY)
        self.it["spark_fill"] = cv.create_polygon(
            P, 368, W - P, 368, W - P, 368, fill="#1e3a46", outline="")
        self.it["spark"] = cv.create_line(
            P, 358, W - P, 358, fill=C["green"], width=1.8,
            smooth=True, capstyle="round")
        self.it["spark_dot"] = cv.create_oval(
            P - 2, 356, P + 2, 360, fill=C["green"], outline="")

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
        h = self.HMINI if self.mini else self.H
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

    def set_interval(self, v):
        self.interval = v
        self.s.interval = v

    def set_alpha(self, v):
        self.alpha = v
        self.root.attributes("-alpha", v)

    def quit(self):
        self._save_cfg()
        self.s.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 刷新 ----------
    def _tick(self, force=False):
        with self.s.lock:
            d = dict(self.s.data)
        try:
            self._apply(d)
        except Exception:
            pass
        self.root.after(int(self.interval * 1000), self._tick)

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
        cv.itemconfigure(self.it["hdr_state"],
                         text="运行中" if ok else "部分数据不可用",
                         fill=C["green"] if ok else C["yellow"])
        cv.itemconfigure(self.it["cpu_val"],
                         text=("%.0f°C" % cpu_t) if cpu_t else "--°C",
                         fill=temp_color(cpu_t))
        cv.itemconfigure(self.it["gpu_val"],
                         text=("%.0f°C" % gpu_t) if gpu_t else "--°C",
                         fill=temp_color(gpu_t))

        if self.mini:
            f = d.get("cpu_freq")
            parts = []
            if f:
                parts.append("%.2f GHz" % (f / 1000.0))
            if d.get("cpu_load") is not None:
                parts.append("占用 %.0f%%" % d["cpu_load"])
            cv.itemconfigure(self.it["cpu_sub"], text=" · ".join(parts))
            f1, f2 = d.get("fan1"), d.get("fan2")
            if f1 is not None and f2 is None:
                txt = "风扇 %d 转/分" % f1
            elif f1 is not None or f2 is not None:
                txt = "风扇 %s / %s 转/分" % (
                    ("%d" % f1) if f1 is not None else "--",
                    ("%d" % f2) if f2 is not None else "--")
            else:
                txt = "风扇 --"
            cv.itemconfigure(self.it["fan_val"], text=txt,
                             fill=C["blue"] if (f1 is not None or f2 is not None) else C["faint"])
            return

        IX, IR = self.P + 9, self.W - self.P - 9

        # 处理器
        st, col = temp_state(cpu_t)
        cv.itemconfigure(self.it["cpu_state"], text=st, fill=col)
        self._fill_bar("cpu", (cpu_t - 20) / 80.0 if cpu_t else 0, col, IX, IR)
        f = d.get("cpu_freq")
        cv.itemconfigure(self.it["cpu_f"],
                         text=("%.2f GHz" % (f / 1000.0)) if f else "-- GHz")
        cv.itemconfigure(self.it["cpu_l"],
                         text=("占用 %.0f%%" % d["cpu_load"])
                         if d.get("cpu_load") is not None else "占用 --")

        # 显卡
        st, col = temp_state(gpu_t)
        cv.itemconfigure(self.it["gpu_state"], text=st, fill=col)
        self._fill_bar("gpu", (gpu_t - 20) / 80.0 if gpu_t else 0, col, IX, IR)
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

        # 风扇
        fmax = d.get("fan_max")
        if fmax:
            cv.itemconfigure(self.it["fan_max"], text="上限 %d 转/分" % fmax)
        peak = max((d.get("fan1") or 0), (d.get("fan2") or 0))
        self.fan_scale = max(self.fan_scale, peak)
        single = d.get("fan1") is not None and d.get("fan2") is None
        for key in ("fan1", "fan2"):
            rpm = d.get(key)
            # 只有一个真风扇时不显示空出来的第二行；两行都无数据仍保留 "--"
            state = "hidden" if (single and key == "fan2") else "normal"
            for iid in self.fan_rows[key]:
                cv.itemconfigure(iid, state=state)
            cv.itemconfigure(self.it[key + "_val"],
                             text=("%d 转/分" % rpm) if rpm is not None else "-- 转/分",
                             fill=C["blue"] if rpm is not None else C["faint"])
            scale = float(fmax or self.fan_scale)
            self._fill_bar(key, (rpm / scale) if rpm else 0,
                           C["blue"], IX + 46, IR - 66)

        # 系统
        mp = d.get("mem_pct") or 0
        cv.itemconfigure(self.it["ram_val"],
                         text="%d%%  %s/%s" % (mp, fmt_gb(d.get("mem_used")),
                                               fmt_gb(d.get("mem_total"))))
        self._fill_bar("ram", mp / 100.0, C["cyan"], IX + 34, IR - 92)
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
        coords = self.cv.coords(item)
        if len(coords) != 4:
            return
        _, y0, _, y1 = coords
        pct = max(0.0, min(1.0, float(pct or 0)))
        self.cv.coords(item, x0, y0, x0 + (x1 - x0) * pct, y1)
        self.cv.itemconfigure(item, fill=color)

    def _draw_spark(self):
        if not self.cpu_hist:
            return
        W, P = self.W, self.P
        x0, x1 = P, W - P
        top, bot = 344, 368
        vals = list(self.cpu_hist)
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
