Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class BenchmarkWindow {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr handle, StringBuilder text, int count);
}
'@
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Pulsar 253 Benchmark Target'
$form.ClientSize = New-Object System.Drawing.Size(960,540)
$form.StartPosition = 'CenterScreen'
$form.BackColor = [System.Drawing.Color]::DarkBlue
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 16
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$form.Add_Paint({
    param($sender, $event)
    $x = [int](($clock.ElapsedMilliseconds / 3) % 960)
    $event.Graphics.Clear([System.Drawing.Color]::DarkBlue)
    $event.Graphics.FillRectangle([System.Drawing.Brushes]::Orange, $x, 0, 80, 540)
    $event.Graphics.DrawString([string]$clock.ElapsedMilliseconds, $form.Font, [System.Drawing.Brushes]::White, 10, 10)
})
$timer.Add_Tick({ $form.Invalidate() })
$form.Add_Shown({
    $className = New-Object System.Text.StringBuilder(512)
    [void][BenchmarkWindow]::GetClassName($form.Handle, $className, 512)
    Write-Host ('WINDOW_DESCRIPTOR=' + $form.Text + ':' + $className.ToString() + ':pwsh.exe')
    $timer.Start()
})
try { [System.Windows.Forms.Application]::Run($form) }
finally { $timer.Stop(); $timer.Dispose(); $form.Dispose() }
