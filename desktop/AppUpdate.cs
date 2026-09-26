using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace AIHub.Desktop
{
    internal static class AppUpdateApi
    {
        private const int MaximumResponseBytes = 65536;
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

        internal static Dictionary<string, object> Status()
        {
            var request = Request("GET", "/api/app-update/status");
            return Read(request);
        }

        internal static Dictionary<string, object> Check()
        {
            return Post("/api/desktop/update/check", new Dictionary<string, object>());
        }

        internal static Dictionary<string, object> Settings(bool autoCheck, bool autoInstall)
        {
            return Post("/api/desktop/update/settings", new Dictionary<string, object> {
                { "auto_check", autoCheck }, { "auto_install", autoInstall }
            });
        }

        internal static Dictionary<string, object> Download(string releaseId)
        {
            if (String.IsNullOrWhiteSpace(releaseId) || releaseId.Length > 160)
                throw new InvalidDataException("更新标识无效，请重新检查更新。");
            return Post("/api/desktop/update/download", new Dictionary<string, object> { { "release_id", releaseId } });
        }

        internal static Dictionary<string, object> Prepare(string releaseId, bool restart)
        {
            if (String.IsNullOrWhiteSpace(releaseId) || releaseId.Length > 160)
                throw new InvalidDataException("更新标识无效，请重新检查更新。");
            Process current = Process.GetCurrentProcess();
            var desktop = new Dictionary<string, object> {
                { "pid", current.Id },
                { "start_filetime", current.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(CultureInfo.InvariantCulture) }
            };
            return Post("/api/desktop/update/prepare", new Dictionary<string, object> {
                { "release_id", releaseId }, { "restart", restart }, { "desktop", desktop }
            });
        }

        internal static void Cancel(string transactionId)
        {
            Post("/api/desktop/update/cancel", new Dictionary<string, object> { { "transaction_id", transactionId } });
        }

        private static Dictionary<string, object> Post(string route, Dictionary<string, object> arguments)
        {
            Dictionary<string, object> control = ReadControlAndVerifyService();
            object tokenValue;
            string token = control.TryGetValue("token", out tokenValue) ? tokenValue as string : null;
            string instance = Hub.TextValue(control, "instance_id");
            if (String.IsNullOrEmpty(token) || String.IsNullOrEmpty(instance))
                throw new InvalidDataException("本机更新控制信息不完整，请重新打开曜核。");

            var body = new Dictionary<string, object> {
                { "instance_id", instance }, { "install_root", Hub.Root }
            };
            foreach (var argument in arguments) body.Add(argument.Key, argument.Value);
            var request = Request("POST", route);
            request.ContentType = "application/json; charset=utf-8";
            request.Headers["X-AIHub-Control-Token"] = token;
            byte[] bytes = Encoding.UTF8.GetBytes(Serializer.Serialize(body));
            request.ContentLength = bytes.Length;
            using (var stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
            return Read(request);
        }

        private static Dictionary<string, object> ReadControlAndVerifyService()
        {
            string controlPath = Path.Combine(Hub.Cache, "server-control.json");
            if (!File.Exists(controlPath)) throw new InvalidOperationException("本机服务尚未提供更新控制接口，请从托盘重试打开工作台。");
            Dictionary<string, object> control = Serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(controlPath, Encoding.UTF8));
            bool absent;
            Dictionary<string, object> health = Hub.ReadControlHealth(out absent);
            if (absent || !Hub.ControlMatches(control, health))
                throw new InvalidOperationException("本机服务身份已变化，更新操作已停止。请重新打开曜核后重试。");
            return control;
        }

        private static HttpWebRequest Request(string method, string route)
        {
            // Routes are compile-time constants; no caller supplied URL or path is accepted.
            var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + Hub.Port + route);
            request.Method = method;
            request.Proxy = null;
            request.AllowAutoRedirect = false;
            request.Timeout = 8000;
            request.ReadWriteTimeout = 3000;
            request.KeepAlive = false;
            return request;
        }

        private static Dictionary<string, object> Read(HttpWebRequest request)
        {
            try
            {
                using (var response = (HttpWebResponse)request.GetResponse()) return ReadBody(response);
            }
            catch (WebException error)
            {
                var response = error.Response as HttpWebResponse;
                if (response == null) throw new InvalidOperationException("无法连接曜核本机更新服务。请确认工作台已打开后重试。");
                using (response)
                {
                    Dictionary<string, object> body = null;
                    try { body = ReadBody(response); } catch { }
                    string message = Hub.TextValue(body, "message");
                    if (String.IsNullOrWhiteSpace(message)) message = Hub.TextValue(body, "safe_message");
                    if (String.IsNullOrWhiteSpace(message))
                    {
                        if ((int)response.StatusCode == 403) message = "本机更新请求未通过身份校验，请重新打开曜核后重试。";
                        else if ((int)response.StatusCode == 409) message = "当前有更新操作正在进行，请稍后重试。";
                        else message = "更新服务暂时无法完成该操作，请稍后重试。";
                    }
                    throw new InvalidOperationException(message);
                }
            }
        }

        private static Dictionary<string, object> ReadBody(HttpWebResponse response)
        {
            using (var stream = response.GetResponseStream())
            using (var reader = new StreamReader(stream, Encoding.UTF8))
            {
                char[] buffer = new char[MaximumResponseBytes + 1];
                int count = 0, read;
                while (count < buffer.Length && (read = reader.Read(buffer, count, buffer.Length - count)) > 0) count += read;
                if (count > MaximumResponseBytes) throw new InvalidDataException("本机更新服务响应过大。");
                var result = Serializer.Deserialize<Dictionary<string, object>>(new string(buffer, 0, count));
                if (result == null) throw new InvalidDataException("本机更新服务响应格式无效。");
                return result;
            }
        }
    }

    internal sealed class AppUpdateDialog : Form
    {
        private static readonly Color Canvas = Color.FromArgb(16, 21, 39);
        private static readonly Color Surface = Color.FromArgb(25, 32, 55);
        private static readonly Color SurfaceRaised = Color.FromArgb(30, 39, 65);
        private static readonly Color Line = Color.FromArgb(48, 59, 88);
        private static readonly Color Ink = Color.FromArgb(244, 241, 232);
        private static readonly Color Muted = Color.FromArgb(168, 179, 201);
        private static readonly Color Apricot = Color.FromArgb(232, 190, 113);
        private static readonly Color Coral = Color.FromArgb(220, 139, 116);
        private static readonly Color Mint = Color.FromArgb(119, 201, 175);
        private static readonly Font VersionFont = new Font("Microsoft YaHei UI", 16F, FontStyle.Bold, GraphicsUnit.Point);
        private static readonly Font LongVersionFont = new Font("Microsoft YaHei UI", 11F, FontStyle.Bold, GraphicsUnit.Point);
        private const long AutomaticDownloadLimit = 50L * 1024 * 1024;

        private readonly Label stateLabel;
        private readonly Label currentVersionValue;
        private readonly Label latestVersionValue;
        private readonly Label progressLabel;
        private readonly TextBox notes;
        private readonly CheckBox autoCheck;
        private readonly CheckBox autoInstall;
        private readonly Button actionButton;
        private readonly LinkLabel recheckLink;
        private readonly Button closeButton;
        private readonly Label settingsSummary;
        private readonly LinkLabel settingsToggle;
        private readonly TableLayoutPanel settingsPanel;
        private readonly Panel settingsChoices;
        private readonly ProgressLine progressLine;
        private readonly System.Windows.Forms.Timer refreshTimer;
        private readonly TableLayoutPanel layout;
        private Dictionary<string, object> status;
        private bool working;
        private bool renderingSettings;
        private bool refreshing;
        private bool settingsExpanded;
        private int requestSerial;
        private string inlineError;
        private readonly bool previewOnly;
        internal event Action InstallRequested;

        internal AppUpdateDialog() : this(false) { }

        internal AppUpdateDialog(bool previewOnly)
        {
            this.previewOnly = previewOnly;
            Text = "曜核 · 软件更新";
            BackColor = Canvas;
            ForeColor = Ink;
            Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
            AutoScaleMode = AutoScaleMode.Dpi;
            MinimumSize = new Size(650, 590);
            ClientSize = new Size(760, 630);
            StartPosition = FormStartPosition.CenterParent;
            KeyPreview = true;
            Icon = LoadBrandIcon();

            layout = new TableLayoutPanel {
                Dock = DockStyle.Fill, Padding = new Padding(26, 22, 26, 20),
                ColumnCount = 1, RowCount = 8, BackColor = Canvas
            };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 74));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 58));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 122));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 56));
            Controls.Add(layout);

            var header = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Canvas };
            header.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 66));
            header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            var brandMark = new EyeMark { Dock = DockStyle.Fill, Margin = new Padding(0, 4, 12, 4), BackColor = Canvas };
            brandMark.AccessibleName = "曜核 E01 金色眼形标识";
            brandMark.AccessibleRole = AccessibleRole.Graphic;
            var brandText = new TableLayoutPanel {
                Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, BackColor = Canvas,
                Padding = new Padding(0, 9, 0, 8)
            };
            brandText.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
            brandText.RowStyles.Add(new RowStyle(SizeType.Absolute, 21));
            var brandName = new Label {
                Dock = DockStyle.Fill, Text = "曜核", ForeColor = Ink,
                Font = new Font(Font.FontFamily, 17F, FontStyle.Bold, GraphicsUnit.Point),
                TextAlign = ContentAlignment.BottomLeft, AutoEllipsis = true
            };
            var brandSubtitle = new Label {
                Dock = DockStyle.Fill, Text = "本地 AI 资产与协作工作台", ForeColor = Muted,
                Font = new Font(Font.FontFamily, 9F, FontStyle.Regular, GraphicsUnit.Point),
                TextAlign = ContentAlignment.TopLeft, AutoEllipsis = true
            };
            brandText.Controls.Add(brandName, 0, 0);
            brandText.Controls.Add(brandSubtitle, 0, 1);
            header.Controls.Add(brandMark, 0, 0);
            header.Controls.Add(brandText, 1, 0);

            var statusPanel = new Panel { Dock = DockStyle.Fill, BackColor = Surface, Padding = new Padding(16, 4, 12, 4), Margin = new Padding(0, 2, 0, 2) };
            statusPanel.Paint += delegate(object sender, PaintEventArgs e) {
                using (var pen = new Pen(Apricot, 2F)) e.Graphics.DrawLine(pen, 1, 8, 1, statusPanel.Height - 9);
            };
            stateLabel = new Label {
                Dock = DockStyle.Fill, Font = new Font(Font, FontStyle.Bold),
                ForeColor = Ink, Text = "准备检查软件更新", TextAlign = ContentAlignment.MiddleLeft,
                AutoEllipsis = true, AccessibleName = "更新状态"
            };
            statusPanel.Controls.Add(stateLabel);

            var versionCard = BuildVersionCard(out currentVersionValue, out latestVersionValue);

            var notesHeading = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Canvas };
            notesHeading.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            notesHeading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 55));
            notesHeading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 45));
            var notesTitle = new Label {
                Dock = DockStyle.Fill, Text = "更新说明", ForeColor = Ink,
                Font = new Font(Font, FontStyle.Bold), TextAlign = ContentAlignment.MiddleLeft
            };
            recheckLink = new LinkLabel {
                Dock = DockStyle.Fill, Text = "重新检查", LinkColor = Apricot, ActiveLinkColor = Ink, VisitedLinkColor = Apricot,
                Font = new Font(Font.FontFamily, 9F, FontStyle.Regular, GraphicsUnit.Point),
                TextAlign = ContentAlignment.MiddleRight, AutoEllipsis = true, AccessibleName = "重新检查软件更新"
            };
            recheckLink.LinkClicked += async delegate {
                await RunAction(async () => status = await Task.Run(() => AppUpdateApi.Check()));
            };
            notesHeading.Controls.Add(notesTitle, 0, 0);
            notesHeading.Controls.Add(recheckLink, 1, 0);

            var notesSurface = new Panel {
                Dock = DockStyle.Fill, BackColor = Surface, Padding = new Padding(13, 11, 13, 10),
                Margin = new Padding(0, 0, 0, 3), BorderStyle = BorderStyle.FixedSingle
            };
            notes = new TextBox {
                Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, WordWrap = true,
                ScrollBars = ScrollBars.None, BorderStyle = BorderStyle.None,
                BackColor = Surface, ForeColor = Ink, Font = new Font(Font.FontFamily, 10F, FontStyle.Regular, GraphicsUnit.Point),
                Text = "检查更新后，此处会显示版本变化和更新说明。", TabStop = true,
                AccessibleName = "更新说明，可使用方向键和滚动条阅读"
            };
            notes.Resize += delegate { UpdateNotesScrollBar(); };
            notes.TextChanged += delegate { UpdateNotesScrollBar(); };
            notesSurface.Controls.Add(notes);

            var progressPanel = new TableLayoutPanel {
                Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, BackColor = Canvas, Margin = new Padding(0)
            };
            progressPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 24));
            progressPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 4));
            progressLabel = new Label {
                Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft,
                ForeColor = Muted, Font = new Font(Font.FontFamily, 9F, FontStyle.Regular, GraphicsUnit.Point),
                AutoEllipsis = true, AccessibleName = "下载与校验进度", Margin = new Padding(0)
            };
            progressLine = new ProgressLine { Dock = DockStyle.Fill, Visible = false, Margin = new Padding(0) };
            progressPanel.Controls.Add(progressLabel, 0, 0);
            progressPanel.Controls.Add(progressLine, 0, 1);

            settingsPanel = new TableLayoutPanel {
                Dock = DockStyle.Fill, BackColor = Canvas, Margin = new Padding(0),
                ColumnCount = 2, RowCount = 2, AutoSize = false,
                AccessibleName = "更新偏好"
            };
            settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 21F));
            settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 79F));
            settingsPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, ScaleLogical(28)));
            settingsPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 0));
            settingsToggle = new LinkLabel {
                Text = "更新偏好　＋", AutoSize = true, Dock = DockStyle.Fill,
                LinkColor = Apricot, ActiveLinkColor = Ink, VisitedLinkColor = Apricot,
                Font = new Font(Font.FontFamily, 9F, FontStyle.Bold, GraphicsUnit.Point),
                TabIndex = 0, AccessibleName = "展开更新偏好", Margin = new Padding(0, 5, 5, 3)
            };
            settingsSummary = new Label {
                AutoSize = true, Dock = DockStyle.Fill,
                ForeColor = Muted, Font = new Font(Font.FontFamily, 9F, FontStyle.Regular, GraphicsUnit.Point),
                TextAlign = ContentAlignment.MiddleLeft,
                AccessibleName = "当前更新偏好摘要"
            };
            var choicesLayout = new FlowLayoutPanel {
                Dock = DockStyle.Fill, AutoSize = false, AutoScroll = false,
                FlowDirection = FlowDirection.LeftToRight, WrapContents = false, Visible = false, BackColor = Canvas,
                AccessibleName = "更新偏好选项"
            };
            autoCheck = new CheckBox {
                AutoSize = true, Text = "自动检查候选版更新", ForeColor = Ink, BackColor = Canvas,
                TabIndex = 1, Margin = new Padding(0, 3, 22, 3), AccessibleName = "自动检查候选版更新"
            };
            autoInstall = new CheckBox {
                AutoSize = true, Text = "退出曜核时自动安装已准备好的更新", ForeColor = Ink, BackColor = Canvas,
                TabIndex = 2, Margin = new Padding(0, 3, 0, 3), AccessibleName = "退出曜核时自动安装已准备好的更新"
            };
            choicesLayout.Controls.Add(autoCheck);
            choicesLayout.Controls.Add(autoInstall);
            settingsChoices = choicesLayout;
            settingsPanel.Controls.Add(settingsToggle, 0, 0);
            settingsPanel.Controls.Add(settingsSummary, 1, 0);
            settingsPanel.Controls.Add(settingsChoices, 0, 1);
            settingsPanel.SetColumnSpan(settingsChoices, 2);

            var actions = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Canvas };
            actions.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 104));
            actionButton = MakeButton("检查更新", true);
            actionButton.Dock = DockStyle.Right;
            actionButton.Width = 188;
            actionButton.Height = 42;
            actionButton.TabIndex = 0;
            actionButton.AccessibleName = "检查更新";
            closeButton = MakeButton("关闭", false);
            closeButton.Dock = DockStyle.Fill;
            closeButton.Margin = new Padding(8, 0, 0, 0);
            closeButton.TabIndex = 1;
            closeButton.AccessibleName = "关闭软件更新窗口";
            actions.Controls.Add(actionButton, 0, 0);
            actions.Controls.Add(closeButton, 1, 0);

            layout.Controls.Add(header, 0, 0);
            layout.Controls.Add(statusPanel, 0, 1);
            layout.Controls.Add(versionCard, 0, 2);
            layout.Controls.Add(notesHeading, 0, 3);
            layout.Controls.Add(notesSurface, 0, 4);
            layout.Controls.Add(progressPanel, 0, 5);
            layout.Controls.Add(settingsPanel, 0, 6);
            layout.Controls.Add(actions, 0, 7);
            ActiveControl = actionButton;
            AcceptButton = actionButton;
            CancelButton = closeButton;

            closeButton.Click += delegate { Close(); };
            actionButton.Click += async delegate { await RunPrimaryActionAsync(); };
            closeButton.GotFocus += delegate { closeButton.Invalidate(); };
            closeButton.LostFocus += delegate { closeButton.Invalidate(); };
            actionButton.GotFocus += delegate { actionButton.Invalidate(); };
            actionButton.LostFocus += delegate { actionButton.Invalidate(); };
            actionButton.Paint += PaintButtonFocus;
            closeButton.Paint += PaintButtonFocus;
            settingsToggle.LinkClicked += delegate { ToggleSettings(); };
            autoCheck.CheckedChanged += SettingsChanged;
            autoInstall.CheckedChanged += SettingsChanged;
            refreshTimer = new System.Windows.Forms.Timer { Interval = 1800 };
            refreshTimer.Tick += async delegate { if (!working) await RefreshStatusAsync(false); };
            Shown += async delegate
            {
                if (!this.previewOnly)
                {
                    await RefreshStatusAsync(true);
                    if (!IsDisposed) refreshTimer.Start();
                }
            };
            FormClosed += delegate {
                refreshTimer.Stop();
                refreshTimer.Dispose();
                if (Icon != null) { Icon.Dispose(); Icon = null; }
            };
        }

        private static Icon LoadBrandIcon()
        {
            try
            {
                Stream embedded = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico");
                if (embedded != null)
                {
                    using (embedded) return new Icon(embedded);
                }
                string[] candidates = new[] {
                    Path.Combine(Environment.CurrentDirectory, "frontend", "brand.ico"),
                    Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "frontend", "brand.ico"))
                };
                foreach (string path in candidates)
                {
                    if (!File.Exists(path)) continue;
                    using (var stream = File.OpenRead(path)) return new Icon(stream);
                }
            }
            catch { }
            return null;
        }

        private static Control BuildVersionCard(out Label currentValue, out Label latestValue)
        {
            var card = new TableLayoutPanel {
                Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, BackColor = SurfaceRaised,
                Padding = new Padding(14, 10, 14, 10), Margin = new Padding(0, 3, 0, 3)
            };
            card.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            card.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 45));
            card.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 54));
            card.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 55));

            var current = VersionBlock("当前版本", out currentValue);
            var latest = VersionBlock("更新版本", out latestValue);
            var arrow = new Label {
                Dock = DockStyle.Fill, Text = "→", ForeColor = Apricot,
                Font = new Font("Segoe UI Symbol", 19F, FontStyle.Regular, GraphicsUnit.Point),
                TextAlign = ContentAlignment.MiddleCenter, AccessibleName = "版本更新方向"
            };
            card.Controls.Add(current, 0, 0);
            card.Controls.Add(arrow, 1, 0);
            card.Controls.Add(latest, 2, 0);
            return card;
        }

        private static Control VersionBlock(string title, out Label value)
        {
            var block = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, BackColor = SurfaceRaised };
            block.RowStyles.Add(new RowStyle(SizeType.Absolute, 24));
            block.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            var heading = new Label {
                Dock = DockStyle.Fill, Text = title, ForeColor = Muted,
                Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point),
                TextAlign = ContentAlignment.MiddleLeft
            };
            value = new Label {
                Dock = DockStyle.Fill, Text = "—", ForeColor = Ink,
                Font = new Font("Microsoft YaHei UI", 16F, FontStyle.Bold, GraphicsUnit.Point),
                TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = false,
                AccessibleName = title + "号"
            };
            block.Controls.Add(heading, 0, 0);
            block.Controls.Add(value, 0, 1);
            return block;
        }

        private static Button MakeButton(string text, bool primary)
        {
            var button = new Button {
                Text = text, Height = 42, Margin = new Padding(0), FlatStyle = FlatStyle.Flat,
                BackColor = primary ? Apricot : SurfaceRaised,
                ForeColor = primary ? Canvas : Ink, Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold, GraphicsUnit.Point),
                UseVisualStyleBackColor = false, AutoSize = false, Cursor = Cursors.Hand
            };
            button.FlatAppearance.BorderSize = 1;
            button.FlatAppearance.BorderColor = primary ? Apricot : Line;
            button.FlatAppearance.MouseOverBackColor = primary ? Color.FromArgb(243, 204, 132) : Color.FromArgb(38, 48, 76);
            button.FlatAppearance.MouseDownBackColor = primary ? Color.FromArgb(211, 166, 91) : Surface;
            return button;
        }

        private void PaintButtonFocus(object sender, PaintEventArgs e)
        {
            var button = sender as Button;
            if (button == null || !button.Focused || button.ClientSize.Width < 6 || button.ClientSize.Height < 6) return;
            using (var pen = new Pen(button.Enabled ? Color.FromArgb(130, 216, 201) : Muted, 2F))
                e.Graphics.DrawRectangle(pen, 2, 2, button.ClientSize.Width - 5, button.ClientSize.Height - 5);
        }

        private void ToggleSettings()
        {
            settingsExpanded = !settingsExpanded;
            settingsChoices.Visible = settingsExpanded;
            settingsToggle.Text = settingsExpanded ? "更新偏好　－" : "更新偏好　＋";
            settingsToggle.AccessibleName = settingsExpanded ? "收起更新偏好" : "展开更新偏好";
            settingsPanel.RowStyles[0].Height = ScaleLogical(28);
            settingsPanel.RowStyles[1].Height = settingsExpanded ? ScaleLogical(42) : 0;
            layout.RowStyles[6].Height = ScaleLogical(settingsExpanded ? 78 : 42);
            settingsPanel.PerformLayout();
            layout.PerformLayout();
            if (settingsExpanded) autoCheck.Focus();
        }

        private int ScaleLogical(int value)
        {
            return (int)Math.Ceiling(value * (DeviceDpi > 0 ? DeviceDpi : 96) / 96D);
        }

        private void UpdateNotesScrollBar()
        {
            if (notes == null || notes.IsDisposed || notes.ClientSize.Width <= 0 || notes.ClientSize.Height <= 0) return;
            int lineHeight = Math.Max(1, TextRenderer.MeasureText("国Ag", notes.Font).Height);
            int contentHeight = 0;
            string[] lines = (notes.Text ?? "").Replace("\r\n", "\n").Split('\n');
            foreach (string line in lines)
            {
                Size measured = TextRenderer.MeasureText(String.IsNullOrEmpty(line) ? " " : line, notes.Font,
                    new Size(Math.Max(1, notes.ClientSize.Width - 2), Int32.MaxValue),
                    TextFormatFlags.WordBreak | TextFormatFlags.TextBoxControl | TextFormatFlags.ExpandTabs);
                contentHeight += Math.Max(lineHeight, measured.Height);
                if (contentHeight > notes.ClientSize.Height) break;
            }
            ScrollBars wanted = contentHeight > notes.ClientSize.Height ? ScrollBars.Vertical : ScrollBars.None;
            if (notes.ScrollBars != wanted) notes.ScrollBars = wanted;
        }

        private async Task RunPrimaryActionAsync()
        {
            if (working) return;
            string state = Value(status, "state");
            if (state == "ready")
            {
                if (InstallRequested != null) InstallRequested();
                return;
            }
            if (state == "available")
            {
                if (Number(status, "bytes") > AutomaticDownloadLimit) return;
                await DownloadAsync();
                return;
            }
            await RunAction(async () => status = await Task.Run(() => AppUpdateApi.Check()));
        }

        private async Task DownloadAsync()
        {
            string releaseId = Value(status, "release_id");
            if (String.IsNullOrEmpty(releaseId)) { await RefreshStatusAsync(false); return; }
            await RunAction(async () => status = await Task.Run(() => AppUpdateApi.Download(releaseId)));
        }

        private async Task SaveSettingsAsync()
        {
            bool check = autoCheck.Checked;
            bool install = autoInstall.Checked;
            await RunAction(async () => status = await Task.Run(() => AppUpdateApi.Settings(check, install)));
        }

        private async void SettingsChanged(object sender, EventArgs e)
        {
            if (renderingSettings || working) return;
            if (autoInstall.Checked && !autoCheck.Checked)
            {
                renderingSettings = true;
                autoCheck.Checked = true;
                renderingSettings = false;
            }
            await SaveSettingsAsync();
        }

        private async Task RefreshStatusAsync(bool showError)
        {
            if (refreshing || working || IsDisposed) return;
            refreshing = true;
            int serial = ++requestSerial;
            try
            {
                var result = await Task.Run(() => AppUpdateApi.Status());
                if (!IsDisposed && !working && serial == requestSerial)
                {
                    inlineError = null;
                    status = result;
                    RenderStatus();
                }
            }
            catch (Exception error)
            {
                if (!IsDisposed && serial == requestSerial)
                {
                    if (showError || String.IsNullOrEmpty(inlineError))
                        ShowInlineError("无法读取更新状态", error.Message);
                }
            }
            finally { refreshing = false; }
        }

        private async Task RunAction(Func<Task> action)
        {
            if (working) return;
            working = true;
            ++requestSerial;
            UpdateButtons();
            Exception failure = null;
            try { await action(); inlineError = null; if (!IsDisposed) RenderStatus(); }
            catch (Exception error)
            {
                failure = error;
            }
            working = false;
            if (!IsDisposed && failure != null)
            {
                if (!IsDisposed) await RefreshStatusAsync(false);
                if (!IsDisposed) ShowInlineError("操作未完成，可重试", failure.Message);
            }
            if (!IsDisposed) UpdateButtons();
        }

        private void RenderStatus()
        {
            string state = Value(status, "state");
            string channel = Value(status, "channel");
            string latest = Value(status, "latest_version");
            string current = Value(status, "current_version");
            stateLabel.Text = StateText(state) + (String.IsNullOrEmpty(channel) ? "" : " · " + (channel == "candidate" ? "候选版" : channel));
            stateLabel.ForeColor = state == "failed" ? Coral : (state == "ready" || state == "succeeded" ? Mint : Ink);
            currentVersionValue.Text = BreakVersion(String.IsNullOrEmpty(current) ? "2.12.0" : current);
            latestVersionValue.Text = BreakVersion(String.IsNullOrEmpty(latest) ? "—" : latest);
            currentVersionValue.Font = !String.IsNullOrEmpty(current) && current.Length > 18 ? LongVersionFont : VersionFont;
            latestVersionValue.Font = !String.IsNullOrEmpty(latest) && latest.Length > 18 ? LongVersionFont : VersionFont;
            string message = Value(status, "safe_message");
            string detail = Value(status, "notes");
            notes.Text = !String.IsNullOrWhiteSpace(message)
                ? message + (String.IsNullOrWhiteSpace(detail) ? "" : Environment.NewLine + Environment.NewLine + detail)
                : (String.IsNullOrWhiteSpace(detail) ? "检查更新后，此处会显示版本变化和更新说明。" : detail);
            UpdateNotesScrollBar();
            long bytes = Number(status, "bytes");
            long downloaded = Number(status, "downloaded_bytes");
            progressLine.Progress = bytes > 0 ? (int)Math.Max(0L, Math.Min(100L, downloaded * 100L / bytes)) : 0;
            progressLine.Visible = bytes > 0;
            if (bytes <= 0)
                progressLabel.Text = "";
            else if (state == "available" && downloaded <= 0)
                progressLabel.Text = "安装包大小　" + FormatBytes(bytes);
            else if (state == "ready")
                progressLabel.Text = "下载与校验完成　·　" + FormatBytes(bytes);
            else
                progressLabel.Text = "已下载　" + FormatBytes(downloaded) + " / " + FormatBytes(bytes);

            renderingSettings = true;
            autoCheck.Checked = Boolean(status, "auto_check", true);
            autoInstall.Checked = Boolean(status, "auto_install", false);
            settingsSummary.Text = "自动检查 " + (autoCheck.Checked ? "已开启" : "已关闭") +
                "　·　退出时安装 " + (autoInstall.Checked ? "已开启" : "已关闭");
            renderingSettings = false;
            UpdateButtons();
        }

        private void ShowInlineError(string title, string message)
        {
            inlineError = message ?? "";
            stateLabel.Text = title;
            stateLabel.ForeColor = Coral;
            notes.Text = String.IsNullOrWhiteSpace(message)
                ? "更新操作没有完成，请稍后重试。"
                : "更新操作没有完成，详细信息如下：" + Environment.NewLine + Environment.NewLine + message;
            progressLabel.Text = "可以重新检查，或在网络恢复后重试。";
            progressLine.Visible = false;
            UpdateButtons();
        }

        private static string BreakVersion(string value)
        {
            if (String.IsNullOrEmpty(value) || value.Length <= 22) return value;
            var result = new StringBuilder();
            int start = 0;
            while (start < value.Length)
            {
                int end = Math.Min(value.Length, start + 22);
                if (end < value.Length)
                {
                    int split = -1;
                    for (int i = end - 1; i > start + 8; i--)
                        if (value[i] == '.' || value[i] == '-' || value[i] == '+') { split = i + 1; break; }
                    if (split > start) end = split;
                }
                if (start > 0) result.Append(Environment.NewLine);
                result.Append(value.Substring(start, end - start));
                start = end;
            }
            return result.ToString();
        }

        private void UpdateButtons()
        {
            string state = Value(status, "state");
            bool stateBusy = state == "checking" || state == "downloading" || state == "prepared" || state == "applying";
            bool busy = working || stateBusy;
            bool tooLarge = state == "available" && Number(status, "bytes") > AutomaticDownloadLimit;
            actionButton.Enabled = !busy && !tooLarge;
            recheckLink.Enabled = !busy;
            if (busy) actionButton.Text = "正在处理…";
            else if (state == "ready") actionButton.Text = "安装并重启";
            else if (state == "available" && tooLarge) actionButton.Text = "超出自动下载限制";
            else if (state == "available") actionButton.Text = "下载并验证";
            else actionButton.Text = "检查更新";
            actionButton.AccessibleName = actionButton.Text;
            actionButton.Invalidate();
            autoCheck.Enabled = !working;
            autoInstall.Enabled = !working;
            settingsToggle.Enabled = !working;
        }

        private static string FormatBytes(long bytes)
        {
            if (bytes >= 1024L * 1024) return String.Format(CultureInfo.CurrentCulture, "{0:N1} MB", bytes / (1024D * 1024D));
            if (bytes >= 1024) return String.Format(CultureInfo.CurrentCulture, "{0:N0} KB", bytes / 1024D);
            return String.Format(CultureInfo.CurrentCulture, "{0:N0} 字节", Math.Max(0, bytes));
        }

        private static string StateText(string state)
        {
            switch (state)
            {
                case "idle": return "检查软件更新";
                case "checking": return "正在检查候选版更新…";
                case "available": return "发现可用更新";
                case "downloading": return "正在下载并验证…";
                case "ready": return "更新已准备好";
                case "prepared": return "正在准备安装…";
                case "applying": return "正在安装…";
                case "succeeded": return "更新已安装";
                case "failed": return "更新未完成";
                default: return "软件更新";
            }
        }

        private sealed class ProgressLine : Control
        {
            private int progress;

            internal int Progress
            {
                get { return progress; }
                set { progress = Math.Max(0, Math.Min(100, value)); Invalidate(); }
            }

            internal ProgressLine()
            {
                SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
                BackColor = Color.FromArgb(48, 59, 88);
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                base.OnPaint(e);
                int width = (int)Math.Round(ClientSize.Width * progress / 100D);
                if (width > 0) using (var brush = new SolidBrush(Apricot)) e.Graphics.FillRectangle(brush, 0, 0, width, ClientSize.Height);
            }
        }

        private sealed class EyeMark : Control
        {
            private static readonly Image Logo = LoadLogo();

            internal EyeMark()
            {
                SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
                TabStop = false;
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                base.OnPaint(e);
                e.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                if (Logo != null)
                {
                    int inset = Math.Max(2, (int)Math.Round(Math.Min(ClientSize.Width, ClientSize.Height) * 0.08));
                    int side = Math.Max(1, Math.Min(ClientSize.Width, ClientSize.Height) - 2 * inset);
                    var bounds = new Rectangle((ClientSize.Width - side) / 2, (ClientSize.Height - side) / 2, side, side);
                    e.Graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                    e.Graphics.DrawImage(Logo, bounds);
                }
                else
                {
                    using (var brush = new SolidBrush(Apricot))
                    using (var font = new Font("Microsoft YaHei UI", 15F, FontStyle.Bold, GraphicsUnit.Point))
                        TextRenderer.DrawText(e.Graphics, "曜", font, ClientRectangle, Apricot,
                            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
                }
            }

            private static Image LoadLogo()
            {
                try
                {
                    Stream embedded = Assembly.GetExecutingAssembly().GetManifestResourceStream("brand.ico");
                    if (embedded != null)
                    {
                        using (embedded)
                        using (var icon = new Icon(embedded)) return icon.ToBitmap();
                    }
                    string[] candidates = new[] {
                        Path.Combine(Environment.CurrentDirectory, "frontend", "brand.ico"),
                        Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "frontend", "brand.ico"))
                    };
                    foreach (string path in candidates)
                    {
                        if (!File.Exists(path)) continue;
                        using (var stream = File.OpenRead(path))
                        using (var icon = new Icon(stream)) return icon.ToBitmap();
                    }
                }
                catch { }
                return null;
            }
        }

        internal static string Value(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) ? value as string : null;
        }

        internal static long Number(Dictionary<string, object> data, string key)
        {
            object value;
            try { return data != null && data.TryGetValue(key, out value) ? Convert.ToInt64(value, CultureInfo.InvariantCulture) : 0; }
            catch { return 0; }
        }

        internal static bool Boolean(Dictionary<string, object> data, string key, bool fallback)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) && value is bool ? (bool)value : fallback;
        }
    }

    internal sealed class AppUpdateInstall : IDisposable
    {
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();
        internal readonly string TransactionId;
        internal readonly string TransactionDirectory;
        internal readonly string TicketPath;
        internal readonly string ReadyPath;
        internal readonly string CommitPath;
        internal readonly string CancelPath;
        internal readonly Process HelperProcess;
        private bool completed;

        private AppUpdateInstall(string transactionId, string transactionDirectory, string ticketPath,
            string readyPath, string commitPath, string cancelPath, Process helperProcess)
        {
            TransactionId = transactionId;
            TransactionDirectory = transactionDirectory;
            TicketPath = ticketPath;
            ReadyPath = readyPath;
            CommitPath = commitPath;
            CancelPath = cancelPath;
            HelperProcess = helperProcess;
        }

        internal static AppUpdateInstall PrepareAndStart(string releaseId, bool restart)
        {
            Dictionary<string, object> response = AppUpdateApi.Prepare(releaseId, restart);
            string id = AppUpdateDialog.Value(response, "transaction_id");
            if (String.IsNullOrEmpty(id) || id.Length != 32 || !IsLowerHex(id))
                throw new InvalidDataException("更新服务返回了无效的事务标识。");
            string directory = Path.GetFullPath(Path.Combine(Hub.Root, "data", "app-updates", "transactions", id));
            string cancel = Path.Combine(directory, "cancel.json");
            Process child = null;
            try
            {
                string helper = RequiredPath(response, "helper_path", directory, "app_update_helper.py");
                string ticket = RequiredPath(response, "ticket_path", directory, "ticket.json");
                string ready = RequiredPath(response, "ready_path", directory, "ready.json");
                string commit = RequiredPath(response, "commit_path", directory, "commit.json");
                cancel = RequiredPath(response, "cancel_path", directory, "cancel.json");
                if (!File.Exists(helper) || !File.Exists(ticket))
                    throw new InvalidDataException("更新事务缺少已准备好的助手或 ticket。");
                foreach (string path in new[] { ready, commit, cancel })
                    if (File.Exists(path)) throw new InvalidDataException("更新事务目录中已有同名交接文件，已停止安装。");
                VerifyTrustedHelper(helper);

                string python = Hub.FindPython();
                var start = new ProcessStartInfo(python, "-B " + Hub.Quote(helper) + " --ticket " + Hub.Quote(ticket)) {
                    UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                    WorkingDirectory = directory
                };
                child = Process.Start(start);
                if (child == null) throw new InvalidOperationException("无法启动独立更新助手。");
                WaitForReady(child, id, helper, ready);
                return new AppUpdateInstall(id, directory, ticket, ready, commit, cancel, child);
            }
            catch
            {
                if (child != null) child.Dispose();
                TryCancel(id, cancel, directory);
                throw;
            }
        }

        internal void Commit()
        {
            WriteMarker(CommitPath, "ai-hub-update-commit-v1", TransactionId, TransactionDirectory);
            completed = true;
        }

        internal void Cancel()
        {
            if (completed) return;
            TryCancel(TransactionId, CancelPath, TransactionDirectory);
            completed = true;
        }

        internal bool WaitForHelperExit(int milliseconds)
        {
            try { return HelperProcess == null || HelperProcess.WaitForExit(milliseconds); }
            catch { return false; }
        }

        internal void Detach()
        {
            completed = true;
            if (HelperProcess != null) HelperProcess.Dispose();
        }

        private static void WaitForReady(Process child, string id, string helper, string ready)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(18);
            while (DateTime.UtcNow < deadline)
            {
                if (File.Exists(ready))
                {
                    var record = Serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(ready, Encoding.UTF8));
                    long pid = AppUpdateDialog.Number(record, "helper_pid");
                    string start = AppUpdateDialog.Value(record, "helper_start_filetime");
                    string digest = AppUpdateDialog.Value(record, "helper_sha256");
                    string expectedStart = child.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(CultureInfo.InvariantCulture);
                    if (AppUpdateDialog.Value(record, "schema") != "ai-hub-update-ready-v1" ||
                        AppUpdateDialog.Value(record, "transaction_id") != id || pid != child.Id || start != expectedStart ||
                        !String.Equals(digest, HashFile(helper), StringComparison.Ordinal))
                        throw new InvalidDataException("更新助手身份校验失败，安装已取消。");
                    return;
                }
                if (child.HasExited) throw new InvalidOperationException("独立更新助手未能完成就绪握手。");
                Thread.Sleep(100);
            }
            throw new TimeoutException("独立更新助手就绪超时，曜核保持运行。");
        }

        private static string RequiredPath(Dictionary<string, object> response, string key, string directory, string leaf)
        {
            string raw = AppUpdateDialog.Value(response, key);
            if (String.IsNullOrWhiteSpace(raw)) throw new InvalidDataException("更新服务缺少事务路径：" + key);
            string full = Path.GetFullPath(raw);
            string expected = Path.GetFullPath(Path.Combine(directory, leaf));
            if (!String.Equals(full, expected, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("更新事务路径不在固定事务目录中。");
            return full;
        }

        private static void VerifyTrustedHelper(string helper)
        {
            string installed = Path.GetFullPath(Path.Combine(Hub.Root, "tools", "app_update_helper.py"));
            if (!File.Exists(installed) || !File.Exists(helper) || !String.Equals(HashFile(installed), HashFile(helper), StringComparison.Ordinal))
                throw new InvalidDataException("更新助手与本机受信任版本不一致，安装已停止。");
        }

        private static string HashFile(string path)
        {
            using (var sha = SHA256.Create())
            using (var stream = File.OpenRead(path)) return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        private static bool IsLowerHex(string value)
        {
            foreach (char c in value) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
            return true;
        }

        private static void TryCancel(string id, string cancel, string directory)
        {
            try { AppUpdateApi.Cancel(id); }
            catch
            {
                try { if (!File.Exists(cancel)) WriteMarker(cancel, "ai-hub-update-cancel-v1", id, directory); }
                catch { }
            }
        }

        private static void WriteMarker(string path, string schema, string transactionId, string transactionDirectory)
        {
            string directory = Path.GetFullPath(Path.GetDirectoryName(path));
            string expectedDirectory = Path.GetFullPath(transactionDirectory);
            if (!String.Equals(directory, expectedDirectory, StringComparison.OrdinalIgnoreCase) ||
                (File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("更新交接标记路径无效。");
            if (File.Exists(path)) throw new IOException("更新交接标记已存在。");
            byte[] bytes = Encoding.UTF8.GetBytes(Serializer.Serialize(new { schema = schema, transaction_id = transactionId }));
            string temporary = Path.Combine(directory, ".marker-" + Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }
                File.Move(temporary, path);
            }
            finally { try { if (File.Exists(temporary)) File.Delete(temporary); } catch { } }
        }

        public void Dispose()
        {
            if (!completed) Cancel();
            if (HelperProcess != null) HelperProcess.Dispose();
        }
    }
}
