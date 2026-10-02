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
import stat
import subprocess
import tempfile
import time
import zipfile

SDK_VERSION = "1.0.4191.47"
SDK_SHA256 = "f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0"
ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def native_fixture_root(folder, mode, minimum_length=0):
    """Keep fixture/EXE paths launchable while derived cache paths exceed MAX_PATH."""
    root = folder / ("native-fixture-" + mode)
    while len(str(root)) < minimum_length:
        remaining = minimum_length - len(str(root)) - 1
        # Spaces/Unicode also exercise the native entry's Windows argument quoting.
        component = ("owned path 深 " + "x" * 60)[:min(60, remaining)]
        if not component:
            component = "x"
        elif component.endswith(" "):
            component = component[:-1] + "x"
        root /= component
    if len(str(root / "AI Hub.exe")) >= 260:
        raise ValueError("Native fixture base is too deep to launch its candidate EXE: " + str(root))
    return root


def native_component_base():
    if os.name != "nt":
        return None
    # Match Environment.GetFolderPath(LocalApplicationData), rather than trusting
    # an inherited environment-variable override for a cleanup target.
    import ctypes
    location = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x001C, None, 0, location)
    if result != 0:
        raise OSError("Cannot determine the fixture's native local component base")
    return Path(location.value) / "AIHub" / "components"


def native_component_cache(root):
    base = native_component_base()
    if base is None:
        return None
    identity = hashlib.sha256(str(root.resolve()).upper().encode("utf-8")).hexdigest()[:24]
    cache = base / identity
    for path in (base.parent.parent, base.parent, base, cache):
        if path.exists() and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Native fixture component cache must not follow a reparse point: " + str(path))
    return cache


def remove_native_component_cache(root, cache):
    if cache is None or not cache.exists():
        return
    expected = native_component_cache(root)
    if expected != cache or cache.resolve().parent != native_component_base().resolve():
        raise ValueError("Refusing cleanup outside this fixture's installation identity")
    pending = [cache]
    while pending:
        path = pending.pop()
        attributes = path.lstat()
        if path.is_symlink() or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Refusing cleanup of a reparsed native fixture cache")
        if path.is_dir():
            pending.extend(path.iterdir())
    shutil.rmtree(cache)


def run_native_initialization_tests(common, candidate, folder, legacy_candidate=None):
    """Test binding and the independent EXE entry on exclusively synthetic data.

    The optional legacy candidate is a negative control: its own entry must fail
    with PathTooLongException. It is never substituted for the candidate under test.
    """
    probe = folder / "native-initialization-tests.exe"
    subprocess.run(common + ["/target:exe", "/reference:System.Drawing.dll",
                             "/reference:System.Windows.Forms.dll", f"/out:{probe}",
                             str(DESKTOP / "NativeInitializationTests.cs")], check=True)
    sidecars = folder / "stale-sdk"
    sidecars.mkdir()
    source = sidecars / "stale-sdk.cs"
    source.write_text('using System.Reflection; [assembly: AssemblyVersion("0.0.1.0")]\n'
                      'public class StaleSdkSentinel {}\n', encoding="utf-8")
    for name in ("Microsoft.Web.WebView2.Core", "Microsoft.Web.WebView2.WinForms"):
        subprocess.run(common + ["/target:library", f"/out:{sidecars / (name + '.dll')}", str(source)], check=True)
    binding_modes = ("clean", "current-sidecars", "stale-sidecars", "preloaded-conflict")
    scenarios = [(mode, 0, False, candidate) for mode in binding_modes]
    scenarios += [("deep-" + mode, 156, False, candidate) for mode in binding_modes]
    scenarios.append(("entry-deep-sdk", 156, True, candidate))
    # Root239 keeps the EXE path250 below the CLR's separate startup limit.
    # Its unchanged profile path261 must be rejected by the product before IO,
    # components or service startup; a CLR startup failure is not this evidence.
    scenarios.append(("entry-deep-profile-rejected", 239, True, candidate))
    if legacy_candidate is not None:
        scenarios.append(("entry-legacy-failure", 156, True, legacy_candidate))
    for mode, minimum_length, entry, source_candidate in scenarios:
        root = native_fixture_root(folder, mode, minimum_length)
        component_cache = native_component_cache(root)
        if component_cache is not None and component_cache.exists():
            raise ValueError("Refusing to reuse an existing installation component cache: " + str(component_cache))
        # Never reuse an existing fixture, even if a previous run was interrupted.
        root.mkdir(parents=True)
        (root / "native-test-fixture.marker").write_text("synthetic only", encoding="utf-8")
        (root / "frontend").mkdir()
        (root / "frontend/index.html").write_text("synthetic only", encoding="utf-8")
        # Never start the real application backend if fixture health identity fails.
        for script in ("server.py", "launcher.pyw"):
            (root / script).write_text('raise RuntimeError("native fixture must reuse its own listener")\n', encoding="utf-8")
        copied = root / "AI Hub.exe"
        shutil.copy2(source_candidate, copied)
        copied_probe = root / probe.name
        if not entry:
            shutil.copy2(probe, copied_probe)
        binding_mode = mode.removeprefix("deep-")
        if binding_mode in ("current-sidecars", "stale-sidecars", "preloaded-conflict"):
            dll_folder = folder if binding_mode == "current-sidecars" else sidecars
            for dll in dll_folder.glob("Microsoft.Web.WebView2.*.dll"):
                shutil.copy2(dll, root / dll.name)
        print("Native initialization fixture:", mode, "; root chars:", len(str(root)), flush=True)
        error = None
        try:
            subprocess.run([str(probe if entry else copied_probe), str(copied), str(root)] +
                           (["--entry-point", "--expect-entry-failure"] if mode == "entry-legacy-failure" else
                            ["--entry-point", "--expect-profile-rejection"] if mode == "entry-deep-profile-rejected" else
                            ["--entry-point"] if entry else
                            ["--preload-conflict"] if binding_mode == "preloaded-conflict" else
                            ["--stale-sidecars"] if binding_mode == "stale-sidecars" else []),
                           check=True, timeout=100 if entry else 50)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as caught:
            error = caught
        # This identity was absent before launching the newly created fixture.
        # Reject reparse points and re-check its exact scope before any deletion.
        # WebView2 shuts down asynchronously after its owning Form is disposed.
        if root.resolve() == folder.resolve() or not root.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Refusing cleanup outside the native fixture folder")
        for attempt in range(30):
            try:
                remove_native_component_cache(root, component_cache)
                if root.exists():
                    shutil.rmtree(root)
                break
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(0.2)
        if error is not None:
            raise error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--render-output", type=Path, help="With --test, save offline source-control frames (not real-window screenshots)")
    parser.add_argument("--update-render-output", type=Path, help="With --test, save an off-screen native update dialog preview")
    parser.add_argument("--execution-render-output", type=Path, help="With --test, save an off-screen native collaboration access preview")
    parser.add_argument("--alias-root", type=Path, help="Optional existing junction to test against the physical application folder")
    parser.add_argument("--startup-guard-candidate", type=Path,
                        help="With --test, also run the native update-race fixture against this candidate EXE")
    args = parser.parse_args()
    if (args.render_output or args.update_render_output or args.execution_render_output) and not args.test:
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
        subprocess.run(command + [str(DESKTOP / "Core.cs"), str(DESKTOP / "AppUpdate.cs"), str(DESKTOP / "ExecutionAccess.cs"),
                                  str(DESKTOP / "StartupAnimation.cs"), str(DESKTOP / "Program.cs")], check=True)
        if args.test:
            tests = folder / "desktop-tests.exe"
            test_command = common + ["/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll",
                                      "/target:exe", f"/out:{tests}", str(DESKTOP / "Core.cs"),
                                      str(DESKTOP / "AppUpdate.cs"), str(DESKTOP / "ExecutionAccess.cs"),
                                      str(DESKTOP / "ExecutionAccessTests.cs"), str(DESKTOP / "Tests.cs")]
            subprocess.run(test_command, check=True)
            candidate = args.startup_guard_candidate.resolve() if args.startup_guard_candidate else exe
            test_args = [str(tests), str(ROOT), "--candidate-exe", str(candidate)]
            if args.alias_root:
                test_args += ["--alias-root", str(args.alias_root)]
            if args.update_render_output:
                test_args += ["--update-render-output", str(args.update_render_output.resolve())]
            if args.execution_render_output:
                test_args += ["--execution-render-output", str(args.execution_render_output.resolve())]
            subprocess.run(test_args, check=True)
            icon_tests = folder / "icon-tests.exe"
            subprocess.run(common + ["/target:exe", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", f"/out:{icon_tests}",
                                      str(DESKTOP / "StartupAnimation.cs"), str(DESKTOP / "IconTests.cs")], check=True)
            subprocess.run([str(icon_tests), str(ROOT / "frontend/brand.ico"), str(exe)] +
                           ([str(args.render_output.resolve())] if args.render_output else []), check=True)
            run_native_initialization_tests(common, exe, folder)
        shutil.copy2(exe, output)
    result = {"exe": str(output), "bytes": output.stat().st_size,
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "architecture": "x64", "subsystem": "Windows GUI", "sdk": SDK_VERSION,
              "sdk_sha256": SDK_SHA256, "contains_user_data": False,
              "display_name": "曜核", "desktop_version": "2.13.11.0",
              "icon_sha256": hashlib.sha256((ROOT / "frontend/brand.ico").read_bytes()).hexdigest()}
    output.with_suffix(".build.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
