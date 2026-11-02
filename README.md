# 硬件监控面板 (HWPanel)

一个 Windows 桌面悬浮硬件监控面板：常驻置顶、可拖动、只读、不联网。

![platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-blue)
![release](https://img.shields.io/badge/release-v2.1-brightgreen)
![downloads](https://img.shields.io/github/downloads/XiaoShips/hwpanel/total)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.11-yellow)

> 下载：[Releases](https://github.com/XiaoShips/hwpanel/releases) 里的 `硬件监控面板_安装包_v2.1.exe`（约 16 MB）

| 完整模式 | 迷你模式 |
| :-: | :-: |
| ![完整模式](docs/screenshot-full.png) | ![迷你模式](docs/screenshot-mini.png) |

面板上的数字全部来自本机硬件实时读数，不是演示数据。

## 功能

- **处理器**：温度、实时频率、占用率
- **显卡**：温度、占用率、显存已用 / 总量
- **主板与硬盘**：Super I/O 主板温度、NVMe/SATA 硬盘 SMART 温度
- **散热风扇**：只列出真正接了线的转速接口，未接线的空接口不会伪装成“风扇 2”
- **系统**：内存占用、网络上下行、磁盘读写速率
- **趋势**：处理器温度曲线，带最高温标记

## 使用方法

- **左键拖动**移动面板，位置会自动记住
- **双击**在完整模式和迷你模式之间切换
- **右键**打开菜单：刷新间隔、透明度、置顶、平滑刷新、退出
- **`Esc`** 退出
- 单实例：重复启动只会把已有面板提到前台
- 偏好保存在 `%USERPROFILE%\.hwpanel.json`，删掉即恢复默认

## 驱动与管理员权限

CPU 温度和风扇转速不在 Windows 的标准接口里，需要读取 MSR 与主板 Super I/O，因此：

1. 安装时勾选 **“安装 PawnIO 传感器驱动”**。PawnIO 是 namazso 签名的开源内核驱动，可在“设置 → 应用”里单独卸载。
2. 启动时允许管理员权限。

没有驱动或被拒绝提权时，面板仍能显示频率、占用、显存、内存、网络、磁盘这些系统级数据，温度与风扇一栏显示 `--`，状态栏提示“部分数据不可用”。

## 数据来源与优先级

| 项目 | 来源 | 备注 |
| --- | --- | --- |
| CPU 温度 / 风扇转速 / 主板温度 | LibreHardwareMonitorLib + PawnIO | 需管理员；无驱动则显示 `--` |
| 硬盘温度 | LHM，缺失时用 WMI SMART 兜底 | 兜底路径有限流，约 45 秒一次 |
| 戴尔机型温度 / 风扇 | AWCC WMI（`root\WMI`） | 存在时优先于 LHM |
| GPU 温度 / 占用 / 显存 | `nvidia-smi`，温度回退到 LHM | AMD / Intel 显卡只有温度 |
| CPU 频率 / 内存 / 网络 / 磁盘 IO | psutil + Windows 性能计数器（PDH） | |

## 隐私与安全

- 只读取传感器数据，**不写**风扇曲线、不改变压、不切换散热档位，退出时不复位任何硬件状态。
- **不联网**：整个程序没有任何网络请求，也不上传遥测数据。
- 唯一涉及系统改动的是可选的 PawnIO 驱动安装，由官方安装器完成，可单独卸载。

## 已知限制

- 需要 Windows 10 / 11 64 位与管理员权限。
- 核显机器没有独立显卡温度；无风扇设备没有转速读数——属于硬件本身不上报。
- 风扇一栏的数量取决于主板实际接线的转速接口；真风扇停转时仍显示 `0 转/分`。
- 安装包未购买代码签名，SmartScreen 可能提示“未知发布者”，点“更多信息 → 仍要运行”即可；部分杀毒软件会对 PyInstaller 打的包误报。
- 在 Dell G3 3590（i7-9750H + GTX 1660 Ti Max-Q）上实测适配，其他机型按上表逐级降级。

## 常见问题

**状态显示“部分数据不可用”** —— 多为未装 PawnIO 或拒绝了管理员权限，重新运行安装包勾选驱动，或以管理员身份启动。

**显卡占用 / 显存显示 `--`** —— 这两项来自 NVIDIA 驱动自带的 `nvidia-smi`，AMD / Intel 显卡只能显示温度。

**只列出一个风扇** —— 主板 Super I/O 上未接线的转速输入口恒为 0，面板不会把它们当成额外的风扇显示；看到几个风扇，就是几个真正在转的风扇。

**升级安装时提示面板正在运行** —— 先在面板右键菜单选“退出”，再运行新安装包。

## 从源码运行

```bash
# Python 3.11（自带 tkinter）
pip install -r requirements.txt
python hwpanel.py
```

`tools/lhm/` 里已附带编译好的 `sensors.exe`（对 LibreHardwareMonitorLib 的薄封装，源码 `sensors.cs` 同目录）。想自己编译：

```powershell
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo `
  "-reference:tools\lhm\LibreHardwareMonitorLib.dll" `
  "-out:tools\lhm\sensors.exe" "tools\lhm\sensors.cs"
```

## 打包与发布

```bat
packaging\build.bat
```

依次做三件事：生成图标 → PyInstaller onedir 打成 `dist\HWPanel\` → Inno Setup 编译 `packaging\installer.iss` 输出到 `release\`。

构建安装包前需要把 [PawnIO 2.2.0](https://github.com/namazso/PawnIO/releases) 的 `PawnIO_setup.exe` 放到 `packaging\redist\`（该驱动不在本仓库里再分发）。

## 版本历史

见 [CHANGELOG.md](CHANGELOG.md)，安装包随各 [Release](https://github.com/XiaoShips/hwpanel/releases) 提供。

## 第三方组件

二进制分发涉及的 LibreHardwareMonitorLib、HidSharp、PawnIO、Python / Tcl-Tk / psutil / pywin32 等的许可证与源码地址见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 致谢

- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) —— 传感器读取
- [PawnIO](https://github.com/namazso/PawnIO) —— 内核驱动
- [TrafficMonitor](https://github.com/zhongyang219/TrafficMonitor)、[LiteMonitor](https://github.com/Diorser/LiteMonitor) —— 桌面悬浮窗交互上的参考

## 声明

本仓库的 Releases 是该面板**唯一**的官方分发渠道，没有官网、网盘或第三方下载站。本项目与 LibreHardwareMonitor、PawnIO、Dell 均无隶属关系。安全相关问题请按 [SECURITY.md](SECURITY.md) 联系。

## License

[MIT](LICENSE)（本项目自身代码）。随包分发的第三方组件仍受各自许可证约束。
