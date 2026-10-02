using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Web.Script.Serialization;

namespace AIHub.Desktop
{
    internal static class ExecutionAccessTests
    {
        private const string Authority = "e57a8a8f-48b6-4769-86db-72e67bdb83c7", Epoch = "f39cdd21-e1fc-40d2-85e8-5a1a2a73ab40";
        private static void Check(bool passed, string name)
        {
            if (!passed) throw new Exception("FAILED: execution access " + name);
            Console.WriteLine("PASS execution access " + name);
        }

        private static void Complete(Task task)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(20);
            while (!task.IsCompleted && DateTime.UtcNow < deadline) { Application.DoEvents(); Thread.Sleep(5); }
            if (!task.IsCompleted) throw new Exception("Execution access fixture timed out.");
            task.GetAwaiter().GetResult();
        }

        private static Dictionary<string, object> Description(string root)
        {
            return new Dictionary<string, object> {
                { "protocol", "aihub-execution/1" }, { "connection_revision", new string('b', 64) },
                { "execution_authority_id", Authority }, { "ledger_epoch", Epoch },
                { "workspace", new Dictionary<string, object> {
                    { "status", root == null ? "workspace_unavailable" : "available" },
                    { "root", root }, { "binding_revision", new string('a', 64) }
                } }
            };
        }

        private static string[] DecodeArguments(string value)
        {
            int count;
            IntPtr argv = CommandLineToArgvW("python.exe " + value, out count);
            if (argv == IntPtr.Zero) throw new Exception("Fixture argv decode failed.");
            try
            {
                var result = new string[count];
                for (int n = 0; n < count; n++) result[n] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv, n * IntPtr.Size));
                return result;
            }
            finally { LocalFree(argv); }
        }

        private static void ArgumentsAndValidation(string install, string workspace)
        {
            string output = Path.Combine(workspace, "私有 接入.json");
            string subject = "映序持久身份 & literal \"quoted\"";
            var request = new ExecutionAccessRequest(install, workspace, "source_read", subject, output, null);
            string[] arguments = DecodeArguments(ExecutionAccessApi.Arguments(request));
            Check(arguments.Length == 13 && arguments[1] == "-B" && arguments[2] == Path.Combine(install, "tools", "execution_admin.py") &&
                arguments[4] == install && arguments[6] == workspace && arguments[8] == "source_read" && arguments[10] == subject && arguments[12] == output,
                "Windows argv retains Chinese, spaces, quotes and shell punctuation in one subject argument");
            foreach (string role in new[] { "source", "source_read", "worker" }) Check(ExecutionAccessApi.ValidRole(role), "supported role " + role);
            Check(!ExecutionAccessApi.ValidRole("owner") && !ExecutionAccessApi.ValidRole("source --grant-id x"), "fixed roles reject injected flags");
            foreach (string unsafeValue in new[] { "", " client", "client ", "line\nother", "Bearer " + new string('x', 32),
                "sk-" + new string('x', 24), new string('x', 43), new string('c', 64), "github_pat_" + new string('x', 24) })
                Check(!ExecutionAccessApi.SafeText(unsafeValue, 200), "reject blank, control or credential-shaped identity");
            string existing = Path.Combine(workspace, "existing.json"); File.WriteAllText(existing, "keep original");
            foreach (string unsafePath in new[] { existing, Path.Combine(workspace, "CON.json"), Path.Combine(workspace, "export.json:stream"),
                Path.Combine(workspace, "..", "outside.json"), Path.Combine(workspace, new string('x', 43) + ".json"),
                Path.Combine(workspace, "plain.txt"), @"\\localhost\share\export.json", "C:drive-relative.json", @"\root-relative.json" })
                Check(!ExecutionAccessApi.ValidOutput(unsafePath), "refuse existing, unsafe or credential-shaped export path");
            Check(File.ReadAllText(existing) == "keep original", "validation preserves existing export");
            Check(ExecutionAccessApi.ValidOutput(output), "accept new local JSON with Chinese and spaces");
            string grant = "d6086a0c-c7d5-43f5-aebd-0e98dc5c84c3";
            Check(ExecutionAccessApi.ValidGrantId(grant) && !ExecutionAccessApi.ValidGrantId(grant.ToUpperInvariant()) &&
                !ExecutionAccessApi.ValidGrantId(new string('x', 43)), "revoke accepts only canonical UUID, never token");
            arguments = DecodeArguments(ExecutionAccessApi.Arguments(new ExecutionAccessRequest(install, workspace, null, null, null, null, true, 7, grant)));
            Check(arguments.Length == 12 && arguments[7] == "--list" && arguments[8] == "--limit" && arguments[9] == "7" &&
                arguments[10] == "--after-grant-id" && arguments[11] == grant, "list uses exclusive fixed argv and canonical cursor");
            string savedUrl = Hub.Url; int savedPort = Hub.Port;
            try
            {
                Hub.Port = 19123; Hub.Url = "http://127.0.0.1:19123/";
                Check(ExecutionAccessApi.DescribeUri().AbsoluteUri == "http://127.0.0.1:19123/api/execution/describe", "describe stays on current loopback origin");
                foreach (string invalid in new[] { "http://example.com:19123/", "http://localhost:19123/", "https://127.0.0.1:19123/", "http://127.0.0.1:19124/", "http://user@127.0.0.1:19123/", "http://127.0.0.1:19123/?token=x" })
                {
                    Hub.Url = invalid; bool rejected = false;
                    try { ExecutionAccessApi.DescribeUri(); } catch { rejected = true; }
                    Check(rejected, "reject changed or untrusted describe origin");
                }
                var health = new Dictionary<string, object> { { "app", "ai-hub" }, { "control_protocol", Hub.ControlProtocol },
                    { "service_instance_id", grant }, { "install_root", install } };
                var description = Description(workspace);
                description["identity"] = new Dictionary<string, object>(health) { { "status", "available" }, { "port", Hub.Port } };
                Check(ExecutionAccessApi.ValidateDescribe(description, health) == description, "same-install matching health and describe identity accepted");
                health["service_instance_id"] = Guid.NewGuid().ToString("D"); bool mismatch = false;
                try { ExecutionAccessApi.ValidateDescribe(description, health); } catch { mismatch = true; }
                Check(mismatch, "instance replacement rejects describe");
            }
            finally { Hub.Url = savedUrl; Hub.Port = savedPort; }
        }

        private static void DialogOperations(string workspace, string renderOutput)
        {
            int calls = 0; bool confirmed = true;
            string currentRoot = workspace;
            var pending = new TaskCompletionSource<bool>();
            ExecutionAccessRequest captured = null;
            using (var dialog = new ExecutionAccessDialog(() => Task.FromResult(Description(currentRoot)), request => {
                calls++; captured = request; return pending.Task;
            }, () => confirmed))
            {
                // Create an off-screen handle, without showing or controlling a desktop window.
                IntPtr unused = dialog.Handle;
                Complete(dialog.RefreshAsync());
                Check(dialog.WorkspaceRoot == workspace && dialog.CreateButton.Enabled && dialog.RevokeButton.Enabled, "available workspace enables both native operations");
                dialog.SubjectInput.Text = "yingxu-persistent-subject"; dialog.RoleInput.SelectedIndex = 2;
                string output = Path.Combine(workspace, "form-export.json");
                Task creation = dialog.CreateAccessAsync(output);
                Check(dialog.IsBusy && calls == 1 && captured.Role == "worker" && captured.Subject == "yingxu-persistent-subject" && captured.Output == output,
                    "native inputs frozen for single create request");
                Check(!dialog.CreateButton.Enabled && !dialog.RevokeButton.Enabled && !dialog.CloseButton.Enabled && !dialog.RefreshButton.Enabled && !dialog.SubjectInput.Enabled,
                    "busy state disables create, revoke, refresh, edit and close");
                Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "second-export.json")));
                dialog.GrantIdInput.Text = Guid.NewGuid().ToString("D");
                Complete(dialog.RevokeAccessAsync()); Complete(dialog.RefreshAsync());
                dialog.Close();
                Check(calls == 1 && !dialog.IsDisposed && dialog.IsBusy, "duplicate click, revoke, refresh and close cannot interrupt or duplicate pending creation");
                pending.SetResult(false); Complete(creation);
                Check(!dialog.IsBusy && dialog.CreateButton.Enabled && !dialog.IsDisposed && dialog.SubjectInput.Text == "yingxu-persistent-subject" &&
                    dialog.StatusText.Contains("请勿自动重复创建") && !dialog.StatusText.Contains("yingxu-persistent-subject"),
                    "unconfirmed operation preserves form and identity without automatic retry or identity display");
                pending = new TaskCompletionSource<bool>(); creation = dialog.CreateAccessAsync(output);
                pending.SetException(new Exception("DO NOT DISPLAY Bearer " + new string('k', 43)));
                Complete(creation);
                Check(calls == 2 && !dialog.IsBusy && !dialog.StatusText.Contains("DO NOT DISPLAY") && !dialog.StatusText.Contains(new string('k', 43)),
                    "runner exception never leaks raw diagnostics or credentials");
                confirmed = false; Complete(dialog.RevokeAccessAsync());
                Check(calls == 2 && !dialog.IsBusy, "declining native revoke confirmation does not invoke runner");
                confirmed = true; pending = new TaskCompletionSource<bool>();
                Task revoke = dialog.RevokeAccessAsync();
                Check(dialog.IsBusy && calls == 3 && captured.GrantId == dialog.GrantIdInput.Text && captured.Subject == null && captured.Output == null && captured.Role == null,
                    "confirmed revoke contains only UUID and workspace metadata");
                Complete(dialog.CreateAccessAsync(output)); dialog.Close();
                Check(calls == 3 && !dialog.IsDisposed, "pending revoke also prevents create and closing");
                pending.SetResult(true); Complete(revoke);
                Check(dialog.StatusText.Contains("已撤销") && dialog.StatusText.Contains("原生任务"), "revoke success retains independent native-task boundary");
                pending = new TaskCompletionSource<bool>(); creation = dialog.CreateAccessAsync(output); pending.SetResult(true); Complete(creation);
                Check(dialog.StatusText.EndsWith(output) && !dialog.StatusText.Contains(captured.Subject), "create success displays selected file location only");
                if (renderOutput != null) Render(dialog, renderOutput);
                currentRoot = Path.Combine(workspace, "changed-workspace"); Directory.CreateDirectory(currentRoot);
                int before = calls; Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "changed-export.json")));
                Check(calls == before && dialog.WorkspaceRoot == null && !dialog.CreateButton.Enabled && dialog.StatusText.Contains("未提交"), "workspace change prevents grant submission");
                currentRoot = null; Complete(dialog.RefreshAsync());
                Check(!dialog.CreateButton.Enabled && !dialog.RevokeButton.Enabled && dialog.StatusText.Contains("工作环境"), "unmanaged workspace guides work-environment setup");
                dialog.Close(); Check(dialog.IsDisposed, "finished operations allow normal close");
            }
        }

        private static void Render(ExecutionAccessDialog dialog, string folder)
        {
            Directory.CreateDirectory(folder);
            dialog.StartPosition = FormStartPosition.Manual;
            dialog.Location = new Point(-30000, -30000);
            dialog.ShowInTaskbar = false;
            dialog.Show();
            Application.DoEvents();
            foreach (Size size in new[] { new Size(720, 610), new Size(624, 610), new Size(624, 521) })
            {
                dialog.ClientSize = size; dialog.PerformLayout();
                using (var bitmap = new Bitmap(dialog.Width, dialog.Height))
                {
                    dialog.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                    bitmap.Save(Path.Combine(folder, "execution-access-" + size.Width + "-" + size.Height + ".png"));
                }
                Check(dialog.CloseButton.Parent.Bottom <= dialog.ClientSize.Height && dialog.CreateButton.Width > 100 && dialog.CloseButton.Height >= 30 && dialog.RefreshButton.Height >= 30,
                    "normal/narrow layout keeps action and close controls inside form");
                var body = dialog.GrantList.Parent.Parent as Panel;
                Check(body != null && body.AutoScroll && body.VerticalScroll.Visible && dialog.GrantList.ScrollBars == ScrollBars.Both,
                    "narrow form scrolls to list and wide public metadata scrolls horizontally");
                body.ScrollControlIntoView(dialog.GrantList); Application.DoEvents();
                using (var bitmap = new Bitmap(dialog.Width, dialog.Height))
                {
                    dialog.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                    bitmap.Save(Path.Combine(folder, "execution-access-list-" + size.Width + "-" + size.Height + ".png"));
                }
                body.AutoScrollPosition = Point.Empty;
            }
        }

        private static ExecutionGrantPage Page(int number, bool more)
        {
            var items = new ExecutionGrantItem[more ? 20 : 2];
            for (int i = 0; i < items.Length; i++)
                items[i] = new ExecutionGrantItem("00000000-0000-0000-0000-" + (number * 100 + i + 1).ToString("D12"),
                    i % 2 == 0 ? "source" : "worker", "映序持久身份 " + (i + 1), "2026-10-02T01:02:03+00:00", i == 0 ? "2026-10-02T01:02:04+00:00" : null);
            return new ExecutionGrantPage(Authority, Epoch, items, more, more ? items[items.Length - 1].GrantId : null);
        }

        private static void ListOperations(string workspace, string renderOutput)
        {
            int reads = 0, mutations = 0; bool fail = false; string current = workspace;
            Dictionary<string, object> description = null;
            TaskCompletionSource<ExecutionGrantPage> pending = null;
            TaskCompletionSource<Dictionary<string, object>> pendingDescription = null;
            ExecutionAccessRequest captured = null;
            using (var dialog = new ExecutionAccessDialog(() => pendingDescription != null ? pendingDescription.Task : Task.FromResult(description ?? Description(current)),
                request => { mutations++; captured = request; return Task.FromResult(false); }, () => true, null,
                request => { reads++; captured = request; return pending != null ? pending.Task : Task.FromResult(fail ? null : Page(request.AfterGrantId == null ? 0 : 1, request.AfterGrantId == null)); }))
            {
                IntPtr unused = dialog.Handle; Complete(dialog.RefreshAsync());
                dialog.SubjectInput.Text = "未保存持久身份";
                Complete(dialog.LoadListAsync());
                Check(reads == 1 && mutations == 0 && captured.List && captured.Role == null && captured.Output == null &&
                    dialog.GrantList.Rows.Count == 20 && dialog.ListNextButton.Enabled && !dialog.ListPreviousButton.Enabled && dialog.ListPageNumber == 1,
                    "first owner list is read-only and enables correct page controls");
                dialog.GrantList.Rows[0].Selected = true; Application.DoEvents();
                Check(dialog.GrantIdInput.Text == Page(0, true).Items[0].GrantId && dialog.GrantList.Rows[0].Cells[2].Value.ToString() == "已撤销" &&
                    dialog.GrantList.Rows[0].Cells[0].Value.ToString() == "发起端" && dialog.GrantList.Rows[1].Cells[0].Value.ToString() == "执行端",
                    "selecting public row fills only UUID and preserves revoked records");
                Complete(dialog.LoadListAsync(false, 1));
                Check(reads == 2 && captured.AfterGrantId == Page(0, true).NextAfterGrantId && dialog.ListPageNumber == 2 &&
                    dialog.ListPreviousButton.Enabled && !dialog.ListNextButton.Enabled, "next page uses prior cursor and retains previous-page navigation");
                Complete(dialog.LoadListAsync(false, -1));
                Check(dialog.ListPageNumber == 1 && captured.AfterGrantId == null, "previous page restores original cursor");
                fail = true; Complete(dialog.LoadListAsync(false, 1));
                Check(dialog.ListPageNumber == 1 && dialog.GrantList.Rows.Count == 20 && !dialog.GrantList.Enabled &&
                    !dialog.ListNextButton.Enabled && dialog.SubjectInput.Text == "未保存持久身份" && dialog.ListStatusText.Contains("上一页保留"),
                    "failed page retains public rows and unsaved form but disables stale selection and navigation");
                fail = false; Complete(dialog.LoadListAsync());
                Check(dialog.GrantList.Enabled && dialog.ListNextButton.Enabled && mutations == 0, "explicit refresh revalidates same page without creating a grant");
                pending = new TaskCompletionSource<ExecutionGrantPage>(); Task reading = dialog.LoadListAsync();
                Check(dialog.IsBusy && !dialog.CloseButton.Enabled && !dialog.CreateButton.Enabled && !dialog.ListRefreshButton.Enabled && !dialog.GrantList.Enabled,
                    "list subprocess is included in host exit/update busy interval");
                int before = reads; Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "list-busy.json")));
                Complete(dialog.RevokeAccessAsync()); Complete(dialog.LoadListAsync()); dialog.Close();
                Check(!dialog.IsDisposed && reads == before && mutations == 0, "pending list blocks close, mutation and duplicate read");
                dialog.SetHostAvailable(false); pending.SetResult(Page(0, true)); Complete(reading); pending = null;
                Check(!dialog.GrantList.Enabled && !dialog.ListRefreshButton.Enabled && dialog.SubjectInput.Text == "未保存持久身份",
                    "parent admission change during list preserves state and disables operations");
                dialog.SetHostAvailable(true); Complete(dialog.LoadListAsync());
                pendingDescription = new TaskCompletionSource<Dictionary<string, object>>(); reading = dialog.LoadListAsync(); before = reads;
                dialog.SetHostAvailable(false); pendingDescription.SetResult(Description(workspace)); Complete(reading); pendingDescription = null;
                Check(reads == before, "parent admission change during pre-list describe blocks CLI start");
                dialog.SetHostAvailable(true); Complete(dialog.LoadListAsync());
                description = Description(workspace); description["ledger_epoch"] = Guid.NewGuid().ToString("D"); Complete(dialog.RefreshAsync());
                Check(!dialog.GrantList.Enabled && dialog.GrantList.Rows.Count == 20, "same root with changed ledger invalidates retained page");
                description = null; Complete(dialog.LoadListAsync(true));
                if (renderOutput != null) Render(dialog, renderOutput);
                Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "list-failed-create.json")));
                Check(mutations == 1 && !dialog.GrantList.Enabled && dialog.ListRefreshButton.Enabled && dialog.StatusText.Contains("请勿自动重复创建"),
                    "uncertain create leaves explicit list reconciliation available without automatic refresh or mint");
                Complete(dialog.LoadListAsync()); Check(mutations == 1 && dialog.GrantList.Enabled, "manual reconciliation after failed create performs only owner list");
                current = null; Complete(dialog.RefreshAsync());
                Check(!dialog.ListRefreshButton.Enabled && !dialog.GrantList.Enabled && dialog.GrantList.Rows.Count == 20, "unavailable workspace preserves old rows as inactive history");
            }
        }

        private static byte[] PublicJson(ExecutionGrantPage page)
        {
            var items = new List<Dictionary<string, object>>();
            foreach (var item in page.Items) items.Add(new Dictionary<string, object> {
                { "grant_id", item.GrantId }, { "role", item.Role }, { "subject", item.Subject }, { "created_at", item.CreatedAt }, { "revoked_at", item.RevokedAt } });
            string json = new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "protocol", "aihub-execution/1" }, { "authority_id", page.AuthorityId }, { "ledger_epoch", page.LedgerEpoch },
                { "items", items }, { "has_more", page.HasMore }, { "next_after_grant_id", page.NextAfterGrantId } });
            var ascii = new StringBuilder();
            foreach (char c in json) { if (c > 127) ascii.Append("\\u" + ((int)c).ToString("x4")); else ascii.Append(c); }
            return Encoding.ASCII.GetBytes(ascii.ToString());
        }

        private static void ListOutputValidation(string install, string workspace)
        {
            byte[] safe = PublicJson(Page(0, true));
            Check(ExecutionAccessApi.ParseList(safe, 20, null).Items.Length == 20, "public ASCII list JSON accepts bounded Chinese identity metadata");
            string json = Encoding.ASCII.GetString(safe);
            foreach (string invalid in new[] {
                json.Replace("\"has_more\":true", "\"has_more\":true,\"has_more\":false"),
                json.Replace("\"protocol\":", "\"token\":\"" + new string('k', 43) + "\",\"protocol\":"),
                json.Replace("\"source\"", "\"owner\""), json.Replace("2026-10-02T01:02:03+00:00", "2026-13-02T01:02:03+00:00"),
                json.Replace("2026-10-02T01:02:04+00:00", "2026-13-02T01:02:04+00:00"),
                json.Replace("\"authority_id\":\"" + Authority + "\"", "\"authority_id\":null"),
                json + " private diagnostic " + new string('k', 43), "{}", new string('x', 131073) })
            {
                bool rejected = false; try { ExecutionAccessApi.ParseList(Encoding.ASCII.GetBytes(invalid), 20, null); } catch { rejected = true; }
                Check(rejected, "reject malformed, duplicate, secret, oversized or inconsistent public list output");
            }
            Check(ExecutionAccessApi.ParseList(PublicJson(new ExecutionGrantPage(null, null, new ExecutionGrantItem[0], false, null)), 20, null).Items.Length == 0,
                "empty ledger permits only paired null authority and epoch with empty terminal page");
            var unicode = new ExecutionGrantPage(Authority, Epoch, new[] { new ExecutionGrantItem(Guid.NewGuid().ToString("D"), "source", "映序 😀", "2026-10-02T01:02:03.123456Z", null) }, false, null);
            Check(ExecutionAccessApi.ParseList(PublicJson(unicode), 20, null).Items[0].Subject == "映序 😀", "valid Unicode scalar identity and UTC microsecond timestamp survive safe JSON");
            string script = Path.Combine(install, "tools", "execution_admin.py");
            var request = new ExecutionAccessRequest(install, workspace, null, null, null, null, true);
            File.WriteAllText(script, "import sys\nsys.stdout.write(" + new JavaScriptSerializer().Serialize(Encoding.ASCII.GetString(safe)) + ")\nsys.stderr.write('discarded private diagnostic')\n", new UTF8Encoding(false));
            var run = ExecutionAccessApi.ListAsync(request); Complete(run);
            Check(run.Result != null && run.Result.Items.Length == 20, "real list subprocess decodes only dedicated safe JSON and discards stderr");
            File.WriteAllText(script, "import sys\nsys.stdout.write('x' * 160000)\nsys.stderr.write('y' * 160000)\n", new UTF8Encoding(false));
            run = ExecutionAccessApi.ListAsync(request); Complete(run);
            Check(run.Result == null, "oversized list streams drain concurrently without retained diagnostics or deadlock");
            File.WriteAllText(script, "import sys\nsys.stdout.write(" + new JavaScriptSerializer().Serialize(Encoding.ASCII.GetString(safe)) + ")\nsys.exit(1)\n", new UTF8Encoding(false));
            run = ExecutionAccessApi.ListAsync(request); Complete(run);
            Check(run.Result == null, "nonzero list exit never presents a plausible public page as verified");
        }

        private static void HostAdmission(string workspace)
        {
            int calls = 0, descriptions = 0, confirmations = 0, selections = 0;
            TaskCompletionSource<Dictionary<string, object>> pending = null;
            using (var dialog = new ExecutionAccessDialog(() => {
                descriptions++; return pending == null ? Task.FromResult(Description(workspace)) : pending.Task;
            }, request => { calls++; return Task.FromResult(true); }, () => { confirmations++; return true; },
            () => { selections++; return Path.Combine(workspace, "host-export.json"); }))
            {
                Complete(dialog.RefreshAsync());
                dialog.SubjectInput.Text = "source-persistent-draft";
                dialog.GrantIdInput.Text = Guid.NewGuid().ToString("D");
                dialog.SetHostAvailable(false);
                Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "host-export.json")));
                Complete(dialog.ChooseAndCreateAsync()); Complete(dialog.RevokeAccessAsync()); Complete(dialog.RefreshAsync());
                Check(calls == 0 && confirmations == 0 && selections == 0 && descriptions == 1 && !dialog.CreateButton.Enabled && !dialog.RevokeButton.Enabled,
                    "parent exit/update admission blocks all existing-dialog operations before any await");
                Check(dialog.SubjectInput.Text == "source-persistent-draft" && dialog.CloseButton.Enabled,
                    "host suspension preserves unsaved identity and leaves idle close available");
                dialog.SetHostAvailable(true);
                Check(dialog.CreateButton.Enabled && dialog.RevokeButton.Enabled && dialog.SubjectInput.Enabled,
                    "deferred parent exit/update restores admission without losing the form");
                pending = new TaskCompletionSource<Dictionary<string, object>>();
                Task creation = dialog.CreateAccessAsync(Path.Combine(workspace, "host-export.json"));
                Check(dialog.IsBusy && calls == 0, "scope validation can wait without invoking CLI");
                var closingMethod = typeof(ExecutionAccessDialog).GetMethod("OnFormClosing", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                foreach (CloseReason reason in new[] { CloseReason.UserClosing, CloseReason.FormOwnerClosing, CloseReason.WindowsShutDown, CloseReason.TaskManagerClosing })
                {
                    var closing = new FormClosingEventArgs(reason, false);
                    closingMethod.Invoke(dialog, new object[] { closing });
                    Check(closing.Cancel == (reason == CloseReason.UserClosing || reason == CloseReason.FormOwnerClosing),
                        "busy dialog preserves user close protection while allowing system ending: " + reason);
                }
                dialog.SetSystemEnding(true);
                var ownedSystemClose = new FormClosingEventArgs(CloseReason.FormOwnerClosing, false);
                closingMethod.Invoke(dialog, new object[] { ownedSystemClose });
                Check(!ownedSystemClose.Cancel, "owner session-end query is not vetoed by a busy child");
                dialog.SetSystemEnding(false);
                var ownedUserClose = new FormClosingEventArgs(CloseReason.FormOwnerClosing, false);
                closingMethod.Invoke(dialog, new object[] { ownedUserClose });
                Check(ownedUserClose.Cancel, "ordinary owner close protection returns after session query");
                dialog.SetHostAvailable(false); pending.SetResult(Description(workspace)); Complete(creation);
                Check(calls == 0 && !dialog.IsBusy && !dialog.CreateButton.Enabled && dialog.StatusText.Contains("未提交"),
                    "parent admission change during describe is rechecked before owner CLI submission");
                dialog.SetHostAvailable(true); pending = null;
                Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "host-export.json")));
                Check(calls == 1 && dialog.StatusText.Contains("已创建"), "restored host can submit exactly one original operation");
            }
        }

        private static void NativeSelection(string workspace)
        {
            int calls = 0; string output = Path.Combine(workspace, "selection-export.json");
            ExecutionAccessDialog dialog = null;
            dialog = new ExecutionAccessDialog(() => Task.FromResult(Description(workspace)), request => { calls++; return Task.FromResult(true); }, () => true, () => {
                Check(dialog.IsBusy && !dialog.CreateButton.Enabled && !dialog.CloseButton.Enabled, "native save chooser is inside busy interval");
                Complete(dialog.CreateAccessAsync(Path.Combine(workspace, "nested-export.json"))); Complete(dialog.RefreshAsync());
                dialog.Close();
                Check(!dialog.IsDisposed, "nested chooser message loop cannot close form");
                return output;
            });
            using (dialog)
            {
                IntPtr unused = dialog.Handle; Complete(dialog.RefreshAsync()); dialog.SubjectInput.Text = "frameweave-persistent-id";
                Complete(dialog.ChooseAndCreateAsync());
                Check(calls == 1 && !dialog.IsBusy && dialog.StatusText.EndsWith(output), "chosen export runs once after protected chooser returns");
                output = null; Complete(dialog.ChooseAndCreateAsync());
                Check(calls == 1 && !dialog.IsBusy && dialog.SubjectInput.Text == "frameweave-persistent-id", "cancelled chooser preserves identity and does not create grant");
            }
        }

        private static void ProcessOutput(string install, string workspace)
        {
            string tools = Path.Combine(install, "tools"); Directory.CreateDirectory(tools);
            string script = Path.Combine(tools, "execution_admin.py");
            var request = new ExecutionAccessRequest(install, workspace, "source", "fixture-client", Path.Combine(workspace, "process-export.json"), null);
            File.WriteAllText(script, "import sys\nsys.stdout.write('fixture token must not be decoded\\n')\nsys.stderr.write('private error\\n')\n", new UTF8Encoding(false));
            Task<bool> run = ExecutionAccessApi.RunAsync(request); Complete(run);
            Check(run.Result, "isolated CLI exits successfully with discarded stdout and stderr");
            File.WriteAllText(script, "import sys\nsys.stdout.write('x' * 160000)\nsys.stderr.write('y' * 160000)\n", new UTF8Encoding(false));
            run = ExecutionAccessApi.RunAsync(request); Complete(run);
            Check(!run.Result, "both oversized process streams are drained concurrently without success or deadlock");
            File.WriteAllText(script, "import sys\nsys.stderr.write('untrusted credential diagnostic')\nsys.exit(1)\n", new UTF8Encoding(false));
            run = ExecutionAccessApi.RunAsync(request); Complete(run);
            Check(!run.Result, "nonzero process exit is unconfirmed with no returned diagnostic text");
        }

        internal static void Run(string tempFolder, string renderOutput = null)
        {
            string originalRoot = Hub.Root;
            string install = Path.Combine(tempFolder, "协作 接入原生安装"), workspace = Path.Combine(tempFolder, "协作 接入工作区");
            Directory.CreateDirectory(install); Directory.CreateDirectory(workspace);
            try
            {
                Hub.Root = install;
                ArgumentsAndValidation(install, workspace);
                DialogOperations(workspace, renderOutput);
                HostAdmission(workspace);
                NativeSelection(workspace);
                ProcessOutput(install, workspace);
                ListOutputValidation(install, workspace);
                ListOperations(workspace, renderOutput);
            }
            finally { Hub.Root = originalRoot; }
        }

        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);
    }
}
