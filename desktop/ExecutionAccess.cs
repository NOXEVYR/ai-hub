using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace AIHub.Desktop
{
    // Contains only user-selected metadata. Owner and scoped credentials never enter this object.
    internal sealed class ExecutionAccessRequest
    {
        internal readonly string InstallRoot, WorkspaceRoot, Role, Subject, Output, GrantId;
        internal readonly bool List;
        internal readonly int Limit;
        internal readonly string AfterGrantId;
        internal ExecutionAccessRequest(string install, string workspace, string role, string subject, string output, string grantId,
            bool list = false, int limit = 20, string afterGrantId = null)
        {
            InstallRoot = install; WorkspaceRoot = workspace; Role = role;
            Subject = subject; Output = output; GrantId = grantId;
            List = list; Limit = limit; AfterGrantId = afterGrantId;
        }
    }

    internal sealed class ExecutionGrantItem
    {
        internal readonly string GrantId, Role, Subject, CreatedAt, RevokedAt;
        internal ExecutionGrantItem(string id, string role, string subject, string created, string revoked)
        { GrantId = id; Role = role; Subject = subject; CreatedAt = created; RevokedAt = revoked; }
    }

    internal sealed class ExecutionGrantPage
    {
        internal readonly string AuthorityId, LedgerEpoch, NextAfterGrantId;
        internal readonly bool HasMore;
        internal readonly ExecutionGrantItem[] Items;
        internal ExecutionGrantPage(string authority, string epoch, ExecutionGrantItem[] items, bool more, string next)
        { AuthorityId = authority; LedgerEpoch = epoch; Items = items; HasMore = more; NextAfterGrantId = next; }
    }

    internal static class ExecutionAccessApi
    {
        private static readonly Regex Secrets = new Regex(
            @"Bearer\s+\S{12,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|https?://[^\s/@]+:[^\s/@]+@|[?&](?:api_key|token|secret)=|(?:^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?:$|[^A-Za-z0-9_-])|(?:^|[^A-Za-z0-9])[a-f0-9]{64}(?:$|[^A-Za-z0-9])",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        private static readonly Regex Revision = new Regex(@"\A[a-f0-9]{64}\z", RegexOptions.CultureInvariant);

        internal static bool SafeText(string value, int maximum)
        {
            if (String.IsNullOrWhiteSpace(value) || value != value.Trim() || value.Length > maximum || Secrets.IsMatch(value)) return false;
            foreach (char c in value) if (Char.IsControl(c)) return false;
            return true;
        }

        internal static bool ValidRole(string role) { return role == "source" || role == "source_read" || role == "worker"; }
        internal static bool ValidGrantId(string value)
        {
            Guid id;
            return Guid.TryParseExact(value, "D", out id) && id.ToString("D") == value;
        }

        private static bool LocalPath(string value, bool directory)
        {
            try
            {
                if (!SafeText(value, 2048) || value.Length < 3 || !Char.IsLetter(value[0]) || value[1] != ':' || (value[2] != '\\' && value[2] != '/') ||
                    value.IndexOf(':', 2) >= 0 || value.IndexOfAny(new[] { '*', '?', '"', '<', '>', '|' }) >= 0) return false;
                foreach (string part in value.Replace('/', '\\').Split('\\'))
                    if (part == ".." || part == "." || part.EndsWith(" ") || part.EndsWith(".")) return false;
                string full = Path.GetFullPath(value);
                if (directory && !Directory.Exists(full)) return false;
                string ancestor = directory ? full : Path.GetDirectoryName(full);
                if (!Directory.Exists(ancestor)) return false;
                while (!String.IsNullOrEmpty(ancestor))
                {
                    if ((File.GetAttributes(ancestor) & FileAttributes.ReparsePoint) != 0) return false;
                    ancestor = Path.GetDirectoryName(ancestor);
                }
                return true;
            }
            catch { return false; }
        }

        internal static bool ValidOutput(string value)
        {
            if (!LocalPath(value, false)) return false;
            try
            {
                string name = Path.GetFileName(value);
                if (name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || !String.Equals(Path.GetExtension(name), ".json", StringComparison.OrdinalIgnoreCase)) return false;
                string stem = name.Split('.')[0].ToUpperInvariant();
                if (Regex.IsMatch(stem, @"\A(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\z")) return false;
                // CLI also uses O_EXCL and verifies ancestors immediately before writing.
                if (File.Exists(value) || Directory.Exists(value)) return false;
                try { File.GetAttributes(value); return false; }
                catch (FileNotFoundException) { return true; }
                catch (DirectoryNotFoundException) { return false; }
            }
            catch { return false; }
        }

        internal static Uri DescribeUri()
        {
            Uri current;
            if (!Uri.TryCreate(Hub.Url, UriKind.Absolute, out current) || current.Scheme != "http" ||
                current.Host != "127.0.0.1" || current.Port != Hub.Port || Hub.Port < 1 || Hub.Port > 65535 ||
                current.UserInfo.Length != 0 || current.Query.Length != 0 || current.Fragment.Length != 0)
                throw new InvalidDataException("本机服务地址不可验证。");
            return new Uri(current.GetLeftPart(UriPartial.Authority) + "/api/execution/describe");
        }

        internal static Dictionary<string, object> ValidateDescribe(Dictionary<string, object> result, Dictionary<string, object> health)
        {
            object raw, port;
            Dictionary<string, object> identity = result != null && result.TryGetValue("identity", out raw) ? raw as Dictionary<string, object> : null;
            if (Hub.TextValue(result, "protocol") != "aihub-execution/1" || Hub.TextValue(identity, "status") != "available" ||
                Hub.TextValue(identity, "app") != "ai-hub" || Hub.TextValue(identity, "control_protocol") != Hub.ControlProtocol ||
                Hub.TextValue(health, "app") != "ai-hub" || Hub.TextValue(health, "control_protocol") != Hub.ControlProtocol ||
                String.IsNullOrWhiteSpace(Hub.TextValue(health, "service_instance_id")) ||
                Hub.TextValue(identity, "service_instance_id") != Hub.TextValue(health, "service_instance_id") ||
                !identity.TryGetValue("port", out port) || !(port is int) || (int)port != Hub.Port ||
                !String.Equals(Hub.NormalizeRoot(Hub.TextValue(identity, "install_root")), Hub.Root, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Hub.NormalizeRoot(Hub.TextValue(health, "install_root")), Hub.Root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("本机服务身份不可验证。");
            return result;
        }

        internal static string Workspace(Dictionary<string, object> result)
        {
            object raw;
            var workspace = result != null && result.TryGetValue("workspace", out raw) ? raw as Dictionary<string, object> : null;
            if (Hub.TextValue(workspace, "status") != "available") return null;
            string root = Hub.TextValue(workspace, "root"), revision = Hub.TextValue(workspace, "binding_revision");
            string connection = Hub.TextValue(result, "connection_revision");
            if (!LocalPath(root, true) || revision == null || !Revision.IsMatch(revision) || connection == null || !Revision.IsMatch(connection))
                throw new InvalidDataException("工作区不可验证。");
            return Path.GetFullPath(root);
        }

        internal static Dictionary<string, object> Describe()
        {
            Uri uri = DescribeUri();
            bool absent;
            // Existing bounded, proxy-free health helper; no native control file is read here.
            var health = Hub.ReadControlHealth(out absent);
            if (absent) throw new InvalidDataException("本机服务不可用。");
            var request = (HttpWebRequest)WebRequest.Create(uri);
            request.Method = "GET"; request.Proxy = null; request.AllowAutoRedirect = false;
            request.Timeout = 5000; request.ReadWriteTimeout = 2000; request.KeepAlive = false;
            using (var response = (HttpWebResponse)request.GetResponse())
            {
                if (response.StatusCode != HttpStatusCode.OK) throw new InvalidDataException("本机描述不可用。");
                using (var stream = response.GetResponseStream())
                using (var bytes = new MemoryStream())
                {
                    var buffer = new byte[4096]; int count;
                    while ((count = stream.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        if (bytes.Length + count > 131072) throw new InvalidDataException("本机描述过大。");
                        bytes.Write(buffer, 0, count);
                    }
                    var result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(new UTF8Encoding(false, true).GetString(bytes.ToArray()));
                    ValidateDescribe(result, health);
                    // Recheck the instance after describe to avoid combining two service instances.
                    var current = Hub.ReadControlHealth(out absent);
                    if (absent) throw new InvalidDataException("本机服务已变化。");
                    return ValidateDescribe(result, current);
                }
            }
        }

        internal static string Arguments(ExecutionAccessRequest request)
        {
            if (request == null || !LocalPath(request.InstallRoot, true) || !LocalPath(request.WorkspaceRoot, true)) throw new InvalidDataException();
            string args = "-B " + Hub.Quote(Path.Combine(request.InstallRoot, "tools", "execution_admin.py")) +
                " --install-root " + Hub.Quote(request.InstallRoot) + " --workspace-root " + Hub.Quote(request.WorkspaceRoot);
            if (request.List)
            {
                if (request.Role != null || request.Subject != null || request.Output != null || request.GrantId != null ||
                    request.Limit < 1 || request.Limit > 50 || (request.AfterGrantId != null && !ValidGrantId(request.AfterGrantId))) throw new InvalidDataException();
                return args + " --list --limit " + request.Limit.ToString(CultureInfo.InvariantCulture) +
                    (request.AfterGrantId == null ? "" : " --after-grant-id " + Hub.Quote(request.AfterGrantId));
            }
            if (request.AfterGrantId != null) throw new InvalidDataException();
            if (request.GrantId != null)
            {
                if (!ValidGrantId(request.GrantId) || request.Role != null || request.Subject != null || request.Output != null) throw new InvalidDataException();
                return args + " --grant-id " + Hub.Quote(request.GrantId);
            }
            if (!ValidRole(request.Role) || !SafeText(request.Subject, 200) || !ValidOutput(request.Output)) throw new InvalidDataException();
            return args + " --role " + Hub.Quote(request.Role) + " --subject " + Hub.Quote(request.Subject) + " --output " + Hub.Quote(request.Output);
        }

        // Drain with constant memory. Never decode, retain, log, or display either stream.
        internal static async Task<bool> DiscardAsync(Stream stream)
        {
            var buffer = new byte[4096]; long total = 0; int count;
            while ((count = await stream.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                total = Math.Min(131073L, total + count);
            return total <= 131072;
        }

        internal static Task<bool> RunAsync(ExecutionAccessRequest request)
        {
            return Task.Run(async () => {
                try
                {
                    if (request == null || request.List) return false;
                    string arguments = Arguments(request);
                    var start = new ProcessStartInfo(Hub.FindPython(), arguments) {
                        WorkingDirectory = request.InstallRoot, UseShellExecute = false, CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden, RedirectStandardOutput = true, RedirectStandardError = true
                    };
                    using (var process = Process.Start(start))
                    {
                        if (process == null) return false;
                        var stdout = DiscardAsync(process.StandardOutput.BaseStream);
                        var stderr = DiscardAsync(process.StandardError.BaseStream);
                        // Once started, retain the window until the mutation process ends.
                        // Do not kill/retry an operation whose grant status may be unknown.
                        process.WaitForExit();
                        bool[] bounded = await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
                        return process.ExitCode == 0 && bounded[0] && bounded[1];
                    }
                }
                catch { return false; }
            });
        }

        // Only --list has a public stdout schema. Mutation streams remain undecoded.
        private static async Task<byte[]> ReadListAsync(Stream stream)
        {
            using (var bytes = new MemoryStream())
            {
                var buffer = new byte[4096]; int count; bool oversized = false;
                while ((count = await stream.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0)
                {
                    if (bytes.Length + count > 131072) oversized = true;
                    if (!oversized) bytes.Write(buffer, 0, count);
                }
                return oversized ? null : bytes.ToArray();
            }
        }

        private static void Keys(Dictionary<string, object> value, params string[] expected)
        {
            if (value == null || value.Count != expected.Length) throw new InvalidDataException();
            foreach (string key in expected) if (!value.ContainsKey(key)) throw new InvalidDataException();
        }

        private static string NullableString(Dictionary<string, object> value, string key)
        {
            object raw = value[key];
            if (raw != null && !(raw is string)) throw new InvalidDataException();
            return raw as string;
        }

        private static DateTimeOffset Time(string value)
        {
            DateTimeOffset time;
            if (value == null || !Regex.IsMatch(value, @"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:\+00:00|Z)\z") ||
                !DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.None, out time) || time.Offset != TimeSpan.Zero) throw new InvalidDataException();
            return time;
        }

        private static bool SafeListSubject(string value)
        {
            if (!SafeText(value, 400)) return false;
            int count = 0;
            for (int i = 0; i < value.Length; i++, count++)
            {
                if (Char.IsHighSurrogate(value[i]))
                { if (i + 1 >= value.Length || !Char.IsLowSurrogate(value[++i])) return false; }
                else if (Char.IsLowSurrogate(value[i])) return false;
            }
            return count <= 200;
        }

        // The CLI emits compact ASCII JSON with an exact field vocabulary. Reject
        // duplicate keys before JavaScriptSerializer can silently replace them.
        private sealed class ListJson
        {
            private readonly string text; private int at;
            internal ListJson(string text) { this.text = text; }
            private void Space() { while (at < text.Length && Char.IsWhiteSpace(text[at])) at++; }
            private string String()
            {
                int start = at++; bool escaped = false;
                while (at < text.Length)
                {
                    char c = text[at++];
                    if (c < 32) throw new InvalidDataException();
                    if (!escaped && c == '"') return new JavaScriptSerializer().Deserialize<string>(text.Substring(start, at - start));
                    if (!escaped && c == '\\') escaped = true; else escaped = false;
                }
                throw new InvalidDataException();
            }
            private void Value(int depth)
            {
                Space(); if (at >= text.Length || depth > 8) throw new InvalidDataException();
                char c = text[at];
                if (c == '"') { String(); return; }
                if (c == '{' || c == '[')
                {
                    bool obj = c == '{'; char end = obj ? '}' : ']'; at++; Space();
                    var seen = new HashSet<string>(StringComparer.Ordinal);
                    if (at < text.Length && text[at] == end) { at++; return; }
                    while (true)
                    {
                        Space();
                        if (obj)
                        {
                            if (at >= text.Length || text[at] != '"' || !seen.Add(String())) throw new InvalidDataException();
                            Space(); if (at >= text.Length || text[at++] != ':') throw new InvalidDataException();
                        }
                        Value(depth + 1); Space();
                        if (at >= text.Length) throw new InvalidDataException();
                        char separator = text[at++]; if (separator == end) return;
                        if (separator != ',') throw new InvalidDataException();
                    }
                }
                foreach (string literal in new[] { "true", "false", "null" })
                    if (text.Substring(at).StartsWith(literal, StringComparison.Ordinal)) { at += literal.Length; return; }
                // Public list metadata contains no numeric fields.
                throw new InvalidDataException();
            }
            internal void Validate() { Value(0); Space(); if (at != text.Length) throw new InvalidDataException(); }
        }

        internal static ExecutionGrantPage ParseList(byte[] bytes, int limit, string after)
        {
            if (bytes == null || bytes.Length == 0 || bytes.Length > 131072 || limit < 1 || limit > 50) throw new InvalidDataException();
            foreach (byte b in bytes) if (b > 127) throw new InvalidDataException();
            string text = new UTF8Encoding(false, true).GetString(bytes);
            new ListJson(text).Validate();
            var value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(text);
            Keys(value, "protocol", "authority_id", "ledger_epoch", "items", "has_more", "next_after_grant_id");
            if (Hub.TextValue(value, "protocol") != "aihub-execution/1" || !(value["has_more"] is bool)) throw new InvalidDataException();
            string authority = NullableString(value, "authority_id"), epoch = NullableString(value, "ledger_epoch"), next = NullableString(value, "next_after_grant_id");
            var rawItems = value["items"] as System.Collections.IList;
            if (rawItems == null || rawItems.Count > limit) throw new InvalidDataException();
            bool more = (bool)value["has_more"];
            if (authority == null || epoch == null)
            { if (authority != null || epoch != null || rawItems.Count != 0 || more || next != null) throw new InvalidDataException(); }
            else if (!ValidGrantId(authority) || !ValidGrantId(epoch)) throw new InvalidDataException();
            var items = new List<ExecutionGrantItem>(); var seen = new HashSet<string>();
            DateTimeOffset previousTime = DateTimeOffset.MinValue; string previousId = null;
            foreach (object raw in rawItems)
            {
                var item = raw as Dictionary<string, object>;
                Keys(item, "grant_id", "role", "subject", "created_at", "revoked_at");
                string id = NullableString(item, "grant_id"), role = NullableString(item, "role"), subject = NullableString(item, "subject"),
                    created = NullableString(item, "created_at"), revoked = NullableString(item, "revoked_at");
                if (!ValidGrantId(id) || id == after || !seen.Add(id) || !ValidRole(role) || !SafeListSubject(subject)) throw new InvalidDataException();
                DateTimeOffset time = Time(created);
                if (revoked != null) Time(revoked);
                if (previousId != null && (time < previousTime || time == previousTime && String.CompareOrdinal(id, previousId) <= 0)) throw new InvalidDataException();
                previousTime = time; previousId = id;
                items.Add(new ExecutionGrantItem(id, role, subject, created, revoked));
            }
            if (more ? items.Count != limit || items.Count == 0 || next != items[items.Count - 1].GrantId : next != null) throw new InvalidDataException();
            return new ExecutionGrantPage(authority, epoch, items.ToArray(), more, next);
        }

        internal static Task<ExecutionGrantPage> ListAsync(ExecutionAccessRequest request)
        {
            return Task.Run(async () => {
                try
                {
                    if (request == null || !request.List) return null;
                    var start = new ProcessStartInfo(Hub.FindPython(), Arguments(request)) {
                        WorkingDirectory = request.InstallRoot, UseShellExecute = false, CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden, RedirectStandardOutput = true, RedirectStandardError = true
                    };
                    using (var process = Process.Start(start))
                    {
                        if (process == null) return null;
                        var stdout = ReadListAsync(process.StandardOutput.BaseStream);
                        var stderr = DiscardAsync(process.StandardError.BaseStream);
                        process.WaitForExit();
                        byte[] bytes = await stdout.ConfigureAwait(false); bool bounded = await stderr.ConfigureAwait(false);
                        return process.ExitCode == 0 && bounded ? ParseList(bytes, request.Limit, request.AfterGrantId) : null;
                    }
                }
                catch { return null; }
            });
        }
    }

    internal sealed class ExecutionAccessDialog : Form
    {
        private static readonly Color Canvas = Color.FromArgb(16, 21, 39), Surface = Color.FromArgb(25, 32, 55),
            Ink = Color.FromArgb(244, 241, 232), Muted = Color.FromArgb(168, 179, 201), Gold = Color.FromArgb(232, 190, 113);
        private const string Failure = "操作未确认完成。请保留此窗口与所选文件，核对接入状态；请勿自动重复创建接入。";
        private readonly Func<Task<Dictionary<string, object>>> describe;
        private readonly Func<ExecutionAccessRequest, Task<bool>> runner;
        private readonly Func<ExecutionAccessRequest, Task<ExecutionGrantPage>> listRunner;
        private readonly Func<bool> confirmRevoke;
        private readonly Func<string> chooseOutput;
        private readonly Icon brandIcon;
        internal readonly TextBox SubjectInput, GrantIdInput;
        internal readonly ComboBox RoleInput;
        internal readonly Button CreateButton, RevokeButton, RefreshButton, CloseButton;
        internal readonly Button ListRefreshButton, ListFirstButton, ListPreviousButton, ListNextButton;
        internal readonly DataGridView GrantList;
        private readonly Label workspaceLabel, statusLabel, listLabel;
        private readonly string installRoot;
        private string workspaceRoot;
        private bool hostAvailable = true;
        private bool systemEnding;
        private bool listVerified;
        private ExecutionGrantPage page;
        private readonly List<string> pageCursors = new List<string> { null };
        private int pageIndex;
        private string pageBinding, pageConnection;
        internal bool IsWorking { get; private set; }
        internal bool IsBusy { get { return IsWorking; } }
        internal string StatusText { get { return statusLabel.Text; } }
        internal string WorkspaceRoot { get { return workspaceRoot; } }
        internal string ListStatusText { get { return listLabel.Text; } }
        internal int ListPageNumber { get { return pageIndex + 1; } }

        internal ExecutionAccessDialog() : this(() => Task.Run(() => ExecutionAccessApi.Describe()), ExecutionAccessApi.RunAsync, null) { }

        internal ExecutionAccessDialog(Func<Task<Dictionary<string, object>>> describe, Func<ExecutionAccessRequest, Task<bool>> runner, Func<bool> confirmRevoke, Func<string> chooseOutput = null,
            Func<ExecutionAccessRequest, Task<ExecutionGrantPage>> listRunner = null)
        {
            if (describe == null || runner == null) throw new ArgumentNullException();
            this.describe = describe; this.runner = runner; this.confirmRevoke = confirmRevoke ?? ConfirmRevoke;
            this.chooseOutput = chooseOutput ?? SelectOutput;
            this.listRunner = listRunner ?? ExecutionAccessApi.ListAsync;
            installRoot = Hub.Root;
            Text = "曜核 · 协作接入"; BackColor = Canvas; ForeColor = Ink;
            brandIcon = LoadBrandIcon();
            if (brandIcon != null) Icon = brandIcon; else ShowIcon = false;
            Font = new Font("Microsoft YaHei UI", 10F); AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(720, 610); MinimumSize = new Size(640, 560);
            StartPosition = FormStartPosition.CenterParent;
            MaximizeBox = false;
            var outer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = new Padding(0) };
            outer.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); outer.RowStyles.Add(new RowStyle(SizeType.Absolute, 68)); outer.RowStyles.Add(new RowStyle(SizeType.Absolute, 54));
            Controls.Add(outer);
            var body = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Margin = new Padding(0) };
            outer.Controls.Add(body, 0, 0);
            var layout = new TableLayoutPanel { Dock = DockStyle.Top, Height = 844, Padding = new Padding(26), ColumnCount = 1, RowCount = 13 };
            foreach (int height in new[] { 44, 62, 66, 28, 38, 28, 38, 44, 82, 38, 44, 224, 58 }) layout.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
            body.Controls.Add(layout);
            var title = Label("协作接入", Ink); title.Font = new Font(Font.FontFamily, 18F, FontStyle.Bold); layout.Controls.Add(title);
            layout.Controls.Add(Label("为合作软件配置本机可选执行接入。曜核、映序和棱光各自独立，接入仅限当前工作区。\n持久身份由合作软件提供，请勿填写 API Key、接入钥匙或所有者控制钥匙。", Muted));
            workspaceLabel = Label("正在核对本机工作区…", Gold); layout.Controls.Add(workspaceLabel);
            layout.Controls.Add(Label("接入角色", Muted));
            RoleInput = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Surface, ForeColor = Ink };
            RoleInput.Items.AddRange(new object[] { "source · 提交与观察本来源执行", "source_read · 只读观察本来源执行", "worker · 领取与回报执行" });
            RoleInput.SelectedIndex = 0; layout.Controls.Add(RoleInput);
            layout.Controls.Add(Label("合作软件给出的持久身份（subject，最多 200 字）", Muted));
            SubjectInput = Input(); SubjectInput.MaxLength = 200; layout.Controls.Add(SubjectInput);
            CreateButton = Button("选择新私有 JSON 并创建", true); layout.Controls.Add(CreateButton);
            var revoke = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, Margin = new Padding(0, 12, 0, 0) };
            revoke.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); revoke.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 110));
            revoke.Controls.Add(Label("撤销：选择下方授权，或填写 grant_id（UUID）", Muted), 0, 0); revoke.SetColumnSpan(revoke.GetControlFromPosition(0, 0), 2);
            GrantIdInput = Input(); GrantIdInput.MaxLength = 36; revoke.Controls.Add(GrantIdInput, 0, 1);
            RevokeButton = Button("撤销接入", false); revoke.Controls.Add(RevokeButton, 1, 1); layout.Controls.Add(revoke);
            layout.Controls.Add(Label("授权核对列表 · 不包含接入钥匙", Gold));
            var listActions = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 1, Margin = new Padding(0) };
            for (int i = 0; i < 4; i++) listActions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));
            ListRefreshButton = Button("刷新本页", false); ListFirstButton = Button("回到首页", false);
            ListPreviousButton = Button("上一页", false); ListNextButton = Button("下一页", false);
            listActions.Controls.Add(ListRefreshButton, 0, 0); listActions.Controls.Add(ListFirstButton, 1, 0);
            listActions.Controls.Add(ListPreviousButton, 2, 0); listActions.Controls.Add(ListNextButton, 3, 0); layout.Controls.Add(listActions);
            GrantList = new DataGridView { Dock = DockStyle.Fill, BackgroundColor = Surface, ForeColor = Ink, ReadOnly = true,
                AllowUserToAddRows = false, AllowUserToDeleteRows = false, AllowUserToOrderColumns = false, RowHeadersVisible = false,
                MultiSelect = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect, AutoSizeRowsMode = DataGridViewAutoSizeRowsMode.AllCells,
                EnableHeadersVisualStyles = false, ScrollBars = ScrollBars.Both, BorderStyle = BorderStyle.FixedSingle };
            GrantList.DefaultCellStyle.BackColor = Surface; GrantList.DefaultCellStyle.ForeColor = Ink;
            GrantList.DefaultCellStyle.SelectionBackColor = Color.FromArgb(58, 68, 96); GrantList.DefaultCellStyle.SelectionForeColor = Ink;
            GrantList.DefaultCellStyle.WrapMode = DataGridViewTriState.True;
            GrantList.ColumnHeadersDefaultCellStyle.BackColor = Canvas; GrantList.ColumnHeadersDefaultCellStyle.ForeColor = Gold;
            foreach (var column in new[] { new[] { "role", "角色", "105" }, new[] { "subject", "持久身份", "180" },
                new[] { "state", "状态", "86" }, new[] { "id", "grant_id", "290" }, new[] { "created", "创建时间（UTC）", "190" }, new[] { "revoked", "撤销时间（UTC）", "190" } })
            {
                int index = GrantList.Columns.Add(column[0], column[1]); GrantList.Columns[index].Width = Int32.Parse(column[2], CultureInfo.InvariantCulture);
                GrantList.Columns[index].SortMode = DataGridViewColumnSortMode.NotSortable;
            }
            layout.Controls.Add(GrantList);
            listLabel = Label("列表尚未读取。创建回复不确定或保存失败时，请显式刷新核对；不能重新导出原钥匙。\n可横向滚动查看完整 grant_id 与时间，选择一项后在上方确认撤销。", Muted); layout.Controls.Add(listLabel);
            statusLabel = Label("尚未创建接入。私有文件含接入钥匙，请由合作软件本机导入并妥善保管。", Muted); statusLabel.Padding = new Padding(26, 4, 26, 4); outer.Controls.Add(statusLabel, 0, 1);
            var footer = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, Padding = new Padding(26, 4, 26, 4), Margin = new Padding(0) };
            CloseButton = Button("关闭", false); RefreshButton = Button("核对工作区", false);
            CloseButton.Dock = RefreshButton.Dock = DockStyle.None;
            CloseButton.Width = 100; RefreshButton.Width = 128; footer.Controls.Add(CloseButton); footer.Controls.Add(RefreshButton); outer.Controls.Add(footer, 0, 2);
            CreateButton.Click += async (s, e) => await ChooseAndCreateAsync();
            RevokeButton.Click += async (s, e) => await RevokeAccessAsync();
            RefreshButton.Click += async (s, e) => await RefreshAsync();
            ListRefreshButton.Click += async (s, e) => await LoadListAsync();
            ListFirstButton.Click += async (s, e) => await LoadListAsync(true);
            ListPreviousButton.Click += async (s, e) => await LoadListAsync(false, -1);
            ListNextButton.Click += async (s, e) => await LoadListAsync(false, 1);
            GrantList.SelectionChanged += (s, e) => SelectGrant();
            CloseButton.Click += (s, e) => Close();
            Shown += async (s, e) => await RefreshAsync();
            SetBusy(false);
        }

        private static Label Label(string text, Color color) { return new Label { Dock = DockStyle.Fill, Text = text, ForeColor = color, AutoSize = false, TextAlign = ContentAlignment.MiddleLeft }; }
        private static Icon LoadBrandIcon()
        {
            try
            {
                using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico"))
                {
                    if (stream == null) return null;
                    using (var icon = new Icon(stream)) return (Icon)icon.Clone();
                }
            }
            catch { return null; }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && brandIcon != null) brandIcon.Dispose();
            base.Dispose(disposing);
        }
        private static TextBox Input() { return new TextBox { Dock = DockStyle.Fill, BackColor = Surface, ForeColor = Ink, BorderStyle = BorderStyle.FixedSingle }; }
        private static Button Button(string text, bool primary)
        {
            var button = new Button { Dock = DockStyle.Fill, Text = text, FlatStyle = FlatStyle.Flat, BackColor = primary ? Gold : Surface, ForeColor = primary ? Canvas : Ink, Height = 36, AutoSize = false };
            button.FlatAppearance.BorderColor = Gold;
            button.EnabledChanged += (s, e) => { button.BackColor = button.Enabled && primary ? Gold : Surface; button.ForeColor = button.Enabled ? (primary ? Canvas : Ink) : Muted; button.FlatAppearance.BorderColor = button.Enabled ? Gold : Color.FromArgb(87, 99, 122); };
            button.Paint += (s, e) => {
                if (button.Enabled) return;
                Rectangle inner = button.ClientRectangle; inner.Inflate(-2, -2);
                using (var fill = new SolidBrush(Surface)) e.Graphics.FillRectangle(fill, inner);
                TextRenderer.DrawText(e.Graphics, button.Text, button.Font, inner, Muted, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine);
            };
            return button;
        }
        private string SelectedRole { get { return RoleInput.SelectedIndex == 0 ? "source" : RoleInput.SelectedIndex == 1 ? "source_read" : RoleInput.SelectedIndex == 2 ? "worker" : null; } }

        private void SetBusy(bool value)
        {
            IsWorking = value;
            CreateButton.Enabled = RevokeButton.Enabled = !value && hostAvailable && workspaceRoot != null;
            RefreshButton.Enabled = RoleInput.Enabled = SubjectInput.Enabled = GrantIdInput.Enabled = !value && hostAvailable;
            ListRefreshButton.Enabled = ListFirstButton.Enabled = !value && hostAvailable && workspaceRoot != null;
            ListPreviousButton.Enabled = !value && hostAvailable && workspaceRoot != null && listVerified && pageIndex > 0;
            ListNextButton.Enabled = !value && hostAvailable && workspaceRoot != null && listVerified && page != null && page.HasMore;
            GrantList.Enabled = !value && hostAvailable && workspaceRoot != null && listVerified;
            CloseButton.Enabled = !value;
            UseWaitCursor = value;
        }

        internal void SetHostAvailable(bool value)
        {
            hostAvailable = value;
            SetBusy(IsWorking);
            if (!value) statusLabel.Text = "曜核正在退出或准备更新。接入操作已暂时停用，表单内容保留。";
        }

        internal void SetSystemEnding(bool value) { systemEnding = value; }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (IsWorking && e.CloseReason != CloseReason.WindowsShutDown && e.CloseReason != CloseReason.TaskManagerClosing &&
                !(systemEnding && e.CloseReason == CloseReason.FormOwnerClosing))
                e.Cancel = true;
            base.OnFormClosing(e);
        }

        internal async Task RefreshAsync()
        {
            if (IsWorking || !hostAvailable) return;
            SetBusy(true);
            try
            {
                var description = await describe();
                string current = ExecutionAccessApi.Workspace(description);
                object raw;
                var scope = description.TryGetValue("workspace", out raw) ? raw as Dictionary<string, object> : null;
                if (!String.Equals(current, workspaceRoot, StringComparison.OrdinalIgnoreCase) || page != null &&
                    (pageBinding != Hub.TextValue(scope, "binding_revision") || pageConnection != Hub.TextValue(description, "connection_revision") ||
                     page.AuthorityId != Hub.TextValue(description, "execution_authority_id") || page.LedgerEpoch != Hub.TextValue(description, "ledger_epoch"))) InvalidateList();
                workspaceRoot = current;
                workspaceLabel.Text = workspaceRoot == null ? "尚无托管工作区。请在工作环境中配置并确认托管目录，再核对工作区。" : "当前工作区：\n" + workspaceRoot;
                statusLabel.Text = workspaceRoot == null ? "完成工作环境配置后才可创建或撤销接入。" : "仅为上方工作区配置接入。不会自动启动或连接合作软件。";
            }
            catch { workspaceRoot = null; InvalidateList(); workspaceLabel.Text = "无法核对当前本机工作区。"; statusLabel.Text = "请确认运行的是当前安装目录中的曜核，再核对工作区。"; }
            finally { SetBusy(false); }
        }

        private void InvalidateList()
        {
            listVerified = false;
            listLabel.Text = page == null ? "列表尚未读取。请核对工作区后显式刷新。" : "上一页保留，仅供核对；当前身份或读取状态未确认。请显式刷新，不能据此自动创建接入。";
        }

        private void SelectGrant()
        {
            if (IsWorking || !hostAvailable || !listVerified || GrantList.SelectedRows.Count != 1) return;
            var item = GrantList.SelectedRows[0].Tag as ExecutionGrantItem;
            if (item != null) GrantIdInput.Text = item.GrantId;
        }

        private static bool SameScope(Dictionary<string, object> first, Dictionary<string, object> second, ExecutionGrantPage listed)
        {
            object raw;
            var a = first != null && first.TryGetValue("workspace", out raw) ? raw as Dictionary<string, object> : null;
            var b = second != null && second.TryGetValue("workspace", out raw) ? raw as Dictionary<string, object> : null;
            return Hub.TextValue(a, "binding_revision") == Hub.TextValue(b, "binding_revision") &&
                Hub.TextValue(first, "connection_revision") == Hub.TextValue(second, "connection_revision") &&
                Hub.TextValue(first, "execution_authority_id") == listed.AuthorityId && Hub.TextValue(second, "execution_authority_id") == listed.AuthorityId &&
                Hub.TextValue(first, "ledger_epoch") == listed.LedgerEpoch && Hub.TextValue(second, "ledger_epoch") == listed.LedgerEpoch;
        }

        internal async Task LoadListAsync(bool first = false, int direction = 0)
        {
            if (IsWorking || !hostAvailable || workspaceRoot == null || (direction != 0 && !listVerified)) return;
            int target = first ? 0 : pageIndex + direction;
            if (target < 0 || direction > 0 && (page == null || !page.HasMore)) return;
            string cursor = first ? null : direction > 0 ? page.NextAfterGrantId : pageCursors[target];
            string root = workspaceRoot;
            SetBusy(true);
            try
            {
                var before = await describe();
                if (!hostAvailable) { InvalidateList(); return; }
                if (!String.Equals(ExecutionAccessApi.Workspace(before), root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException();
                var listed = await listRunner(new ExecutionAccessRequest(installRoot, root, null, null, null, null, true, 20, cursor));
                if (listed == null) throw new InvalidDataException();
                var current = await describe();
                if (!hostAvailable || !String.Equals(ExecutionAccessApi.Workspace(current), root, StringComparison.OrdinalIgnoreCase) || !SameScope(before, current, listed)) throw new InvalidDataException();
                listVerified = false; GrantList.Rows.Clear();
                foreach (var item in listed.Items)
                {
                    string role = item.Role == "source" ? "发起端" : item.Role == "source_read" ? "只读端" : "执行端";
                    int row = GrantList.Rows.Add(role, item.Subject, item.RevokedAt == null ? "有效" : "已撤销", item.GrantId, item.CreatedAt, item.RevokedAt ?? "—");
                    GrantList.Rows[row].Tag = item;
                }
                GrantList.ClearSelection();
                if (first) { pageCursors.Clear(); pageCursors.Add(null); }
                else if (direction > 0)
                {
                    if (pageCursors.Count > target) pageCursors.RemoveRange(target, pageCursors.Count - target);
                    pageCursors.Add(cursor);
                }
                page = listed; pageIndex = target; listVerified = true;
                object raw;
                var scope = current.TryGetValue("workspace", out raw) ? raw as Dictionary<string, object> : null;
                pageBinding = Hub.TextValue(scope, "binding_revision"); pageConnection = Hub.TextValue(current, "connection_revision");
                listLabel.Text = "第 " + (pageIndex + 1) + " 页 · " + listed.Items.Length + " 项（含已撤销）。" + (listed.HasMore ? "还有下一页。" : "已到末页。") +
                    "\n选择一项填入 grant_id，再确认撤销；列表不能重新导出接入钥匙。";
                statusLabel.Text = listed.Items.Length == 0 ? "当前授权列表为空。未创建新接入。" : "已核对当前工作区授权列表。未创建新接入。";
            }
            catch { InvalidateList(); statusLabel.Text = "授权列表未确认。上一页与表单保留，请核对本机工作区后显式刷新；请勿自动重复创建接入。"; }
            finally { SetBusy(false); }
        }

        private string SelectOutput()
        {
            using (var save = new SaveFileDialog { Title = "保存新的私有接入文件（不覆盖现有文件）", Filter = "私有接入 JSON (*.json)|*.json", DefaultExt = "json", AddExtension = true, OverwritePrompt = true, CheckPathExists = true, FileName = "aihub-execution-access.json" })
                return save.ShowDialog(this) == DialogResult.OK ? save.FileName : null;
        }

        internal async Task ChooseAndCreateAsync()
        {
            if (IsWorking || !hostAvailable || workspaceRoot == null) return;
            string role = SelectedRole, subject = SubjectInput.Text, root = workspaceRoot;
            if (!ExecutionAccessApi.ValidRole(role) || !ExecutionAccessApi.SafeText(subject, 200)) { statusLabel.Text = "请输入有效角色与合作软件的持久身份；不能包含钥匙或凭据。"; return; }
            SetBusy(true);
            try
            {
                string output = chooseOutput();
                if (output == null) return;
                ExecutionAccessRequest request = CreateRequest(root, role, subject, output);
                if (request != null) await RunOperationAsync(request);
            }
            catch { statusLabel.Text = Failure; }
            finally { SetBusy(false); }
        }

        private ExecutionAccessRequest CreateRequest(string root, string role, string subject, string output)
        {
            if (!ExecutionAccessApi.ValidOutput(output)) { statusLabel.Text = "请选择本机已有目录中的新 JSON 文件，不能覆盖现有文件或使用链接目录。"; return null; }
            return new ExecutionAccessRequest(installRoot, root, role, subject, Path.GetFullPath(output), null);
        }

        internal async Task CreateAccessAsync(string output)
        {
            if (IsWorking || !hostAvailable || workspaceRoot == null) return;
            string role = SelectedRole, subject = SubjectInput.Text;
            if (!ExecutionAccessApi.ValidRole(role) || !ExecutionAccessApi.SafeText(subject, 200)) { statusLabel.Text = "请输入有效角色与合作软件的持久身份；不能包含钥匙或凭据。"; return; }
            var request = CreateRequest(workspaceRoot, role, subject, output);
            if (request != null) await OperateAsync(request);
        }

        private bool ConfirmRevoke()
        {
            return MessageBox.Show(this, "确认撤销此接入？撤销将阻止该接入后续协议操作，但不会停止合作软件已经领取的原生任务。请在合作软件中核对并处理任务。", "曜核 · 撤销接入", MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
        }

        internal async Task RevokeAccessAsync()
        {
            if (IsWorking || !hostAvailable || workspaceRoot == null) return;
            string grant = GrantIdInput.Text;
            if (!ExecutionAccessApi.ValidGrantId(grant)) { statusLabel.Text = "请输入接入文件内的完整 grant_id（小写 UUID），请勿粘贴 token。"; return; }
            // Busy also covers confirmation: nested message-loop clicks cannot start another mutation.
            SetBusy(true);
            try
            {
                if (!confirmRevoke()) return;
                await RunOperationAsync(new ExecutionAccessRequest(installRoot, workspaceRoot, null, null, null, grant));
            }
            catch { statusLabel.Text = Failure; }
            finally { SetBusy(false); }
        }

        private async Task OperateAsync(ExecutionAccessRequest request)
        {
            SetBusy(true);
            try { await RunOperationAsync(request); }
            catch { statusLabel.Text = Failure; }
            finally { SetBusy(false); }
        }

        private async Task RunOperationAsync(ExecutionAccessRequest request)
        {
            if (!hostAvailable) return;
            statusLabel.Text = "正在处理接入，请保留此窗口…";
            // Refresh scope before invoking the native CLI; the CLI independently verifies it again.
            string current = ExecutionAccessApi.Workspace(await describe());
            if (!hostAvailable)
            {
                statusLabel.Text = "曜核正在退出或准备更新。接入操作未提交，表单内容保留。";
                return;
            }
            if (current == null || !String.Equals(current, request.WorkspaceRoot, StringComparison.OrdinalIgnoreCase))
            {
                workspaceRoot = null; workspaceLabel.Text = "工作区已变化，请重新核对工作环境。"; statusLabel.Text = "接入操作未提交。请核对当前工作区。"; return;
            }
            bool success = await runner(request);
            InvalidateList();
            statusLabel.Text = success ? (request.GrantId == null ? "已创建接入，私有文件已保存：\n" + request.Output : "已撤销接入。已领取的原生任务请在合作软件中处理。") : Failure;
        }
    }
}
