using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace AIHub.Desktop
{
    internal static class Hub
    {
        internal static string Root;
        internal static string Cache;
        internal static string Url;
        internal static int Port;
        internal static bool StartupUnconfirmed;
        private static SafeWaitHandle startupChild;
        internal const string ControlProtocol = "ai-hub-local-control-v1";

        internal sealed class ShutdownResult
        {
            internal readonly bool Stopped;
            internal readonly string Message;
            internal ShutdownResult(bool stopped, string message) { Stopped = stopped; Message = message; }
        }

        internal static bool HideOnClose(bool userClosing, bool exitApproved)
        {
            return userClosing && !exitApproved;
        }

        private static string TextValue(Dictionary<string, object> value, string key)
        {
            object result;
            return value != null && value.TryGetValue(key, out result) ? result as string : null;
        }

        private static HttpWebRequest LocalRequest(string route)
        {
            var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + Port + route);
            request.Proxy = null;
            request.AllowAutoRedirect = false;
            request.Timeout = 5000;
            request.ReadWriteTimeout = 2000;
            request.KeepAlive = false;
            return request;
        }

        private static Dictionary<string, object> ReadJson(WebResponse response)
        {
            using (response)
            using (var reader = new StreamReader(response.GetResponseStream()))
            {
                var body = new char[65537];
                int count = 0, read;
                while (count < body.Length && (read = reader.Read(body, count, body.Length - count)) > 0) count += read;
                if (count == body.Length) throw new InvalidDataException("本地服务响应过大。");
                return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(new string(body, 0, count));
            }
        }

        private static bool ConnectionRefused(WebException error)
        {
            for (Exception cause = error; cause != null; cause = cause.InnerException)
            {
                var socket = cause as SocketException;
                if (socket != null && socket.SocketErrorCode == SocketError.ConnectionRefused) return true;
            }
            return false;
        }

        // A failed health request is not proof of a stopped server. Only an explicit
        // refused loopback connection permits the desktop to report shutdown success.
        private static Dictionary<string, object> ReadControlHealth(out bool absent)
        {
            absent = false;
            try { return ReadJson(LocalRequest("/api/health").GetResponse()); }
            catch (WebException e) { if (!ConnectionRefused(e)) throw; absent = true; return null; }
        }

        internal static bool ControlMatches(Dictionary<string, object> control, Dictionary<string, object> health)
        {
            try
            {
                object port;
                string token = TextValue(control, "token");
                string instance = TextValue(control, "instance_id");
                return TextValue(control, "schema") == ControlProtocol &&
                    TextValue(health, "app") == "ai-hub" && TextValue(health, "control_protocol") == ControlProtocol &&
                    !String.IsNullOrEmpty(instance) && instance == TextValue(health, "service_instance_id") &&
                    !String.IsNullOrEmpty(token) && token.Length >= 32 && token.Length <= 256 &&
                    control.TryGetValue("port", out port) && port is int && (int)port == Port &&
                    String.Equals(NormalizeRoot(TextValue(control, "install_root")), Root, StringComparison.OrdinalIgnoreCase) &&
                    String.Equals(NormalizeRoot(TextValue(health, "install_root")), Root, StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        internal static ShutdownResult ShutdownService()
        {
            try
            {
                bool absent;
                var health = ReadControlHealth(out absent);
                if (absent)
                    return StartupStillPending() ? new ShutdownResult(false, "后台启动结果尚未确认，请稍后再次选择退出。可在托盘菜单中重试打开工作台。") : new ShutdownResult(true, "后台已停止。");
                string path = Path.Combine(Cache, "server-control.json");
                if (!File.Exists(path))
                    return new ShutdownResult(false, "当前后台未提供安全退出接口。请先结束旧版后台并重新打开新版曜核。");
                var control = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
                if (!ControlMatches(control, health))
                    return new ShutdownResult(false, "后台身份或版本不匹配，暂未退出。请确认运行的是同一目录中的新版曜核。");
                ClearStartupChild();
                string instance = TextValue(control, "instance_id");
                var request = LocalRequest("/api/desktop/shutdown");
                request.Method = "POST";
                request.ContentType = "application/json";
                request.Headers["X-AIHub-Control-Token"] = TextValue(control, "token");
                byte[] body = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(new { instance_id = instance, install_root = Root }));
                request.ContentLength = body.Length;
                using (var stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
                Dictionary<string, object> result;
                try
                {
                    var response = (HttpWebResponse)request.GetResponse();
                    if (response.StatusCode != HttpStatusCode.Accepted) { response.Dispose(); return new ShutdownResult(false, "后台尚未确认退出，请稍后重试。"); }
                    result = ReadJson(response);
                }
                catch (WebException e)
                {
                    var response = e.Response as HttpWebResponse;
                    if (response != null)
                    {
                        int status = (int)response.StatusCode;
                        response.Dispose();
                        if (status == 409) return new ShutdownResult(false, "后台正在处理任务，已保留托盘和工作台。请等待任务完成后再次退出。");
                    }
                    throw;
                }
                if (TextValue(result, "status") != "stopping" || TextValue(result, "instance_id") != instance)
                    return new ShutdownResult(false, "后台退出响应不匹配，已保留托盘，请稍后重试。");
                var deadline = DateTime.UtcNow.AddSeconds(10);
                while (DateTime.UtcNow < deadline)
                {
                    Thread.Sleep(200);
                    try { health = ReadControlHealth(out absent); }
                    catch (WebException e)
                    {
                        var response = e.Response as HttpWebResponse;
                        if (response == null) throw;
                        int status = (int)response.StatusCode;
                        var failure = ReadJson(response);
                        if (status == 503 && TextValue(failure, "code") == "service_stopping") continue;
                        throw;
                    }
                    if (absent) return new ShutdownResult(true, "后台已停止。");
                    if (TextValue(health, "service_instance_id") != instance)
                        return new ShutdownResult(false, "端口上的后台实例已发生变化，已保留托盘，请重新确认后退出。");
                }
                return new ShutdownResult(false, "后台仍在结束工作，已保留托盘，请稍后再次退出。");
            }
            catch (Exception e)
            {
                Log("service_shutdown_error " + e.GetType().Name);
                return new ShutdownResult(false, "暂时无法确认后台已停止，已保留托盘。请稍后再次退出。");
            }
        }

        internal static string Identity(string value)
        {
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").ToLowerInvariant();
        }

        internal static string NormalizeRoot(string root)
        {
            string full = Path.GetFullPath(root);
            if (!Directory.Exists(full)) return full;
            // Resolve directory junctions before deriving the mutex and browser profile.
            // All compatibility entries must open the same physical app and same window.
            using (var handle = CreateFileW(full, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero))
            {
                if (handle.IsInvalid) throw new IOException("无法读取曜核程序目录。", new Win32Exception(Marshal.GetLastWin32Error()));
                var path = new StringBuilder(512);
                uint length = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0);
                if (length >= path.Capacity)
                {
                    path.EnsureCapacity(checked((int)length + 1));
                    length = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0);
                }
                if (length == 0 || length >= path.Capacity)
                    throw new IOException("无法解析曜核程序目录。", new Win32Exception(Marshal.GetLastWin32Error()));
                full = path.ToString();
                if (full.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) full = @"\\" + full.Substring(8);
                else if (full.StartsWith(@"\\?\", StringComparison.Ordinal)) full = full.Substring(4);
            }
            return full.Length > Path.GetPathRoot(full).Length ? full.TrimEnd('\\', '/') : full;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, uint capacity, uint flags);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeWaitHandle OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(SafeWaitHandle handle, out long created, out long exited, out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(SafeWaitHandle handle, uint timeout);

        internal static bool IsAppRoot(string root)
        {
            return File.Exists(Path.Combine(root, "server.py")) &&
                   File.Exists(Path.Combine(root, "launcher.pyw")) &&
                   File.Exists(Path.Combine(root, "frontend", "index.html"));
        }

        internal static int ReadPort(string path)
        {
            if (!File.Exists(path)) return 8765;
            Dictionary<string, object> config;
            try { config = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8)); }
            catch { throw new InvalidDataException("data/config.json 格式有误，请修复配置后再打开曜核。"); }
            if (config == null) throw new InvalidDataException("data/config.json 必须是配置对象。");
            object server, port;
            if (!config.TryGetValue("server", out server)) return 8765;
            var settings = server as Dictionary<string, object>;
            if (settings == null) throw new InvalidDataException("server 配置必须是对象。");
            if (!settings.TryGetValue("port", out port)) return 8765;
            if (!(port is int) || (int)port < 1024 || (int)port > 65535)
                throw new InvalidDataException("曜核 端口必须是 1024 至 65535 之间的整数。");
            return (int)port;
        }

        internal static bool IsLocalPage(string address, string origin)
        {
            Uri uri, baseUri;
            return Uri.TryCreate(address, UriKind.Absolute, out uri) &&
                   Uri.TryCreate(origin, UriKind.Absolute, out baseUri) &&
                   uri.Scheme == "http" && uri.Host == "127.0.0.1" &&
                   uri.Port == baseUri.Port && uri.UserInfo.Length == 0;
        }

        internal static bool IsWebLink(string address)
        {
            Uri uri;
            return Uri.TryCreate(address, UriKind.Absolute, out uri) &&
                   (uri.Scheme == "http" || uri.Scheme == "https") &&
                   uri.UserInfo.Length == 0 && !String.IsNullOrEmpty(uri.Host);
        }

        // Windows command-line quoting, including trailing backslashes and quotes.
        internal static string Quote(string value)
        {
            var result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                result.Append('\\', slashes * (c == '"' ? 2 : 1));
                if (c == '"') result.Append('\\');
                result.Append(c);
                slashes = 0;
            }
            return result.Append('\\', slashes * 2).Append('"').ToString();
        }

        internal static bool Healthy(int port)
        {
            try
            {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/health");
                request.Proxy = null;
                request.AllowAutoRedirect = false;
                request.Timeout = 1200;
                request.ReadWriteTimeout = 1200;
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    var health = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                    object name;
                    return health != null && health.TryGetValue("app", out name) && (name as string) == "ai-hub";
                }
            }
            catch { return false; }
        }

        private static IEnumerable<string> PythonCandidates()
        {
            yield return Path.Combine(Root, "runtime", "python.exe");
            var installs = new List<KeyValuePair<Version, string>>();
            foreach (RegistryHive hive in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
            foreach (RegistryView view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
            {
                using (var root = RegistryKey.OpenBaseKey(hive, view))
                using (var core = root.OpenSubKey(@"Software\Python\PythonCore"))
                {
                    if (core == null) continue;
                    foreach (string tag in core.GetSubKeyNames())
                    {
                        Version version;
                        if (!Version.TryParse(tag.Split('-')[0], out version) || version.Major != 3 || version.Minor < 9) continue;
                        using (var install = core.OpenSubKey(tag + @"\InstallPath"))
                        {
                            if (install == null) continue;
                            string path = install.GetValue("ExecutablePath") as string;
                            string directory = install.GetValue("") as string;
                            if (String.IsNullOrEmpty(path) && !String.IsNullOrEmpty(directory)) path = Path.Combine(directory, "python.exe");
                            if (!String.IsNullOrEmpty(path)) installs.Add(new KeyValuePair<Version, string>(version, path));
                        }
                    }
                }
            }
            installs.Sort((a, b) => b.Key.CompareTo(a.Key));
            foreach (var entry in installs) yield return entry.Value;
        }

        internal static string FindPython()
        {
            foreach (string path in PythonCandidates()) if (File.Exists(path)) return Path.GetFullPath(path);
            throw new FileNotFoundException("未找到 Python 3.9 或更新版本。请保留原 Python 安装，或把运行环境放到曜核的 runtime 文件夹。");
        }

        internal static bool ReadStartupReceipt(string requestId)
        {
            string file = Path.Combine(Cache, "startup-" + requestId + ".json");
            try
            {
                var record = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(file, Encoding.UTF8));
                object alive;
                if (TextValue(record, "schema") != "ai-hub-desktop-start-v1" || TextValue(record, "request_id") != requestId ||
                    !String.Equals(NormalizeRoot(TextValue(record, "install_root")), Root, StringComparison.OrdinalIgnoreCase) ||
                    !record.TryGetValue("child_alive", out alive) || !(alive is bool)) return false;
                if (!(bool)alive) ClearStartupChild();
                else if (StartupUnconfirmed) CaptureStartupChild(record);
                File.Delete(file);
                return true;
            }
            catch { return false; }
        }

        private static void ClearStartupChild()
        {
            StartupUnconfirmed = false;
            if (startupChild != null) { startupChild.Dispose(); startupChild = null; }
        }

        internal static bool StartupStillPending()
        {
            if (StartupUnconfirmed && startupChild != null)
            {
                if (WaitForSingleObject(startupChild, 0) == 0) ClearStartupChild();
            }
            return StartupUnconfirmed;
        }

        private static void CaptureStartupChild(Dictionary<string, object> record)
        {
            object pid;
            long created;
            if (!record.TryGetValue("child_pid", out pid) || !(pid is int) || (int)pid <= 0 ||
                !Int64.TryParse(TextValue(record, "child_start_filetime"), out created)) return;
            SafeWaitHandle child = null;
            try
            {
                child = OpenProcess(0x00100000 | 0x1000, false, (int)pid); // SYNCHRONIZE | QUERY_LIMITED_INFORMATION
                if (child.IsInvalid)
                {
                    if (Marshal.GetLastWin32Error() == 87) ClearStartupChild(); // Process has already gone.
                    return;
                }
                // Keeping a process handle ties subsequent exit checks to this exact
                // startup child even if Windows later recycles its numeric PID.
                long actualCreated, exited, kernel, user;
                if (!GetProcessTimes(child, out actualCreated, out exited, out kernel, out user)) return;
                if (WaitForSingleObject(child, 0) == 0 || actualCreated != created)
                {
                    ClearStartupChild();
                    return;
                }
                if (startupChild != null) startupChild.Dispose();
                startupChild = child;
                child = null;
            }
            finally { if (child != null) child.Dispose(); }
        }

        internal static string EnsureService()
        {
            if (Healthy(Port)) { ClearStartupChild(); return "reused"; }
            if (StartupStillPending() && startupChild != null)
                throw new InvalidOperationException("本次后台进程仍在启动，请稍后重试。可从托盘菜单再次尝试退出。");
            string python = FindPython();
            string requestId = Guid.NewGuid().ToString("N");
            var start = new ProcessStartInfo(python,
                "-B " + Quote(Path.Combine(Root, "launcher.pyw")) + " --no-browser --no-dialog --port " + Port + " --desktop-result-id " + requestId);
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.WorkingDirectory = Root;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            using (var process = Process.Start(start))
            {
                StartupUnconfirmed = true;
                try
                {
                    // Drain pipes concurrently so a diagnostic message cannot block startup.
                    var output = process.StandardOutput.ReadToEndAsync();
                    var error = process.StandardError.ReadToEndAsync();
                    if (!process.WaitForExit(30000))
                        throw new TimeoutException("后台启动超时，请查看 data/launcher.log 和 data/server.log。稍后重新打开会复用已启动服务。");
                    if (process.ExitCode != 0 || !Healthy(Port))
                        throw new InvalidOperationException("后台服务未能启动。端口可能已被占用，详细原因请查看 data/launcher.log 和 data/server.log。");
                    string text = output.GetAwaiter().GetResult();
                    error.GetAwaiter().GetResult();
                    ClearStartupChild();
                    return text.Contains("\"started\"") ? "started" : "reused";
                }
                finally { ReadStartupReceipt(requestId); }
            }
        }

        internal static void Log(string message)
        {
            try
            {
                string directory = Path.Combine(Root, "data");
                Directory.CreateDirectory(directory);
                File.AppendAllText(Path.Combine(directory, "desktop.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine, Encoding.UTF8);
            }
            catch { }
        }
    }
}
