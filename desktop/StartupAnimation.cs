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
            using (var icon = new Icon(iconSource, 128, 128)) mark = icon.ToBitmap();
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

        private Rectangle MarkBounds(float dpi)
        {
            int size = Math.Min(Math.Min(ClientSize.Width, ClientSize.Height), (int)Math.Round(128 * dpi / 96));
            return new Rectangle((ClientSize.Width - size) / 2, (ClientSize.Height - size) / 2, size, size);
        }

        private void OnFrame(object sender, EventArgs args)
        {
            if (!IsAnimating) return;
            var bounds = MarkBounds(paintDpi);
            bounds.Inflate(4, 4);
            Invalidate(bounds);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            if (released) return;
            paintDpi = e.Graphics.DpiX;
            Rectangle bounds = MarkBounds(e.Graphics.DpiX);
            if (bounds.Width < 1) return;
            bool moving = IsAnimating;
            double seconds = elapsed.Elapsed.TotalSeconds;
            float brightness = moving ? (float)(.96 + .04 * Math.Sin(seconds * Math.PI / 1.8)) : 1;
            var matrix = new ColorMatrix { Matrix00 = brightness, Matrix11 = brightness, Matrix22 = brightness };
            colors.SetColorMatrix(matrix);
            e.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
            e.Graphics.DrawImage(mark, bounds, 0, 0, mark.Width, mark.Height, GraphicsUnit.Pixel, colors);
            double phase = seconds % 3.2;
            if (!moving || phase < .18 || phase > .92) return;
            float progress = (float)((phase - .18) / .74);
            float alpha = (float)Math.Sin(progress * Math.PI);
            float scale = bounds.Width / 512F;
            var saved = e.Graphics.Save();
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
                    e.Graphics.SetClip(eye, CombineMode.Intersect);
                    e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                    float x = bounds.X + (185 + 108 * progress) * scale;
                    float y = bounds.Y + (321 - 69 * progress) * scale;
                    using (var trail = new Pen(Color.FromArgb((int)(100 * alpha), 255, 198, 104), 5 * scale))
                    using (var glint = new SolidBrush(Color.FromArgb((int)(200 * alpha), 255, 239, 186)))
                    {
                        trail.StartCap = trail.EndCap = LineCap.Round;
                        e.Graphics.DrawLine(trail, x - 34 * scale, y + 21 * scale, x, y);
                        e.Graphics.FillEllipse(glint, x - 4 * scale, y - 4 * scale, 8 * scale, 8 * scale);
                    }
                }
            }
            finally { e.Graphics.Restore(saved); }
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
            }
            base.Dispose(disposing);
        }

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SystemParametersInfo(uint action, uint parameter, [MarshalAs(UnmanagedType.Bool)] out bool value, uint flags);
    }
}
