Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class MuseWinHide {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern int GetWindowLongW(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    public static extern int SetWindowLongW(IntPtr hWnd, int nIndex, int dwNewLong);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    public const int GWL_EXSTYLE = -20;
    public const int WS_EX_TOOLWINDOW = 0x00000080;
    public const int WS_EX_APPWINDOW = 0x00040000;
    public const int SW_HIDE = 0;
    public const int SW_SHOWNOACTIVATE = 4;
    public static readonly IntPtr HWND_BOTTOM = new IntPtr(1);
    public const uint SWP_NOSIZE = 0x0001;
    public const uint SWP_NOMOVE = 0x0002;
    public const uint SWP_NOACTIVATE = 0x0010;
    public const uint SWP_FRAMECHANGED = 0x0020;

    public static int HideMuseChromeWindows() {
        int count = 0;
        EnumWindows((hWnd, lParam) => {
            if (!IsWindowVisible(hWnd)) return true;
            var cls = new StringBuilder(128);
            GetClassNameW(hWnd, cls, cls.Capacity);
            if (cls.ToString() != "Chrome_WidgetWin_1") return true;

            var sb = new StringBuilder(512);
            GetWindowTextW(hWnd, sb, sb.Capacity);
            string title = sb.ToString();
            if (string.IsNullOrEmpty(title)) return true;

            // Match ONLY Muse.ai worker windows, NEVER AI Movie Studio or personal tabs
            bool isMuse = title.IndexOf("Muse", StringComparison.OrdinalIgnoreCase) >= 0;
            bool isStudio = title.IndexOf("AI Movie Studio", StringComparison.OrdinalIgnoreCase) >= 0;
            if (!isMuse || isStudio) return true;

            int exStyle = GetWindowLongW(hWnd, GWL_EXSTYLE);
            int newStyle = (exStyle & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW;
            ShowWindow(hWnd, SW_HIDE);
            SetWindowLongW(hWnd, GWL_EXSTYLE, newStyle);
            ShowWindow(hWnd, SW_SHOWNOACTIVATE);
            SetWindowPos(hWnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED);
            count++;
            return true;
        }, IntPtr.Zero);
        return count;
    }
}
"@

$hidden = [MuseWinHide]::HideMuseChromeWindows()
Write-Output ("Hidden Muse Chrome windows from Taskbar: " + $hidden)
