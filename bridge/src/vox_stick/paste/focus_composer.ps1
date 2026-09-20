param([ValidateSet('ChatGPT', 'Claude')][string]$AppName)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class VoiceWindow {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
try {
    $processName = if ($AppName -eq 'ChatGPT') { 'ChatGPT' } else { 'claude' }
    $windows = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0)
    if (!$windows.Count) {
        $apps = @(Get-StartApps | Where-Object Name -eq $AppName)
        if ($apps.Count -ne 1) { throw "$AppName installation could not be uniquely identified" }
        Start-Process explorer.exe -ArgumentList ('shell:AppsFolder\' + $apps[0].AppID) -WindowStyle Hidden
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    $activated = [IntPtr]::Zero
    do {
        $windows = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0)
        if ($windows.Count) {
            $foreground = [VoiceWindow]::GetForegroundWindow()
            $selected = $windows | Where-Object MainWindowHandle -eq $foreground | Select-Object -First 1
            if (!$selected -and $windows.Count -eq 1) { $selected = $windows[0] }
            if (!$selected) { throw "Multiple $AppName windows are open; activate the intended conversation first" }
            $handle = $selected.MainWindowHandle
            if ($activated -ne $handle) {
                [void][VoiceWindow]::ShowWindow($handle, 9)
                [void][VoiceWindow]::SetForegroundWindow($handle)
                $activated = $handle
            }
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            $condition = [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Edit)
            $edits = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
            $matches = @($edits | Where-Object {
                $_.Current.IsEnabled -and !$_.Current.IsOffscreen -and
                ($_.Current.AutomationId -eq 'prompt-textarea' -or
                 $_.Current.Name -match 'Write your prompt|Reply to Claude|Message Claude|Ask anything|Send a message|Message ChatGPT|随心输入|输入消息|发送消息|向.*提问')
            })
            if ($matches.Count -eq 1) {
                $matches[0].SetFocus()
                if ($matches[0].Current.HasKeyboardFocus -and [VoiceWindow]::GetForegroundWindow() -eq $handle) {
                    @{ hwnd = $handle.ToInt64() } | ConvertTo-Json -Compress
                    exit 0
                }
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Could not focus the $AppName conversation input; open a conversation and retry"
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
