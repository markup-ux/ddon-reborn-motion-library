using System.Collections.Concurrent;
using System.Diagnostics;
using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

internal static class Program
{
    [STAThread]
    private static void Main() => new Application().Run(new ViewerWindow());
}

internal sealed record ViewerSettings(string Root, string Python, string Ddon, string Dd2, string Skilltool, string Revil,
                                      string? NativePython = null)
{
    public static ViewerSettings Read(string file)
    {
        var settings = JsonSerializer.Deserialize<ViewerSettings>(File.ReadAllText(file))
            ?? throw new InvalidDataException("The viewer settings file is empty.");
        foreach (var path in new[] {settings.Root, settings.Python, settings.Ddon, settings.Dd2, settings.Skilltool, settings.Revil})
        {
            if (string.IsNullOrWhiteSpace(path) || !Path.Exists(path))
                throw new FileNotFoundException($"Check the installation paths in {file}. The path '{path}' is unavailable.");
        }
        return settings;
    }

    public ProcessStartInfo StartInfo()
    {
        var start = new ProcessStartInfo(Python)
        {
            WorkingDirectory = Root,
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var arg in new[] {"-X", "utf8", "-B", "viewer_library.py", "--ddon", Ddon, "--dd2", Dd2,
                                   "--skilltool", Skilltool, "--revil", Revil, "--port", "0"})
            start.ArgumentList.Add(arg);
        if (!string.IsNullOrEmpty(NativePython))
        {
            start.ArgumentList.Add("--python315");
            start.ArgumentList.Add(NativePython);
        }
        return start;
    }
}

internal sealed class ViewerWindow : Window
{
    private readonly WebView2 webView = new();
    private readonly TextBlock status = new()
    {
        Text = "Loading the local motion library...",
        Foreground = Brushes.White,
        TextWrapping = TextWrapping.Wrap,
        MaxWidth = 720,
        FontSize = 16,
        Margin = new Thickness(32),
        VerticalAlignment = VerticalAlignment.Center,
        HorizontalAlignment = HorizontalAlignment.Center,
    };
    private readonly CancellationTokenSource lifetime = new();
    private readonly ConcurrentQueue<string> diagnostics = new();
    private Process? backend;
    private string? outfitFile;
    private bool closingOutfit;
    private bool closeAfterSave;

    public ViewerWindow()
    {
        Title = "DDON Reborn Motion Library (Beta)";
        Width = Math.Min(1560, SystemParameters.WorkArea.Width);
        Height = Math.Min(980, SystemParameters.WorkArea.Height);
        MinWidth = 640;
        MinHeight = 480;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = new SolidColorBrush(Color.FromRgb(16, 20, 28));
        var layout = new Grid();
        layout.Children.Add(webView);
        layout.Children.Add(status);
        webView.Visibility = Visibility.Hidden;
        webView.DefaultBackgroundColor = System.Drawing.Color.FromArgb(16, 20, 28);
        Content = layout;
        Loaded += StartViewer;
    }

    private async void StartViewer(object sender, RoutedEventArgs args)
    {
        Loaded -= StartViewer;
        try
        {
            var settings = ViewerSettings.Read(Path.Combine(AppContext.BaseDirectory, "viewer-settings.json"));
            var endpoint = await StartBackend(settings);
            var profile = Path.Combine(settings.Root, "work", "desktop-profile");
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
            outfitFile = Path.Combine(environment.UserDataFolder, "outfit.json");
            if (lifetime.IsCancellationRequested) return;
            await webView.EnsureCoreWebView2Async(environment);
            if (lifetime.IsCancellationRequested) return;
            var core = webView.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.NewWindowRequested += (_, request) => request.Handled = true;
            core.NavigationStarting += (_, request) =>
            {
                if (!Uri.TryCreate(request.Uri, UriKind.Absolute, out var uri) ||
                    uri.Scheme != endpoint.Scheme || uri.Host != endpoint.Host || uri.Port != endpoint.Port)
                    request.Cancel = true;
            };
            core.NavigationCompleted += (_, navigation) =>
            {
                if (navigation.IsSuccess)
                {
                    status.Visibility = Visibility.Collapsed;
                    webView.Visibility = Visibility.Visible;
                }
                else
                    ShowError($"The viewer page did not load. {navigation.WebErrorStatus}");
            };
            if (File.Exists(outfitFile))
            {
                try
                {
                    using var saved = JsonDocument.Parse(File.ReadAllText(outfitFile));
                    var json = JsonSerializer.Serialize(saved.RootElement);
                    await core.AddScriptToExecuteOnDocumentCreatedAsync(
                        $"if (window === window.top) window.savedViewerOutfit = {json};");
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
                {
                    diagnostics.Enqueue($"Saved equipment could not be read. {error.Message}");
                }
            }
            if (!lifetime.IsCancellationRequested)
                core.Navigate(endpoint.AbsoluteUri);
        }
        catch (Exception error)
        {
            if (lifetime.IsCancellationRequested) return;
            StopBackend();
            ShowError(error.Message);
        }
    }

    private async Task<Uri> StartBackend(ViewerSettings settings)
    {
        var ready = new TaskCompletionSource<Uri>(TaskCreationOptions.RunContinuationsAsynchronously);
        var process = new Process { StartInfo = settings.StartInfo(), EnableRaisingEvents = true };
        process.OutputDataReceived += (_, eventArgs) =>
        {
            var match = Regex.Match(eventArgs.Data ?? "", @"^Motion Library at (http://127[.]0[.]0[.]1:[0-9]+) ");
            if (match.Success)
                ready.TrySetResult(new Uri(match.Groups[1].Value));
        };
        process.ErrorDataReceived += (sender, eventArgs) =>
        {
            if (eventArgs.Data is null) return;
            diagnostics.Enqueue(eventArgs.Data);
            if (diagnostics.Count > 12) diagnostics.TryDequeue(out _);
        };
        process.Exited += (_, _) =>
        {
            ready.TrySetException(new InvalidOperationException("The motion library stopped during startup."));
            Dispatcher.BeginInvoke(() =>
            {
                if (!lifetime.IsCancellationRequested && backend == process)
                    ShowError("The motion library has stopped.");
            });
        };
        backend = process;
        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return await ready.Task.WaitAsync(TimeSpan.FromMinutes(5), lifetime.Token);
    }

    private void ShowError(string message)
    {
        webView.Visibility = Visibility.Hidden;
        status.Text = string.Join(Environment.NewLine, new[] {message, string.Empty}.Concat(diagnostics));
        status.Visibility = Visibility.Visible;
    }

    private void StopBackend()
    {
        var process = backend;
        backend = null;
        if (process is null) return;
        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // The process may have exited or failed before it started.
        }
        finally
        {
            process.Dispose();
        }
    }

    protected override void OnClosing(CancelEventArgs args)
    {
        base.OnClosing(args);
        if (args.Cancel || closeAfterSave || outfitFile is null || webView.CoreWebView2 is null) return;
        args.Cancel = true;
        if (closingOutfit) return;
        closingOutfit = true;
        SaveOutfitAndClose();
    }

    private async void SaveOutfitAndClose()
    {
        await Task.Yield();
        string? temporary = null;
        try
        {
            var json = await webView.ExecuteScriptAsync("window.getViewerOutfit?.() ?? null")
                .WaitAsync(TimeSpan.FromSeconds(3));
            using var saved = JsonDocument.Parse(json);
            if (saved.RootElement.ValueKind == JsonValueKind.Object && outfitFile is not null)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(outfitFile)!);
                temporary = outfitFile + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllText(temporary, json);
                File.Move(temporary, outfitFile, overwrite: true);
                temporary = null;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(this, "The current equipment could not be saved. " + error.Message,
                            "DDON Reborn", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
        finally
        {
            if (temporary is not null)
            {
                try { File.Delete(temporary); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            closeAfterSave = true;
            Close();
        }
    }

    protected override void OnClosed(EventArgs args)
    {
        lifetime.Cancel();
        webView.Dispose();
        StopBackend();
        base.OnClosed(args);
    }
}
