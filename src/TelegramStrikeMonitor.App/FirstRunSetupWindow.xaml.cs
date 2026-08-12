using System.Diagnostics;
using System.IO;
using System.Windows;

namespace TelegramStrikeMonitor.App;

public partial class FirstRunSetupWindow : Window
{
    private readonly string _engineFolder;
    private readonly string _engineExecutable;

    public FirstRunSetupWindow(string engineFolder, string engineExecutable)
    {
        InitializeComponent();
        _engineFolder = engineFolder;
        _engineExecutable = engineExecutable;
        LoadExistingSettings();
    }

    public bool SetupCompleted { get; private set; }

    private string EnvPath => Path.Combine(_engineFolder, ".env");
    private string SessionPath => Path.Combine(_engineFolder, "autotrend_session.session");

    private void LoadExistingSettings()
    {
        IReadOnlyDictionary<string, string> settings = ReadDotEnv(EnvPath);
        if (settings.TryGetValue("TELEGRAM_API_ID", out string? apiId)
            && !apiId.Equals("YOUR_API_ID", StringComparison.OrdinalIgnoreCase))
        {
            ApiIdTextBox.Text = apiId;
        }
        if (settings.TryGetValue("TELEGRAM_API_HASH", out string? apiHash)
            && !apiHash.Equals("YOUR_API_HASH", StringComparison.OrdinalIgnoreCase))
        {
            ApiHashPasswordBox.Password = apiHash;
        }
        if (settings.TryGetValue("TELEGRAM_CHANNEL", out string? channel)
            && !string.IsNullOrWhiteSpace(channel))
        {
            ChannelTextBox.Text = channel;
        }
        if (settings.TryGetValue("TELEGRAM_REPORT_TARGET", out string? target)
            && !string.IsNullOrWhiteSpace(target))
        {
            ReportTargetTextBox.Text = target;
        }
    }

    private async void ConfigureButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            string apiId = ApiIdTextBox.Text.Trim();
            string apiHash = ApiHashPasswordBox.Password.Trim();
            string channel = ChannelTextBox.Text.Trim().TrimStart('@');
            string reportTarget = ReportTargetTextBox.Text.Trim();

            ValidateSettings(apiId, apiHash, channel, reportTarget);
            Directory.CreateDirectory(_engineFolder);
            File.WriteAllLines(EnvPath, new[]
            {
                $"TELEGRAM_API_ID={apiId}",
                $"TELEGRAM_API_HASH={apiHash}",
                $"TELEGRAM_CHANNEL={channel}",
                $"TELEGRAM_REPORT_TARGET={reportTarget}",
            });

            string authorizationScript = Path.Combine(_engineFolder, "authorize_telegram.py");
            if (!File.Exists(authorizationScript))
            {
                throw new FileNotFoundException("The Telegram authorization helper is missing.", authorizationScript);
            }
            if (!File.Exists(_engineExecutable))
            {
                throw new FileNotFoundException("The bundled Python executable is missing.", _engineExecutable);
            }

            ConfigureButton.IsEnabled = false;
            SetupStatusText.Text = "Complete Telegram authorization in the console window that just opened...";

            var startInfo = new ProcessStartInfo
            {
                FileName = "cmd.exe",
                WorkingDirectory = _engineFolder,
                UseShellExecute = true,
                WindowStyle = ProcessWindowStyle.Normal,
            };
            startInfo.ArgumentList.Add("/c");
            startInfo.ArgumentList.Add(_engineExecutable);
            startInfo.ArgumentList.Add(authorizationScript);

            using Process? authorization = Process.Start(startInfo);
            if (authorization is null)
            {
                throw new InvalidOperationException("The Telegram authorization console could not be opened.");
            }

            await authorization.WaitForExitAsync();
            if (authorization.ExitCode != 0 || !File.Exists(SessionPath))
            {
                throw new InvalidOperationException(
                    "Telegram authorization was not completed. Check the console message and try again.");
            }

            SetupCompleted = true;
            SetupStatusText.Text = "Telegram authorization completed successfully.";
            DialogResult = true;
        }
        catch (Exception exception)
        {
            SetupStatusText.Text = "Setup failed: " + exception.Message;
            ConfigureButton.IsEnabled = true;
            MessageBox.Show(
                exception.Message,
                "First-time setup failed",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
        }
    }

    private static void ValidateSettings(
        string apiId,
        string apiHash,
        string channel,
        string reportTarget)
    {
        if (!long.TryParse(apiId, out _) || apiId.Length < 4)
        {
            throw new InvalidOperationException("Enter a valid numeric Telegram API ID.");
        }
        if (apiHash.Length < 16 || apiHash.Any(char.IsWhiteSpace))
        {
            throw new InvalidOperationException("Enter a valid Telegram API hash without spaces.");
        }
        if (string.IsNullOrWhiteSpace(channel) || channel.Any(char.IsWhiteSpace))
        {
            throw new InvalidOperationException("Enter a valid Telegram source channel username.");
        }
        if (string.IsNullOrWhiteSpace(reportTarget) || reportTarget.Any(char.IsWhiteSpace))
        {
            throw new InvalidOperationException("Enter 'me' or a valid Telegram report destination.");
        }
    }

    private static IReadOnlyDictionary<string, string> ReadDotEnv(string path)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(path))
        {
            return values;
        }

        foreach (string line in File.ReadLines(path))
        {
            string trimmed = line.Trim();
            if (trimmed.Length == 0 || trimmed.StartsWith('#'))
            {
                continue;
            }
            int separator = trimmed.IndexOf('=');
            if (separator <= 0)
            {
                continue;
            }
            string key = trimmed[..separator].Trim();
            string value = trimmed[(separator + 1)..].Trim().Trim('"', '\'');
            values[key] = value;
        }
        return values;
    }

    private void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        SetupCompleted = false;
        DialogResult = false;
    }
}
