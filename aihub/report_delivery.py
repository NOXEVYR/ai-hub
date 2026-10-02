"""Read current local MCP spool metadata without using or exposing report bodies."""
from . import collaboration


def status(cfg):
    from tools import report_outbox
    root = collaboration.root_path(cfg)
    result = report_outbox.list_current(root)
    return {**result, 'available': bool(result.get('items')) or not bool(result.get('error_code')), 'root': root,
            'automatic_native_report_capture': False, 'cloud_upload': False,
            'retry_requires_current_claim': True}
