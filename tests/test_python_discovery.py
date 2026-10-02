import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
START_VBS = ROOT / "start.vbs"


@unittest.skipUnless(os.name == "nt", "the fallback launcher is Windows Script Host")
class PythonDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cscript = shutil.which("cscript.exe") or str(Path(os.environ["WINDIR"]) / "System32" / "cscript.exe")
        if not Path(cls.cscript).is_file():
            raise unittest.SkipTest("cscript.exe is unavailable")

    def test_check_returns_the_validated_interpreter(self):
        result = subprocess.run(
            [self.cscript, "//nologo", str(START_VBS), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        executable = result.stdout.strip()
        self.assertTrue(Path(executable).is_file(), executable)
        self.assertNotIn("\\windowsapps\\", executable.lower())
        verify = subprocess.run(
            [executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        version, resolved = verify.stdout.splitlines()
        major, minor = map(int, version.split("."))
        self.assertEqual(major, 3)
        self.assertGreaterEqual(minor, 9)
        self.assertEqual(os.path.normcase(os.path.realpath(resolved)), os.path.normcase(os.path.realpath(executable)))

    def test_check_failure_is_silent_and_bounded_without_python(self):
        with tempfile.TemporaryDirectory(prefix="yaohe-no-python-") as temporary:
            script = Path(temporary) / "start.vbs"
            shutil.copy2(START_VBS, script)
            environment = os.environ.copy()
            environment["PATH"] = ""
            result = subprocess.run(
                [self.cscript, "//nologo", str(script), "--check"],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
