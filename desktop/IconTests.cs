using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

namespace AIHub.Desktop
{
    internal static class IconTests
    {
        private static int passed;
        private static void Check(bool condition, string label)
        {
            if (!condition) throw new Exception("FAILED: " + label);
            passed++;
            Console.WriteLine("PASS " + label);
        }

        [STAThread]
        private static int Main(string[] args)
        {
            try { return Run(args); }
            catch (Exception error)
            {
                Console.WriteLine("FAILED " + error.GetType().Name + ": " + error.Message);
                return 1;
            }
        }

        private static int Run(string[] args)
        {
            CheckStartupControl(args[0]);
            CheckStartupScene(args[0], args.Length > 2 ? args[2] : null);
            byte[] ico = File.ReadAllBytes(args[0]);
            int[] sizes = { 16, 20, 24, 32, 48, 64, 128, 256 };
            Check(BitConverter.ToUInt16(ico, 2) == 1 && BitConverter.ToUInt16(ico, 4) == sizes.Length, "eight ICO frames");
            for (int index = 0; index < sizes.Length; index++)
            {
                int size = sizes[index], entry = 6 + index * 16;
                int offset = BitConverter.ToInt32(ico, entry + 12);
                int length = BitConverter.ToInt32(ico, entry + 8);
                Check((ico[entry] == 0 ? 256 : ico[entry]) == size && offset + length <= ico.Length,
                    "ICO directory " + size);
                Check(BitConverter.ToInt32(ico, offset) == 40 && BitConverter.ToInt32(ico, offset + 4) == size &&
                    BitConverter.ToInt32(ico, offset + 8) == 2 * size && BitConverter.ToInt16(ico, offset + 14) == 32,
                    "32-bit DIB frame " + size);
                // Decode each individual DIB frame. .NET's multi-frame selector can choose
                // a nearby standard size (for example 32 for requested 24) independently of validity.
                var single = new byte[22 + length];
                Array.Copy(ico, 0, single, 0, 6);
                single[4] = 1; single[5] = 0;
                Array.Copy(ico, entry, single, 6, 16);
                Array.Copy(BitConverter.GetBytes(22), 0, single, 18, 4);
                Array.Copy(ico, offset, single, 22, length);
                using (var singleStream = new MemoryStream(single))
                using (var icon = new Icon(singleStream, size, size))
                using (var bitmap = icon.ToBitmap())
                {
                    Check(icon.Width == size && icon.Height == size && bitmap.GetPixel(0, 0).A <= 3,
                        "System.Drawing decode and transparent corner " + size + " (actual " + icon.Width + "x" + icon.Height + ", alpha=" + bitmap.GetPixel(0, 0).A + ")");
                    Color center = bitmap.GetPixel(size / 2, size / 2);
                    int pixel = offset + 40 + ((size - 1 - size / 2) * size + size / 2) * 4;
                    Check(center.A == ico[pixel + 3] && Math.Abs(center.R - ico[pixel + 2]) <= 1 &&
                        Math.Abs(center.G - ico[pixel + 1]) <= 1 && Math.Abs(center.B - ico[pixel]) <= 1,
                        "decoded pixel matches original DIB " + size);
                }
            }
            byte[] exe = File.ReadAllBytes(args[1]);
            int pe = BitConverter.ToInt32(exe, 60);
            Check(BitConverter.ToUInt16(exe, pe + 4) == 0x8664 && BitConverter.ToUInt16(exe, pe + 24 + 68) == 2,
                "PE x64 Windows GUI");
            Check(AssemblyName.GetAssemblyName(args[1]).Version.ToString() == "2.12.0.0", "assembly version 2.12.0.0");
            var version = FileVersionInfo.GetVersionInfo(args[1]);
            Check(version.FileVersion == "2.12.0.0" && version.ProductName == "曜核", "EXE version and product display name");
            using (var embedded = Assembly.LoadFile(Path.GetFullPath(args[1])).GetManifestResourceStream("brand.ico"))
            using (var memory = new MemoryStream())
            {
                embedded.CopyTo(memory);
                byte[] actual = memory.ToArray();
                bool same = actual.Length == ico.Length;
                for (int i = 0; same && i < actual.Length; i++) same = actual[i] == ico[i];
                Check(same, "managed embedded icon equals generated ICO bytes");
            }
            IntPtr module = LoadLibraryEx(args[1], IntPtr.Zero, 2);
            Check(module != IntPtr.Zero, "load native PE icon resources");
            try
            {
                int groups = 0;
                ResourceNameCallback callback = delegate(IntPtr library, IntPtr type, IntPtr name, IntPtr parameter)
                {
                    IntPtr info = FindResource(library, name, type);
                    IntPtr resource = LoadResource(library, info);
                    int bytes = (int)SizeofResource(library, info);
                    var data = new byte[bytes];
                    Marshal.Copy(LockResource(resource), data, 0, bytes);
                    Check(BitConverter.ToUInt16(data, 4) == sizes.Length, "native icon group has eight frames");
                    for (int i = 0; i < sizes.Length; i++)
                    {
                        int entry = 6 + i * 14;
                        Check((data[entry] == 0 ? 256 : data[entry]) == sizes[i], "native resource size " + sizes[i]);
                        int id = BitConverter.ToUInt16(data, entry + 12);
                        Check(FindResource(library, new IntPtr(id), new IntPtr(3)) != IntPtr.Zero, "native RT_ICON exists " + sizes[i]);
                    }
                    groups++;
                    return true;
                };
                EnumResourceNames(module, new IntPtr(14), callback, IntPtr.Zero);
                Check(groups == 1, "one native icon group");
            }
            finally { FreeLibrary(module); }
            Console.WriteLine("Icon/PE tests passed: " + passed);
            return 0;
        }

        private static void CheckStartupControl(string iconFile)
        {
            bool allowMotion = true;
            using (var source = File.OpenRead(iconFile))
            using (var animation = new StartupAnimation(source, delegate { return allowMotion; }))
            {
                animation.Size = new Size(360, 280);
                animation.CreateControl();
                Check(!animation.IsAnimating && !animation.IsLoading, "startup control begins idle without a visible window");
                animation.BeginLoading();
                Check(animation.IsLoading && !animation.IsAnimating, "startup waits for active host before running timer");
                animation.SetHostActive(true);
                Check(animation.IsAnimating && animation.Text == "" && !animation.TabStop, "active loading animates without loading text or focus");
                animation.Visible = false;
                Check(!animation.IsAnimating, "hidden startup control stops timer");
                animation.Visible = true;
                Check(animation.IsAnimating, "still-loading visible control resumes timer");
                animation.SetHostActive(false);
                Check(!animation.IsAnimating, "inactive or minimized host stops timer");
                animation.SetHostActive(true);
                Check(animation.IsAnimating, "loading host restore resumes timer");
                allowMotion = false;
                animation.RefreshMotionPreference();
                Check(!animation.IsAnimating && animation.Visible, "reduced motion keeps a static icon with no timer");
                using (var bitmap = new Bitmap(360, 280))
                {
                    animation.DrawToBitmap(bitmap, new Rectangle(0, 0, 360, 280));
                    bool gold = false;
                    for (int y = 70; y < 210 && !gold; y += 4)
                        for (int x = 100; x < 260 && !gold; x += 4)
                        {
                            Color pixel = bitmap.GetPixel(x, y);
                            gold = pixel.R > 150 && pixel.R > pixel.B + 40;
                        }
                    Check(gold, "static startup renders the embedded-style golden eye offscreen");
                }
                allowMotion = true;
                animation.RefreshMotionPreference();
                Check(animation.IsAnimating, "live motion preference change resumes only pending loading");
                var completion = Stopwatch.StartNew();
                animation.Complete();
                completion.Stop();
                Check(!animation.IsAnimating && !animation.IsLoading && !animation.Visible && completion.ElapsedMilliseconds < 250,
                    "navigation completion immediately stops and hides startup without a minimum duration");
                animation.SetHostActive(false);
                animation.SetHostActive(true);
                Check(!animation.IsAnimating && !animation.Visible, "restoring completed workbench never replays startup");
                animation.BeginLoading();
                Check(animation.IsAnimating, "explicit retry starts a new real loading interval");
                animation.Dispose();
                Check(animation.ResourcesReleased && !animation.IsAnimating && !animation.IsLoading,
                    "dispose releases startup timer and drawing resources");
            }
            using (var source = File.OpenRead(iconFile))
            using (var animation = new StartupAnimation(source, delegate { throw new InvalidOperationException("preference unavailable"); }))
            {
                animation.CreateControl();
                animation.SetHostActive(true);
                animation.BeginLoading();
                Check(!animation.IsAnimating && animation.Visible, "unavailable Windows motion preference fails to static icon");
            }
        }

        private static Bitmap RenderFrame(StartupAnimation animation, Size size, float dpi, double seconds, bool moving)
        {
            var bitmap = new Bitmap(size.Width, size.Height);
            using (var graphics = Graphics.FromImage(bitmap))
            {
                graphics.Clear(animation.BackColor);
                animation.RenderScene(graphics, size, dpi, seconds, moving);
            }
            return bitmap;
        }

        private static bool SameFrame(Bitmap first, Bitmap second)
        {
            for (int y = 0; y < first.Height; y += 2)
                for (int x = 0; x < first.Width; x += 2)
                    if (first.GetPixel(x, y) != second.GetPixel(x, y)) return false;
            return true;
        }

        private static void CheckStartupScene(string iconFile, string outputDirectory)
        {
            var size = new Size(720, 520);
            using (var animation = CreateMeasuredControl(iconFile))
            using (var first = RenderFrame(animation, size, 96, 0, true))
            using (var next = RenderFrame(animation, size, 96, .6, true))
            using (var later = RenderFrame(animation, size, 96, 1.8, true))
            using (var still = RenderFrame(animation, size, 96, 0, false))
            using (var stillLater = RenderFrame(animation, size, 96, 9, false))
            {
                var scene = StartupAnimation.SceneBounds(size, 96);
                Check(scene.Width == 400 && scene.Left == 160 && scene.Top == 60, "scene is centered at 400px design scale");
                Check(StartupAnimation.SceneBounds(new Size(1000, 900), 192).Width == 800,
                    "scene and its logo scale together at 200 percent DPI");
                int outsideLogo = 0;
                bool escaped = false;
                for (int y = 0; y < size.Height; y += 2)
                    for (int x = 0; x < size.Width; x += 2)
                    {
                        Color pixel = first.GetPixel(x, y);
                        if (!scene.Contains(x, y) && pixel.ToArgb() != animation.BackColor.ToArgb()) escaped = true;
                        if (Math.Abs(x - 360) > 80 || Math.Abs(y - 260) > 80)
                            if (pixel.R > 29 || pixel.B > 58) outsideLogo++;
                    }
                Check(outsideLogo > 1500, "first frame already has a halo, rings and points beyond the central icon");
                Check(!escaped, "all painted layers stay inside the same invalidated scene bounds");
                Check(!SameFrame(first, next) && !SameFrame(next, later), "deterministic waiting frames move arcs and converging points");
                Check(SameFrame(still, stillLater) && SameFrame(first, still), "reduced motion keeps the complete first-frame composition static");
                using (var small = RenderFrame(animation, new Size(180, 140), 192, .6, true))
                {
                    Check(StartupAnimation.SceneBounds(small.Size, 192).Width == 124 &&
                        small.GetPixel(0, 0).ToArgb() == animation.BackColor.ToArgb(), "small windows fit the entire scene without edge clipping");
                    if (outputDirectory != null)
                    {
                        Directory.CreateDirectory(outputDirectory);
                        small.Save(Path.Combine(outputDirectory, "startup-small-200dpi.png"), System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
                if (outputDirectory != null)
                {
                    first.Save(Path.Combine(outputDirectory, "startup-t0.png"), System.Drawing.Imaging.ImageFormat.Png);
                    next.Save(Path.Combine(outputDirectory, "startup-t0.6.png"), System.Drawing.Imaging.ImageFormat.Png);
                    later.Save(Path.Combine(outputDirectory, "startup-t1.8.png"), System.Drawing.Imaging.ImageFormat.Png);
                    still.Save(Path.Combine(outputDirectory, "startup-reduced-motion.png"), System.Drawing.Imaging.ImageFormat.Png);
                    Console.WriteLine("Offscreen source-control frames only (not a real window acceptance): " + Path.GetFullPath(outputDirectory));
                }
                // Reuse one canvas: exercise disposal of the small per-frame GDI objects.
                using (var canvas = new Bitmap(400, 400))
                using (var graphics = Graphics.FromImage(canvas))
                using (var process = Process.GetCurrentProcess())
                {
                    int before = GetGuiResources(process.Handle, 0);
                    var duration = Stopwatch.StartNew();
                    for (int frame = 0; frame < 90; frame++)
                    {
                        graphics.Clear(animation.BackColor);
                        animation.RenderScene(graphics, canvas.Size, 96, frame / 30.0, true);
                    }
                    duration.Stop();
                    Check(GetGuiResources(process.Handle, 0) <= before + 2, "repeated scene painting does not grow GDI object handles");
                    Console.WriteLine("90 offscreen paint calls: " + duration.ElapsedMilliseconds + " ms (synthetic, not actual-window CPU measurement)");
                }
            }
        }

        private static StartupAnimation CreateMeasuredControl(string iconFile)
        {
            using (var source = File.OpenRead(iconFile))
            {
                var duration = Stopwatch.StartNew();
                var animation = new StartupAnimation(source, delegate { return false; });
                duration.Stop();
                Console.WriteLine("Control construction including cached halo: " + duration.Elapsed.TotalMilliseconds.ToString("F2") +
                    " ms (synthetic, not application startup measurement)");
                return animation;
            }
        }

        private delegate bool ResourceNameCallback(IntPtr module, IntPtr type, IntPtr name, IntPtr parameter);
        [DllImport("user32.dll")] private static extern int GetGuiResources(IntPtr process, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
        [DllImport("kernel32.dll")] private static extern bool FreeLibrary(IntPtr module);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern bool EnumResourceNames(IntPtr module, IntPtr type, ResourceNameCallback callback, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr FindResource(IntPtr module, IntPtr name, IntPtr type);
        [DllImport("kernel32.dll")] private static extern IntPtr LoadResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll")] private static extern IntPtr LockResource(IntPtr resource);
        [DllImport("kernel32.dll")] private static extern uint SizeofResource(IntPtr module, IntPtr resource);
    }
}
