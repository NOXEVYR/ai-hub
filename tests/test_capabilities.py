import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aihub import capabilities as cap, collaboration, config, harnesses


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        self.data = self.base / 'data'
        self.patcher = patch.object(config, 'DATA_DIR', str(self.data))
        self.patcher.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.patcher.stop)
        self.heartbeat('alice', 'codex')
        self.heartbeat('bob', 'workbuddy')
        self.item = {'key': 'demo.video', 'name': '视频与图片制作', 'kind': 'mcp_tool',
            'provider': '示例提供方', 'server': 'media', 'domains': ['video', 'image'],
            'description': '按文字生成短视频；未实际调用。', 'tags': ['短片'],
            'inputs': {'type': 'object', 'properties': {
                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                'seconds': {'type': 'integer', 'minimum': 1, 'maximum': 15}},
                'required': ['prompt'], 'additionalProperties': False},
            'outputs': ['video artifact'], 'constraints': ['执行前确认成本'],
            'hints': {'cost': '未知，提供方另计', 'quality': '未实测'}}

    def heartbeat(self, client, tool='codex', cfg=None):
        # Explicit per-workspace fixture registration, including cross-root tests.
        if tool not in harnesses.allowed_ids(cfg or self.cfg, include_disabled=True):
            harnesses.save(cfg or self.cfg, {'id': tool, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        return collaboration.execute(cfg or self.cfg, 'client_heartbeat', {
            'client_id': client, 'tool': tool, 'name': client, 'protocol_version': 1})

    def publish(self, items=None, client='alice', cfg=None):
        return cap.execute(cfg or self.cfg, 'capability_publish', {
            'client_id': client, 'capabilities_json': json.dumps([self.item] if items is None else items, ensure_ascii=False)}, actor='mcp')

    def dispatch(self, inputs=None, **extra):
        identifier = cap.catalog(self.cfg)['items'][0]['id']
        return cap.execute(self.cfg, 'capability_dispatch', dict(
            capability_id=identifier, project='中文项目', title='制作短片',
            input_json=json.dumps(inputs if inputs is not None else {'prompt': '一只猫', 'seconds': 5}, ensure_ascii=False), **extra))

    def test_publish_snapshot_stable_ids_and_other_clients_preserved(self):
        result = self.publish()
        self.assertEqual(result['target_tool'], 'codex')
        identifier = cap.catalog(self.cfg)['items'][0]['id']
        self.publish(client='bob')
        self.publish()
        items = cap.catalog(self.cfg)['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['id'], identifier)
        self.assertNotEqual(items[0]['id'], items[1]['id'])
        self.assertEqual(items[1]['target_tool'], 'workbuddy')
        self.publish([])
        self.assertEqual([v['client_id'] for v in cap.catalog(self.cfg)['items']], ['bob'])

    def test_root_and_client_isolation(self):
        self.publish()
        other = self.base / 'Other'
        other.mkdir()
        cfg = dict(self.cfg, ai_root=str(other))
        self.assertEqual(cap.catalog(cfg)['total'], 0)
        with self.assertRaises(ValueError):
            self.publish(cfg=cfg)
        with self.assertRaises(ValueError):
            self.publish(client='not-registered')
        self.heartbeat('alice', cfg=cfg)
        self.publish(cfg=cfg)
        self.assertNotEqual(cap.catalog(cfg)['items'][0]['id'], cap.catalog(self.cfg)['items'][0]['id'])
        with self.assertRaises(ValueError):
            cap.execute(self.cfg, 'capability_list', {'_workspace_root': str(other)})

    def test_no_spoofing_tool_or_verification_and_transaction_rejection(self):
        self.publish()
        for field, value in [('target_tool', 'dsh'), ('verification_status', 'verified'),
                              ('client_id', 'bob'), ('command', 'cmd.exe'), ('url', 'https://example.com')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.publish([dict(self.item, **{field: value})])
        self.assertEqual(cap.catalog(self.cfg)['total'], 1)
        self.assertEqual(cap.catalog(self.cfg)['items'][0]['target_tool'], 'codex')

    def test_publish_limits_duplicates_secret_rejection(self):
        invalid = [None, {}, [self.item, self.item], [self.item] * 129,
                   [dict(self.item, name='x' * 161)], [dict(self.item, domains=['unknown'])],
                   [dict(self.item, description='Bearer abcdef01234567890123456')],
                   [dict(self.item, tags=['same', 'same'])],
                   [dict(self.item, hints={'api_key': 'private'})]]
        for value in invalid:
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                cap.publish(self.cfg, {'client_id': 'alice', 'capabilities_json': json.dumps(value)})
        for text in ('[{"key":"a","key":"b"}]', '[NaN]', '[' * 1100, 'x' * 256001):
            with self.assertRaises(ValueError):
                cap.publish(self.cfg, {'client_id': 'alice', 'capabilities_json': text})
        with patch.object(cap, 'MAX_ROOT_CAPABILITIES', 0), self.assertRaises(ValueError):
            self.publish()

    def test_schema_rejects_external_refs_credentials_and_unknown_required(self):
        schemas = [
            {'type': 'object', '$ref': 'https://example.org/schema'},
            {'type': 'object', 'properties': {'api_key': {'type': 'string'}}},
            {'type': 'object', 'properties': {'client_secret': {'type': 'string'}}},
            {'type': 'object', 'properties': {'token': {'type': 'string'}}},
            {'type': 'object', 'required': ['missing']},
            {'type': 'object', 'additionalProperties': True},
            {'type': 'object', 'properties': {'n': {'type': 'number', 'minimum': 5, 'maximum': 1}}},
            {'type': 'array', 'items': {'type': 'string'}},
        ]
        for schema in schemas:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                self.publish([dict(self.item, inputs=schema)])

    def test_recent_heartbeat_is_distinct_from_verified(self):
        self.publish()
        item = cap.catalog(self.cfg)['items'][0]
        self.assertTrue(item['client_online'])
        self.assertEqual(item['client_status'], 'recent_heartbeat')
        self.assertEqual(item['verification_status'], 'unverified')
        self.assertEqual(item['execution_mode'], 'harness_queue')
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=6)).isoformat()
        with collaboration.store(self.cfg) as (con, root):
            con.execute('UPDATE clients SET last_seen=? WHERE root=?', (old, root))
        item = cap.catalog(self.cfg)['items'][0]
        self.assertFalse(item['client_online'])
        self.assertEqual(item['client_status'], 'not_recently_seen')

    def test_recommend_explains_metadata_without_quality_promises(self):
        self.publish()
        result = cap.recommend(self.cfg, {'query': '生成视频'})
        self.assertEqual(result['recommendation_basis'], 'metadata_match')
        self.assertEqual(result['total'], 1)
        self.assertTrue(result['items'][0]['reasons'])
        self.assertEqual(cap.recommend(self.cfg, {'query': '语音', 'domain': 'audio'})['items'], [])
        self.assertEqual(cap.recommend(self.cfg, {'query': '无匹配内容'})['items'], [])
        self.assertEqual(cap.catalog(self.cfg, {'kind': 'skill'})['items'], [])
        with self.assertRaises(ValueError):
            cap.catalog(self.cfg, {'domain': 'anything'})

    def test_tool_filter_matches_mcp_and_retains_target_tool_compatibility(self):
        self.publish()
        self.publish(client='bob')
        for filters in ({'tool': 'codex'}, {'target_tool': 'codex'},
                        {'tool': 'codex', 'target_tool': 'codex'}):
            items = cap.execute(self.cfg, 'capability_list', filters, actor='mcp')['items']
            self.assertEqual([i['client_id'] for i in items], ['alice'])
        harnesses.save(self.cfg, {'id': 'dsh', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        self.assertEqual(cap.catalog(self.cfg, {'tool': 'dsh'})['items'], [])
        for filters in ({'tool': 'unknown'}, {'tool': 'codex', 'target_tool': 'workbuddy'}):
            with self.assertRaises(ValueError):
                cap.catalog(self.cfg, filters)

    def test_catalog_query_searches_declared_metadata_with_length_limit(self):
        self.publish()
        self.publish([dict(self.item, key='other', name='音频制作', domains=['audio'],
                           provider='Another', description='', server='voice', tags=['播客'])], client='bob')
        for query, client in [('示例提供方', 'alice'), ('MEDIA', 'alice'), ('短片', 'alice'),
                              ('播客', 'bob'), ('another', 'bob')]:
            found = cap.execute(self.cfg, 'capability_list', {'query': query}, actor='mcp')
            self.assertEqual([i['client_id'] for i in found['items']], [client])
        self.assertEqual(cap.catalog(self.cfg, {'query': '无关'})['items'], [])
        self.assertEqual(cap.catalog(self.cfg, {'query': '   '})['total'], 2)
        with self.assertRaises(ValueError):
            cap.catalog(self.cfg, {'query': 'x' * 2001})
        self.assertEqual(cap.recommend(self.cfg, {'query': '生成视频'})['total'], 1)

    def test_dispatch_is_real_queue_brief_and_artifact_lifecycle(self):
        self.publish()
        result = self.dispatch()
        self.assertEqual(result['status'], 'queued')
        self.assertTrue(result['worker_required'])
        task = result['task']
        self.assertEqual(task['target_tool'], 'codex')
        brief = (Path(task['paths']['work']) / 'TASK_BRIEF.md').read_text(encoding='utf-8')
        for expected in ('demo.video', '一只猫', '执行前确认成本', 'unverified'):
            self.assertIn(expected, brief)
        with self.assertRaises(ValueError):
            collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'bob'})
        lease = collaboration.execute(self.cfg, 'task_claim', {'task_id': task['id'], 'client_id': 'alice'})
        args = {'task_id': task['id'], 'client_id': 'alice', 'lease_token': lease['lease_token']}
        artifact = collaboration.execute(self.cfg, 'artifact_write', dict(args, kind='report',
                 title='隔离测试报告', filename='result.md', content='合成输出'))
        self.assertTrue(Path(artifact['path']).is_file())
        finished = collaboration.execute(self.cfg, 'task_finish', dict(args, summary='测试完成'))
        self.assertEqual(finished['status'], 'completed')

    def test_dispatch_input_validation_no_partial_tasks(self):
        self.publish()
        for inputs in ({}, {'prompt': ''}, {'prompt': 'x', 'seconds': True}, {'prompt': 'x', 'seconds': 16},
                       {'prompt': 'x', 'extra': 'unknown'}, {'prompt': 'x', 'api_key': 'private'},
                       {'prompt': 'x', 'seconds': float('nan')}, ['wrong']):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.dispatch(inputs)
        self.assertEqual(collaboration.execute(self.cfg, 'task_list', {})['items'], [])
        with self.assertRaises(ValueError):
            cap.execute(self.cfg, 'capability_dispatch', {'capability_id': 'missing', 'input_json': '{}'})
        with self.assertRaises(ValueError):
            cap.execute(self.cfg, 'capability_dispatch', {'capability_id': cap.catalog(self.cfg)['items'][0]['id'],
                        'project': '../escape', 'title': 'x', 'input_json': '{"prompt":"x"}'})

    def test_mcp_dispatch_requires_registered_caller_and_discover_is_ui_only(self):
        self.publish()
        with self.assertRaises(ValueError):
            cap.execute(self.cfg, 'capability_dispatch', {'capability_id': cap.catalog(self.cfg)['items'][0]['id'],
                        'project': 'x', 'title': 'x', 'input_json': '{"prompt":"x"}'}, actor='mcp')
        with self.assertRaises(PermissionError):
            cap.execute(self.cfg, 'capability_discover', {}, actor='mcp')

    def test_database_hardlink_and_linked_root_denied(self):
        self.publish()
        db = self.data / 'capabilities.sqlite3'
        link = self.base / 'db-copy'
        os.link(db, link)
        with self.assertRaises(ValueError):
            cap.catalog(self.cfg)
        link.unlink()
        with patch.object(config, '_is_reparse', return_value=True), self.assertRaises(ValueError):
            cap.catalog(self.cfg)

    def test_discovery_metadata_only_bounded_and_not_published(self):
        root = self.base / 'skills'
        package = root / 'sample'
        package.mkdir(parents=True)
        (package / 'SKILL.md').write_text('---\nname: 示例能力\ndescription: 只读取元数据\n---\n正文不得回传\n', encoding='utf-8')
        nested = root / 'nested' / 'deep'
        nested.mkdir(parents=True)
        (nested / 'SKILL.md').write_text('---\nname: 不递归\n---\n', encoding='utf-8')
        hidden = root / '.native'
        hidden.mkdir()
        (hidden / 'SKILL.md').write_text('---\nname: 不读取\n---\n', encoding='utf-8')
        with patch.object(cap, '_known_skill_roots', return_value=[('codex', root)]):
            result = cap.execute(self.cfg, 'capability_discover', {})
        self.assertEqual(len(result['suggestions']), 1)
        self.assertEqual(result['suggestions'][0]['name'], '示例能力')
        self.assertNotIn('正文不得回传', json.dumps(result, ensure_ascii=False))
        self.assertFalse(result['published'])
        self.assertEqual(cap.catalog(self.cfg)['total'], 0)

    def test_discovery_rejects_linked_skill_file(self):
        root = self.base / 'skills'
        package = root / 'sample'
        package.mkdir(parents=True)
        source = self.base / 'source.md'
        source.write_text('---\nname: linked\n---\n', encoding='utf-8')
        os.link(source, package / 'SKILL.md')
        with patch.object(cap, '_known_skill_roots', return_value=[('codex', root)]):
            result = cap.discover(self.cfg)
        self.assertEqual(result['suggestions'], [])
        self.assertEqual(result['skipped'], 1)

    def test_nested_schema_types_limits_and_enum(self):
        item = copy.deepcopy(self.item)
        item['inputs'] = {'type': 'object', 'properties': {'items': {'type': 'array', 'minItems': 1,
            'maxItems': 2, 'items': {'type': 'string', 'enum': ['一', '二']}}}, 'required': ['items']}
        self.publish([item])
        self.dispatch({'items': ['一']})
        for value in ([], ['三'], ['一', '二', '一']):
            with self.assertRaises(ValueError):
                self.dispatch({'items': value})


if __name__ == '__main__':
    unittest.main()
