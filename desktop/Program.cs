using System;
using System.Diagnostics;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("曜核")]
[assembly: AssemblyDescription("曜核 本地资产与协作管理桌面终端")]
[assembly: AssemblyProduct("曜核")]
[assembly: AssemblyVersion("2.13.11.0")]
[assembly: AssemblyFileVersion("2.13.11.0")]

namespace AIHub.Desktop
{
    internal static class Program
    {
        static Program()
        {
            // The product requires .NET Framework 4.8. Configure modern path IO
            // before any application path access, without changing machine policy.
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        }

        internal static EventWaitHandle ActivateEvent;
        internal static string LoaderFolder;
        private static readonly object libraryLock = new object();
        private static readonly Dictionary<string, Assembly> libraries = new Dictionary<string, Assembly>(StringComparer.Ordinal);
        private static bool resolverRegistered;
        private const string EmbeddedSdkVersion = "1.0.4191.47";

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                if (args.Length != 0 && !(args.Length == 2 && args[0] == "--root"))
                    throw new ArgumentException("支持的参数：AI Hub.exe --root <曜核 程序目录>");
                Hub.Root = Hub.NormalizeRoot(args.Length == 2 ? args[1] : AppDomain.CurrentDomain.BaseDirectory);
                if (!Hub.IsAppRoot(Hub.Root))
                    throw new DirectoryNotFoundException("请把 AI Hub.exe 放在曜核 程序文件夹内，与 server.py、launcher.pyw 和 frontend 同级。桌面请使用快捷方式。");
                Hub.Port = Hub.ReadPort(Path.Combine(Hub.Root, "data", "config.json"));
                Hub.Url = "http://127.0.0.1:" + Hub.Port + "/";
                string id = Hub.Identity(Hub.Root.ToUpperInvariant()).Substring(0, 24);
                // Keep the desktop profile with this installation. Packaged launchers
                // can virtualize LocalAppData, producing a different profile from Explorer.
                Hub.Cache = Path.Combine(Hub.Root, "data", "desktop");
                if (Hub.UpdateStartupPending())
                {
                    Hub.Log("desktop_startup_deferred update_transaction");
                    return 0;
                }
                using (var mutex = new Mutex(false, @"Local\AIHub-desktop-" + id))
                using (ActivateEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\AIHub-desktop-show-" + id))
                {
                    bool owner;
                    try { owner = mutex.WaitOne(0); } catch (AbandonedMutexException) { owner = true; }
                    if (!owner) { ActivateEvent.Set(); Hub.Log("desktop_reused"); return 0; }
                    try
                    {
                        // Close the marker-to-mutex race as well. This check is still
                        // before libraries or UI are opened, and the finally releases
                        // the mutex immediately if a handoff appeared in the gap.
                        if (Hub.UpdateStartupPending())
                        {
                            Hub.Log("desktop_startup_deferred update_transaction_after_mutex");
                            return 0;
                        }
                        if (Path.Combine(Hub.Cache, "WebView2").Length >= 260)
                            throw new PathTooLongException("程序目录过深，浏览器工作目录超过支持长度。请将完整软件放到较短的目录，并保留或备份原 data/。项目和资产不需要移动。");
                        Directory.CreateDirectory(Hub.Cache);
                        SetCurrentProcessExplicitAppUserModelID("AIHub.Desktop");
                        PrepareLibraries();
                        Hub.Log("desktop_started pid=" + Process.GetCurrentProcess().Id);
                        RunWindow();
                        return 0;
                    }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            catch (Exception e)
            {
                Hub.Log("desktop_error " + e.GetType().Name + ": " + e.Message);
                MessageBox.Show(e.Message, "曜核 · 启动提示", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        private static byte[] Resource(string name)
        {
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            using (var memory = new MemoryStream())
            {
                if (source == null) throw new InvalidDataException("EXE 内缺少运行组件：" + name);
                source.CopyTo(memory);
                return memory.ToArray();
            }
        }

        private static string ComponentIOPath(string path)
        {
            // Keep the installation identity and profile unchanged. Only immutable
            // component IO receives the Win32 extended path form when necessary.
            if (path.Length < 240 || path.StartsWith(@"\\?\", StringComparison.Ordinal)) return path;
            return path.StartsWith(@"\\", StringComparison.Ordinal) ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
        }

        private static string ComponentIdentityPath(string path)
        {
            if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + path.Substring(8);
            return path.StartsWith(@"\\?\", StringComparison.Ordinal) ? path.Substring(4) : path;
        }

        private static string ComponentFolder(string kind, string digest, string filename)
        {
            string folder = Path.Combine(Hub.Cache, kind, digest);
            if (Path.Combine(folder, filename).Length < 240) return folder;
            // Framework assembly binding rejects extended paths even when file IO
            // supports them. Keep only immutable runtime components in a shorter,
            // installation-scoped cache; never move the profile or private data.
            string installation = Hub.Identity(Hub.Root.ToUpperInvariant()).Substring(0, 24);
            folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AIHub", "components", installation, kind, digest);
            if (Path.Combine(folder, filename).Length >= 240)
                throw new PathTooLongException("运行组件缓存路径过长。请将程序放到较短的目录，再保留原 data/ 启动。");
            return folder;
        }

        private static void PrepareLibraries()
        {
            lock (libraryLock)
            {
                if (!resolverRegistered)
                {
                    AppDomain.CurrentDomain.AssemblyResolve += ResolveLibrary;
                    resolverRegistered = true;
                }
                // Load into one binding context before the JIT sees HubWindow. Loading
                // the same resource repeatedly with Assembly.Load(byte[]) creates
                // separate type identities, even for identical FullName and MVID.
                LoadLibrary("Microsoft.Web.WebView2.Core");
                LoadLibrary("Microsoft.Web.WebView2.WinForms");
            }
            byte[] loader = Resource("WebView2Loader.dll");
            string digest;
            using (var sha = System.Security.Cryptography.SHA256.Create())
                digest = BitConverter.ToString(sha.ComputeHash(loader)).Replace("-", "");
            LoaderFolder = ComponentFolder("loader", digest, "WebView2Loader.dll");
            Directory.CreateDirectory(ComponentIOPath(LoaderFolder));
            string target = ComponentIOPath(Path.Combine(LoaderFolder, "WebView2Loader.dll"));
            if (!File.Exists(target)) File.WriteAllBytes(target, loader);
            else
            {
                using (var sha = System.Security.Cryptography.SHA256.Create())
                    if (BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(target))).Replace("-", "") != digest)
                        File.WriteAllBytes(target, loader);
            }
        }

        private static Assembly ResolveLibrary(object sender, ResolveEventArgs e)
        {
            var requested = new AssemblyName(e.Name);
            if (requested.Name != "Microsoft.Web.WebView2.Core" && requested.Name != "Microsoft.Web.WebView2.WinForms") return null;
            lock (libraryLock)
            {
                var library = LoadLibrary(requested.Name);
                var actual = library.GetName();
                if (requested.Version != null && requested.FullName != actual.FullName)
                    throw new FileLoadException("WebView2 组件版本不匹配：" + requested.FullName);
                return library;
            }
        }

        private static Assembly LoadLibrary(string name)
        {
            Assembly library;
            if (libraries.TryGetValue(name, out library)) return library;
            byte[] bytes = Resource(name + ".dll");
            string digest;
            using (var sha = System.Security.Cryptography.SHA256.Create())
                digest = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "");
            string folder = ComponentFolder("sdk", digest, name + ".dll");
            Directory.CreateDirectory(ComponentIOPath(folder));
            string target = ComponentIOPath(Path.Combine(folder, name + ".dll"));
            bool valid = false;
            if (File.Exists(target))
                using (var sha = System.Security.Cryptography.SHA256.Create())
                    valid = BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(target))).Replace("-", "") == digest;
            if (!valid) File.WriteAllBytes(target, bytes);
            var expected = AssemblyName.GetAssemblyName(target);
            if (expected.Name != name || expected.Version.ToString() != EmbeddedSdkVersion)
                throw new FileLoadException("EXE 内 WebView2 组件身份不匹配：" + name);
            // AssemblyResolve cannot intercept a DLL found by normal probing. Check
            // every adjacent candidate before binding; identical SDK bytes may use
            // the default context, while stale/replaced files must fail closed.
            string nearby = null;
            foreach (string directory in new[] { AppDomain.CurrentDomain.BaseDirectory,
                Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) })
            {
                string adjacent = ComponentIOPath(Path.Combine(directory, name + ".dll"));
                if (!File.Exists(adjacent)) continue;
                using (var sha = System.Security.Cryptography.SHA256.Create())
                    if (BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(adjacent))).Replace("-", "") != digest)
                        throw new FileLoadException("程序旁存在与内嵌版本不匹配的 WebView2 组件：\n" + adjacent +
                            "\n\n请将完整新包解压到新的空程序目录，并按升级说明保留 data/。不要删除项目、原目录或数据。");
                // Long adjacent SDK paths were verified above, but the Framework
                // binder cannot consume their extended form. Bind the same verified
                // bytes from the short component cache instead.
                if (nearby == null && ComponentIdentityPath(adjacent).Length < 240) nearby = adjacent;
            }
            // Do not silently accept a component supplied by a neighboring DLL or
            // an earlier resolver. Such an instance may already define other types.
            foreach (var loaded in AppDomain.CurrentDomain.GetAssemblies())
                if (loaded.GetName().Name == name)
                    throw new FileLoadException("WebView2 组件已从非受控位置载入：" + name);
            if (nearby != null) target = nearby;
            library = nearby != null ? Assembly.Load(expected) : Assembly.LoadFrom(target);
            if (library.FullName != expected.FullName || !String.Equals(ComponentIdentityPath(library.Location), ComponentIdentityPath(target), StringComparison.OrdinalIgnoreCase))
                throw new FileLoadException("WebView2 组件载入身份不匹配：" + name);
            libraries.Add(name, library);
            // Establish the default full-name binding as well, before a normal
            // reference from Program/HubWindow can probe and create another copy.
            if (!Object.ReferenceEquals(library, Assembly.Load(library.FullName)))
                throw new FileLoadException("WebView2 组件绑定出现重复实例：" + name);
            return library;
        }

        // Resolve embedded WebView2 assemblies before the JIT sees the window type.
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void RunWindow()
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(ComponentIOPath(LoaderFolder));
            using (var window = new HubWindow()) Application.Run(window);
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern int SetCurrentProcessExplicitAppUserModelID(string id);
    }

    internal sealed class WindowStateRecord
    {
        public int X { get; set; }
        public int Y { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
        public bool Maximized { get; set; }
    }

    internal sealed class HubWindow : Form
    {
        private enum UpdateAttempt { Installed, Skipped, Cancelled, Blocked }

        private WebView2 web;
        private readonly Label failure;
        private readonly StartupAnimation startupAnimation;
        private readonly RegisteredWaitHandle activation;
        private readonly CancellationTokenSource closing = new CancellationTokenSource();
        private readonly NotifyIcon tray;
        private readonly ContextMenuStrip trayMenu;
        private readonly ToolStripMenuItem retryItem;
        private readonly ToolStripMenuItem updateItem;
        private readonly ToolStripMenuItem executionAccessItem;
        private readonly ToolStripMenuItem exitItem;
        private AppUpdateDialog updateDialog;
        private ExecutionAccessDialog executionAccessDialog;
        private AppUpdateInstall activeInstall;
        private Task<string> serviceStartup;
        private bool initializing;
        private bool initializationInterrupted;
        private bool exiting;
        private bool exitApproved;
        private bool resourcesReleased;
        private bool loaded;
        private bool installingUpdate;
        private readonly AppUpdateUiReady uiReady = new AppUpdateUiReady();

        internal HubWindow()
        {
            Text = "曜核 · 本地资产与协作管理";
            BackColor = Color.FromArgb(18, 22, 41);
            ForeColor = Color.FromArgb(242, 239, 230);
            Font = new Font("Microsoft YaHei UI", 10F);
            AutoScaleMode = AutoScaleMode.Dpi;
            MinimumSize = new Size(860, 600);
            ClientSize = new Size(1360, 880);
            StartPosition = FormStartPosition.CenterScreen;
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico"))
                Icon = new Icon(source);
            trayMenu = new ContextMenuStrip();
            var openItem = new ToolStripMenuItem("打开曜核");
            openItem.Click += delegate { BringToUser(); };
            retryItem = new ToolStripMenuItem("重试打开工作台") { Enabled = false };
            retryItem.Click += async delegate { BringToUser(); await InitializeSafelyAsync(); };
            updateItem = new ToolStripMenuItem("软件更新…");
            updateItem.Click += delegate { ShowUpdateDialog(); };
            executionAccessItem = new ToolStripMenuItem("协作接入…");
            executionAccessItem.Click += delegate { ShowExecutionAccessDialog(); };
            exitItem = new ToolStripMenuItem("退出曜核（含后台）");
            exitItem.Click += async delegate { await ExitAsync(); };
            trayMenu.Items.Add(openItem);
            trayMenu.Items.Add(retryItem);
            trayMenu.Items.Add(new ToolStripSeparator());
            trayMenu.Items.Add(updateItem);
            trayMenu.Items.Add(executionAccessItem);
            trayMenu.Items.Add(new ToolStripSeparator());
            trayMenu.Items.Add(exitItem);
            tray = new NotifyIcon { Icon = Icon, Text = "曜核 · 本地 AI 工作台", ContextMenuStrip = trayMenu, Visible = true };
            tray.DoubleClick += delegate { BringToUser(); };
            failure = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
                Visible = false, Font = new Font("Microsoft YaHei UI", 15F) };
            Controls.Add(failure);
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico"))
                startupAnimation = new StartupAnimation(source) { Dock = DockStyle.Fill };
            Controls.Add(startupAnimation);
            startupAnimation.BeginLoading();
            startupAnimation.BringToFront();
            RestoreWindow();
            activation = ThreadPool.RegisterWaitForSingleObject(Program.ActivateEvent, delegate
            {
                if (IsDisposed || !IsHandleCreated) return;
                try { BeginInvoke((Action)BringToUser); } catch (InvalidOperationException) { }
            }, null, Timeout.Infinite, false);
            Shown += async delegate { await InitializeSafelyAsync(); };
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            try
            {
                int dark = 1;
                DwmSetWindowAttribute(Handle, 20, ref dark, sizeof(int));
                int caption = 0x00291612;
                DwmSetWindowAttribute(Handle, 35, ref caption, sizeof(int));
            }
            catch (DllNotFoundException) { }
        }

        private void SyncStartupVisibility()
        {
            if (startupAnimation != null)
                startupAnimation.SetHostActive(Visible && WindowState != FormWindowState.Minimized && !exiting && !resourcesReleased);
        }

        private bool StartupInterruptedByExit()
        {
            if (!exiting && !closing.IsCancellationRequested) return false;
            startupAnimation.Complete();
            if (exiting && !closing.IsCancellationRequested) initializationInterrupted = true;
            return true;
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            SyncStartupVisibility();
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            SyncStartupVisibility();
        }

        protected override void WndProc(ref Message message)
        {
            HandleSessionMessage(ref message);
            if (message.Msg == 0x001A && startupAnimation != null)
                startupAnimation.RefreshMotionPreference();
        }

        private void BringToUser()
        {
            if (IsDisposed || closing.IsCancellationRequested) return;
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Show();
            Activate();
            SetForegroundWindow(Handle);
        }

        // A failure while JIT-compiling InitializeAsync occurs before that method's
        // own try/catch. Keep the event boundary free of SDK method signatures.
        private async Task InitializeSafelyAsync()
        {
            try { await InitializeAsync(); }
            catch (Exception error)
            {
                initializing = false;
                if (StartupInterruptedByExit()) return;
                loaded = false;
                Hub.Log("window_initialization_error " + error.GetType().Name + ": " + error.Message);
                ShowFailure("桌面运行组件未能初始化：" + error.Message +
                    "\n\n可从托盘重试；若仍失败，请将完整新包解压到新的空程序目录，并按升级说明保留 data/。不要删除项目或数据。");
            }
        }

        private async Task InitializeAsync()
        {
            if (initializing || exiting || closing.IsCancellationRequested) return;
            initializing = true;
            initializationInterrupted = false;
            retryItem.Enabled = false;
            loaded = false;
            try
            {
                if (web != null) { web.Dispose(); web = null; }
                failure.Visible = false;
                startupAnimation.BeginLoading();
                SyncStartupVisibility();
                startupAnimation.BringToFront();
                serviceStartup = Task.Run(() => Hub.EnsureService());
                string status = await serviceStartup;
                if (StartupInterruptedByExit()) return;
                Hub.Log("service_" + status + " port=" + Hub.Port);
                web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
                Controls.Add(web);
                startupAnimation.BringToFront();
                var options = new CoreWebView2EnvironmentOptions();
                options.Language = "zh-CN";
                var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Hub.Cache, "WebView2"), options);
                if (StartupInterruptedByExit()) return;
                await web.EnsureCoreWebView2Async(environment);
                if (StartupInterruptedByExit()) return;
                var core = web.CoreWebView2;
                core.Profile.PreferredColorScheme = CoreWebView2PreferredColorScheme.Dark;
                core.Settings.IsStatusBarEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = true;
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.AreHostObjectsAllowed = false;
                core.Settings.IsWebMessageEnabled = true;
                core.Settings.IsPasswordAutosaveEnabled = false;
                core.Settings.IsGeneralAutofillEnabled = false;
                core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e)
                {
                    if (Hub.IsLocalPage(e.Uri, Hub.Url)) return;
                    e.Cancel = true;
                    if (e.IsUserInitiated) OpenWebLink(e.Uri);
                };
                core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e)
                {
                    e.Handled = true;
                    if (e.IsUserInitiated) OpenWebLink(e.Uri);
                };
                core.PermissionRequested += delegate(object sender, CoreWebView2PermissionRequestedEventArgs e)
                {
                    // Copying model paths works with the ordinary user-gesture clipboard API.
                    // This app does not require camera, mic, location or clipboard-read access.
                    e.State = CoreWebView2PermissionState.Deny;
                };
                core.WebMessageReceived += delegate(object sender, CoreWebView2WebMessageReceivedEventArgs e)
                {
                    if (closing.IsCancellationRequested || exiting || installingUpdate ||
                        !Hub.IsTrustedUpdateMessageSource(e.Source, core.Source, Hub.Url)) return;
                    try
                    {
                        string message = e.TryGetWebMessageAsString();
                        if (message == "open-app-update") ShowUpdateDialog();
                        else if (message == "open-execution-access") ShowExecutionAccessDialog();
                    }
                    catch { }
                };
                core.DownloadStarting += delegate(object sender, CoreWebView2DownloadStartingEventArgs e)
                {
                    e.Cancel = true;
                    MessageBox.Show(this, "请在浏览器中打开来源页面后下载文件。", "曜核", MessageBoxButtons.OK, MessageBoxIcon.Information);
                };
                core.ProcessFailed += delegate
                {
                    if (!exiting && !closing.IsCancellationRequested) ShowFailure("页面运行环境意外退出。请从托盘菜单选择“重试打开工作台”。");
                };
                core.NavigationCompleted += async delegate(object sender, CoreWebView2NavigationCompletedEventArgs e)
                {
                    if (StartupInterruptedByExit()) return;
                    if (!e.IsSuccess)
                    {
                        if (e.WebErrorStatus != CoreWebView2WebErrorStatus.OperationCanceled)
                            ShowFailure("工作台页面加载失败，请从托盘菜单重试。错误：" + e.WebErrorStatus);
                        return;
                    }
                    startupAnimation.Complete();
                    failure.Visible = false;
                    web.BringToFront();
                    if (!loaded)
                    {
                        loaded = true;
                        Hub.Log("window_ready runtime=" + environment.BrowserVersionString + " console=" + (GetConsoleWindow() != IntPtr.Zero));
                        web.Focus();
                    }
                    await uiReady.NotifyAsync();
                };
                core.Navigate(Hub.Url + "#/overview");
            }
            catch (Exception e)
            {
                if (StartupInterruptedByExit()) return;
                Hub.Log("window_error " + e.GetType().Name + ": " + e.Message);
                ShowFailure(e is WebView2RuntimeNotFoundException ?
                    "未找到 Microsoft Edge WebView2 运行环境。请安装微软官方 WebView2 Runtime 后再打开。" : e.Message);
            }
            finally
            {
                initializing = false;
                if (!closing.IsCancellationRequested) retryItem.Enabled = !exiting && !loaded;
            }
        }

        private void ShowFailure(string message)
        {
            if (web != null) web.Visible = false;
            startupAnimation.Complete();
            failure.Visible = true;
            failure.Text = "曜核\n\n工作台未能打开\n\n请右键任务栏托盘中的曜核图标，重试打开工作台或退出。";
            failure.BringToFront();
            retryItem.Enabled = !exiting;
            if (Visible) MessageBox.Show(this, message, "曜核 · 启动提示", MessageBoxButtons.OK, MessageBoxIcon.Error);
            else tray.ShowBalloonTip(5000, "曜核 · 启动提示", message, ToolTipIcon.Error);
        }

        private async Task ExitAsync()
        {
            if (exiting || closing.IsCancellationRequested) return;
            if (executionAccessDialog != null && executionAccessDialog.IsBusy) { ShowExecutionAccessDialog(); return; }
            exiting = true;
            if (executionAccessDialog != null && !executionAccessDialog.IsDisposed) executionAccessDialog.SetHostAvailable(false);
            SyncStartupVisibility();
            retryItem.Enabled = false;
            executionAccessItem.Enabled = false;
            exitItem.Enabled = false;
            tray.Text = "曜核 · 正在退出后台…";
            try
            {
                // Startup must settle before shutdown. Otherwise its late completion
                // could leave a newly started service behind a removed tray icon.
                if (serviceStartup != null)
                {
                    try { await serviceStartup; } catch { }
                }
                if (closing.IsCancellationRequested) return;
                if (!ClearFinishedUpdateHelper()) return;

                Dictionary<string, object> updateStatus = null;
                try { updateStatus = await Task.Run(() => AppUpdateApi.Status()); }
                catch { /* An older or unreachable service must retain the existing exit path. */ }
                if (AppUpdateDialog.Boolean(updateStatus, "auto_install", false) &&
                    AppUpdateDialog.Value(updateStatus, "state") == "ready")
                {
                    var attempt = await TryInstallReadyUpdateAsync(updateStatus, false, true);
                    if (attempt == UpdateAttempt.Installed || attempt == UpdateAttempt.Cancelled || attempt == UpdateAttempt.Blocked)
                        return;
                }
                if (closing.IsCancellationRequested) return;
                var result = await Task.Run(() => Hub.ShutdownService());
                if (closing.IsCancellationRequested) return;
                if (result.Stopped)
                {
                    exitApproved = true;
                    Hub.Log("desktop_exit service_stopped");
                    Close();
                }
                else
                {
                    Hub.Log("desktop_exit_deferred");
                    tray.ShowBalloonTip(7000, "曜核暂未退出", result.Message, ToolTipIcon.Warning);
                    BringToUser();
                    MessageBox.Show(this, result.Message, "曜核暂未退出", MessageBoxButtons.OK, MessageBoxIcon.Information);
                }
            }
            finally
            {
                if (!closing.IsCancellationRequested)
                {
                    exiting = false;
                    if (executionAccessDialog != null && !executionAccessDialog.IsDisposed) executionAccessDialog.SetHostAvailable(true);
                    if (initializationInterrupted)
                    {
                        initializationInterrupted = false;
                        loaded = false;
                        startupAnimation.Complete();
                        if (web != null) web.Visible = false;
                        failure.Text = "曜核\n\n启动已中断\n\n后台尚未退出，请右键托盘中的曜核图标，选择重试打开工作台。";
                        failure.Visible = true;
                        failure.BringToFront();
                    }
                    SyncStartupVisibility();
                    exitItem.Enabled = true;
                    executionAccessItem.Enabled = true;
                    retryItem.Enabled = !initializing;
                    tray.Text = "曜核 · 本地 AI 工作台";
                }
            }
        }

        internal async Task InstallReadyUpdateAsync(bool automatic)
        {
            if (automatic || exiting || closing.IsCancellationRequested || installingUpdate) return;
            if (executionAccessDialog != null && executionAccessDialog.IsBusy) { ShowExecutionAccessDialog(); return; }
            exiting = true;
            installingUpdate = true;
            if (executionAccessDialog != null && !executionAccessDialog.IsDisposed) executionAccessDialog.SetHostAvailable(false);
            SyncStartupVisibility();
            retryItem.Enabled = false;
            updateItem.Enabled = false;
            executionAccessItem.Enabled = false;
            exitItem.Enabled = false;
            tray.Text = "曜核 · 正在准备软件更新…";
            try
            {
                if (serviceStartup != null) { try { await serviceStartup; } catch { } }
                if (closing.IsCancellationRequested) return;
                Dictionary<string, object> updateStatus = await Task.Run(() => AppUpdateApi.Status());
                await TryInstallReadyUpdateAsync(updateStatus, true, false);
            }
            finally
            {
                installingUpdate = false;
                if (!closing.IsCancellationRequested)
                {
                    exiting = false;
                    if (executionAccessDialog != null && !executionAccessDialog.IsDisposed) executionAccessDialog.SetHostAvailable(true);
                    SyncStartupVisibility();
                    updateItem.Enabled = true;
                    executionAccessItem.Enabled = true;
                    exitItem.Enabled = true;
                    retryItem.Enabled = !initializing && !loaded;
                    tray.Text = "曜核 · 本地 AI 工作台";
                }
            }
        }

        private async Task<UpdateAttempt> TryInstallReadyUpdateAsync(Dictionary<string, object> updateStatus, bool restart, bool automatic)
        {
            bool wasEnabled = web != null && web.Enabled;
            if (web != null) web.Enabled = false;
            try { return await TryInstallReadyUpdateCoreAsync(updateStatus, restart, automatic); }
            finally { if (web != null && !web.IsDisposed) web.Enabled = wasEnabled; }
        }

        private async Task<UpdateAttempt> TryInstallReadyUpdateCoreAsync(Dictionary<string, object> updateStatus, bool restart, bool automatic)
        {
            if (!ClearFinishedUpdateHelper()) return UpdateAttempt.Blocked;
            if (AppUpdateDialog.Value(updateStatus, "state") != "ready")
            {
                if (!automatic) MessageBox.Show(this, "已验证的更新包尚未准备好。请先检查并下载更新。", "曜核 · 软件更新", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return UpdateAttempt.Skipped;
            }

            bool? dirty = await HasUnsavedChangesAsync();
            if (!AppUpdateInstallGuard.CanInstall(dirty))
            {
                BringToUser();
                MessageBox.Show(this, dirty == null ?
                    "目前无法确认页面是否有未保存内容。请保存页面后重试安装。" :
                    "页面有未保存内容。请先保存，再安装更新。曜核和后台保持运行。",
                    "曜核 · 保存页面内容", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return UpdateAttempt.Blocked;
            }

            AppUpdateInstall install = null;
            Exception operationError = null;
            try
            {
                string releaseId = AppUpdateDialog.Value(updateStatus, "release_id");
                install = await Task.Run(() => AppUpdateInstall.PrepareAndStart(releaseId, restart));
                activeInstall = install;
                var result = await Task.Run(() => Hub.ShutdownService());
                if (closing.IsCancellationRequested) return UpdateAttempt.Blocked;
                if (!result.Stopped)
                {
                    bool canceled = await CancelPreparedInstallAsync(install);
                    if (!canceled) return UpdateAttempt.Blocked;
                    MessageBox.Show(this, result.Message, "曜核 · 更新暂未安装", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return UpdateAttempt.Blocked;
                }
                Exception commitError = null;
                try { install.Commit(); }
                catch (Exception error) { commitError = error; }
                if (commitError != null)
                {
                    bool canceled = await CancelPreparedInstallAsync(install);
                    try { await Task.Run(() => Hub.EnsureService()); } catch { }
                    MessageBox.Show(this, canceled ? "无法安全提交更新事务；当前版本已保留，后台已尝试重新启动。" :
                        "无法提交或取消更新事务。为了避免安装助手继续写入，曜核会保持打开。" + Environment.NewLine + commitError.Message,
                        "曜核 · 软件更新", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return canceled ? UpdateAttempt.Skipped : UpdateAttempt.Blocked;
                }
                exitApproved = true;
                Hub.Log(restart ? "desktop_update_committed restart=true" : "desktop_update_committed restart=false");
                Close();
                return UpdateAttempt.Installed;
            }
            catch (Exception error)
            {
                operationError = error;
            }
            if (operationError == null) return UpdateAttempt.Skipped;
            if (install != null)
            {
                bool canceled = await CancelPreparedInstallAsync(install);
                if (!canceled)
                {
                    MessageBox.Show(this, "更新事务仍在等待安全取消。请保持曜核打开，稍后重试。" + Environment.NewLine + operationError.Message,
                        "曜核 · 软件更新", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return UpdateAttempt.Blocked;
                }
            }
            if (!automatic) throw new InvalidOperationException(operationError.Message, operationError);
            return UpdateAttempt.Skipped;
        }

        private async Task<bool> CancelPreparedInstallAsync(AppUpdateInstall install)
        {
            try { await Task.Run(() => install.Cancel()); } catch { }
            bool stopped = await Task.Run(() => install.WaitForHelperExit(5000));
            if (stopped)
            {
                install.Dispose();
                if (ReferenceEquals(activeInstall, install)) activeInstall = null;
            }
            return stopped;
        }

        private bool ClearFinishedUpdateHelper()
        {
            if (activeInstall == null) return true;
            if (!activeInstall.WaitForHelperExit(0))
            {
                MessageBox.Show(this, "更新助手仍在完成事务收尾。请稍后再试，曜核和后台保持运行。", "曜核 · 软件更新", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return false;
            }
            activeInstall.Detach();
            activeInstall = null;
            return true;
        }

        private async Task<bool?> HasUnsavedChangesAsync()
        {
            if (web == null || web.IsDisposed || web.CoreWebView2 == null || !loaded) return null;
            try
            {
                Task<string> evaluation = web.CoreWebView2.ExecuteScriptAsync(
                    "typeof window.aiHubHasUnsavedChanges === 'function' ? window.aiHubHasUnsavedChanges() : null");
                if (await Task.WhenAny(evaluation, Task.Delay(2500)) != evaluation) return null;
                string value = await evaluation;
                if (value == "true") return true;
                if (value == "false") return false;
            }
            catch { }
            return null;
        }

        private void ShowUpdateDialog()
        {
            if (exiting || closing.IsCancellationRequested || installingUpdate) return;
            BringToUser();
            if (updateDialog == null || updateDialog.IsDisposed)
            {
                updateDialog = new AppUpdateDialog();
                updateDialog.InstallRequested += delegate { return InstallReadyUpdateAsync(false); };
                updateDialog.FormClosed += delegate { updateDialog = null; };
            }
            if (!updateDialog.Visible) updateDialog.Show(this);
            updateDialog.BringToFront();
            updateDialog.Activate();
        }

        private void ShowExecutionAccessDialog()
        {
            if (exiting || closing.IsCancellationRequested || installingUpdate) return;
            BringToUser();
            if (executionAccessDialog == null || executionAccessDialog.IsDisposed)
            {
                executionAccessDialog = new ExecutionAccessDialog();
                executionAccessDialog.FormClosed += delegate { executionAccessDialog = null; };
            }
            if (!executionAccessDialog.Visible) executionAccessDialog.Show(this);
            executionAccessDialog.BringToFront();
            executionAccessDialog.Activate();
        }

        private void OpenWebLink(string address)
        {
            if (!Hub.IsWebLink(address)) return;
            try { Process.Start(new ProcessStartInfo(address) { UseShellExecute = true }); }
            catch (Exception e) { Hub.Log("open_link_error " + e.GetType().Name); }
        }

        private void RestoreWindow()
        {
            try
            {
                string file = Path.Combine(Hub.Cache, "window.json");
                if (!File.Exists(file)) return;
                var state = new JavaScriptSerializer().Deserialize<WindowStateRecord>(File.ReadAllText(file));
                var rectangle = new Rectangle(state.X, state.Y, Math.Max(860, state.Width), Math.Max(600, state.Height));
                foreach (Screen screen in Screen.AllScreens)
                {
                    Rectangle visible = Rectangle.Intersect(screen.WorkingArea, rectangle);
                    if (visible.Width < 160 || visible.Height < 100) continue;
                    StartPosition = FormStartPosition.Manual;
                    Bounds = new Rectangle(
                        Math.Max(screen.WorkingArea.Left, Math.Min(rectangle.X, screen.WorkingArea.Right - 160)),
                        Math.Max(screen.WorkingArea.Top, Math.Min(rectangle.Y, screen.WorkingArea.Bottom - 100)),
                        Math.Min(rectangle.Width, screen.WorkingArea.Width), Math.Min(rectangle.Height, screen.WorkingArea.Height));
                    if (state.Maximized) WindowState = FormWindowState.Maximized;
                    return;
                }
            }
            catch { }
        }

        private void HandleSessionMessage(ref Message message)
        {
            // .NET Framework queries owned forms before the owner's OnFormClosing.
            // Mark the query interval first, so a busy child cannot veto session end.
            // Ordinary user close retains its separate tray/busy behavior.
            var dialog = executionAccessDialog;
            if (message.Msg == 0x0011) // WM_QUERYENDSESSION
            {
                if (dialog != null && !dialog.IsDisposed) dialog.SetSystemEnding(true);
                try { base.WndProc(ref message); }
                finally { if (dialog != null && !dialog.IsDisposed) dialog.SetSystemEnding(false); }
                return;
            }
            if (message.Msg == 0x0016 && dialog != null && !dialog.IsDisposed) // WM_ENDSESSION
                dialog.SetSystemEnding(message.WParam != IntPtr.Zero);
            base.WndProc(ref message);
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            base.OnFormClosing(e);
            if (e.Cancel) return;
            SaveWindow();
            if (Hub.HideOnClose(e.CloseReason == CloseReason.UserClosing, exitApproved))
            {
                e.Cancel = true;
                Hide();
                Hub.Log("window_hidden service_preserved");
                return;
            }
            // Session ending and Windows shutdown must never be blocked by an HTTP
            // request or busy service. Windows owns process lifetime in that path.
            ReleaseResources();
            Hub.Log(exitApproved ? "window_closed service_stopped" : "window_closed session_ending");
        }

        private void SaveWindow()
        {
            try
            {
                Rectangle bounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
                var state = new WindowStateRecord { X = bounds.X, Y = bounds.Y, Width = bounds.Width, Height = bounds.Height,
                    Maximized = WindowState == FormWindowState.Maximized };
                File.WriteAllText(Path.Combine(Hub.Cache, "window.json"), new JavaScriptSerializer().Serialize(state), Encoding.UTF8);
            }
            catch { }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) ReleaseResources();
            base.Dispose(disposing);
        }

        private void ReleaseResources()
        {
            if (resourcesReleased) return;
            resourcesReleased = true;
            closing.Cancel();
            if (activeInstall != null) { activeInstall.Detach(); activeInstall = null; }
            if (startupAnimation != null) startupAnimation.Dispose();
            if (activation != null) activation.Unregister(null);
            if (tray != null) { tray.Visible = false; tray.Dispose(); }
            if (trayMenu != null) trayMenu.Dispose();
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hwnd);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetConsoleWindow();
    }
}
