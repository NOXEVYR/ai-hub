"""Workbench GET reads bounded spool metadata for only the selected workspace."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aihub import api, report_delivery


class ReportDeliveryStatusTests(unittest.TestCase):
    def test_get_binds_current_workspace_without_native_capture_or_cloud_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = {'workspace_managed': True, 'ai_root': temp}
            with patch('tools.report_outbox.list_current', return_value={'items': [], 'error_code': None}) as listing:
                code, _, body = api.dispatch(None, cfg, 'GET', '/api/collaboration/report_delivery_status', {'_workspace_root': [temp]}, None)
                self.assertEqual(code, 200)
                result = json.loads(body)
                self.assertFalse(result['cloud_upload'])
                self.assertFalse(result['automatic_native_report_capture'])
                self.assertTrue(result['retry_requires_current_claim'])
                listing.assert_called_once_with(str(Path(temp).resolve()))
            with patch('tools.report_outbox.list_current') as listing:
                for params in ({}, {'_workspace_root': str(Path(temp) / 'other')}):
                    code, _, _ = api.dispatch(None, cfg, 'GET', '/api/collaboration/report_delivery_status', params, None)
                    self.assertEqual(code, 400)
                listing.assert_not_called()

    def test_unavailable_spool_remains_unknown_not_empty_success(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('tools.report_outbox.list_current', return_value={'items': [], 'error_code': 'storage_unavailable'}):
                result = report_delivery.status({'workspace_managed': True, 'ai_root': temp})
                self.assertFalse(result['available'])
