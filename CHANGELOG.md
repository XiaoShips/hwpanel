# 更新日志

本文件记录值得注意的变更，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## v2.1 - 2026-11-02

### 新增

- 接入 LibreHardwareMonitorLib + PawnIO 后端，读取 CPU 温度、风扇转速、主板与硬盘温度
- 戴尔 AWCC WMI 通道，存在时优先于通用后端
- 可选的 PawnIO 驱动安装步骤，已装则自动跳过
- 迷你模式、右键菜单、位置与偏好持久化、单实例
- Inno Setup 中文安装包与 `packaging\build.bat` 一键重建脚本

### 变更

- 风扇一栏只列出真正接线的转速接口，未接线的 Super I/O 输入口不再占用显示位
- 状态判定放宽：核显机器无 GPU 温度、无风扇机器无转速不再误报“部分数据不可用”
- 打包为 PyInstaller onedir，脱离本机 Python 环境运行

### 修复

- 提权重启时冻结版重复传入自身路径，导致第二次启动失败
- 依赖本机 Python 环境的路径写法，改为按打包后的资源目录解析

## 更早版本

v2.1 之前的改动未单独记录。
