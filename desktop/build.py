"""Build a windowless x64 desktop EXE with the local .NET Framework compiler.

The Microsoft SDK archive must already be downloaded. No network access, package
installation, data copying or service restart occurs during this build.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

SDK_VERSION = "1.0.4191.47"
SDK_SHA256 = "f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0"
ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--render-output", type=Path, help="With --test, save offline source-control frames (not real-window screenshots)")
    parser.add_argument("--update-render-output", type=Path, help="With --test, save an off-screen native update dialog preview")
    parser.add_argument("--alias-root", type=Path, help="Optional existing junction to test against the physical application folder")
    parser.add_argument("--startup-guard-candidate", type=Path,
                        help="With --test, also run the native update-race fixture against this candidate EXE")
    args = parser.parse_args()
    if (args.render_output or args.update_render_output) and not args.test:
        parser.error("render output options require --test")
    if args.startup_guard_candidate and not args.test:
        parser.error("--startup-guard-candidate requires --test")
    if args.startup_guard_candidate and not args.startup_guard_candidate.is_file():
        parser.error("--startup-guard-candidate must name an existing EXE")
    if hashlib.sha256(args.sdk_package.read_bytes()).hexdigest() != SDK_SHA256:
        raise SystemExit("WebView2 SDK archive SHA-256 does not match the pinned official package.")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise SystemExit("The Windows .NET Framework C# compiler is not available.")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aihub-desktop-build-") as temporary:
        folder = Path(temporary)
        members = {
            "Microsoft.Web.WebView2.Core.dll": "lib/net462/Microsoft.Web.WebView2.Core.dll",
            "Microsoft.Web.WebView2.WinForms.dll": "lib/net462/Microsoft.Web.WebView2.WinForms.dll",
            "WebView2Loader.dll": "runtimes/win-x64/native/WebView2Loader.dll",
            "WebView2-LICENSE.txt": "LICENSE.txt",
        }
        with zipfile.ZipFile(args.sdk_package) as archive:
            for name, member in members.items():
                (folder / name).write_bytes(archive.read(member))
        common = [str(compiler), "/nologo", "/optimize+", "/platform:x64", "/utf8output",
                  "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll"]
        exe = folder / "AI Hub.exe"
        command = common + ["/target:winexe", f"/out:{exe}",
            "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
            f"/reference:{folder / 'Microsoft.Web.WebView2.Core.dll'}",
            f"/reference:{folder / 'Microsoft.Web.WebView2.WinForms.dll'}",
            f"/win32manifest:{DESKTOP / 'app.manifest'}", f"/win32icon:{ROOT / 'frontend/brand.ico'}",
            f"/resource:{ROOT / 'frontend/brand.ico'},brand.ico"]
        for name in members:
            command.append(f"/resource:{folder / name},{name}")
        subprocess.run(command + [str(DESKTOP / "Core.cs"), str(DESKTOP / "AppUpdate.cs"),
                                  str(DESKTOP / "StartupAnimation.cs"), str(DESKTOP / "Program.cs")], check=True)
        if args.test:
            tests = folder / "desktop-tests.exe"
            test_command = common + ["/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                                      "/target:exe", f"/out:{tests}", str(DESKTOP / "Core.cs"),
                                      str(DESKTOP / "AppUpdate.cs"), str(DESKTOP / "Tests.cs")]
            subprocess.run(test_command, check=True)
            candidate = args.startup_guard_candidate.resolve() if args.startup_guard_candidate else exe
            test_args = [str(tests), str(ROOT), "--candidate-exe", str(candidate)]
            if args.alias_root:
                test_args += ["--alias-root", str(args.alias_root)]
            if args.update_render_output:
                test_args += ["--update-render-output", str(args.update_render_output.resolve())]
            subprocess.run(test_args, check=True)
            icon_tests = folder / "icon-tests.exe"
            subprocess.run(common + ["/target:exe", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", f"/out:{icon_tests}",
                                      str(DESKTOP / "StartupAnimation.cs"), str(DESKTOP / "IconTests.cs")], check=True)
            subprocess.run([str(icon_tests), str(ROOT / "frontend/brand.ico"), str(exe)] +
                           ([str(args.render_output.resolve())] if args.render_output else []), check=True)
        shutil.copy2(exe, output)
    result = {"exe": str(output), "bytes": output.stat().st_size,
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "architecture": "x64", "subsystem": "Windows GUI", "sdk": SDK_VERSION,
              "sdk_sha256": SDK_SHA256, "contains_user_data": False,
              "display_name": "曜核", "desktop_version": "2.13.0.0",
              "icon_sha256": hashlib.sha256((ROOT / "frontend/brand.ico").read_bytes()).hexdigest()}
    output.with_suffix(".build.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
