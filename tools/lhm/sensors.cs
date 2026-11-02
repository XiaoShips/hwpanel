// sensors.exe —— LibreHardwareMonitor 采样助手
// 由 hwpanel.py 以管理员身份拉起，stdout 每秒输出一行 JSON。
// 需要系统已安装 PawnIO（https://pawnio.eu）才能读取 Intel CPU MSR 与主板 Super I/O。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Threading;
using LibreHardwareMonitor.Hardware;

class SensorDumper
{
    static string F(double? v)
    {
        if (!v.HasValue || double.IsNaN(v.Value)) return "null";
        return v.Value.ToString("0.##", CultureInfo.InvariantCulture);
    }

    static string Q(string s)
    {
        if (s == null) return "\"\"";
        StringBuilder b = new StringBuilder("\"");
        foreach (char c in s)
        {
            if (c == '"' || c == '\\') b.Append('\\').Append(c);
            else if (c == '\n') b.Append("\\n");
            else if (c == '\r') b.Append("\\r");
            else if (c == '\t') b.Append("\\t");
            else if (c < ' ') b.Append(' ');
            else b.Append(c);
        }
        return b.Append('"').ToString();
    }

    static double? Temp(IHardware hw, string name)
    {
        foreach (ISensor s in hw.Sensors)
            if (s.SensorType == SensorType.Temperature && s.Name == name && s.Value.HasValue)
                return s.Value;
        return null;
    }

    static double? CpuTemp(IHardware hw)
    {
        double? t = Temp(hw, "CPU Package");
        if (t.HasValue) return t;
        t = Temp(hw, "Core Max");
        if (t.HasValue) return t;
        double? best = null;
        foreach (ISensor s in hw.Sensors)
            if (s.SensorType == SensorType.Temperature && s.Value.HasValue
                && s.Name.StartsWith("CPU Core #")
                && (!best.HasValue || s.Value.Value > best.Value))
                best = s.Value;
        return best;
    }

    static void Main(string[] args)
    {
        int parentPid = 0;
        for (int i = 0; i + 1 < args.Length; i++)
            if (args[i] == "--parent") int.TryParse(args[i + 1], out parentPid);
        Process parent = null;
        if (parentPid > 0)
        {
            try { parent = Process.GetProcessById(parentPid); } catch { }
        }

        Computer computer = new Computer();
        computer.IsCpuEnabled = true;
        computer.IsGpuEnabled = true;
        computer.IsMotherboardEnabled = true;
        computer.IsStorageEnabled = true;

        try
        {
            computer.Open();
        }
        catch (Exception ex)
        {
            Console.WriteLine("{\"fatal\":" + Q(ex.Message) + "}");
            Console.Out.Flush();
            return;
        }

        while (true)
        {
            try
            {
                double? cpuTemp = null;
                double? gpuTemp = null;
                double? boardTemp = null;
                List<string[]> fans = new List<string[]>();
                List<string[]> disks = new List<string[]>();

                foreach (IHardware hw in computer.Hardware)
                {
                    hw.Update();
                    switch (hw.HardwareType)
                    {
                        case HardwareType.Cpu:
                            cpuTemp = CpuTemp(hw);
                            break;

                        case HardwareType.GpuNvidia:
                        case HardwareType.GpuAmd:
                        case HardwareType.GpuIntel:
                            if (!gpuTemp.HasValue) gpuTemp = Temp(hw, "GPU Core");
                            break;

                        case HardwareType.Storage:
                            foreach (ISensor s in hw.Sensors)
                                if (s.SensorType == SensorType.Temperature
                                    && s.Name == "Composite Temperature" && s.Value.HasValue)
                                    disks.Add(new string[] { hw.Name, F(s.Value) });
                            break;

                        case HardwareType.Motherboard:
                            foreach (IHardware sub in hw.SubHardware)
                            {
                                sub.Update();
                                foreach (ISensor s in sub.Sensors)
                                {
                                    if (!s.Value.HasValue) continue;
                                    if (s.SensorType == SensorType.Temperature)
                                    {
                                        if (!boardTemp.HasValue || s.Value.Value > boardTemp.Value)
                                            boardTemp = s.Value;
                                    }
                                    else if (s.SensorType == SensorType.Fan)
                                    {
                                        fans.Add(new string[] { s.Name, F(s.Value) });
                                    }
                                }
                            }
                            break;
                    }
                }

                fans.Sort(delegate(string[] a, string[] b)
                {
                    return double.Parse(b[1], CultureInfo.InvariantCulture)
                        .CompareTo(double.Parse(a[1], CultureInfo.InvariantCulture));
                });

                StringBuilder sb = new StringBuilder();
                sb.Append("{\"cpu_temp\":").Append(F(cpuTemp));
                sb.Append(",\"gpu_temp\":").Append(F(gpuTemp));
                sb.Append(",\"board_temp\":").Append(F(boardTemp));
                sb.Append(",\"fans\":[");
                for (int i = 0; i < fans.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append('[').Append(Q(fans[i][0])).Append(',').Append(fans[i][1]).Append(']');
                }
                sb.Append("],\"disks\":[");
                for (int i = 0; i < disks.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append('[').Append(Q(disks[i][0])).Append(',').Append(disks[i][1]).Append(']');
                }
                sb.Append("]}");
                Console.WriteLine(sb.ToString());
                Console.Out.Flush();
            }
            catch (Exception ex)
            {
                Console.WriteLine("{\"fatal\":" + Q(ex.Message) + "}");
                Console.Out.Flush();
            }

            if (parent != null && parent.HasExited) break;
            Thread.Sleep(1000);
        }

        try { computer.Close(); } catch { }
    }
}
