// HardwareMonitor - prints CPU/GPU temps and load as one JSON line per second.
// Compiled with the .NET Framework 4.8 csc.exe (C# 5), references LibreHardwareMonitorLib.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Threading;
using LibreHardwareMonitor.Hardware;

class HardwareMonitor
{
    static string Num(float? v)
    {
        if (!v.HasValue) return "null";
        return v.Value.ToString("F1", CultureInfo.InvariantCulture);
    }

    static string Escape(string s)
    {
        if (s == null) return "";
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    // Higher rank = better sensor to represent the value.
    static int RankCpuTemp(string name)
    {
        if (name.Contains("Tctl/Tdie")) return 4;   // AMD Ryzen
        if (name.Contains("Package")) return 3;     // Intel
        if (name.Contains("Average")) return 2;
        if (name.Contains("Core")) return 1;
        return 0;
    }

    static int RankGpuTemp(string name)
    {
        if (name == "GPU Core") return 2;
        if (name.Contains("Hot Spot")) return 1;
        return 0;
    }

    static int RankGpuLoad(string name)
    {
        if (name == "GPU Core") return 2;
        if (name.Contains("D3D 3D")) return 1;
        return 0;
    }

    // Lists every piece of hardware and every sensor found, so a machine that
    // reports no CPU temperature can say why (usually the ring0 driver failed
    // to load: sensors of type Load appear, Temperature ones do not).
    static void Dump()
    {
        Computer c = new Computer();
        c.IsCpuEnabled = true;
        c.IsGpuEnabled = true;
        c.IsMotherboardEnabled = true;
        try
        {
            c.Open();
        }
        catch (Exception ex)
        {
            Console.WriteLine("Computer.Open() FAILED: " + ex.GetType().Name + ": " + ex.Message);
            Console.WriteLine("(this usually means the kernel driver could not load - see");
            Console.WriteLine(" Windows Security > Device security > Core isolation)");
            return;
        }
        foreach (IHardware hw in c.Hardware)
        {
            try { hw.Update(); } catch (Exception ex)
            {
                Console.WriteLine("[" + hw.HardwareType + "] " + hw.Name +
                                  "  UPDATE FAILED: " + ex.Message);
                continue;
            }
            Console.WriteLine("[" + hw.HardwareType + "] " + hw.Name);
            int n = 0;
            foreach (ISensor s in hw.Sensors)
            {
                Console.WriteLine("    " + s.SensorType + " / " + s.Name + " = " +
                                  (s.Value.HasValue ? s.Value.Value.ToString("F1") : "null"));
                n++;
            }
            if (n == 0) Console.WriteLine("    (no sensors exposed)");
            foreach (IHardware sub in hw.SubHardware)
            {
                try { sub.Update(); } catch (Exception) { }
                Console.WriteLine("    -- " + sub.Name);
                foreach (ISensor s in sub.Sensors)
                    Console.WriteLine("        " + s.SensorType + " / " + s.Name + " = " +
                                      (s.Value.HasValue ? s.Value.Value.ToString("F1") : "null"));
            }
        }
        try { c.Close(); } catch (Exception) { }
    }

    static void Main(string[] args)
    {
        foreach (string a in args)
        {
            if (a == "--dump")
            {
                Dump();
                return;
            }
        }

        // Exit when the parent process closes our stdin.
        Thread watchdog = new Thread(delegate()
        {
            try { while (Console.In.Read() != -1) { } }
            catch (Exception) { }
            Environment.Exit(0);
        });
        watchdog.IsBackground = true;
        watchdog.Start();

        Computer computer = new Computer();
        computer.IsCpuEnabled = true;
        computer.IsGpuEnabled = true;
        try { computer.Open(); }
        catch (Exception ex)
        {
            Console.Error.WriteLine("open failed: " + ex.Message);
            Environment.Exit(1);
        }

        while (true)
        {
            float? cpuTemp = null, cpuLoad = null;
            int cpuTempRank = -1;
            List<string> gpus = new List<string>();

            foreach (IHardware hw in computer.Hardware)
            {
                try { hw.Update(); } catch (Exception) { continue; }

                if (hw.HardwareType == HardwareType.Cpu)
                {
                    foreach (ISensor s in hw.Sensors)
                    {
                        if (!s.Value.HasValue) continue;
                        if (s.SensorType == SensorType.Temperature)
                        {
                            int rank = RankCpuTemp(s.Name);
                            if (rank > cpuTempRank) { cpuTempRank = rank; cpuTemp = s.Value; }
                        }
                        else if (s.SensorType == SensorType.Load && s.Name == "CPU Total")
                        {
                            cpuLoad = s.Value;
                        }
                    }
                }
                else if (hw.HardwareType == HardwareType.GpuAmd ||
                         hw.HardwareType == HardwareType.GpuNvidia ||
                         hw.HardwareType == HardwareType.GpuIntel)
                {
                    float? gTemp = null, gLoad = null;
                    int gTempRank = -1, gLoadRank = -1;
                    foreach (ISensor s in hw.Sensors)
                    {
                        if (!s.Value.HasValue) continue;
                        if (s.SensorType == SensorType.Temperature)
                        {
                            int rank = RankGpuTemp(s.Name);
                            if (rank > gTempRank) { gTempRank = rank; gTemp = s.Value; }
                        }
                        else if (s.SensorType == SensorType.Load)
                        {
                            int rank = RankGpuLoad(s.Name);
                            if (rank > gLoadRank) { gLoadRank = rank; gLoad = s.Value; }
                        }
                    }
                    gpus.Add(string.Format(
                        "{{\"name\":\"{0}\",\"temp\":{1},\"load\":{2}}}",
                        Escape(hw.Name), Num(gTemp), Num(gLoad)));
                }
            }

            StringBuilder sb = new StringBuilder();
            sb.Append("{\"cpu_temp\":").Append(Num(cpuTemp));
            sb.Append(",\"cpu_load\":").Append(Num(cpuLoad));
            sb.Append(",\"gpus\":[").Append(string.Join(",", gpus.ToArray())).Append("]}");
            Console.WriteLine(sb.ToString());
            Console.Out.Flush();
            Thread.Sleep(1000);
        }
    }
}
