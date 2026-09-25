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
            Check(AssemblyName.GetAssemblyName(args[1]).Version.ToString() == "2.9.0.0", "assembly version 2.9.0.0");
            var version = FileVersionInfo.GetVersionInfo(args[1]);
            Check(version.FileVersion == "2.9.0.0" && version.ProductName == "曜核", "EXE version and product display name");
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

        private delegate bool ResourceNameCallback(IntPtr module, IntPtr type, IntPtr name, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
        [DllImport("kernel32.dll")] private static extern bool FreeLibrary(IntPtr module);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern bool EnumResourceNames(IntPtr module, IntPtr type, ResourceNameCallback callback, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr FindResource(IntPtr module, IntPtr name, IntPtr type);
        [DllImport("kernel32.dll")] private static extern IntPtr LoadResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll")] private static extern IntPtr LockResource(IntPtr resource);
        [DllImport("kernel32.dll")] private static extern uint SizeofResource(IntPtr module, IntPtr resource);
    }
}
