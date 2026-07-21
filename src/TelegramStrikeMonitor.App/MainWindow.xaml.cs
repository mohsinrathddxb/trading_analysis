using Microsoft.Win32;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using TelegramStrikeMonitor.App.Services;

namespace TelegramStrikeMonitor.App;

public partial class MainWindow : Window
{
    private readonly PythonMonitorService _monitorService = new();
    private readonly string _engineFolder;
    private readonly string _settingsPath;
    private FileSystemWatcher? _reportWatcher;
    private FileSystemWatcher? _imageWatcher;
    private bool _isClosing;

    public MainWindow()
    {
        InitializeComponent();

        _engineFolder = FindEngineFolder();
        _settingsPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TelegramStrikeMonitor",
            "appsettings.json");

        EngineFolderTextBox.Text = _engineFolder;
        PythonPathTextBox.Text = LoadPythonPath();

        _monitorService.OutputReceived += MonitorService_OutputReceived;
        _monitorService.ProcessExited += MonitorService_ProcessExited;

        EnsureEngineFolders();
        StartFolderWatchers();
        RefreshDashboard();
        AppendLog("Application started.");
    }

    private static string FindEngineFolder()
    {
        // Published build: the project copies the engine beside the executable.
        string besideExecutable = Path.Combine(AppContext.BaseDirectory, "python");
        if (File.Exists(Path.Combine(besideExecutable, "live_strike_monitor.py")))
        {
            return besideExecutable;
        }

        // Visual Studio debug build: walk upward until the solution-level
        // python folder is found. This keeps the virtual environment and
        // runtime data in one stable location instead of under bin/Debug.
        DirectoryInfo? current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            string candidate = Path.Combine(current.FullName, "python");
            if (File.Exists(Path.Combine(candidate, "live_strike_monitor.py")))
            {
                return candidate;
            }
            current = current.Parent;
        }

        return besideExecutable;
    }

    private string DownloadFolder => Path.Combine(_engineFolder, "downloaded_images");
    private string ReportFolder => Path.Combine(_engineFolder, "reports");
    private string DebugFolder => Path.Combine(_engineFolder, "debug_ocr");

    private void EnsureEngineFolders()
    {
        Directory.CreateDirectory(_engineFolder);
        Directory.CreateDirectory(DownloadFolder);
        Directory.CreateDirectory(ReportFolder);
        Directory.CreateDirectory(DebugFolder);
    }

    private string LoadPythonPath()
    {
        try
        {
            if (File.Exists(_settingsPath))
            {
                var data = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(_settingsPath));
                if (!string.IsNullOrWhiteSpace(data?.PythonExecutable))
                {
                    return data.PythonExecutable;
                }
            }
        }
        catch
        {
            // Invalid local settings should not prevent app startup.
        }

        string embeddedVenv = Path.Combine(_engineFolder, ".venv", "Scripts", "python.exe");
        if (File.Exists(embeddedVenv))
        {
            return embeddedVenv;
        }

        return "python.exe";
    }

    private void SaveSettings()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_settingsPath)!);
        var data = new AppSettings(PythonPathTextBox.Text.Trim());
        File.WriteAllText(_settingsPath, JsonSerializer.Serialize(data, new JsonSerializerOptions
        {
            WriteIndented = true,
        }));
    }

    private async void StartButton_Click(object sender, RoutedEventArgs e)
    {
        await StartMonitorAsync(GetSelectedArguments());
    }

    private async void RegenerateButton_Click(object sender, RoutedEventArgs e)
    {
        await StartMonitorAsync("--existing-only");
    }

    private async Task StartMonitorAsync(string arguments)
    {
        if (_monitorService.IsRunning)
        {
            MessageBox.Show("The monitor is already running.", "Telegram Strike Monitor", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        try
        {
            ValidateBeforeStart();
            SaveSettings();

            AppendLog($"Starting engine with {arguments}...");
            _monitorService.Start(PythonPathTextBox.Text.Trim(), _engineFolder, arguments);
            SetRunningState(true);
            FooterText.Text = "Python engine is running.";
        }
        catch (Exception exception)
        {
            AppendLog("START FAILED | " + exception.Message);
            MessageBox.Show(exception.Message, "Unable to start monitor", MessageBoxButton.OK, MessageBoxImage.Error);
            SetRunningState(false);
        }

        await Task.CompletedTask;
    }

    private void ValidateBeforeStart()
    {
        string scriptPath = Path.Combine(_engineFolder, "live_strike_monitor.py");
        if (!File.Exists(scriptPath))
        {
            throw new FileNotFoundException("live_strike_monitor.py is missing from the Python engine folder.", scriptPath);
        }

        string envPath = Path.Combine(_engineFolder, ".env");
        if (!File.Exists(envPath))
        {
            throw new FileNotFoundException(
                "The Python engine .env file is missing. Open .env and add TELEGRAM_API_ID and TELEGRAM_API_HASH.",
                envPath);
        }

        string python = PythonPathTextBox.Text.Trim();
        if (!python.Equals("python.exe", StringComparison.OrdinalIgnoreCase) && !File.Exists(python))
        {
            throw new FileNotFoundException("The selected Python executable was not found.", python);
        }
    }

    private async void StopButton_Click(object sender, RoutedEventArgs e)
    {
        FooterText.Text = "Stopping Python engine...";
        await _monitorService.StopAsync(TimeSpan.FromSeconds(4));
        SetRunningState(false);
        AppendLog("Monitor stopped.");
        FooterText.Text = "Stopped.";
    }

    private string GetSelectedArguments()
    {
        if (ModeComboBox.SelectedItem is ComboBoxItem selected && selected.Tag is string tag)
        {
            return tag;
        }
        return "--today-and-live";
    }

    private void MonitorService_OutputReceived(object? sender, string line)
    {
        Dispatcher.Invoke(() =>
        {
            AppendLog(line);
            RefreshDashboard();
        });
    }

    private void MonitorService_ProcessExited(object? sender, int exitCode)
    {
        Dispatcher.Invoke(() =>
        {
            SetRunningState(false);
            AppendLog($"Python engine exited with code {exitCode}.");
            FooterText.Text = exitCode == 0 ? "Processing completed." : $"Engine stopped with error code {exitCode}.";
            RefreshDashboard();
        });
    }

    private void SetRunningState(bool running)
    {
        StartButton.IsEnabled = !running;
        StopButton.IsEnabled = running;
        StatusText.Text = running ? "Running" : "Stopped";
        StatusDot.Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(running ? "#31B66C" : "#7A879A"));
    }

    private void AppendLog(string message)
    {
        string timestamp = DateTime.Now.ToString("HH:mm:ss");
        LogTextBox.AppendText($"{timestamp} | {message}{Environment.NewLine}");
        LogTextBox.ScrollToEnd();
    }

    private void StartFolderWatchers()
    {
        _reportWatcher = CreateWatcher(ReportFolder, (_, _) => Dispatcher.BeginInvoke(RefreshDashboard));
        _imageWatcher = CreateWatcher(DownloadFolder, (_, _) => Dispatcher.BeginInvoke(RefreshDashboard));
    }

    private static FileSystemWatcher CreateWatcher(string folder, FileSystemEventHandler handler)
    {
        var watcher = new FileSystemWatcher(folder)
        {
            IncludeSubdirectories = true,
            EnableRaisingEvents = true,
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.CreationTime,
        };
        watcher.Created += handler;
        watcher.Changed += handler;
        watcher.Renamed += (_, eventArgs) => handler(watcher, eventArgs);
        return watcher;
    }

    private void RefreshDashboard()
    {
        ImageCountText.Text = CountFiles(DownloadFolder, new[] { ".jpg", ".jpeg", ".png", ".webp", ".bmp" }).ToString();
        ReportCountText.Text = CountFiles(ReportFolder, new[] { ".txt" }).ToString();
        LoadLatestReport();
    }

    private static int CountFiles(string folder, IReadOnlyCollection<string> extensions)
    {
        if (!Directory.Exists(folder))
        {
            return 0;
        }
        return Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories)
            .Count(path => extensions.Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase));
    }

    private void LoadLatestReport()
    {
        if (!Directory.Exists(ReportFolder))
        {
            return;
        }

        IEnumerable<string> combinedReports = Directory.EnumerateFiles(
            ReportFolder,
            "*_combined_report.txt",
            SearchOption.AllDirectories);
        IEnumerable<string> reportCandidates = combinedReports.Any()
            ? combinedReports
            : Directory.EnumerateFiles(
                ReportFolder,
                "*_report.txt",
                SearchOption.AllDirectories);

        FileInfo? latest = reportCandidates
            .Select(path => new FileInfo(path))
            .OrderByDescending(file => file.LastWriteTimeUtc)
            .FirstOrDefault();

        if (latest is null)
        {
            LatestReportNameText.Text = "No report generated yet";
            LatestReportTextBox.Text = string.Empty;
            LastReportTimeText.Text = "—";
            return;
        }

        try
        {
            LatestReportNameText.Text = latest.Name;
            LatestReportTextBox.Text = File.ReadAllText(latest.FullName);
            LastReportTimeText.Text = latest.LastWriteTime.ToString("dd-MMM HH:mm:ss");
        }
        catch (IOException)
        {
            // The Python engine may still be writing the report. A later watcher event refreshes it.
        }
    }

    private static void OpenFolder(string folder)
    {
        Directory.CreateDirectory(folder);
        Process.Start(new ProcessStartInfo("explorer.exe", folder) { UseShellExecute = true });
    }

    private static void OpenFile(string path)
    {
        if (!File.Exists(path))
        {
            File.WriteAllText(path, string.Empty);
        }
        Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
    }

    private void OpenImagesButton_Click(object sender, RoutedEventArgs e) => OpenFolder(DownloadFolder);
    private void OpenReportsButton_Click(object sender, RoutedEventArgs e) => OpenFolder(ReportFolder);
    private void OpenDebugButton_Click(object sender, RoutedEventArgs e) => OpenFolder(DebugFolder);
    private void OpenDatabaseFolderButton_Click(object sender, RoutedEventArgs e) => OpenFolder(_engineFolder);
    private void OpenConfigButton_Click(object sender, RoutedEventArgs e) => OpenFile(Path.Combine(_engineFolder, "monitor_config.json"));

    private void OpenEnvButton_Click(object sender, RoutedEventArgs e)
    {
        string envPath = Path.Combine(_engineFolder, ".env");
        if (!File.Exists(envPath))
        {
            string example = Path.Combine(_engineFolder, ".env.example");
            File.WriteAllText(envPath, File.Exists(example) ? File.ReadAllText(example) : string.Empty);
        }
        OpenFile(envPath);
    }

    private void BrowsePythonButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog
        {
            Title = "Select python.exe",
            Filter = "Python executable (python.exe)|python.exe|Executable files (*.exe)|*.exe|All files (*.*)|*.*",
            CheckFileExists = true,
        };
        if (dialog.ShowDialog(this) == true)
        {
            PythonPathTextBox.Text = dialog.FileName;
            SaveSettings();
        }
    }

    private void RefreshReportButton_Click(object sender, RoutedEventArgs e) => RefreshDashboard();
    private void ClearLogButton_Click(object sender, RoutedEventArgs e) => LogTextBox.Clear();

    private async void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_isClosing)
        {
            return;
        }

        if (_monitorService.IsRunning)
        {
            e.Cancel = true;
            _isClosing = true;
            await _monitorService.StopAsync(TimeSpan.FromSeconds(3));
            Close();
            return;
        }

        _reportWatcher?.Dispose();
        _imageWatcher?.Dispose();
        _monitorService.Dispose();
    }

    private sealed record AppSettings(string PythonExecutable);
}
