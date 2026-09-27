using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AIHub.Desktop
{
    // Decoration for the real startup interval only. It owns no task or navigation.
    internal sealed class StartupAnimation : Control
    {
        private readonly Timer frames;
        private readonly Stopwatch elapsed = new Stopwatch();
        private readonly Bitmap mark;
        private readonly Bitmap halo;
        private readonly ImageAttributes colors = new ImageAttributes();
        private readonly Func<bool> motionPreference;
        private bool loading;
        private bool hostActive;
        private bool motionAllowed;
        private bool released;
        private float paintDpi = 96F;

        internal StartupAnimation(Stream iconSource, Func<bool> motionPreference = null)
        {
            this.motionPreference = motionPreference ?? ReadMotionPreference;
            using (var icon = new Icon(iconSource, 256, 256)) mark = icon.ToBitmap();
            halo = CreateHalo();
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint |
                ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            SetStyle(ControlStyles.Selectable, false);
            TabStop = false;
            AccessibleRole = AccessibleRole.Graphic;
            AccessibleName = "曜核";
            BackColor = Color.FromArgb(18, 22, 41);
            frames = new Timer { Interval = 33 };
            frames.Tick += OnFrame;
            RefreshMotionPreference();
        }

        internal bool IsAnimating { get { return !released && frames.Enabled; } }
        internal bool IsLoading { get { return loading; } }
        internal bool ResourcesReleased { get { return released; } }

        internal void BeginLoading()
        {
            if (released) return;
            loading = true;
            StopTimer();
            elapsed.Reset();
            Visible = true;
            RefreshMotionPreference();
            SyncTimer();
            Invalidate();
        }

        internal void Complete()
        {
            if (released) return;
            loading = false;
            StopTimer();
            elapsed.Reset();
            Visible = false;
        }

        internal void SetHostActive(bool active)
        {
            if (released) return;
            hostActive = active;
            SyncTimer();
            if (Visible) Invalidate();
        }

        internal void RefreshMotionPreference()
        {
            if (released) return;
            try { motionAllowed = motionPreference(); }
            catch { motionAllowed = false; }
            SyncTimer();
            if (Visible) Invalidate();
        }

        private static bool ReadMotionPreference()
        {
            // SPI_GETCLIENTAREAANIMATION respects Windows' accessibility preference.
            bool enabled;
            return SystemParametersInfo(0x1042, 0, out enabled, 0) && enabled;
        }

        private void SyncTimer()
        {
            if (frames == null || released) return;
            if (loading && hostActive && Visible && IsHandleCreated && motionAllowed)
            {
                if (!frames.Enabled) { elapsed.Start(); frames.Start(); }
            }
            else StopTimer();
        }

        private void StopTimer()
        {
            if (frames != null && !released) frames.Stop();
            elapsed.Stop();
        }

        internal static Rectangle SceneBounds(Size clientSize, float dpi)
        {
            int available = Math.Max(0, Math.Min(clientSize.Width, clientSize.Height) - 16);
            int size = Math.Min(available, Math.Max(0, (int)Math.Round(400 * dpi / 96)));
            return new Rectangle((clientSize.Width - size) / 2, (clientSize.Height - size) / 2, size, size);
        }

        private void OnFrame(object sender, EventArgs args)
        {
            if (!IsAnimating) return;
            // Every animated layer is clipped to this same bounded scene.
            Invalidate(SceneBounds(ClientSize, paintDpi));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            if (released) return;
            paintDpi = e.Graphics.DpiX;
            RenderScene(e.Graphics, ClientSize, paintDpi, elapsed.Elapsed.TotalSeconds, IsAnimating);
        }

        // The same deterministic painter is used by the control and offline frame checks.
        // The caller owns the Graphics; no window, service or clock is created here.
        internal void RenderScene(Graphics graphics, Size clientSize, float dpi, double seconds, bool moving)
        {
            if (released) return;
            Rectangle bounds = SceneBounds(clientSize, dpi);
            if (bounds.Width < 1) return;
            // Reduced motion always has the full first-frame composition, independent of time.
            if (!moving) seconds = 0;
            var saved = graphics.Save();
            try
            {
                graphics.SetClip(bounds, CombineMode.Intersect);
                graphics.TranslateTransform(bounds.X, bounds.Y);
                graphics.ScaleTransform(bounds.Width / 400F, bounds.Height / 400F);
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                graphics.DrawImage(halo, new Rectangle(0, 0, 400, 400));
                DrawRings(graphics, seconds);
                DrawParticles(graphics, seconds);
                float brightness = moving ? (float)(.985 + .015 * Math.Cos(seconds * Math.PI / 1.8)) : 1;
                var matrix = new ColorMatrix { Matrix00 = brightness, Matrix11 = brightness, Matrix22 = brightness };
                colors.SetColorMatrix(matrix);
                var markBounds = new Rectangle(128, 128, 144, 144);
                graphics.DrawImage(mark, markBounds, 0, 0, mark.Width, mark.Height, GraphicsUnit.Pixel, colors);
                DrawEyeGlint(graphics, markBounds, seconds, moving);
            }
            finally { graphics.Restore(saved); }
        }

        private static Bitmap CreateHalo()
        {
            // A small transparent glow is computed once, not blurred or allocated per frame.
            var bitmap = new Bitmap(400, 400, PixelFormat.Format32bppArgb);
            var data = bitmap.LockBits(new Rectangle(0, 0, 400, 400), ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
            try
            {
                var pixels = new byte[data.Stride * 400];
                for (int y = 0; y < 400; y++)
                    for (int x = 0; x < 400; x++)
                    {
                        double dx = x - 199.5, dy = y - 199.5;
                        double radius = Math.Sqrt(dx * dx + dy * dy);
                        double edge = Math.Max(0, Math.Min(1, (198 - radius) / 32));
                        edge = edge * edge * (3 - 2 * edge);
                        double indigo = .36 * Math.Exp(-(dx * dx + dy * dy) / (2 * 91 * 91));
                        double gold = .27 * Math.Exp(-(dx * dx + (dy + 10) * (dy + 10)) / (2 * 62 * 62));
                        double sum = indigo + gold;
                        int index = y * data.Stride + x * 4;
                        pixels[index] = (byte)((193 * indigo + 98 * gold) / sum);
                        pixels[index + 1] = (byte)((85 * indigo + 171 * gold) / sum);
                        pixels[index + 2] = (byte)((95 * indigo + 236 * gold) / sum);
                        pixels[index + 3] = (byte)(255 * Math.Min(1, sum * edge));
                    }
                Marshal.Copy(pixels, 0, data.Scan0, pixels.Length);
            }
            catch { bitmap.UnlockBits(data); bitmap.Dispose(); throw; }
            bitmap.UnlockBits(data);
            return bitmap;
        }

        private static void DrawRings(Graphics graphics, double seconds)
        {
            float outerRadius = 158 + (float)(2.5 * Math.Sin(seconds * .55));
            float innerRadius = 118 + (float)(2 * Math.Sin(seconds * .65 + .8));
            var outer = new RectangleF(200 - outerRadius, 200 - outerRadius, outerRadius * 2, outerRadius * 2);
            var inner = new RectangleF(200 - innerRadius, 200 - innerRadius, innerRadius * 2, innerRadius * 2);
            using (var ring = new Pen(Color.FromArgb(40, 157, 142, 211), .8F))
            using (var arc = new Pen(Color.FromArgb(117, 215, 180, 118), 1.15F))
            {
                graphics.DrawEllipse(ring, outer);
                ring.Color = Color.FromArgb(43, 209, 173, 111);
                graphics.DrawEllipse(ring, inner);
                arc.StartCap = arc.EndCap = LineCap.Round;
                float angle = (float)(seconds * 7 % 360) - 62;
                graphics.DrawArc(arc, outer, angle, 46);
                arc.Color = Color.FromArgb(75, 159, 146, 220);
                graphics.DrawArc(arc, outer, angle + 170, 27);
                arc.Color = Color.FromArgb(109, 239, 194, 120);
                graphics.DrawArc(arc, inner, 122 - (float)(seconds * 9 % 360), 60);
                arc.Color = Color.FromArgb(57, 158, 139, 213);
                graphics.DrawArc(arc, inner, 298 - (float)(seconds * 9 % 360), 25);
            }
        }

        private static void DrawParticles(Graphics graphics, double seconds)
        {
            // Ten staggered points are visible from the first frame. They fade before reset.
            for (int i = 0; i < 10; i++)
            {
                double progress = (.07 + i * .381966 + seconds / (6.6 + i * .17)) % 1;
                double angle = i * 2.399963 + .18 * Math.Sin(seconds * .23 + i);
                float radius = (float)(177 - 73 * progress);
                float dx = (float)Math.Cos(angle), dy = (float)Math.Sin(angle);
                float x = 200 + dx * radius, y = 200 + dy * radius;
                float strength = (float)Math.Sin(progress * Math.PI);
                bool gold = i % 3 != 1;
                Color tint = gold ? Color.FromArgb(238, 190, 116) : Color.FromArgb(154, 143, 218);
                using (var trail = new Pen(tint, 1F))
                using (var point = new SolidBrush(Color.FromArgb((int)(155 * strength), tint)))
                using (var bloom = new SolidBrush(Color.FromArgb((int)(19 * strength), tint)))
                {
                    trail.StartCap = trail.EndCap = LineCap.Round;
                    for (int segment = 0; segment < 3; segment++)
                    {
                        trail.Color = Color.FromArgb((int)((60 - segment * 18) * strength), tint);
                        float from = 3 + segment * 4;
                        graphics.DrawLine(trail, x + dx * from, y + dy * from,
                            x + dx * (from + 4), y + dy * (from + 4));
                    }
                    graphics.FillEllipse(bloom, x - 4, y - 4, 8, 8);
                    float size = i % 3 == 0 ? 3F : 2.3F;
                    graphics.FillEllipse(point, x - size / 2, y - size / 2, size, size);
                }
            }
        }

        private static void DrawEyeGlint(Graphics graphics, Rectangle bounds, double seconds, bool moving)
        {
            double phase = seconds % 3.2;
            if (!moving || phase < .18 || phase > .92) return;
            float progress = (float)((phase - .18) / .74);
            float alpha = (float)Math.Sin(progress * Math.PI);
            float scale = bounds.Width / 512F;
            var saved = graphics.Save();
            try
            {
                // The E01 inner eye, in the original 512px artwork coordinates.
                using (var eye = new GraphicsPath())
                using (var transform = new Matrix(scale, 0, 0, scale, bounds.X, bounds.Y))
                {
                    eye.AddBezier(171, 328, 211, 344, 263, 306, 310, 239);
                    eye.AddBezier(310, 239, 247, 257, 197, 278, 171, 328);
                    eye.CloseFigure();
                    eye.Transform(transform);
                    graphics.SetClip(eye, CombineMode.Intersect);
                    graphics.SmoothingMode = SmoothingMode.AntiAlias;
                    float x = bounds.X + (185 + 108 * progress) * scale;
                    float y = bounds.Y + (321 - 69 * progress) * scale;
                    using (var trail = new Pen(Color.FromArgb((int)(100 * alpha), 255, 198, 104), 5 * scale))
                    using (var glint = new SolidBrush(Color.FromArgb((int)(200 * alpha), 255, 239, 186)))
                    {
                        trail.StartCap = trail.EndCap = LineCap.Round;
                        graphics.DrawLine(trail, x - 34 * scale, y + 21 * scale, x, y);
                        graphics.FillEllipse(glint, x - 4 * scale, y - 4 * scale, 8 * scale, 8 * scale);
                    }
                }
            }
            finally { graphics.Restore(saved); }
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            SyncTimer();
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            SyncTimer();
        }

        protected override void OnHandleDestroyed(EventArgs e)
        {
            StopTimer();
            base.OnHandleDestroyed(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && !released)
            {
                StopTimer();
                loading = false;
                released = true;
                frames.Tick -= OnFrame;
                frames.Dispose();
                colors.Dispose();
                mark.Dispose();
                halo.Dispose();
            }
            base.Dispose(disposing);
        }

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SystemParametersInfo(uint action, uint parameter, [MarshalAs(UnmanagedType.Bool)] out bool value, uint flags);
    }
}
