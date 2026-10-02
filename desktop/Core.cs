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
using System.Threading.Tasks;
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

        internal sealed class PythonCandidate
        {
            internal readonly string Executable;
            internal readonly string PrefixArguments;
            internal PythonCandidate(string executable, string prefixArguments = "")
            {
                Executable = executable;
                PrefixArguments = prefixArguments;
            }
        }

        internal static bool HideOnClose(bool userClosing, bool exitApproved)
        {
            return userClosing && !exitApproved;
        }

        internal static string TextValue(Dictionary<string, object> value, string key)
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
        internal static Dictionary<string, object> ReadControlHealth(out bool absent)
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
                        if (status == 409) return new ShutdownResult(false, "本机仍有扫描、更新或数据操作正在执行，已保留托盘和工作台。请等待操作完成后再次退出。");
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

        private static bool IsPlainDirectory(string path)
        {
            var attributes = File.GetAttributes(path);
            return (attributes & FileAttributes.Directory) != 0 && (attributes & FileAttributes.ReparsePoint) == 0;
        }

        private static bool PathDefinitelyAbsent(string path)
        {
            try { File.GetAttributes(path); return false; }
            catch (FileNotFoundException) { return true; }
            catch (DirectoryNotFoundException) { return true; }
            catch { return false; }
        }

        private static Dictionary<string, object> ReadGuardJson(string path, int limit)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                if (stream.Length < 2 || stream.Length > limit) throw new InvalidDataException("update marker size");
                using (var reader = new StreamReader(stream, new UTF8Encoding(false, true)))
                {
                    string text = reader.ReadToEnd();
                    var value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(text);
                    if (value == null) throw new InvalidDataException("update marker shape");
                    return value;
                }
            }
        }

        private static long NumberValue(Dictionary<string, object> value, string name)
        {
            object raw;
            long result;
            if (value == null || !value.TryGetValue(name, out raw) ||
                !(raw is int || raw is long) || !Int64.TryParse(Convert.ToString(raw, System.Globalization.CultureInfo.InvariantCulture), out result))
                throw new InvalidDataException("update marker number");
            return result;
        }

        private static bool IsSameProcess(Dictionary<string, object> identity)
        {
            if (identity == null) return false;
            long pid, expected;
            string start = TextValue(identity, "start_filetime");
            if (!Int64.TryParse(start, out expected) || expected <= 0) return false;
            try { pid = NumberValue(identity, "pid"); } catch { return false; }
            if (pid <= 0 || pid > Int32.MaxValue) return false;
            using (SafeWaitHandle process = OpenProcess(0x00100000 | 0x1000, false, (int)pid))
            {
                if (process == null || process.IsInvalid || process.IsClosed) return false;
                long actual, exited, kernel, user;
                if (!GetProcessTimes(process, out actual, out exited, out kernel, out user)) return false;
                return WaitForSingleObject(process, 0) == 0x00000102 && actual == expected;
            }
        }

        // This probe never starts Python or changes transaction state. Holding the
        // same byte-range lock as msvcrt makes an active helper handoff immediately
        // visible, including the interval before it takes the desktop mutex.
        internal static bool UpdateStartupPending()
        {
            string updates = Path.Combine(Root, "data", "app-updates");
            string marker = Path.Combine(updates, "install-lock.json");
            if (PathDefinitelyAbsent(marker)) return false;
            FileStream transactionLock = null;
            bool locked = false;
            try
            {
                string data = Path.Combine(Root, "data");
                string transactions = Path.Combine(updates, "transactions");
                if (!IsPlainDirectory(data) || !IsPlainDirectory(updates) || !IsPlainDirectory(transactions))
                    return true;
                string lockPath = Path.Combine(updates, "transaction.lock");
                if (!PathDefinitelyAbsent(lockPath))
                {
                    if ((File.GetAttributes(lockPath) & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0) return true;
                    transactionLock = new FileStream(lockPath, FileMode.Open, FileAccess.ReadWrite,
                        FileShare.ReadWrite | FileShare.Delete);
                    try { transactionLock.Lock(0, 1); locked = true; }
                    catch (IOException) { return true; }
                }

                if (PathDefinitelyAbsent(marker)) return false;
                if ((File.GetAttributes(marker) & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0) return true;
                Dictionary<string, object> lockRecord = ReadGuardJson(marker, 32768);
                if (TextValue(lockRecord, "schema") != "ai-hub-update-lock-v1") return true;
                string transactionId = TextValue(lockRecord, "transaction_id");
                if (transactionId == null || transactionId.Length != 32) return true;
                foreach (char value in transactionId) if (!(value >= '0' && value <= '9') && !(value >= 'a' && value <= 'f')) return true;
                string directory = Path.Combine(transactions, transactionId);
                if (!IsPlainDirectory(directory)) return true;
                string statePath = Path.Combine(directory, "transaction.json");
                if ((File.GetAttributes(statePath) & FileAttributes.ReparsePoint) != 0) return true;
                Dictionary<string, object> transaction = ReadGuardJson(statePath, 128 * 1024);
                if (TextValue(transaction, "schema") != "ai-hub-update-transaction-v1" ||
                    TextValue(transaction, "transaction_id") != transactionId) return true;

                // A terminal journal still owns the handoff until the helper exits.
                if (IsSameProcess(transaction.ContainsKey("helper_identity") ? transaction["helper_identity"] as Dictionary<string, object> : null) ||
                    IsSameProcess(transaction.ContainsKey("service_identity") ? transaction["service_identity"] as Dictionary<string, object> : null))
                    return true;

                string state = TextValue(transaction, "state");
                if (state == "succeeded" || state == "failed" || state == "cancelled" ||
                    state == "recovered_no_write" || state == "recovered_rollback") return false;
                if (state == "prepared" || state == "helper_starting")
                {
                    long created = NumberValue(lockRecord, "created_at");
                    long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
                    if (now - created < 90) return true;
                }
                return false;
            }
            catch
            {
                // A damaged marker must never make a native instance hold the
                // desktop mutex while an update may still be in flight.
                return true;
            }
            finally
            {
                if (transactionLock != null)
                {
                    if (locked) { try { transactionLock.Unlock(0, 1); } catch { } }
                    transactionLock.Dispose();
                }
            }
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
        [DllImport("kernel32.dll")]
        private static extern uint GetOEMCP();
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

        internal static bool IsTrustedUpdateMessageSource(string eventSource, string currentSource, string expectedOrigin)
        {
            Uri message, current, origin;
            if (!IsLocalPage(eventSource, expectedOrigin) || !IsLocalPage(currentSource, expectedOrigin) ||
                !Uri.TryCreate(eventSource, UriKind.Absolute, out message) ||
                !Uri.TryCreate(currentSource, UriKind.Absolute, out current) ||
                !Uri.TryCreate(expectedOrigin, UriKind.Absolute, out origin)) return false;
            return String.Equals(message.AbsoluteUri, current.AbsoluteUri, StringComparison.Ordinal) &&
                   message.AbsolutePath == "/" && current.AbsolutePath == "/" &&
                   String.IsNullOrEmpty(message.Query) && String.IsNullOrEmpty(current.Query) &&
                   message.Scheme == "http" && message.Host == "127.0.0.1" &&
                   message.Port == origin.Port && message.UserInfo.Length == 0;
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
                    return TextValue(health, "app") == "ai-hub" &&
                        TextValue(health, "control_protocol") == ControlProtocol &&
                        !String.IsNullOrWhiteSpace(TextValue(health, "service_instance_id")) &&
                        !String.IsNullOrWhiteSpace(TextValue(health, "install_root")) &&
                        String.Equals(NormalizeRoot(TextValue(health, "install_root")), Root, StringComparison.OrdinalIgnoreCase);
                }
            }
            catch { return false; }
        }

        private static bool IsWindowsStoreAlias(string path)
        {
            return !String.IsNullOrWhiteSpace(path) &&
                path.IndexOf("\\WindowsApps\\", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static async Task<byte[]> ReadBoundedBytesAsync(Stream stream, int maximumBytes)
        {
            using (var value = new MemoryStream())
            {
                var buffer = new byte[512];
                int count;
                while ((count = await stream.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                {
                    if (value.Length + count > maximumBytes) throw new InvalidDataException("Python probe output exceeded its limit.");
                    value.Write(buffer, 0, count);
                }
                return value.ToArray();
            }
        }

        internal static string DecodePythonProbeOutput(byte[] output)
        {
            try { return new UTF8Encoding(false, true).GetString(output ?? new byte[0]); }
            catch { return null; }
        }

        internal static IList<string> ParsePythonLauncherBytes(byte[] output)
        {
            byte[] bytes = output ?? new byte[0];
            IList<string> paths = ParsePythonLauncherOutput(DecodePythonProbeOutput(bytes));
            if (paths.Count > 0) return paths;
            var codePages = new List<int> { Encoding.Default.CodePage };
            try
            {
                int oemCodePage = (int)GetOEMCP();
                if (!codePages.Contains(oemCodePage)) codePages.Add(oemCodePage);
            }
            catch { }
            foreach (int codePage in codePages)
            {
                try
                {
                    var encoding = Encoding.GetEncoding(codePage, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback);
                    paths = ParsePythonLauncherOutput(encoding.GetString(bytes));
                    if (paths.Count > 0) return paths;
                }
                catch { }
            }
            return new List<string>();
        }

        internal static string ParsePythonProbeResult(string output)
        {
            try
            {
                using (var reader = new StringReader(output ?? ""))
                {
                    Version version;
                    string versionText = reader.ReadLine();
                    string executable = reader.ReadLine();
                    if (!Version.TryParse(versionText, out version) || version.Major != 3 || version.Minor < 9 ||
                        String.IsNullOrWhiteSpace(executable)) return null;
                    string fullPath = Path.GetFullPath(executable.Trim());
                    return !IsWindowsStoreAlias(fullPath) && File.Exists(fullPath) ? fullPath : null;
                }
            }
            catch { return null; }
        }

        internal static IList<string> ParsePythonLauncherOutput(string output)
        {
            var paths = new List<string>();
            using (var reader = new StringReader(output ?? ""))
            {
                string line;
                while ((line = reader.ReadLine()) != null)
                {
                    for (int i = 0; i + 2 < line.Length; i++)
                    {
                        if (!Char.IsLetter(line[i]) || line[i + 1] != ':' ||
                            (line[i + 2] != '\\' && line[i + 2] != '/')) continue;
                        string path = line.Substring(i).Trim().Trim('"');
                        if (path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) &&
                            !IsWindowsStoreAlias(path) && File.Exists(path)) paths.Add(path);
                        break;
                    }
                }
            }
            return paths;
        }

        private static IEnumerable<string> PathExecutables(string searchPath, string name)
        {
            if (String.IsNullOrWhiteSpace(searchPath)) yield break;
            foreach (string rawDirectory in searchPath.Split(Path.PathSeparator))
            {
                string directory = (rawDirectory ?? "").Trim().Trim('"');
                if (directory.Length == 0 || IsWindowsStoreAlias(directory)) continue;
                string path;
                try { path = Path.GetFullPath(Path.Combine(directory, name)); }
                catch { continue; }
                if (!IsWindowsStoreAlias(path) && File.Exists(path)) yield return path;
            }
        }

        private static IList<string> PythonLauncherExecutables(string launcher)
        {
            var paths = new List<string>();
            if (String.IsNullOrWhiteSpace(launcher) || IsWindowsStoreAlias(launcher) || !HasExecutablePeHeaders(launcher)) return paths;
            try
            {
                var start = new ProcessStartInfo(Path.GetFullPath(launcher), "-0p") {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    RedirectStandardOutput = true
                };
                using (var process = Process.Start(start))
                {
                    if (process == null) return paths;
                    var output = ReadBoundedBytesAsync(process.StandardOutput.BaseStream, 65536);
                    if (!process.WaitForExit(1500))
                    {
                        try { process.Kill(); } catch { }
                        try { process.WaitForExit(500); } catch { }
                        return paths;
                    }
                    if (process.ExitCode != 0) return paths;
                    if (!output.Wait(500) || output.IsFaulted || output.IsCanceled) return paths;
                    return ParsePythonLauncherBytes(output.GetAwaiter().GetResult());
                }
            }
            catch { }
            return paths;
        }

        internal static IEnumerable<PythonCandidate> PythonCandidates(string searchPath)
        {
            yield return new PythonCandidate(Path.Combine(Root, "runtime", "python.exe"));
            var installs = new List<KeyValuePair<Version, string>>();
            foreach (RegistryHive hive in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
            foreach (RegistryView view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
            {
                try
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
                                if (String.IsNullOrWhiteSpace(path) && !String.IsNullOrWhiteSpace(directory)) path = Path.Combine(directory.Trim().Trim('"'), "python.exe");
                                if (!String.IsNullOrWhiteSpace(path)) installs.Add(new KeyValuePair<Version, string>(version, path.Trim().Trim('"')));
                            }
                        }
                    }
                }
                catch { }
            }
            installs.Sort((a, b) => b.Key.CompareTo(a.Key));
            foreach (var entry in installs)
                if (!IsWindowsStoreAlias(entry.Value)) yield return new PythonCandidate(entry.Value);
            foreach (PythonCandidate candidate in PathPythonCandidates(searchPath)) yield return candidate;
        }

        internal static IEnumerable<PythonCandidate> PathPythonCandidates(string searchPath)
        {
            foreach (string path in PathExecutables(searchPath, "python.exe")) yield return new PythonCandidate(path);
            foreach (string path in PathExecutables(searchPath, "python3.exe")) yield return new PythonCandidate(path);
            int launchersChecked = 0;
            foreach (string launcher in PathExecutables(searchPath, "py.exe"))
            {
                if (++launchersChecked > 3) yield break;
                foreach (string path in PythonLauncherExecutables(launcher)) yield return new PythonCandidate(path);
            }
        }

        internal static string FindPython()
        {
            return FindPython(PythonCandidates(Environment.GetEnvironmentVariable("PATH")));
        }

        internal static string FindPython(IEnumerable<PythonCandidate> candidates)
        {
            var timer = Stopwatch.StartNew();
            int examined = 0;
            foreach (PythonCandidate candidate in candidates)
            {
                if (++examined > 32 || timer.ElapsedMilliseconds >= 10000) break;
                int timeout = (int)Math.Min(2500, 10000 - timer.ElapsedMilliseconds);
                string path = ProbePython(candidate, timeout);
                if (!String.IsNullOrWhiteSpace(path)) return path;
            }
            throw new FileNotFoundException("未找到可运行的 Python 3.9 或更新版本。请检查原 Python 安装，或把有效运行环境放到曜核的 runtime 文件夹。");
        }

        // Reject obvious malformed executables before Process.Start, which itself
        // has no managed timeout. This is a bounded header sanity check, not loader
        // validation or proof that starting an otherwise valid image cannot block.
        internal static bool HasExecutablePeHeaders(string path)
        {
            try
            {
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                using (var reader = new BinaryReader(stream))
                {
                    long length = stream.Length;
                    if (length < 64) return false;
                    byte[] dos = reader.ReadBytes(64);
                    if (dos.Length != 64 || dos[0] != 'M' || dos[1] != 'Z') return false;
                    long offset = BitConverter.ToUInt32(dos, 60);
                    if (offset < 64 || offset > length - 24) return false;
                    stream.Position = offset;
                    byte[] coff = reader.ReadBytes(24);
                    if (coff.Length != 24 || BitConverter.ToUInt32(coff, 0) != 0x00004550) return false;
                    int sections = BitConverter.ToUInt16(coff, 6);
                    int optionalSize = BitConverter.ToUInt16(coff, 20);
                    int flags = BitConverter.ToUInt16(coff, 22);
                    if (BitConverter.ToUInt16(coff, 4) == 0 || sections < 1 || sections > 96 ||
                        (flags & 2) == 0 || (flags & 0x2000) != 0 || optionalSize < 96) return false;
                    long table = offset + 24 + optionalSize;
                    long tableEnd = table + sections * 40L;
                    if (tableEnd > length) return false;
                    byte[] optional = reader.ReadBytes(Math.Min(optionalSize, 112));
                    int magic = BitConverter.ToUInt16(optional, 0);
                    // PE32 and PE32+ are both valid: 32-bit Python remains eligible.
                    int minimum = magic == 0x10b ? 96 : magic == 0x20b ? 112 : 0;
                    if (minimum == 0 || optionalSize < minimum || optional.Length < minimum) return false;
                    uint headerSize = BitConverter.ToUInt32(optional, 60);
                    if (headerSize < tableEnd || headerSize > length) return false;
                    stream.Position = table;
                    bool hasPayload = false;
                    for (int i = 0; i < sections; i++)
                    {
                        byte[] section = reader.ReadBytes(40);
                        if (section.Length != 40) return false;
                        uint size = BitConverter.ToUInt32(section, 16);
                        uint start = BitConverter.ToUInt32(section, 20);
                        if (size == 0) continue;
                        if (start < headerSize || (long)start + size > length) return false;
                        hasPayload = true;
                    }
                    return hasPayload;
                }
            }
            catch { return false; }
        }

        private static string ProbePython(PythonCandidate candidate, int timeoutMilliseconds)
        {
            if (candidate == null || String.IsNullOrWhiteSpace(candidate.Executable) ||
                IsWindowsStoreAlias(candidate.Executable) || timeoutMilliseconds <= 0 || !HasExecutablePeHeaders(candidate.Executable))
                return null;
            try
            {
                const string probe = "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(sys.executable)";
                string prefix = String.IsNullOrWhiteSpace(candidate.PrefixArguments) ? "" : candidate.PrefixArguments.Trim() + " ";
                var start = new ProcessStartInfo(Path.GetFullPath(candidate.Executable), prefix + "-c " + Quote(probe)) {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    RedirectStandardOutput = true,
                    RedirectStandardError = false
                };
                start.StandardOutputEncoding = Encoding.UTF8;
                start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                using (var process = Process.Start(start))
                {
                    if (process == null) return null;
                    var output = ReadBoundedBytesAsync(process.StandardOutput.BaseStream, 4096);
                    if (!process.WaitForExit(timeoutMilliseconds))
                    {
                        try { process.Kill(); } catch { }
                        try { process.WaitForExit(500); } catch { }
                        return null;
                    }
                    if (process.ExitCode != 0) return null;
                    if (!output.Wait(500) || output.IsFaulted || output.IsCanceled) return null;
                    return ParsePythonProbeResult(DecodePythonProbeOutput(output.GetAwaiter().GetResult()));
                }
            }
            catch { return null; }
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
