using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Reflection;
using System.Windows.Forms;

namespace AIHub.Desktop
{
    internal static class Tests
    {
        private static int passed;
        private static void Check(bool ok, string name)
        {
            if (!ok) throw new Exception("FAILED: " + name);
            passed++;
            Console.WriteLine("PASS " + name);
        }

        private static void InvalidPort(string file, string json)
        {
            File.WriteAllText(file, json);
            bool rejected = false;
            try { Hub.ReadPort(file); } catch (InvalidDataException) { rejected = true; }
            Check(rejected, "reject invalid config " + json);
        }

        private static void QuoteRoundTrip(string value)
        {
            int count;
            IntPtr arguments = CommandLineToArgvW("test.exe " + Hub.Quote(value), out count);
            try { Check(count == 2 && Marshal.PtrToStringUni(Marshal.ReadIntPtr(arguments, IntPtr.Size)) == value, "Windows argument round-trip"); }
            finally { LocalFree(arguments); }
        }

        private static void HealthResponse(string body, bool expected)
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            var respond = Task.Run(() =>
            {
                using (var client = listener.AcceptTcpClient())
                using (var stream = client.GetStream())
                {
                    var buffer = new byte[4096];
                    stream.Read(buffer, 0, buffer.Length);
                    var bytes = Encoding.UTF8.GetBytes(body);
                    var header = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + bytes.Length + "\r\nConnection: close\r\n\r\n");
                    stream.Write(header, 0, header.Length);
                    stream.Write(bytes, 0, bytes.Length);
                }
            });
            try { Check(Hub.Healthy(port) == expected, "identify health service " + body); respond.Wait(); }
            finally { listener.Stop(); }
        }

        private static void ColdStart(string folder, string sourceRoot)
        {
            Hub.Root = folder;
            Hub.Cache = Path.Combine(folder, "data", "desktop");
            var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            Hub.Port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            Hub.Url = "http://127.0.0.1:" + Hub.Port + "/";
            File.Copy(Path.Combine(sourceRoot, "launcher.pyw"), Path.Combine(folder, "launcher.pyw"), true);
            File.WriteAllText(Path.Combine(folder, "server.py"),
                "import ctypes,json,sys,threading\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\n" +
                "from pathlib import Path\n" +
                "Path('data/console.txt').write_text(str(ctypes.windll.kernel32.GetConsoleWindow()))\n" +
                "class Handler(BaseHTTPRequestHandler):\n" +
                " def do_GET(self):\n" +
                "  body=b'{\"app\":\"ai-hub\"}'\n" +
                "  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)\n" +
                " def log_message(self,*args): pass\n" +
                "server=HTTPServer(('127.0.0.1',int(sys.argv[sys.argv.index('--port')+1])),Handler)\n" +
                "threading.Timer(4,server.shutdown).start()\nserver.serve_forever()\nserver.server_close()\n");
            CopyDirectory(Path.Combine(sourceRoot, "aihub"), Path.Combine(folder, "aihub"));
            Directory.CreateDirectory(Path.Combine(folder, "tools"));
            File.Copy(Path.Combine(sourceRoot, "tools", "app_update_helper.py"),
                Path.Combine(folder, "tools", "app_update_helper.py"), true);
            WriteTransactionMarker(folder, "helper_waiting", false, 3600);
            Check(!Hub.UpdateStartupPending(), "dead update marker is left for the existing startup recovery path");
            Check(Hub.EnsureService() == "started", "cold startup in isolated Unicode path");
            var recovery = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(
                File.ReadAllText(Path.Combine(folder, "data", "app-updates", "result.json"), Encoding.UTF8));
            Check(!File.Exists(Path.Combine(folder, "data", "app-updates", "install-lock.json")) &&
                Hub.TextValue(recovery, "state") == "recovered_no_write", "stale update marker is recovered before cold service startup");
            Check(Directory.GetFiles(Hub.Cache, "startup-*.json").Length == 0, "real launcher startup receipt is validated and removed");
            string pidRecord = Path.Combine(folder, "data", "server.pid.json");
            string before = File.ReadAllText(pidRecord);
            Check(Hub.EnsureService() == "reused" && File.ReadAllText(pidRecord) == before, "cold-started service reused without replacing PID");
            Check(File.ReadAllText(Path.Combine(folder, "data", "console.txt")) == "0", "background Python has no console window");
            var record = new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<System.Collections.Generic.Dictionary<string, object>>(before);
            using (var process = Process.GetProcessById((int)record["pid"]))
                Check(process.WaitForExit(10000), "owned fixture stops itself cleanly");
        }

        private static Dictionary<string, object> CurrentProcessIdentity()
        {
            using (var process = Process.GetCurrentProcess())
                return new Dictionary<string, object> {
                    { "pid", process.Id },
                    { "start_filetime", process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(System.Globalization.CultureInfo.InvariantCulture) },
                    { "executable_path", process.MainModule.FileName }
                };
        }

        private static Dictionary<string, object> DeadProcessIdentity()
        {
            return new Dictionary<string, object> {
                { "pid", Int32.MaxValue }, { "start_filetime", "1" }, { "executable_path", Path.Combine(Path.GetTempPath(), "missing.exe") }
            };
        }

        private static string WriteTransactionMarker(string root, string state, bool liveHelper, int ageSeconds)
        {
            string updates = Path.Combine(root, "data", "app-updates");
            string id = Guid.NewGuid().ToString("N");
            string directory = Path.Combine(updates, "transactions", id);
            Directory.CreateDirectory(directory);
            var helperIdentity = liveHelper ? CurrentProcessIdentity() : DeadProcessIdentity();
            var transaction = new Dictionary<string, object> {
                { "schema", "ai-hub-update-transaction-v1" }, { "transaction_id", id }, { "state", state },
                { "service_identity", DeadProcessIdentity() }, { "helper_identity", helperIdentity }
            };
            var marker = new Dictionary<string, object> {
                { "schema", "ai-hub-update-lock-v1" }, { "transaction_id", id },
                { "transaction_dir", directory },
                { "created_at", (long)((DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds) - ageSeconds }
            };
            var serializer = new JavaScriptSerializer();
            File.WriteAllText(Path.Combine(directory, "transaction.json"), serializer.Serialize(transaction), new UTF8Encoding(false));
            File.WriteAllText(Path.Combine(updates, "install-lock.json"), serializer.Serialize(marker), new UTF8Encoding(false));
            File.WriteAllText(Path.Combine(updates, "transaction.lock"), "0", new UTF8Encoding(false));
            return id;
        }

        private static void CopyCandidateFixture(string root, string sourceRoot, string candidateExe)
        {
            Directory.CreateDirectory(root);
            Directory.CreateDirectory(Path.Combine(root, "frontend"));
            Directory.CreateDirectory(Path.Combine(root, "data"));
            File.Copy(Path.Combine(sourceRoot, "server.py"), Path.Combine(root, "server.py"));
            File.Copy(Path.Combine(sourceRoot, "launcher.pyw"), Path.Combine(root, "launcher.pyw"));
            File.Copy(Path.Combine(sourceRoot, "frontend", "index.html"), Path.Combine(root, "frontend", "index.html"));
            File.Copy(candidateExe, Path.Combine(root, "AI Hub.exe"));
            var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            int port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            if (port == 8765) throw new InvalidOperationException("The native update fixture must not use the formal service port.");
            File.WriteAllText(Path.Combine(root, "data", "config.json"),
                "{\"server\":{\"port\":" + port + "}}", new UTF8Encoding(false));
        }

        private static void RunCandidateUnderUpdateMarker(string sourceRoot, string candidateExe, bool liveHelper, bool holdByteLock, bool directoryMarker = false, bool directoryLock = false)
        {
            string root = Path.Combine(Path.GetTempPath(), "yaohe-native-update-" + Guid.NewGuid().ToString("N"));
            string id = null;
            Process lockHolder = null;
            try
            {
                CopyCandidateFixture(root, sourceRoot, candidateExe);
                id = WriteTransactionMarker(root, "helper_waiting", liveHelper, 3600);
                if (directoryMarker)
                {
                    string marker = Path.Combine(root, "data", "app-updates", "install-lock.json");
                    File.Delete(marker);
                    Directory.CreateDirectory(marker);
                }
                if (directoryLock)
                {
                    string lockPath = Path.Combine(root, "data", "app-updates", "transaction.lock");
                    File.Delete(lockPath);
                    Directory.CreateDirectory(lockPath);
                }
                if (holdByteLock)
                {
                    string python = Hub.FindPython();
                    string lockPath = Path.Combine(root, "data", "app-updates", "transaction.lock");
                    string code = "import msvcrt,sys,time;f=open(sys.argv[1],'r+b');f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1);print('locked',flush=True);time.sleep(30)";
                    var lockStart = new ProcessStartInfo(python, "-c " + Hub.Quote(code) + " " + Hub.Quote(lockPath)) {
                        UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                        RedirectStandardOutput = true, WorkingDirectory = root
                    };
                    lockHolder = Process.Start(lockStart);
                    var ready = lockHolder.StandardOutput.ReadLineAsync();
                    if (!ready.Wait(5000) || ready.Result != "locked") throw new TimeoutException("Python transaction lock fixture did not become ready.");
                }

                Hub.Root = Hub.NormalizeRoot(root);
                string desktopId = Hub.Identity(Hub.Root.ToUpperInvariant()).Substring(0, 24);
                string mutexName = @"Local\AIHub-desktop-" + desktopId;
                string eventName = @"Local\AIHub-desktop-show-" + desktopId;
                using (var activate = new EventWaitHandle(false, EventResetMode.AutoReset, eventName))
                {
                    var start = new ProcessStartInfo(Path.Combine(root, "AI Hub.exe"), String.Empty) {
                        UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = root
                    };
                    using (var candidate = Process.Start(start))
                    {
                        bool exited = candidate.WaitForExit(5000);
                        if (!exited)
                        {
                            try { candidate.Kill(); candidate.WaitForExit(3000); } catch { }
                        }
                        Check(exited && candidate.ExitCode == 0, "real candidate EXE exits promptly" +
                            (directoryMarker ? " with damaged directory marker" : directoryLock ? " with damaged lock directory" :
                            (holdByteLock ? " with Python byte lock" : " with live helper identity")));
                    }
                    Check(!activate.WaitOne(0), "candidate EXE does not signal activation while update helper owns handoff");
                }
                string keptMarker = Path.Combine(root, "data", "app-updates", "install-lock.json");
                Check(directoryMarker ? Directory.Exists(keptMarker) : File.Exists(keptMarker), "candidate EXE leaves update marker untouched");
                Check(!Directory.Exists(Path.Combine(root, "data", "desktop")), "candidate EXE does not create a desktop profile while update is live");
                using (var mutex = new Mutex(false, mutexName))
                {
                    bool acquired = false;
                    try { acquired = mutex.WaitOne(0); } catch (AbandonedMutexException) { acquired = true; }
                    Check(acquired, "candidate EXE leaves no desktop mutex owner or modal window");
                    if (acquired) mutex.ReleaseMutex();
                }
            }
            finally
            {
                if (lockHolder != null)
                {
                    try { if (!lockHolder.HasExited) lockHolder.Kill(); lockHolder.WaitForExit(3000); } catch { }
                    lockHolder.Dispose();
                }
                if (Directory.Exists(root)) Directory.Delete(root, true);
            }
        }

        private static void CopyDirectory(string source, string destination)
        {
            Directory.CreateDirectory(destination);
            foreach (string file in Directory.GetFiles(source))
                File.Copy(file, Path.Combine(destination, Path.GetFileName(file)), true);
            foreach (string child in Directory.GetDirectories(source))
                CopyDirectory(child, Path.Combine(destination, Path.GetFileName(child)));
        }

        private static Dictionary<string, object> Control(string folder, int port)
        {
            return new Dictionary<string, object> { { "schema", Hub.ControlProtocol }, { "install_root", folder },
                { "port", port }, { "instance_id", "fixture-instance" }, { "token", new string('a', 43) } };
        }

        private static Dictionary<string, object> ControlHealth(string folder)
        {
            return new Dictionary<string, object> { { "app", "ai-hub" }, { "control_protocol", Hub.ControlProtocol },
                { "install_root", folder }, { "service_instance_id", "fixture-instance" } };
        }

        private static void ShutdownFixture(string folder, bool busy)
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            Hub.Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            Hub.Root = Hub.NormalizeRoot(folder);
            Hub.Cache = folder;
            Hub.StartupUnconfirmed = false;
            var serializer = new JavaScriptSerializer();
            File.WriteAllText(Path.Combine(folder, "server-control.json"), serializer.Serialize(Control(Hub.Root, Hub.Port)));
            bool authenticated = false;
            var respond = Task.Run(() =>
            {
                for (int i = 0; i < (busy ? 2 : 3); i++)
                {
                    using (var client = listener.AcceptTcpClient())
                    using (var stream = client.GetStream())
                    {
                        client.ReceiveTimeout = 4000;
                        var requestHeader = new StringBuilder();
                        while (!requestHeader.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
                        {
                            int next = stream.ReadByte();
                            if (next < 0 || requestHeader.Length > 8192) throw new IOException("Invalid fixture request");
                            requestHeader.Append((char)next);
                        }
                        string[] lines = requestHeader.ToString().Split(new[] { "\r\n" }, StringSplitOptions.RemoveEmptyEntries);
                        string request = lines[0];
                        int length = 0;
                        bool token = false;
                        foreach (string line in lines)
                        {
                            if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase)) length = Int32.Parse(line.Substring(15).Trim());
                            if (line == "X-AIHub-Control-Token: " + new string('a', 43)) token = true;
                        }
                        if (i == 1)
                        {
                            var content = new byte[length];
                            int count = 0;
                            while (count < content.Length)
                            {
                                int read = stream.Read(content, count, content.Length - count);
                                if (read == 0) throw new IOException("Incomplete fixture request");
                                count += read;
                            }
                            var payload = serializer.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(content));
                            authenticated = token && request.StartsWith("POST /api/desktop/shutdown ") &&
                                (string)payload["instance_id"] == "fixture-instance" && (string)payload["install_root"] == Hub.Root;
                        }
                        string body = i == 0 ? serializer.Serialize(ControlHealth(Hub.Root)) : i == 2 ? "{\"code\":\"service_stopping\"}" :
                            busy ? "{\"code\":\"service_busy\"}" : "{\"status\":\"stopping\",\"instance_id\":\"fixture-instance\"}";
                        var bytes = Encoding.UTF8.GetBytes(body);
                        string status = i == 0 ? "200 OK" : i == 2 ? "503 Service Unavailable" : busy ? "409 Conflict" : "202 Accepted";
                        var header = Encoding.ASCII.GetBytes("HTTP/1.1 " + status + "\r\nContent-Type: application/json\r\nContent-Length: " + bytes.Length + "\r\nConnection: close\r\n\r\n");
                        stream.Write(header, 0, header.Length);
                        stream.Write(bytes, 0, bytes.Length);
                    }
                }
                listener.Stop();
            });
            try
            {
                var result = Hub.ShutdownService();
                Check(result.Stopped == !busy, busy ? "busy backend keeps tray alive" : "authenticated backend shutdown completes");
                respond.GetAwaiter().GetResult();
                Check(authenticated, "shutdown binds token, instance and install root");
            }
            finally { listener.Stop(); }
        }

        private static void LifecycleTests(string folder)
        {
            Hub.Root = Hub.NormalizeRoot(folder);
            Hub.Cache = folder;
            Hub.Port = 8765;
            Check(Hub.HideOnClose(true, false), "titlebar close hides instead of exiting");
            Check(!Hub.HideOnClose(true, true), "approved tray exit closes desktop");
            Check(!Hub.HideOnClose(false, false), "session shutdown is never redirected to tray");
            var control = Control(Hub.Root, Hub.Port);
            var health = ControlHealth(Hub.Root);
            Check(Hub.ControlMatches(control, health), "matching local shutdown authority accepted");
            health["service_instance_id"] = "replacement-instance";
            Check(!Hub.ControlMatches(control, health), "stale control token cannot target replacement instance");
            health = ControlHealth(Hub.Root);
            health["install_root"] = Path.Combine(folder, "other-install");
            Check(!Hub.ControlMatches(control, health), "different installation cannot be shut down");
            health = ControlHealth(Hub.Root);
            health.Remove("control_protocol");
            Check(!Hub.ControlMatches(control, health), "legacy backend has no implicit shutdown authority");
            string requestId = Guid.NewGuid().ToString("N");
            string receipt = Path.Combine(folder, "startup-" + requestId + ".json");
            var record = new Dictionary<string, object> { { "schema", "ai-hub-desktop-start-v1" }, { "request_id", requestId },
                { "install_root", Hub.Root }, { "child_alive", false } };
            File.WriteAllText(receipt, new JavaScriptSerializer().Serialize(record));
            Hub.StartupUnconfirmed = true;
            Check(Hub.ReadStartupReceipt(requestId) && !Hub.StartupUnconfirmed && !File.Exists(receipt), "failed startup with no child can exit and removes own receipt");
            Hub.StartupUnconfirmed = true;
            Check(!Hub.ReadStartupReceipt(Guid.NewGuid().ToString("N")) && Hub.StartupUnconfirmed, "missing startup receipt preserves uncertainty");
            var childStart = new ProcessStartInfo(Hub.FindPython(), "-c \"import time; time.sleep(1)\"")
                { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
            using (var child = Process.Start(childStart))
            {
                record["child_alive"] = true;
                record["child_pid"] = child.Id;
                record["child_start_filetime"] = child.StartTime.ToUniversalTime().ToFileTimeUtc().ToString();
                File.WriteAllText(receipt, new JavaScriptSerializer().Serialize(record));
                Hub.StartupUnconfirmed = true;
                Check(Hub.ReadStartupReceipt(requestId) && Hub.StartupStillPending(), "late startup binds exact child process identity");
                Check(child.WaitForExit(5000) && !Hub.StartupStillPending(), "late child death permits subsequent exit without killing it");
            }
            using (var child = Process.Start(childStart))
            {
                record["child_pid"] = child.Id;
                record["child_start_filetime"] = (child.StartTime.ToUniversalTime().ToFileTimeUtc() - 1).ToString();
                File.WriteAllText(receipt, new JavaScriptSerializer().Serialize(record));
                Hub.StartupUnconfirmed = true;
                Check(Hub.ReadStartupReceipt(requestId) && !Hub.StartupStillPending() && !child.HasExited, "PID reuse cannot target or terminate a different child");
                Check(child.WaitForExit(5000), "identity mismatch fixture exits itself");
            }
            var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start(); Hub.Port = ((IPEndPoint)probe.LocalEndpoint).Port; probe.Stop();
            Hub.StartupUnconfirmed = true;
            Check(!Hub.ShutdownService().Stopped, "uncertain late startup cannot report success");
            Hub.StartupUnconfirmed = false;
            Check(Hub.ShutdownService().Stopped, "already stopped backend permits desktop exit");
            ShutdownFixture(folder, false);
            ShutdownFixture(folder, true);
        }

        private static void UpdateDialogTests(string renderOutput)
        {
            string origin = "http://127.0.0.1:8765/";
            Check(Hub.IsTrustedUpdateMessageSource(origin + "#/overview", origin + "#/overview", origin), "fixed update request accepts current local SPA document");
            Check(!Hub.IsTrustedUpdateMessageSource(origin + "#/overview", origin + "#/models", origin), "fixed update request rejects stale document source");
            Check(!Hub.IsTrustedUpdateMessageSource("http://127.0.0.1.evil.example:8765/", "http://127.0.0.1.evil.example:8765/", origin), "fixed update request rejects hostname suffix");
            Check(!Hub.IsTrustedUpdateMessageSource("http://127.0.0.1:8766/", "http://127.0.0.1:8766/", origin), "fixed update request rejects another port");
            Check(!Hub.IsTrustedUpdateMessageSource("http://127.0.0.1:8765/api/app-update/status", "http://127.0.0.1:8765/api/app-update/status", origin), "fixed update request rejects API route documents");
            Check(!Hub.IsTrustedUpdateMessageSource(origin + "?source=untrusted", origin + "?source=untrusted", origin), "fixed update request rejects query document");
            Check(AppUpdateDialog.Boolean(new Dictionary<string, object> { { "auto_check", true } }, "auto_check", false), "update preference reads strict true value");
            Check(!AppUpdateDialog.Boolean(new Dictionary<string, object> { { "auto_install", "true" } }, "auto_install", false), "update preference rejects non-boolean truthy value");
            Check(AppUpdateDialog.Number(new Dictionary<string, object> { { "bytes", 52428800L } }, "bytes") == 52428800L, "update size accepts bounded integer metadata");

            using (var dialog = new AppUpdateDialog(true))
            {
                var flags = BindingFlags.NonPublic | BindingFlags.Instance;
                var field = typeof(AppUpdateDialog).GetField("status", flags);
                var render = typeof(AppUpdateDialog).GetMethod("RenderStatus", flags);
                var primary = (Button)typeof(AppUpdateDialog).GetField("actionButton", flags).GetValue(dialog);
                var recheck = (LinkLabel)typeof(AppUpdateDialog).GetField("recheckLink", flags).GetValue(dialog);
                var status = new Dictionary<string, object> { { "state", "available" }, { "bytes", 52428801L }, { "auto_check", false } };
                field.SetValue(dialog, status);
                render.Invoke(dialog, null);
                Check(!primary.Enabled && recheck.Enabled, "oversized candidate still permits manual recheck without enabling download");
                status["state"] = "ready";
                render.Invoke(dialog, null);
                Check(primary.Enabled && recheck.Enabled, "ready candidate retains install and secondary recheck actions");
                status["state"] = "checking";
                render.Invoke(dialog, null);
                Check(!primary.Enabled && !recheck.Enabled, "busy check prevents concurrent manual actions");
            }

            if (String.IsNullOrEmpty(renderOutput)) return;
            using (var dialog = new AppUpdateDialog(true))
            {
                var status = new Dictionary<string, object> {
                    { "state", "ready" }, { "channel", "candidate" }, { "current_version", "2.12.0" },
                    { "latest_version", "2.12.0" }, { "notes", "更新已下载并通过完整性验证。\r\n安装前请保存正在编辑的内容。" },
                    { "bytes", 1835000L }, { "downloaded_bytes", 1835000L }, { "auto_check", true }, { "auto_install", false }
                };
                typeof(AppUpdateDialog).GetField("status", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(dialog, status);
                typeof(AppUpdateDialog).GetMethod("RenderStatus", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(dialog, null);
                dialog.StartPosition = FormStartPosition.Manual;
                dialog.Location = new Point(-2000, -2000);
                dialog.ShowInTaskbar = false;
                dialog.Show();
                Application.DoEvents();
                dialog.PerformLayout();
                using (var bitmap = new Bitmap(dialog.Width, dialog.Height))
                {
                    dialog.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                    Directory.CreateDirectory(Path.GetDirectoryName(renderOutput));
                    bitmap.Save(renderOutput, System.Drawing.Imaging.ImageFormat.Png);
                }
                dialog.Hide();
            }
            Console.WriteLine("Update dialog off-screen preview: " + renderOutput);
        }

        private static int Main(string[] args)
        {
            string folder = Path.Combine(Path.GetTempPath(), "aihub-桌面测试 空间-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(folder);
            try
            {
                Check(Hub.NormalizeRoot(args[0]) == Hub.NormalizeRoot(Path.Combine(args[0], ".")), "app root normalization retains physical directory");
                string aliasArgument = null;
                string candidateExe = null;
                for (int i = 1; i + 1 < args.Length; i++)
                {
                    if (args[i] == "--alias-root") aliasArgument = args[i + 1];
                    if (args[i] == "--candidate-exe") candidateExe = args[i + 1];
                }
                if (!String.IsNullOrEmpty(aliasArgument))
                {
                    string physical = Hub.NormalizeRoot(args[0]);
                    string alias = Hub.NormalizeRoot(aliasArgument);
                    Check(physical == alias, "junction and physical app roots resolve equally");
                    Check(Hub.Identity(physical.ToUpperInvariant()) == Hub.Identity(alias.ToUpperInvariant()), "junction uses same mutex and browser profile identity");
                }
                string file = Path.Combine(folder, "config.json");
                Check(Hub.ReadPort(file) == 8765, "default port without config");
                File.WriteAllText(file, "{\"server\":{\"port\":8999}}", new UTF8Encoding(true));
                Check(Hub.ReadPort(file) == 8999, "custom port and BOM");
                foreach (string json in new[] { "{", "null", "{\"server\":null}", "{\"server\":{\"port\":true}}", "{\"server\":{\"port\":\"8765\"}}", "{\"server\":{\"port\":80}}", "{\"server\":{\"port\":65536}}" }) InvalidPort(file, json);
                string origin = "http://127.0.0.1:8765/";
                Check(Hub.IsLocalPage(origin + "#/models", origin), "local routes retained");
                Check(!Hub.IsLocalPage("http://127.0.0.1:8766/", origin), "different port blocked");
                Check(!Hub.IsLocalPage("http://127.0.0.1.evil.example:8765/", origin), "hostname suffix blocked");
                Check(!Hub.IsLocalPage("http://user@127.0.0.1:8765/", origin), "userinfo blocked");
                Check(!Hub.IsWebLink("file:///C:/Windows/System32/cmd.exe") && !Hub.IsWebLink("javascript:alert(1)") && !Hub.IsWebLink("ms-settings:about"), "non-web schemes blocked");
                Check(Hub.IsWebLink("https://huggingface.co/models"), "external source page allowed");
                foreach (string value in new[] { "", @"C:\资料 空间\AI Hub\", "quote\"embedded", @"C:\AI Hub\launcher.pyw", "abc\\\"def\\" }) QuoteRoundTrip(value);
                Check(!Hub.IsAppRoot(folder), "incomplete installation rejected");
                File.WriteAllText(Path.Combine(folder, "server.py"), "");
                File.WriteAllText(Path.Combine(folder, "launcher.pyw"), "");
                Directory.CreateDirectory(Path.Combine(folder, "frontend"));
                File.WriteAllText(Path.Combine(folder, "frontend", "index.html"), "");
                Check(Hub.IsAppRoot(folder), "app folder recognized");
                HealthResponse("{\"app\":\"ai-hub\"}", true);
                HealthResponse("{\"app\":\"other\"}", false);
                HealthResponse("not-json", false);
                LifecycleTests(folder);
                if (!String.IsNullOrEmpty(candidateExe))
                {
                    RunCandidateUnderUpdateMarker(args[0], candidateExe, true, false);
                    RunCandidateUnderUpdateMarker(args[0], candidateExe, false, true);
                    RunCandidateUnderUpdateMarker(args[0], candidateExe, false, false, true);
                    RunCandidateUnderUpdateMarker(args[0], candidateExe, false, false, false, true);
                }
                string updateRender = null;
                for (int i = 1; i + 1 < args.Length; i++)
                    if (args[i] == "--update-render-output") updateRender = args[i + 1];
                UpdateDialogTests(updateRender);
                ColdStart(folder, args[0]);
                Console.WriteLine("Desktop tests passed: " + passed);
                return 0;
            }
            finally
            {
                // Only the GUID-named fixture created in this test is removed.
                Directory.Delete(folder, true);
            }
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string command, out int count);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr pointer);
    }
}
