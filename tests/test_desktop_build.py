"""The build gate must execute the real desktop on isolated synthetic data."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "desktop/build.py"
spec = importlib.util.spec_from_file_location("desktop_build", MODULE)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class NativeInitializationGateTests(unittest.TestCase):
    def test_test_option_requires_native_gate_before_delivering_exe(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            sdk = folder / "sdk.nupkg"
            with build.zipfile.ZipFile(sdk, "w") as archive:
                for member in ("lib/net462/Microsoft.Web.WebView2.Core.dll", "lib/net462/Microsoft.Web.WebView2.WinForms.dll",
                               "runtimes/win-x64/native/WebView2Loader.dll", "LICENSE.txt"):
                    archive.writestr(member, b"synthetic SDK")
            compiler = folder / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"synthetic compiler")
            output = folder / "delivered.exe"

            def compile_fixture(command, **kwargs):
                target = next((item[5:] for item in command if item.startswith("/out:")), None)
                if target:
                    Path(target).write_bytes(b"compiled candidate")

            with patch.object(sys, "argv", [str(MODULE), "--sdk-package", str(sdk), "--output", str(output), "--test"]), \
                    patch.dict(build.os.environ, {"WINDIR": str(folder)}), \
                    patch.object(build, "SDK_SHA256", build.hashlib.sha256(sdk.read_bytes()).hexdigest()), \
                    patch.object(build.subprocess, "run", side_effect=compile_fixture), \
                    patch.object(build, "run_native_initialization_tests", side_effect=RuntimeError("native gate failed")) as gate:
                with self.assertRaisesRegex(RuntimeError, "native gate failed"):
                    build.main()
                gate.assert_called_once()
                self.assertEqual(gate.call_args.args[1].name, "AI Hub.exe")
                self.assertFalse(output.exists(), "a failed native gate must not deliver a candidate EXE")

    def run_fixture(self, failure=None, legacy=False):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            candidate = folder / "final.exe"
            candidate.write_bytes(b"final candidate sentinel")
            baseline = folder / "legacy.exe"
            if legacy:
                baseline.write_bytes(b"legacy candidate sentinel")
            for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
                (folder / name).write_bytes(b"current SDK fixture")
            modes = []

            def run(command, **kwargs):
                self.assertTrue(kwargs["check"])
                output = next((item[5:] for item in command if item.startswith("/out:")), None)
                if output:
                    Path(output).write_bytes(b"compiled fixture")
                    return
                probe, copied, root = map(Path, command[:3])
                mode = root.relative_to(folder).parts[0].removeprefix("native-fixture-")
                binding_mode = mode.removeprefix("deep-")
                entry = mode.startswith("entry-")
                self.assertEqual(probe.parent, folder if entry else root)
                self.assertEqual(copied.parent, root)
                self.assertNotEqual(copied, candidate)
                self.assertEqual(copied.read_bytes(), baseline.read_bytes() if mode == "entry-legacy-failure" else candidate.read_bytes())
                self.assertTrue((root / "native-test-fixture.marker").is_file())
                self.assertNotEqual(root, build.ROOT)
                self.assertIn("must reuse its own listener", (root / "launcher.pyw").read_text())
                self.assertTrue((root / "frontend/index.html").is_file())
                self.assertEqual(kwargs["timeout"], 100 if entry else 50)
                modes.append(mode)
                sidecars = sorted(path.name for path in root.glob("*.dll"))
                self.assertEqual(len(sidecars), 2 if binding_mode in ("current-sidecars", "stale-sidecars", "preloaded-conflict") else 0)
                self.assertEqual("--preload-conflict" in command, binding_mode == "preloaded-conflict")
                self.assertEqual("--stale-sidecars" in command, binding_mode == "stale-sidecars")
                self.assertEqual("--entry-point" in command, entry)
                self.assertEqual("--expect-entry-failure" in command, mode == "entry-legacy-failure")
                self.assertEqual("--expect-profile-rejection" in command, mode == "entry-deep-profile-rejected")
                if mode.startswith("deep-") or mode in ("entry-deep-sdk", "entry-legacy-failure"):
                    self.assertGreaterEqual(len(str(root)), 156)
                    self.assertGreater(len(str(root / "data/desktop/sdk" / ("a" * 64) / "Microsoft.Web.WebView2.Core.dll")), 260)
                if mode == "entry-deep-profile-rejected":
                    self.assertGreater(len(str(root / "data/desktop/WebView2")), 260)
                self.assertLess(len(str(copied)), 260, "candidate launch must not hide a derived-path failure")
                if failure:
                    raise failure

            with patch.object(build.subprocess, "run", side_effect=run):
                build.run_native_initialization_tests(["compiler"], candidate, folder, baseline if legacy else None)
            self.assertEqual(candidate.read_bytes(), b"final candidate sentinel")
            if legacy:
                self.assertEqual(baseline.read_bytes(), b"legacy candidate sentinel")
            self.assertFalse(list(folder.glob("native-fixture-*/*/AI Hub.exe")))
            return modes

    def test_gate_uses_final_exe_and_isolates_all_binding_contexts(self):
        self.assertEqual(self.run_fixture(), ["clean", "current-sidecars", "stale-sidecars", "preloaded-conflict",
                                              "deep-clean", "deep-current-sidecars", "deep-stale-sidecars", "deep-preloaded-conflict", "entry-deep-sdk",
                                              "entry-deep-profile-rejected"])

    def test_legacy_negative_control_runs_its_own_entry_without_replacing_candidate(self):
        self.assertEqual(self.run_fixture(legacy=True)[-1], "entry-legacy-failure")

    def test_overlong_fixture_base_fails_explicitly_instead_of_shortening_it(self):
        with self.assertRaisesRegex(ValueError, "too deep to launch"):
            build.native_fixture_root(Path("x" * 260), "entry-deep-profile", 242)

    def test_existing_component_cache_is_never_reused_or_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            candidate = folder / "candidate.exe"
            candidate.write_bytes(b"candidate")
            base = folder / "synthetic-local-app-data/AIHub/components"
            with patch.object(build, "native_component_base", return_value=base):
                cache = build.native_component_cache(build.native_fixture_root(folder, "clean"))
                cache.mkdir(parents=True)
                sentinel = cache / "existing-installation.txt"
                sentinel.write_text("preserve")

                def compile_fixture(command, **kwargs):
                    target = next((item[5:] for item in command if item.startswith("/out:")), None)
                    if target:
                        Path(target).write_bytes(b"compiled fixture")
                    else:
                        self.fail("an existing installation cache must block process launch")

                with patch.object(build.subprocess, "run", side_effect=compile_fixture):
                    with self.assertRaisesRegex(ValueError, "existing installation component cache"):
                        build.run_native_initialization_tests(["compiler"], candidate, folder)
                self.assertEqual(sentinel.read_text(), "preserve")

    def test_cleanup_verifies_identity_and_preserves_other_installation_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            root = folder / "owned-fixture"
            root.mkdir()
            base = folder / "synthetic-local-app-data/AIHub/components"
            with patch.object(build, "native_component_base", return_value=base):
                own = build.native_component_cache(root)
                own.mkdir(parents=True)
                (own / "component.dll").write_bytes(b"owned")
                foreign = base / ("a" * 24)
                foreign.mkdir()
                sentinel = foreign / "component.dll"
                sentinel.write_bytes(b"other installation")
                with self.assertRaisesRegex(ValueError, "outside this fixture"):
                    build.remove_native_component_cache(root, foreign)
                self.assertTrue(own.exists())
                build.remove_native_component_cache(root, own)
                self.assertFalse(own.exists())
                self.assertEqual(sentinel.read_bytes(), b"other installation")

    def test_native_failure_is_a_build_failure(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_fixture(subprocess.CalledProcessError(1, ["native-probe"]))

    def test_native_timeout_is_a_build_failure(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_fixture(subprocess.TimeoutExpired(["native-probe"], 50))


if __name__ == "__main__":
    unittest.main()
