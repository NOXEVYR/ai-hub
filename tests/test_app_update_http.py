"""Native-only software update routes, tested on an ephemeral listener."""
import sys
from pathlib import Path
from unittest import mock

try:
    import test_service_control
except ImportError:  # 直接以 tests.test_app_update_http 方式运行时补齐同目录导入
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_service_control


class SoftwareUpdateHTTPTests(test_service_control.ControlHTTPTests):
    def setUp(self):
        super().setUp()
        self.manager = mock.Mock()
        self.manager.status.return_value = {'state': 'idle', 'current_version': '2.12.0'}
        self.manager.check.return_value = {'state': 'checking'}
        self.httpd.app_update = self.manager

    def test_status_is_read_only_and_native_check_is_authenticated(self):
        self.assertEqual(self.request('/api/app-update/status', method='GET')[0], 200)
        for headers in ({'X-AIHub-Control-Token': 'wrong'}, {'Origin': 'http://127.0.0.1:%s' % self.port},
                        {'Sec-Fetch-Mode': 'cors'}, {'Sec-Fetch-Site': 'none'}):
            self.assertEqual(self.request('/api/desktop/update/check', headers=headers)[0], 403)
        self.manager.check.assert_not_called()
        self.assertEqual(self.request('/api/desktop/update/check')[0], 200)
        self.manager.check.assert_called_once_with()

    def test_untrusted_path_url_and_unknown_keys_never_reach_manager(self):
        identity = {'instance_id': self.control.record['instance_id'], 'install_root': self.temporary.name}
        for field in ('url', 'package_path', 'command', 'helper_path'):
            self.assertEqual(self.request('/api/desktop/update/check', body={**identity, field: 'untrusted'})[0], 400)
        self.assertEqual(self.request('/api/desktop/update/not-real', body=identity)[0], 404)
        self.manager.check.assert_not_called()

    def test_error_details_are_not_disclosed_and_gate_is_released(self):
        for error, expected in ((ValueError('private path'),400),(RuntimeError('private token'),409),(OSError('secret'),500)):
            self.manager.check.side_effect = error
            status, value = self.request('/api/desktop/update/check')
            self.assertEqual(status, expected)
            self.assertNotIn('private', str(value))
            self.assertNotIn('secret', str(value))
        self.assertTrue(self.gate.request_stop())

    def test_no_update_mutation_after_shutdown_admission(self):
        self.assertTrue(self.gate.request_stop())
        self.assertEqual(self.request('/api/desktop/update/check')[0],503)
        self.manager.check.assert_not_called()
