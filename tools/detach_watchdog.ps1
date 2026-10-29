# 把看门狗"脱离作业对象"启动，让它在助手会话结束、工具进程被回收之后仍然活着。
#
# 为什么需要这个脚本
# ------------------
# 实测：助手每次工具调用都会把子进程放进一个**作业对象（Job Object）**，
# 调用结束（或后台任务被取消）时整个作业被关闭，作业里的进程全部被杀。
# 这就是"站点隔一阵就没了、还得再让人敲一次启动命令"的真正原因——
# 不是服务自己崩了，是被连坐回收了。
#
# Windows 的解法是 CreateProcess 的 CREATE_BREAKAWAY_FROM_JOB 标志：
# 带这个标志创建的进程**不属于**调用者的作业，作业关闭时它不会被杀。
# Start-Process 没有暴露这个标志，所以这里用 P/Invoke 直接调 CreateProcessW。
#
# 如果作业不允许脱离（ERROR_ACCESS_DENIED），脚本会如实说明，而不是假装成功。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\detach_watchdog.ps1
#   powershell ... -File tools\detach_watchdog.ps1 -Port 8848 -IntervalSec 15

param(
    [int]$Port = 8848,
    [int]$IntervalSec = 15
)

$ErrorActionPreference = 'Stop'

# ---- 1) 已经有一个脱离作业的看门狗在跑？那就别再多起一个 ----
$existing = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*watch.ps1*' -and $_.CommandLine -like "*-Port $Port*" }
if ($existing) {
    Write-Host ("已有看门狗在运行：PID " + (($existing | ForEach-Object { $_.ProcessId }) -join ', '))
    Write-Host "不重复启动。"
    exit 0
}

$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$watch = Join-Path $here 'watch.ps1'
if (-not (Test-Path $watch)) { throw "找不到 watch.ps1：$watch" }

$cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
       $watch + '" -Port ' + $Port + ' -IntervalSec ' + $IntervalSec

# ---- 2) P/Invoke：带 CREATE_BREAKAWAY_FROM_JOB 起进程 ----
$cs = @'
using System;
using System.Runtime.InteropServices;

public static class JobBreakaway
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO
    {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize;
        public int dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CreateProcess(
        string lpApplicationName, string lpCommandLine,
        IntPtr lpProcessAttributes, IntPtr lpThreadAttributes,
        bool bInheritHandles, uint dwCreationFlags,
        IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFO si, out PROCESS_INFORMATION pi);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr h);

    public const uint CREATE_BREAKAWAY_FROM_JOB = 0x01000000;
    public const uint CREATE_NEW_PROCESS_GROUP   = 0x00000200;
    public const uint CREATE_NO_WINDOW           = 0x08000000;
    public const uint DETACHED_PROCESS           = 0x00000008;

    public static int Start(string command)
    {
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        PROCESS_INFORMATION pi;
        uint flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP |
                     CREATE_NO_WINDOW | DETACHED_PROCESS;
        bool ok = CreateProcess(null, command, IntPtr.Zero, IntPtr.Zero, false,
                                flags, IntPtr.Zero, null, ref si, out pi);
        if (!ok)
        {
            int err = Marshal.GetLastWin32Error();
            throw new System.ComponentModel.Win32Exception(err);
        }
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        return pi.dwProcessId;
    }
}
'@

try { Add-Type -TypeDefinition $cs -ErrorAction Stop } catch {
    Write-Host "编译 P/Invoke 失败：$($_.Exception.Message)"
    exit 2
}

try {
    $pid2 = [JobBreakaway]::Start($cmd)
    Write-Host "已脱离作业对象启动看门狗。PID = $pid2"
    Write-Host "它有 15 秒一轮的健康检查，服务挂了自己拉起来；源码变了自动重启服务。"
    Write-Host "日志：$here\logs\watchdog.log"
    exit 0
} catch {
    Write-Host "脱离作业失败：$($_.Exception.Message)"
    Write-Host "（若为 Access denied，说明当前环境不允许 CREATE_BREAKAWAY_FROM_JOB；"
    Write-Host "  退路是登录时的启动项 ThesisSuite.cmd。）"
    exit 3
}
