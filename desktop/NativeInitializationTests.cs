// Runs the final EXE's own Program resolver and HubWindow, without a replacement
// AssemblyResolve handler or SDK references in this test executable.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class NativeInitializationTests
{
    private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    private static int passed;
    private static Exception failure;
    private static void Check(bool condition, string name)
    {
        if (!condition) throw new InvalidOperationException("FAILED: " + name);
        passed++;
        Console.WriteLine("PASS native " + name);
    }

    private static byte[] Digest(byte[] bytes)
    {
        using (var sha = SHA256.Create()) return sha.ComputeHash(bytes);
    }

    // Only this probe's fixture I/O uses an explicit extended path. The child
    // candidate keeps its own logical --root and must enable its own I/O policy.
    private static string FixturePath(string path)
    {
        if (path.StartsWith(@"\\?\", StringComparison.Ordinal)) return path;
        return path.StartsWith(@"\\", StringComparison.Ordinal) ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
    }

    private static void CheckLibraries(Assembly candidate, Type program)
    {
        foreach (string name in new[] { "Microsoft.Web.WebView2.Core", "Microsoft.Web.WebView2.WinForms" })
        {
            Assembly actual = null;
            int count = 0;
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
                if (assembly.GetName().Name == name)
                {
                    actual = assembly; count++;
                    Console.WriteLine("SDK " + assembly.FullName + "; location=" + assembly.Location + "; mvid=" + assembly.ManifestModule.ModuleVersionId);
                }
            Check(count == 1, name + " has exactly one Assembly instance");
            Check(actual.GetName().Version.ToString() == "1.0.4191.47", name + " uses pinned SDK identity");
            using (var resource = candidate.GetManifestResourceStream(name + ".dll"))
            using (var memory = new MemoryStream())
            {
                resource.CopyTo(memory);
                Check(Convert.ToBase64String(Digest(File.ReadAllBytes(actual.Location))) ==
                      Convert.ToBase64String(Digest(memory.ToArray())), name + " uses embedded SDK bytes");
            }
            var resolve = program.GetMethod("ResolveLibrary", Flags);
            var tasks = new Task<Assembly>[8];
            for (int i = 0; i < tasks.Length; i++)
                tasks[i] = Task.Run(() => (Assembly)resolve.Invoke(null, new object[] { null, new ResolveEventArgs(actual.FullName) }));
            Task.WaitAll(tasks);
            foreach (var task in tasks) Check(Object.ReferenceEquals(actual, task.Result), name + " concurrent resolver reuses instance");
            var stale = new AssemblyName(actual.FullName) { Version = new Version("0.0.1.0") };
            bool rejected = false;
            try { resolve.Invoke(null, new object[] { null, new ResolveEventArgs(stale.FullName) }); }
            catch (TargetInvocationException error) { rejected = error.InnerException is FileLoadException; }
            Check(rejected, name + " rejects requested version mismatch");
        }
    }

    private sealed class Fixture : IDisposable
    {
        private readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        private readonly Thread worker;
        private readonly string health;
        internal readonly int Port;
        internal int Pages;
        internal int Ready;
        internal int HealthRequests;
        internal int ProfilePosts;
        internal int RetainedProfiles;
        internal Exception Error;
        private readonly string root;
        private readonly string instance = "native-fixture-" + Guid.NewGuid().ToString("N");
        private const string Token = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        internal Fixture(string root, string cache)
        {
            this.root = root;
            listener.Start();
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            var serial = new JavaScriptSerializer();
            health = serial.Serialize(new Dictionary<string, object> {
                { "app", "ai-hub" }, { "control_protocol", "ai-hub-local-control-v1" },
                { "service_instance_id", instance }, { "install_root", root }
            });
            File.WriteAllText(FixturePath(Path.Combine(root, "data", "config.json")), serial.Serialize(new Dictionary<string, object> {
                { "server", new Dictionary<string, object> { { "port", Port } } }
            }));
            File.WriteAllText(FixturePath(Path.Combine(cache, "server-control.json")), serial.Serialize(new Dictionary<string, object> {
                { "schema", "ai-hub-local-control-v1" }, { "instance_id", instance },
                { "token", Token }, { "port", Port }, { "install_root", root }
            }));
            worker = new Thread(Serve) { IsBackground = true };
            worker.Start();
        }
        private void Serve()
        {
            try
            {
                while (true)
                    using (var client = listener.AcceptTcpClient())
                    using (var stream = client.GetStream())
                    {
                        client.ReceiveTimeout = 3000;
                        var header = new StringBuilder();
                        while (header.Length < 16384 && !header.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
                        {
                            int value = stream.ReadByte();
                            if (value < 0) break;
                            header.Append((char)value);
                        }
                        string request = header.ToString();
                        byte[] payload = new byte[0];
                        foreach (string line in request.Split(new[] { "\r\n" }, StringSplitOptions.None))
                            if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                            {
                                int length = Int32.Parse(line.Substring(15).Trim());
                                if (length < 0 || length > 16384) throw new InvalidDataException("fixture request too large");
                                payload = new byte[length];
                                for (int i = 0; i < length; i++)
                                {
                                    int value = stream.ReadByte();
                                    if (value < 0) throw new EndOfStreamException("fixture request truncated");
                                    payload[i] = (byte)value;
                                }
                            }
                        string first = request.Split(new[] { "\r\n" }, StringSplitOptions.None)[0];
                        Console.WriteLine("FIXTURE " + first);
                        bool page = first.StartsWith("GET / HTTP", StringComparison.Ordinal);
                        if (first.StartsWith("GET /api/health ", StringComparison.Ordinal)) Interlocked.Increment(ref HealthRequests);
                        bool ready = first.StartsWith("POST /api/desktop/update/ui_ready ", StringComparison.Ordinal);
                        if (ready)
                        {
                            var value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(payload));
                            bool authenticated = false;
                            foreach (string line in request.Split(new[] { "\r\n" }, StringSplitOptions.None))
                                if (line.StartsWith("X-AIHub-Control-Token:", StringComparison.OrdinalIgnoreCase))
                                    authenticated = line.Substring("X-AIHub-Control-Token:".Length).Trim() == Token;
                            if (!authenticated || value == null || value.Count != 2 ||
                                !value.ContainsKey("instance_id") || !value.ContainsKey("install_root") ||
                                (string)value["instance_id"] != instance || (string)value["install_root"] != root)
                                throw new InvalidDataException("fixture ui_ready did not authenticate this installation");
                        }
                        if (first.StartsWith("POST /native-entry/profile ", StringComparison.Ordinal))
                        {
                            string value = Encoding.UTF8.GetString(payload);
                            if (value != "fresh" && value != "retained") throw new InvalidDataException("fixture localStorage unavailable");
                            if (value == "retained") Interlocked.Increment(ref RetainedProfiles);
                            Interlocked.Increment(ref ProfilePosts);
                        }
                        string body = first.StartsWith("GET /api/health ", StringComparison.Ordinal) ? health : page ?
                            "<!doctype html><html><body>Isolated native WebView2 initialization fixture<script>" +
                            "let state='error';try{state=localStorage.getItem('native-entry-fixture')==='retained'?'retained':'fresh';" +
                            "localStorage.setItem('native-entry-fixture','retained');}catch{}" +
                            "fetch('/native-entry/profile',{method:'POST',body:state});</script></body></html>" : "{\"status\":\"ready\"}";
                        byte[] bytes = Encoding.UTF8.GetBytes(body);
                        byte[] headers = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: " + (page ? "text/html" : "application/json") +
                            "\r\nContent-Length: " + bytes.Length + "\r\nConnection: close\r\n\r\n");
                        stream.Write(headers, 0, headers.Length);
                        stream.Write(bytes, 0, bytes.Length);
                        if (page) Interlocked.Increment(ref Pages);
                        if (ready) Interlocked.Increment(ref Ready);
                    }
            }
            catch (SocketException) { }
            catch (ObjectDisposedException) { }
            catch (Exception error) { Error = error; }
        }
        public void Dispose() { listener.Stop(); worker.Join(5000); }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        internal int Size; internal string Reserved, Desktop, Title;
        internal uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
        internal short ShowWindow, ReservedSize;
        internal IntPtr ReservedPointer, StandardInput, StandardOutput, StandardError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation { internal IntPtr Process, Thread; internal uint ProcessId, ThreadId; }
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        internal long ProcessTime, JobTime; internal uint Flags;
        internal UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        internal uint ActiveProcessLimit; internal UIntPtr Affinity; internal uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters { internal ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        internal BasicLimitInformation Basic; internal IoCounters Io;
        internal UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct JobAccounting
    {
        internal long TotalUserTime, TotalKernelTime, ThisUserTime, ThisKernelTime;
        internal uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }
    private delegate bool DesktopWindow(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateDesktopW(string name, IntPtr device, IntPtr mode, uint flags, uint access, IntPtr security);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool EnumDesktopWindows(IntPtr desktop, DesktopWindow callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextW(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessageW(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr SendMessageTimeoutW(IntPtr window, uint message,
        IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out UIntPtr result);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity,
        bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfo startup, out ProcessInformation information);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateJobObjectW(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimitInformation information, uint size);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool QueryInformationJobObject(IntPtr job, int kind, out JobAccounting information, uint size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);

    // A private desktop is never switched to or shown. Only this newly created
    // candidate and its descendants enter the job; closing it cannot target any
    // pre-existing user process. No mouse/keyboard or production listener is used.
    private sealed class EntryProcess : IDisposable
    {
        private IntPtr desktop, job, process;
        private uint childId;
        internal bool HasExited { get { return WaitForSingleObject(process, 0) == 0; } }
        internal uint ExitCode
        {
            get
            {
                uint code;
                if (!GetExitCodeProcess(process, out code)) throw new Win32Exception(Marshal.GetLastWin32Error());
                return code;
            }
        }
        internal bool WaitForExit(uint milliseconds) { return WaitForSingleObject(process, milliseconds) == 0; }
        internal EntryProcess(string candidate, string root, string profile)
        {
            string name = "AIHub-native-fixture-" + Guid.NewGuid().ToString("N");
            desktop = CreateDesktopW(name, IntPtr.Zero, IntPtr.Zero, 0, 0x10000000, IntPtr.Zero);
            if (desktop == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot create private fixture desktop");
            ProcessInformation information = new ProcessInformation();
            IntPtr environment = IntPtr.Zero;
            try
            {
                job = CreateJobObjectW(IntPtr.Zero, null);
                if (job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
                var limits = new ExtendedLimitInformation(); limits.Basic.Flags = 0x2000; // KILL_ON_JOB_CLOSE
                if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits))) throw new Win32Exception(Marshal.GetLastWin32Error());
                var entries = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
                    if (!((string)entry.Key).StartsWith("WEBVIEW2_", StringComparison.OrdinalIgnoreCase)) entries[(string)entry.Key] = (string)entry.Value;
                // Scope any installed policy override to synthetic data. This is
                // a child-only environment, not a machine/user setting change.
                entries["WEBVIEW2_USER_DATA_FOLDER"] = profile;
                var block = new StringBuilder();
                foreach (var entry in entries) block.Append(entry.Key).Append('=').Append(entry.Value).Append('\0');
                block.Append('\0'); environment = Marshal.StringToHGlobalUni(block.ToString());
                var startup = new StartupInfo { Size = Marshal.SizeOf(typeof(StartupInfo)), Desktop = name, Flags = 1, ShowWindow = 0 };
                if (!CreateProcessW(candidate, new StringBuilder(Quote(candidate) + " --root " + Quote(root)), IntPtr.Zero, IntPtr.Zero,
                    false, 0x00000004 | 0x00000400, environment, root, ref startup, out information))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot start actual candidate entry point");
                if (!AssignProcessToJobObject(job, information.Process)) throw new Win32Exception(Marshal.GetLastWin32Error());
                process = information.Process; childId = information.ProcessId;
                information.Process = IntPtr.Zero; // Retain the original handle through exit observation.
                if (ResumeThread(information.Thread) == UInt32.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            catch
            {
                IntPtr ownedProcess = process != IntPtr.Zero ? process : information.Process;
                if (ownedProcess != IntPtr.Zero) TerminateProcess(ownedProcess, 1); // Only our still-owned child.
                Dispose(); throw;
            }
            finally
            {
                if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment);
                if (information.Thread != IntPtr.Zero) CloseHandle(information.Thread);
                if (information.Process != IntPtr.Zero) CloseHandle(information.Process);
            }
        }
        internal void CloseWindows(bool sessionEnd)
        {
            uint id = childId;
            int posted = 0;
            DesktopWindow callback = delegate(IntPtr window, IntPtr unused) {
                uint owner; GetWindowThreadProcessId(window, out owner);
                var title = new StringBuilder(256);
                if (owner == id) GetWindowTextW(window, title, title.Capacity);
                if (owner == id && title.ToString().StartsWith("曜核 · ", StringComparison.Ordinal))
                {
                    if (sessionEnd)
                    {
                        // Simulate session-end messages only on this owned hidden
                        // window. This does not request an OS/session shutdown.
                        UIntPtr result;
                        if (SendMessageTimeoutW(window, 0x0011, IntPtr.Zero, new IntPtr(1), 2, 3000, out result) != IntPtr.Zero &&
                            result != UIntPtr.Zero && PostMessageW(window, 0x0016, new IntPtr(1), new IntPtr(1))) posted++;
                    }
                    else if (PostMessageW(window, 0x0010, IntPtr.Zero, IntPtr.Zero)) posted++;
                }
                return true;
            };
            // Startup logs may precede the error dialog by a few milliseconds.
            // EnumDesktopWindows returns false/ERROR_SUCCESS on an empty desktop.
            var elapsed = Stopwatch.StartNew();
            while (!HasExited && posted == 0 && elapsed.Elapsed.TotalSeconds < 5)
            {
                if (!EnumDesktopWindows(desktop, callback, IntPtr.Zero))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error != 0) throw new Win32Exception(error);
                }
                if (posted == 0) Thread.Sleep(100);
            }
            GC.KeepAlive(callback);
        }
        internal void WaitForBrowserExit()
        {
            var elapsed = Stopwatch.StartNew();
            while (elapsed.Elapsed.TotalSeconds < 10)
            {
                JobAccounting accounting;
                if (!QueryInformationJobObject(job, 1, out accounting, (uint)Marshal.SizeOf(typeof(JobAccounting)), IntPtr.Zero))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if (accounting.ActiveProcesses == 0) return;
                Thread.Sleep(100);
            }
            throw new TimeoutException("Owned candidate/browser did not finish closing");
        }
        public void Dispose()
        {
            if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
            if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; }
            if (desktop != IntPtr.Zero) { CloseDesktop(desktop); desktop = IntPtr.Zero; }
        }
    }

    private static string Quote(string value)
    {
        var quoted = new StringBuilder("\""); int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            quoted.Append('\\', character == '"' ? slashes * 2 + 1 : slashes); slashes = 0; quoted.Append(character);
        }
        return quoted.Append('\\', slashes * 2).Append('"').ToString();
    }

    private static string ComponentCache(string root)
    {
        string identity = BitConverter.ToString(Digest(Encoding.UTF8.GetBytes(root.ToUpperInvariant()))).Replace("-", "").ToLowerInvariant().Substring(0, 24);
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AIHub", "components", identity);
    }

    private static void RunEntryPoint(string candidate, string root, string cache, bool expectFailure, bool profileRejection)
    {
        string profile = Path.Combine(cache, "WebView2");
        Console.WriteLine("ENTRY candidate-sha256=" + BitConverter.ToString(Digest(File.ReadAllBytes(candidate))).Replace("-", "") +
            "; root-chars=" + root.Length + "; profile-chars=" + profile.Length + "; desktop=private-hidden; child-profile-override=synthetic-only");
        using (var fixture = new Fixture(root, cache))
        {
            for (int run = 1; run <= (expectFailure ? 1 : 2); run++)
            {
                using (var entry = new EntryProcess(candidate, root, profile))
                {
                    var elapsed = Stopwatch.StartNew();
                    bool failed = false;
                    while (elapsed.Elapsed.TotalSeconds < 30)
                    {
                        if (fixture.Error != null) throw fixture.Error;
                        string log = Path.Combine(root, "data", "desktop.log");
                        if (File.Exists(FixturePath(log)) && File.ReadAllText(FixturePath(log)).Contains("desktop_error PathTooLongException")) { failed = true; break; }
                        if (fixture.Ready >= run && fixture.Pages >= run && fixture.ProfilePosts >= run) break;
                        if (entry.HasExited) break;
                        Thread.Sleep(100);
                    }
                    if (expectFailure)
                    {
                        Check(failed && fixture.Ready == 0 && fixture.Pages == 0, profileRejection ?
                            "candidate rejects unsupported profile length in its own entry" : "legacy candidate fails its own deep-path entry before navigation");
                        if (profileRejection)
                        {
                            Check(profile.Length >= 260, "profile rejection fixture crosses the explicit path boundary");
                            Check(File.ReadAllText(FixturePath(Path.Combine(root, "data", "desktop.log"))).Contains("浏览器工作目录超过支持长度"),
                                "profile rejection reports the product's explicit supported-length boundary");
                            Check(fixture.HealthRequests == 0 && fixture.ProfilePosts == 0, "profile rejection precedes all service and browser startup");
                            Check(!Directory.Exists(FixturePath(profile)) && !Directory.Exists(FixturePath(ComponentCache(root))) &&
                                  !Directory.Exists(FixturePath(Path.Combine(cache, "sdk"))) && !Directory.Exists(FixturePath(Path.Combine(cache, "loader"))),
                                "profile rejection creates no profile or immutable component state");
                            Console.WriteLine(File.ReadAllText(FixturePath(Path.Combine(root, "data", "desktop.log"))));
                        }
                    }
                    else
                    {
                        if (failed || fixture.Ready != run || fixture.Pages != run || fixture.ProfilePosts != run)
                        {
                            string log = Path.Combine(root, "data", "desktop.log");
                            if (File.Exists(FixturePath(log))) Console.Error.WriteLine(File.ReadAllText(FixturePath(log)));
                            Console.Error.WriteLine("ENTRY failed: exited=" + entry.HasExited + "; pages=" + fixture.Pages +
                                "; ready=" + fixture.Ready + "; profile-posts=" + fixture.ProfilePosts + "; exit-code=" + entry.ExitCode);
                        }
                        Check(!failed && fixture.Ready == run && fixture.Pages == run && fixture.ProfilePosts == run,
                            "actual candidate process " + run + " completes navigation, JavaScript and authenticated ui_ready");
                    }
                    entry.CloseWindows(!expectFailure);
                    Check(entry.WaitForExit(10000), "actual candidate process exits after closing only its own windows");
                    Check(entry.ExitCode == (expectFailure ? 1u : 0u), "actual candidate entry exit code is observed");
                    entry.WaitForBrowserExit();
                }
            }
            if (!expectFailure)
            {
                Check(Directory.Exists(FixturePath(profile)), "candidate browser data remains under this installation's synthetic profile");
                Check(fixture.RetainedProfiles == 1, "second independent candidate process retains the original profile localStorage");
                foreach (string name in new[] { "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll" })
                {
                    var files = new List<string>(Directory.GetFiles(FixturePath(cache), name, SearchOption.AllDirectories));
                    string components = ComponentCache(root);
                    if (Directory.Exists(FixturePath(components))) files.AddRange(Directory.GetFiles(FixturePath(components), name, SearchOption.AllDirectories));
                    Check(files.Count == 1, name + " remains in exactly one installation-scoped component cache");
                    string digest = BitConverter.ToString(Digest(File.ReadAllBytes(files[0]))).Replace("-", "");
                    Check(Path.GetFileName(Path.GetDirectoryName(files[0])) == digest, name + " retains its complete SHA256 cache name");
                    Console.WriteLine("ENTRY cache " + name + "; logical-path-chars=" + (files[0].StartsWith(@"\\?\", StringComparison.Ordinal) ? files[0].Length - 4 : files[0].Length));
                }
            }
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        // Reflection modes bypass the candidate's entry point. These switches
        // permit the probe's own fixture I/O; the --entry-point mode starts a new
        // process, whose System.IO policy can only come from the candidate itself.
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        try
        {
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += delegate(object sender, ThreadExceptionEventArgs error) { failure = error.Exception; };
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string root = Path.GetFullPath(args[1]);
            Check(File.Exists(FixturePath(Path.Combine(root, "native-test-fixture.marker"))), "synthetic workspace required");
            string cache = Path.Combine(root, "data", "desktop");
            Directory.CreateDirectory(FixturePath(cache));
            if (args.Length > 2 && args[2] == "--entry-point")
            {
                bool profileRejection = args.Length > 3 && args[3] == "--expect-profile-rejection";
                RunEntryPoint(Path.GetFullPath(args[0]), root, cache, profileRejection || (args.Length > 3 && args[3] == "--expect-entry-failure"), profileRejection);
                Console.WriteLine("Native independent entry assertions passed: " + passed);
                return 0;
            }
            var candidate = Assembly.LoadFrom(Path.GetFullPath(args[0]));
            var hub = candidate.GetType("AIHub.Desktop.Hub", true);
            var program = candidate.GetType("AIHub.Desktop.Program", true);
            hub.GetField("Root", Flags).SetValue(null, root);
            hub.GetField("Cache", Flags).SetValue(null, cache);
            var prepare = program.GetMethod("PrepareLibraries", Flags);
            if (args.Length > 2 && (args[2] == "--preload-conflict" || args[2] == "--stale-sidecars"))
            {
                bool preloaded = args[2] == "--preload-conflict";
                if (preloaded)
                {
                    // Keep this distinct from the adjacent-DLL rejection: inject a
                    // foreign in-memory instance, then remove only fixture sidecars.
                    Assembly.Load(File.ReadAllBytes(Path.Combine(root, "Microsoft.Web.WebView2.Core.dll")));
                    File.Delete(Path.Combine(root, "Microsoft.Web.WebView2.Core.dll"));
                    File.Delete(Path.Combine(root, "Microsoft.Web.WebView2.WinForms.dll"));
                }
                bool rejected = false;
                try { prepare.Invoke(null, null); }
                catch (TargetInvocationException error) { rejected = error.InnerException is FileLoadException; }
                Check(rejected, preloaded ? "preloaded foreign SDK fails closed before HubWindow JIT" :
                    "stale adjacent SDK rejected before HubWindow JIT");
                if (!preloaded)
                {
                    int loaded = 0;
                    foreach (var library in AppDomain.CurrentDomain.GetAssemblies())
                        if (library.GetName().Name.StartsWith("Microsoft.Web.WebView2.", StringComparison.Ordinal)) loaded++;
                    Check(loaded == 0, "stale adjacent SDK never executed");
                }
                return 0;
            }
            prepare.Invoke(null, null);
            prepare.Invoke(null, null);
            CheckLibraries(candidate, program);
            using (var fixture = new Fixture(root, cache))
            using (var activate = new EventWaitHandle(false, EventResetMode.AutoReset))
            {
                hub.GetField("Port", Flags).SetValue(null, fixture.Port);
                hub.GetField("Url", Flags).SetValue(null, "http://127.0.0.1:" + fixture.Port + "/");
                program.GetField("ActivateEvent", Flags).SetValue(null, activate);
                Assembly coreLibrary = null;
                foreach (var library in AppDomain.CurrentDomain.GetAssemblies())
                    if (library.GetName().Name == "Microsoft.Web.WebView2.Core") coreLibrary = library;
                var environment = coreLibrary.GetType("Microsoft.Web.WebView2.Core.CoreWebView2Environment", true);
                object loaderFolder = program.GetField("LoaderFolder", Flags).GetValue(null);
                var componentPath = program.GetMethod("ComponentIOPath", Flags);
                if (componentPath != null) loaderFolder = componentPath.Invoke(null, new[] { loaderFolder });
                environment.GetMethod("SetLoaderDllFolderPath").Invoke(null, new[] { loaderFolder });
                uint browserPid = 0;
                try
                {
                using (var window = (Form)Activator.CreateInstance(candidate.GetType("AIHub.Desktop.HubWindow", true), true))
                using (var timer = new System.Windows.Forms.Timer { Interval = 100 })
                {
                    var type = window.GetType();
                    window.ShowInTaskbar = false;
                    window.StartPosition = FormStartPosition.Manual;
                    window.Location = new Point(-16000, -16000);
                    var elapsed = Stopwatch.StartNew();
                    bool retried = false;
                    bool complete = false;
                    timer.Tick += delegate
                    {
                        try
                        {
                            if (fixture.Error != null) throw fixture.Error;
                            if (failure != null) throw failure;
                            if (((Label)type.GetField("failure", Flags).GetValue(window)).Visible)
                                throw new InvalidOperationException("HubWindow displayed an initialization failure");
                            if (elapsed.Elapsed.TotalSeconds > 30) throw new TimeoutException("HubWindow WebView2 initialization timed out");
                            bool loaded = (bool)type.GetField("loaded", Flags).GetValue(window);
                            if (!loaded || fixture.Ready == 0) return;
                            if (!retried)
                            {
                                Check(fixture.Pages == 1, "real HubWindow first navigation completes");
                                Check(fixture.Ready == 1, "real ui_ready API notification completes");
                                var web = type.GetField("web", Flags).GetValue(window);
                                var core = web.GetType().GetProperty("CoreWebView2").GetValue(web, null);
                                browserPid = (uint)core.GetType().GetProperty("BrowserProcessId").GetValue(core, null);
                                retried = true;
                                var task = (Task)type.GetMethod("InitializeSafelyAsync", Flags).Invoke(window, null);
                                task.ContinueWith(t => { if (t.IsFaulted) failure = t.Exception; });
                                return;
                            }
                            Check(fixture.Pages == 2, "real HubWindow reinitialization navigates again");
                            CheckLibraries(candidate, program);
                            Check(!((Label)type.GetField("failure", Flags).GetValue(window)).Visible, "startup error label stays hidden");
                            complete = true;
                            timer.Stop();
                            type.GetField("exitApproved", Flags).SetValue(window, true);
                            window.Close();
                        }
                        catch (Exception error)
                        {
                            string log = Path.Combine(root, "data", "desktop.log");
                            if (File.Exists(log)) Console.Error.WriteLine(File.ReadAllText(log));
                            foreach (var library in AppDomain.CurrentDomain.GetAssemblies())
                                if (library.GetName().Name.StartsWith("Microsoft.Web.WebView2.", StringComparison.Ordinal))
                                    Console.Error.WriteLine("FAILURE SDK " + library.FullName + ";location=" + library.Location);
                            failure = error;
                            timer.Stop();
                            type.GetField("exitApproved", Flags).SetValue(window, true);
                            window.Close();
                        }
                    };
                    timer.Start();
                    Application.Run(window);
                    if (failure != null) throw failure;
                    Check(complete, "native initialization finished without thread exception");
                }
                }
                finally
                {
                // Wait only for this fixture's browser to release its synthetic profile.
                if (browserPid != 0)
                    try { using (var browser = Process.GetProcessById((int)browserPid)) browser.WaitForExit(10000); }
                    catch (ArgumentException) { }
                }
            }
            Console.WriteLine("Native initialization assertions passed: " + passed);
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
