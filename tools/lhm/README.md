# tools/lhm —— 通用传感器后端（可选）

戴尔机型走 AWCC 就够了，**这个目录可以留空**。其他机型想读 CPU 温度和风扇转速，才需要这里的
`LibreHardwareMonitorLib` + `PawnIO` 组合。

## 需要放什么

```
tools/lhm/
├── sensors.exe                 # 对 LibreHardwareMonitorLib 的薄封装，每秒输出一行 JSON
├── sensors.cs                  # 源码（自己编译时用）
├── LibreHardwareMonitorLib.dll # 从 LibreHardwareMonitor 发布包取
└── HidSharp.dll                # LibreHardwareMonitorLib 的依赖
```

`sensors.exe --parent <pid>` 启动时绑定父进程 PID，父进程退出后自动结束，不会留下孤儿进程。
标准输出每行一个 JSON 对象，字段包括 `cpu_temp` / `gpu_temp` / `board_temp` / `fans` / `disks`；
驱动缺失时输出 `{"fatal": "..."}`，面板读取该字段并在「关于」里展示原因。

## 依赖

- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 的发布包（取 `LibreHardwareMonitorLib.dll`、`HidSharp.dll`）
- [PawnIO](https://pawnio.eu)（签名内核驱动，可单独卸载；未安装时 LHM 读不到 MSR 与 Super I/O）

## 自己编译 sensors.exe

```powershell
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo `
  "-reference:tools\lhm\LibreHardwareMonitorLib.dll" `
  "-out:tools\lhm\sensors.exe" "tools\lhm\sensors.cs"
```

## 说明

二进制 DLL 不入库（体积与许可证），首次使用请按上面步骤自行获取。面板对缺失是容错的：
找不到 `sensors.exe` 时只在内部记一条 `缺少 tools/lhm/sensors.exe`，界面照常显示系统级数据。
