using System.Diagnostics;
using System.IO;

namespace TelegramStrikeMonitor.App.Services;

public sealed class PythonMonitorService : IDisposable
{
    private Process? _process;

    public event EventHandler<string>? OutputReceived;
    public event EventHandler<int>? ProcessExited;

    public bool IsRunning => _process is { HasExited: false };

    public void Start(string pythonExecutable, string engineFolder, string arguments)
    {
        if (IsRunning)
        {
            throw new InvalidOperationException("The monitor is already running.");
        }

        string scriptPath = Path.Combine(engineFolder, "live_strike_monitor.py");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("The Python monitor script was not found.", scriptPath);
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = pythonExecutable,
            WorkingDirectory = engineFolder,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add(scriptPath);
        if (!string.IsNullOrWhiteSpace(arguments))
        {
            startInfo.ArgumentList.Add(arguments);
        }
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";

        _process = new Process
        {
            StartInfo = startInfo,
            EnableRaisingEvents = true,
        };

        _process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                OutputReceived?.Invoke(this, e.Data);
            }
        };
        _process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                OutputReceived?.Invoke(this, "ERROR | " + e.Data);
            }
        };
        _process.Exited += (_, _) =>
        {
            int exitCode = _process?.ExitCode ?? -1;
            ProcessExited?.Invoke(this, exitCode);
        };

        if (!_process.Start())
        {
            throw new InvalidOperationException("The Python process could not be started.");
        }

        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();
    }

    public async Task StopAsync(TimeSpan timeout)
    {
        if (_process is null || _process.HasExited)
        {
            return;
        }

        try
        {
            _process.CloseMainWindow();
        }
        catch
        {
            // Console-style child processes often have no main window.
        }

        using var cancellation = new CancellationTokenSource(timeout);
        try
        {
            await _process.WaitForExitAsync(cancellation.Token);
        }
        catch (OperationCanceledException)
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
                await _process.WaitForExitAsync();
            }
        }
    }

    public void Dispose()
    {
        if (_process is { HasExited: false })
        {
            _process.Kill(entireProcessTree: true);
        }
        _process?.Dispose();
    }
}
