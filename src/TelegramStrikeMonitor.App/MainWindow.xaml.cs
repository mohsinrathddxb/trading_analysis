using Microsoft.Win32;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using TelegramStrikeMonitor.App.Services;

namespace TelegramStrikeMonitor.App;

public partial class MainWindow : Window
{
    private readonly PythonMonitorService _monitorService = new();
    private readonly string _projectRoot;
    private readonly string _engineFolder;
    private readonly string _settingsPath;
    private readonly string _updateStatusPath;
    private readonly DispatcherTimer _dataHealthTimer = new();
    private FileSystemWatcher? _reportWatcher;
    private FileSystemWatcher? _imageWatcher;
    private DateTimeOffset? _monitorStartedAtUtc;
    private DateTimeOffset _lastDataHealthAlertAtUtc = DateTimeOffset.MinValue;
    private string? _lastDataHealthAlertKey;
    private bool _autoStartAttempted;
    private bool _allowClose;
    private bool _dataHealthAlertsEnabled;
    private bool _reportRegenerationRunning;

    private static readonly TimeSpan MarketTimeZoneOffset = TimeSpan.FromMinutes(330);
    private static readonly TimeSpan DataHealthStartupGrace = TimeSpan.FromMinutes(3);
    private static readonly TimeSpan ReportArrivalGrace = TimeSpan.FromSeconds(90);
    private static readonly TimeSpan MissingInstrumentTolerance = TimeSpan.FromMinutes(15);
    private static readonly TimeSpan StaleFeedTolerance = TimeSpan.FromMinutes(25);
    private static readonly TimeSpan AlertCooldown = TimeSpan.FromMinutes(15);
    private static readonly Regex LatestTableTimePattern = new(
        @"^Latest table time:\s*(?<hour>\d{1,2}):(?<minute>\d{2})\s+IST",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);

    public MainWindow()
    {
        InitializeComponent();

        _projectRoot = FindProjectRoot();
        _engineFolder = FindEngineFolder(_projectRoot);
        _settingsPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TelegramStrikeMonitor",
            "appsettings.json");
        _updateStatusPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TelegramStrikeMonitor",
            "update-status.json");

        EngineFolderTextBox.Text = _engineFolder;
        PythonPathTextBox.Text = LoadPythonPath();
        VersionText.Text = $"Version {GetApplicationVersion()}";
        LoadLastUpdateStatus();

        _monitorService.OutputReceived += MonitorService_OutputReceived;
        _monitorService.ProcessExited += MonitorService_ProcessExited;

        _dataHealthTimer.Interval = TimeSpan.FromMinutes(1);
        _dataHealthTimer.Tick += (_, _) => UpdateMarketDataHealth(showMessageBox: true);

        EnsureEngineFolders();
        StartFolderWatchers();
        RefreshDashboard();
        AppendLog("Application started.");
    }

    private static string FindProjectRoot()
    {
        DirectoryInfo? current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "TelegramStrikeMonitor.sln")))
            {
                return current.FullName;
            }
            current = current.Parent;
        }

        return AppContext.BaseDirectory;
    }

    private static string FindEngineFolder(string projectRoot)
    {
        // Visual Studio debug build: prefer the solution-level engine folder.
        // The build output also contains a copied python folder, but it does
        // not contain runtime-only files such as .env, the venv, or sessions.
        string projectEngine = Path.Combine(projectRoot, "python");
        if (File.Exists(Path.Combine(projectEngine, "live_strike_monitor.py")))
        {
            return projectEngine;
        }

        // Published build: the project copies the engine beside the executable.
        string besideExecutable = Path.Combine(AppContext.BaseDirectory, "python");
        return besideExecutable;
    }

    private static string GetApplicationVersion()
    {
        Version? version = Assembly.GetExecutingAssembly().GetName().Version;
        return version is null ? "unknown" : $"{version.Major}.{version.Minor}.{version.Build}";
    }

    private void LoadLastUpdateStatus()
    {
        try
        {
            if (!File.Exists(_updateStatusPath))
            {
                UpdateStatusText.Text = "Updates use the configured GitHub origin.";
                return;
            }

            UpdateStatus? status = JsonSerializer.Deserialize<UpdateStatus>(
                File.ReadAllText(_updateStatusPath));
            if (status is null)
            {
                return;
            }

            string result = status.Succeeded ? "succeeded" : "failed";
            UpdateStatusText.Text = $"Last update {result}: {status.Message}";
        }
        catch (Exception exception) when (exception is IOException or JsonException)
        {
            UpdateStatusText.Text = "The previous update status could not be read.";
        }
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

        string bundledRuntime = Path.Combine(_engineFolder, "runtime", "python.exe");
        if (File.Exists(bundledRuntime))
        {
            return bundledRuntime;
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

    private async void Window_ContentRendered(object? sender, EventArgs e)
    {
        if (_autoStartAttempted)
        {
            return;
        }

        _autoStartAttempted = true;
        _dataHealthAlertsEnabled = true;
        _dataHealthTimer.Start();
        if (!EnsureFirstRunSetup())
        {
            AppendLog("First-time setup was not completed. Monitoring was not started.");
            FooterText.Text = "Complete first-time Telegram setup to start monitoring.";
            return;
        }

        AppendLog("One-click startup: starting today's catch-up and live monitoring.");
        await StartMonitorAsync("--today-and-live");
    }

    private bool EnsureFirstRunSetup()
    {
        string envPath = Path.Combine(_engineFolder, ".env");
        string sessionPath = Path.Combine(_engineFolder, "autotrend_session.session");
        if (HasConfiguredCredentials(envPath) && File.Exists(sessionPath))
        {
            return true;
        }

        var setupWindow = new FirstRunSetupWindow(
            _engineFolder,
            PythonPathTextBox.Text.Trim())
        {
            Owner = this,
        };
        return setupWindow.ShowDialog() == true && setupWindow.SetupCompleted;
    }

    private static bool HasConfiguredCredentials(string envPath)
    {
        if (!File.Exists(envPath))
        {
            return false;
        }

        string text = File.ReadAllText(envPath);
        return text.Contains("TELEGRAM_API_ID=", StringComparison.Ordinal)
            && text.Contains("TELEGRAM_API_HASH=", StringComparison.Ordinal)
            && !text.Contains("YOUR_API_ID", StringComparison.OrdinalIgnoreCase)
            && !text.Contains("YOUR_API_HASH", StringComparison.OrdinalIgnoreCase);
    }

    private async void RegenerateButton_Click(object sender, RoutedEventArgs e)
    {
        await StartMonitorAsync("--existing-only");
    }

    private async void Regenerate15Button_Click(object sender, RoutedEventArgs e)
    {
        await RegenerateRecentReportAsync(15);
    }

    private async void Regenerate30Button_Click(object sender, RoutedEventArgs e)
    {
        await RegenerateRecentReportAsync(30);
    }

    private async void Regenerate45Button_Click(object sender, RoutedEventArgs e)
    {
        await RegenerateRecentReportAsync(45);
    }

    private async Task RegenerateRecentReportAsync(int minutes)
    {
        if (_reportRegenerationRunning)
        {
            MessageBox.Show(
                this,
                "A report is already being regenerated. Please wait for it to finish.",
                "Report regeneration",
                MessageBoxButton.OK,
                MessageBoxImage.Information);
            return;
        }

        string scriptPath = Path.Combine(_engineFolder, "live_strike_monitor.py");
        string python = PythonPathTextBox.Text.Trim();
        try
        {
            if (!File.Exists(scriptPath))
            {
                throw new FileNotFoundException(
                    "live_strike_monitor.py is missing from the Python engine folder.",
                    scriptPath);
            }
            if (!python.Equals("python.exe", StringComparison.OrdinalIgnoreCase)
                && !File.Exists(python))
            {
                throw new FileNotFoundException(
                    "The selected Python executable was not found.",
                    python);
            }

            _reportRegenerationRunning = true;
            SetReportRegenerationButtonsEnabled(false);
            FooterText.Text = $"Re-generating the last {minutes}-minute NIFTY and BANKNIFTY report...";
            AppendLog($"Re-generating last {minutes}-minute report for NIFTY and BANKNIFTY...");

            var startInfo = new ProcessStartInfo
            {
                FileName = python,
                WorkingDirectory = _engineFolder,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add(scriptPath);
            startInfo.ArgumentList.Add("--regenerate-minutes");
            startInfo.ArgumentList.Add(minutes.ToString());
            startInfo.Environment["PYTHONUNBUFFERED"] = "1";

            using Process process = new() { StartInfo = startInfo };
            if (!process.Start())
            {
                throw new InvalidOperationException(
                    "The Python report generator could not be started.");
            }

            Task<string> outputTask = process.StandardOutput.ReadToEndAsync();
            Task<string> errorTask = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            string output = await outputTask;
            string error = await errorTask;

            foreach (string line in error.Split(
                new[] { "\r\n", "\n" },
                StringSplitOptions.RemoveEmptyEntries))
            {
                AppendLog(line);
            }
            if (process.ExitCode != 0)
            {
                throw new InvalidOperationException(
                    string.IsNullOrWhiteSpace(error)
                        ? $"Report generator exited with code {process.ExitCode}."
                        : error.Trim());
            }

            string? reportPath = output.Split(
                    new[] { "\r\n", "\n" },
                    StringSplitOptions.RemoveEmptyEntries)
                .FirstOrDefault(line => line.StartsWith(
                    "REPORT_PATH=",
                    StringComparison.Ordinal));
            if (reportPath is not null)
            {
                AppendLog("Report created: " + reportPath["REPORT_PATH=".Length..]);
            }
            RefreshDashboard();
            FooterText.Text = $"Last {minutes}-minute NIFTY and BANKNIFTY report created.";
            MessageBox.Show(
                this,
                $"The last {minutes}-minute easy report for NIFTY and BANKNIFTY has been created and loaded on the dashboard.",
                "Report ready",
                MessageBoxButton.OK,
                MessageBoxImage.Information);
        }
        catch (Exception exception)
        {
            AppendLog("REPORT REGENERATION FAILED | " + exception.Message);
            FooterText.Text = "Report regeneration failed.";
            MessageBox.Show(
                this,
                exception.Message,
                $"Unable to regenerate {minutes}-minute report",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
        }
        finally
        {
            _reportRegenerationRunning = false;
            SetReportRegenerationButtonsEnabled(true);
        }
    }

    private void SetReportRegenerationButtonsEnabled(bool enabled)
    {
        Regenerate15Button.IsEnabled = enabled;
        Regenerate30Button.IsEnabled = enabled;
        Regenerate45Button.IsEnabled = enabled;
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
            _monitorStartedAtUtc = DateTimeOffset.UtcNow;
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

    private async void UpdateButton_Click(object sender, RoutedEventArgs e)
    {
        string updaterPath = Path.Combine(_projectRoot, "update-app.ps1");
        string solutionPath = Path.Combine(_projectRoot, "TelegramStrikeMonitor.sln");
        if (!File.Exists(updaterPath) || !File.Exists(solutionPath))
        {
            MessageBox.Show(
                "The update source checkout or update-app.ps1 was not found. "
                + "Run this application from the project checkout to use source updates.",
                "Update unavailable",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
            return;
        }

        MessageBoxResult confirmation = MessageBox.Show(
            "The monitor will stop briefly, fetch safe updates from GitHub when possible, "
            + "run tests, rebuild the app, and restart monitoring automatically. Continue?",
            "Update and restart",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (confirmation != MessageBoxResult.Yes)
        {
            return;
        }

        UpdateButton.IsEnabled = false;
        UpdateStatusText.Text = "Preparing update...";
        FooterText.Text = "Stopping monitor for update...";

        try
        {
            if (_monitorService.IsRunning)
            {
                await _monitorService.StopAsync(TimeSpan.FromSeconds(5));
                SetRunningState(false);
            }

            string configuration = AppContext.BaseDirectory.Contains(
                $"{Path.DirectorySeparatorChar}Release{Path.DirectorySeparatorChar}",
                StringComparison.OrdinalIgnoreCase)
                ? "Release"
                : "Debug";
            var startInfo = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                WorkingDirectory = _projectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add("-NoProfile");
            startInfo.ArgumentList.Add("-ExecutionPolicy");
            startInfo.ArgumentList.Add("Bypass");
            startInfo.ArgumentList.Add("-File");
            startInfo.ArgumentList.Add(updaterPath);
            startInfo.ArgumentList.Add("-ProjectRoot");
            startInfo.ArgumentList.Add(_projectRoot);
            startInfo.ArgumentList.Add("-TargetDirectory");
            startInfo.ArgumentList.Add(AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar));
            startInfo.ArgumentList.Add("-AppProcessId");
            startInfo.ArgumentList.Add(Environment.ProcessId.ToString());
            startInfo.ArgumentList.Add("-Configuration");
            startInfo.ArgumentList.Add(configuration);

            if (Process.Start(startInfo) is null)
            {
                throw new InvalidOperationException("The external updater could not be started.");
            }

            AppendLog("Updater started. The application will close and reopen automatically.");
            _allowClose = true;
            Close();
        }
        catch (Exception exception)
        {
            AppendLog("UPDATE START FAILED | " + exception.Message);
            UpdateStatusText.Text = "Update could not start: " + exception.Message;
            UpdateButton.IsEnabled = true;
            FooterText.Text = "Update could not start.";
            await StartMonitorAsync("--today-and-live");
        }
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
        UpdateMarketDataHealth(showMessageBox: false);
    }

    private void UpdateMarketDataHealth(bool showMessageBox)
    {
        MarketDataHealth health = EvaluateMarketDataHealth();
        DataHealthTitleText.Text = health.Title;
        DataHealthText.Text = health.StatusMessage;
        DataHealthBorder.Background = new SolidColorBrush(
            (Color)ColorConverter.ConvertFromString(health.Level switch
            {
                MarketDataHealthLevel.Healthy => "#E6F6EC",
                MarketDataHealthLevel.Warning => "#FFF3DB",
                MarketDataHealthLevel.Error => "#FDE8EA",
                _ => "#EEF2F7",
            }));

        if (health.Level != MarketDataHealthLevel.Error)
        {
            _lastDataHealthAlertKey = null;
            return;
        }

        if (!showMessageBox
            || !_dataHealthAlertsEnabled
            || string.IsNullOrWhiteSpace(health.AlertMessage)
            || health.AlertKey == _lastDataHealthAlertKey
            || DateTimeOffset.UtcNow - _lastDataHealthAlertAtUtc < AlertCooldown)
        {
            return;
        }

        _lastDataHealthAlertKey = health.AlertKey;
        _lastDataHealthAlertAtUtc = DateTimeOffset.UtcNow;
        AppendLog("DATA FEED ERROR | " + health.StatusMessage);
        MessageBox.Show(
            this,
            health.AlertMessage,
            "NIFTY / BANKNIFTY data warning",
            MessageBoxButton.OK,
            MessageBoxImage.Warning);
    }

    private MarketDataHealth EvaluateMarketDataHealth()
    {
        DateTimeOffset nowIst = DateTimeOffset.UtcNow.ToOffset(MarketTimeZoneOffset);
        bool marketHours = nowIst.TimeOfDay >= new TimeSpan(9, 45, 0)
            && nowIst.TimeOfDay <= new TimeSpan(15, 30, 0);
        string todayFolder = Path.Combine(ReportFolder, nowIst.ToString("yyyy-MM-dd"));

        if (!Directory.Exists(todayFolder))
        {
            if (ShouldRaiseFeedError(marketHours))
            {
                return CreateFeedError(
                    new[] { "NIFTY", "BANKNIFTY" },
                    null,
                    null,
                    "No reports have been generated for the current market day.");
            }

            return new MarketDataHealth(
                MarketDataHealthLevel.Waiting,
                "Market data health",
                _monitorService.IsRunning
                    ? "Waiting for today's NIFTY and BANKNIFTY reports."
                    : "Monitoring is stopped. Start the monitor to check NIFTY and BANKNIFTY data.",
                "waiting",
                null);
        }

        List<InstrumentReport> reports = Directory.EnumerateFiles(
                todayFolder,
                "*_combined_report.txt",
                SearchOption.TopDirectoryOnly)
            .Where(path => Path.GetFileName(path).StartsWith(
                "snapshot_",
                StringComparison.OrdinalIgnoreCase))
            .Select(TryReadInstrumentReport)
            .Where(report => report is not null)
            .Cast<InstrumentReport>()
            .ToList();

        Dictionary<string, InstrumentReport> latestByInstrument = reports
            .Where(report => report.Instrument is "NIFTY" or "BANKNIFTY")
            .GroupBy(report => report.Instrument)
            .ToDictionary(
                group => group.Key,
                group => group.OrderByDescending(report => report.MarketTime).First());
        InstrumentReport? newestReport = reports
            .OrderByDescending(report => report.File.LastWriteTimeUtc)
            .FirstOrDefault();
        InstrumentReport? newestUnidentified = reports
            .Where(report => report.Instrument == "MARKET")
            .OrderByDescending(report => report.File.LastWriteTimeUtc)
            .FirstOrDefault();

        latestByInstrument.TryGetValue("NIFTY", out InstrumentReport? nifty);
        latestByInstrument.TryGetValue("BANKNIFTY", out InstrumentReport? bankNifty);

        if (!marketHours)
        {
            return new MarketDataHealth(
                MarketDataHealthLevel.Waiting,
                "Market data health — market closed",
                BuildFeedTimesText(nifty, bankNifty),
                "market-closed",
                null);
        }

        if (!ShouldRaiseFeedError(marketHours))
        {
            return new MarketDataHealth(
                MarketDataHealthLevel.Waiting,
                "Market data health — starting",
                "The monitor is starting. Allowing time for NIFTY and BANKNIFTY catch-up reports.",
                "startup-grace",
                null);
        }

        bool newestReportSettled = newestReport is null
            || DateTime.UtcNow - newestReport.File.LastWriteTimeUtc >= ReportArrivalGrace;
        if (!newestReportSettled)
        {
            return new MarketDataHealth(
                MarketDataHealthLevel.Waiting,
                "Market data health — receiving reports",
                BuildFeedTimesText(nifty, bankNifty) + " Waiting briefly for the matching index report.",
                "arrival-grace",
                null);
        }

        List<string> affected = new();
        DateTimeOffset? newestMarketTime = new[] { nifty?.MarketTime, bankNifty?.MarketTime }
            .Where(value => value.HasValue)
            .Select(value => value!.Value)
            .DefaultIfEmpty()
            .Max();

        if (nifty is null
            || (newestMarketTime.HasValue
                && newestMarketTime.Value - nifty.MarketTime >= MissingInstrumentTolerance))
        {
            affected.Add("NIFTY");
        }
        if (bankNifty is null
            || (newestMarketTime.HasValue
                && newestMarketTime.Value - bankNifty.MarketTime >= MissingInstrumentTolerance))
        {
            affected.Add("BANKNIFTY");
        }

        if (affected.Count == 0
            && newestMarketTime.HasValue
            && nowIst - newestMarketTime.Value >= StaleFeedTolerance)
        {
            affected.AddRange(new[] { "NIFTY", "BANKNIFTY" });
        }

        bool unidentifiedIsNew = newestUnidentified is not null
            && DateTime.UtcNow - newestUnidentified.File.LastWriteTimeUtc >= ReportArrivalGrace
            && (newestReport is null
                || newestUnidentified.File.LastWriteTimeUtc
                    >= newestReport.File.LastWriteTimeUtc.AddMinutes(-2));
        if (affected.Count > 0)
        {
            string detail = unidentifiedIsNew
                ? "A recent screenshot was saved as MARKET because its instrument title or table time was not recognized."
                : "One or both expected index reports are late or missing.";
            return CreateFeedError(affected, nifty, bankNifty, detail);
        }

        return new MarketDataHealth(
            MarketDataHealthLevel.Healthy,
            "Market data health — OK",
            BuildFeedTimesText(nifty, bankNifty),
            "healthy",
            null);
    }

    private bool ShouldRaiseFeedError(bool marketHours)
    {
        return marketHours
            && _monitorService.IsRunning
            && _monitorStartedAtUtc.HasValue
            && DateTimeOffset.UtcNow - _monitorStartedAtUtc.Value >= DataHealthStartupGrace;
    }

    private static InstrumentReport? TryReadInstrumentReport(string path)
    {
        try
        {
            using var reader = new StreamReader(path);
            string header = reader.ReadLine() ?? string.Empty;
            string instrument = header.StartsWith("BANKNIFTY ", StringComparison.OrdinalIgnoreCase)
                ? "BANKNIFTY"
                : header.StartsWith("NIFTY ", StringComparison.OrdinalIgnoreCase)
                    ? "NIFTY"
                    : header.StartsWith("MARKET ", StringComparison.OrdinalIgnoreCase)
                        ? "MARKET"
                        : string.Empty;
            if (instrument.Length == 0)
            {
                return null;
            }

            string? timeLine = null;
            for (int index = 0; index < 6; index++)
            {
                string? line = reader.ReadLine();
                if (line is null)
                {
                    break;
                }
                if (line.StartsWith("Latest table time:", StringComparison.OrdinalIgnoreCase))
                {
                    timeLine = line;
                    break;
                }
            }

            Match match = LatestTableTimePattern.Match(timeLine ?? string.Empty);
            string? dayText = Directory.GetParent(path)?.Name;
            if (!match.Success
                || !DateTime.TryParseExact(
                    $"{dayText} {match.Groups["hour"].Value}:{match.Groups["minute"].Value}",
                    "yyyy-MM-dd H:mm",
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.None,
                    out DateTime marketDateTime))
            {
                return null;
            }

            return new InstrumentReport(
                instrument,
                new DateTimeOffset(
                    DateTime.SpecifyKind(marketDateTime, DateTimeKind.Unspecified),
                    MarketTimeZoneOffset),
                new FileInfo(path));
        }
        catch (IOException)
        {
            return null;
        }
    }

    private static MarketDataHealth CreateFeedError(
        IReadOnlyCollection<string> affectedInstruments,
        InstrumentReport? nifty,
        InstrumentReport? bankNifty,
        string detail)
    {
        string affected = affectedInstruments.Count == 0
            ? "NIFTY / BANKNIFTY"
            : string.Join(" and ", affectedInstruments.Distinct());
        string times = BuildFeedTimesText(nifty, bankNifty);
        string status = $"{affected} data is not coming properly on the dashboard. {times}";
        string message = $"{affected} data is not coming properly on the dashboard.\n\n"
            + $"{times}\n\n{detail}\n\n"
            + "Please check the Telegram source images and OCR debug files. "
            + "Make sure the NIFTY/BANKNIFTY title and Intraday Trend time are fully visible.";
        string key = $"{affected}|{nifty?.MarketTime:HH:mm}|{bankNifty?.MarketTime:HH:mm}|{detail}";
        return new MarketDataHealth(
            MarketDataHealthLevel.Error,
            "Market data health — ERROR",
            status,
            key,
            message);
    }

    private static string BuildFeedTimesText(
        InstrumentReport? nifty,
        InstrumentReport? bankNifty)
    {
        string niftyTime = nifty is null ? "not received" : $"{nifty.MarketTime:HH:mm} IST";
        string bankNiftyTime = bankNifty is null ? "not received" : $"{bankNifty.MarketTime:HH:mm} IST";
        return $"Latest NIFTY: {niftyTime}. Latest BANKNIFTY: {bankNiftyTime}.";
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
        if (_allowClose)
        {
            DisposeResources();
            return;
        }

        if (_monitorService.IsRunning)
        {
            e.Cancel = true;
            await _monitorService.StopAsync(TimeSpan.FromSeconds(3));
            _allowClose = true;
            Close();
            return;
        }

        DisposeResources();
    }

    private void DisposeResources()
    {
        _dataHealthTimer.Stop();
        _reportWatcher?.Dispose();
        _imageWatcher?.Dispose();
        _monitorService.Dispose();
    }

    private enum MarketDataHealthLevel
    {
        Waiting,
        Healthy,
        Warning,
        Error,
    }

    private sealed record InstrumentReport(
        string Instrument,
        DateTimeOffset MarketTime,
        FileInfo File);
    private sealed record MarketDataHealth(
        MarketDataHealthLevel Level,
        string Title,
        string StatusMessage,
        string AlertKey,
        string? AlertMessage);
    private sealed record AppSettings(string PythonExecutable);
    private sealed record UpdateStatus(bool Succeeded, string Message, DateTimeOffset Timestamp);
}
