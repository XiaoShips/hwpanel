# 第三方组件与许可证

本仓库自身代码按 [MIT](LICENSE) 授权。以下第三方组件随仓库或安装包分发，仍受各自许可证约束。许可证与上游地址均已按各仓库的 `LICENSE` 文件核对。

## 仓库内二进制（`tools/lhm/`）

| 组件 | 版本 | 许可证 | 上游 |
| --- | --- | --- | --- |
| LibreHardwareMonitorLib | 0.9.6 | MPL-2.0 | https://github.com/LibreHardwareMonitor/LibreHardwareMonitor |
| HidSharp | 2.6.4 | Apache-2.0 | https://github.com/SeekHisKingdom/HIDSharp |
| BlackSharp.Core | 1.0.7 | MPL-2.0 | https://github.com/Blacktempel/BlackSharp |
| DiskInfoToolkit | 1.1.2 | MPL-2.0 | https://github.com/Blacktempel/DiskInfoToolkit |
| RAMSPDToolkit-NDD | 1.4.2 | MPL-2.0 | https://github.com/Blacktempel/RAMSPDToolkit |
| Microsoft.Bcl.HashCode、`System.*` 运行时库 | — | MIT | https://github.com/dotnet/runtime |
| PawnIO 传感器模块（内嵌在 `LibreHardwareMonitorLib.dll` 的 `Resources/PawnIo/*.bin`） | — | LGPL-2.1 | 见上游 [THIRD-PARTY-NOTICES.txt](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/master/THIRD-PARTY-NOTICES.txt) |
| `sensors.exe` | — | 本项目的封装层，源码 [`tools/lhm/sensors.cs`](tools/lhm/sensors.cs) | — |

这些 DLL 保持上游原样、未作修改，对应源码即上表链接中的公开源码（MPL-2.0 第 3.2/3.3 条：以链接上游仓库与精确版本号的方式提供源码获取途径；本项目自身代码按 MIT 授权，MPL 文件仍归 MPL）。`sensors.exe` 作为独立进程运行，只通过 stdout 的 JSON 与本项目的 Python 代码交换数据。

## 安装包内（不放进仓库）

| 组件 | 版本 | 许可证 | 上游 |
| --- | --- | --- | --- |
| PawnIO | 2.2.0 | GPL-2.0-or-later（含作者声明的特殊例外） | https://github.com/namazso/PawnIO · https://pawnio.eu |

PawnIO 是 namazso 签名的内核驱动，用于访问 CPU MSR 与主板 Super I/O。安装包**原样分发**官方 `PawnIO_setup.exe`：不修改文件、不改动签名、不附加任何额外限制，完整源码见上面的上游仓库。构建前需自行下载到 `packaging/redist/`，因此仓库本身不再分发该驱动二进制。

## 打进 `dist` 可执行文件的运行时

| 组件 | 许可证 |
| --- | --- |
| Python 3.11 | PSF License |
| Tcl / Tk | BSD 风格许可 |
| psutil | BSD-3-Clause |
| pywin32 | PSF 派生许可 |
| PyInstaller bootloader | GPL-2.0+（含运行时链接例外，见其 `Bootloader/COPYING.txt`） |

## 打包工具

Inno Setup 6。`packaging/ChineseSimplified.isl` 为其中文语言文件，版权归 Jordan Russell 及译文作者，遵循 Inno Setup 的发行条款。
