import copy
import contextlib
import datetime as dt
import hashlib
import json
import uuid
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aihub import capabilities as cap, collaboration, config, harnesses
from aihub import capability_discovery as discovery


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

    def data_snapshot(self):
        if not self.data.exists():
            return None
        return tuple(sorted((str(path.relative_to(self.data)), path.is_dir(),
                             path.stat().st_size if path.is_file() else 0,
                             path.stat().st_mtime_ns)
                            for path in self.data.rglob('*')))

    def heartbeat(self, client, tool='codex', cfg=None):
        # Explicit per-workspace fixture registration, including cross-root tests.
        if tool not in harnesses.allowed_ids(cfg or self.cfg, include_disabled=True):
            harnesses.save(cfg or self.cfg, {'id': tool, 'revision': 0, 'connection_mode': 'mcp_stdio'})
        return collaboration.execute(cfg or self.cfg, 'client_heartbeat', {
            'client_id': client, 'tool': tool, 'name': client, 'protocol_version': 1})

    def test_skill_classification_bounds_english_terms_and_handles_common_domains(self):
        oil_title = discovery._category(
            'oil-codex-title',
            '管理 Codex 话题名称；不用于文章标题、视频标题、文件重命名。')
        self.assertNotIn('video', oil_title)
        self.assertNotIn('code', oil_title)

        video_prompt_skill = discovery._category(
            'plan-ai-video-prompts',
            '规划并改写 AI 影视与图像生成提示词，锁定人物、场景和色调；修复空间关系和动作衔接。')
        self.assertIn('video', video_prompt_skill)
        self.assertIn('image', video_prompt_skill)
        self.assertNotIn('code', video_prompt_skill)
        self.assertIn('code', discovery._category('software development helper'))

        lora_skill = discovery._category('lora-train', '画风与角色 LoRA 训练')
        self.assertIn('image', lora_skill)

        for label in ('PDF reader', 'spreadsheet workbook', 'PowerPoint presentation'):
            with self.subTest(label=label):
                self.assertIn('document', discovery._category(label))

        negated_video = discovery._category('title helper', 'Not intended for video titles.')
        self.assertNotIn('video', negated_video)
        mixed_video = discovery._category('title helper', 'Not intended for video titles, but creates video clips.')
        self.assertIn('video', mixed_video)

    def publish(self, items=None, client='alice', cfg=None):
        return cap.execute(cfg or self.cfg, 'capability_publish', {
            'client_id': client, 'capabilities_json': json.dumps([self.item] if items is None else items, ensure_ascii=False)}, actor='mcp')

    def dispatch(self, inputs=None, **extra):
        identifier = cap.catalog(self.cfg)['items'][0]['id']
        return cap.execute(self.cfg, 'capability_dispatch', dict(
            capability_id=identifier, project='中文项目', title='制作短片',
            input_json=json.dumps(inputs if inputs is not None else {'prompt': '一只猫', 'seconds': 5}, ensure_ascii=False), **extra))

    def test_dispatch_request_recovers_after_declaration_withdrawal_without_a_second_task(self):
        self.publish()
        identifier = cap.catalog(self.cfg)['items'][0]['id']
        payload = {'capability_id': identifier, 'project': 'Recovery', 'title': 'Stable intent',
                   'input_json': '{"prompt":"一只猫","seconds":5}', 'request_id': str(uuid.uuid4())}
        first = cap.dispatch(self.cfg, payload)
        self.publish([])
        # A new connection reads the durable receipt, before current catalog lookup.
        replay = cap.dispatch(dict(self.cfg), payload)
        self.assertTrue(replay['deduplicated'])
        self.assertEqual(replay['task']['id'], first['task']['id'])
        with collaboration.store(self.cfg) as (con, root):
            self.assertEqual(con.execute('SELECT COUNT(*) FROM tasks WHERE root=?', (root,)).fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM capability_dispatches WHERE root=?', (root,)).fetchone()[0], 1)
        for field, value in [('title', 'Changed'), ('input_json', '{"prompt":"changed"}')]:
            with self.assertRaisesRegex(ValueError, '同一派单请求'):
                cap.dispatch(self.cfg, {**payload, field: value})

    def test_dispatch_request_survives_concurrent_retry_and_rejects_invalid_uuid(self):
        from concurrent.futures import ThreadPoolExecutor
        self.publish()
        request_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.dispatch(request_id=request_id), range(4)))
        self.assertEqual(len({r['task']['id'] for r in results}), 1)
        self.assertEqual(sum(not r['deduplicated'] for r in results), 1)
        for invalid in ('not-a-uuid', 123, request_id.upper()):
            with self.assertRaisesRegex(ValueError, 'UUID'):
                self.dispatch(request_id=invalid)


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

    def test_discovery_reads_existing_capability_catalog_without_initializing_stores(self):
        self.publish()
        identifier = cap.catalog(self.cfg)['items'][0]['id']
        before = self.data_snapshot()
        with patch.object(cap, '_known_skill_roots', return_value=[]):
            result = cap.discover(self.cfg)
        catalog_source = next(v for v in result['sources'] if v['type'] == 'registered_capability_catalog')
        interface = next(v for v in result['interfaces'] if v['capability_id'] == identifier)
        self.assertEqual(catalog_source['status'], 'scanned_readonly')
        self.assertEqual(interface['kind'], 'mcp_tool')
        self.assertEqual(interface['domains'], ['video', 'image'])
        self.assertTrue(interface['discovery_only'])
        self.assertEqual(interface['evidence']['actual_invocation']['status'], 'not_observed')
        self.assertEqual(self.data_snapshot(), before)

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
                 title='隔离测试报告', filename='result.md', content='合成输出', category='report', memory_candidates=[]))
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

    def test_discovery_works_without_workspace_and_does_not_initialize_catalog_database(self):
        cfg = {'ai_root': '', 'workspace_managed': False}
        before = self.data_snapshot()
        with patch.object(cap, '_known_skill_roots', return_value=[]):
            result = cap.execute(cfg, 'capability_discover', {})
        self.assertEqual(result['suggestions'], [])
        catalog_source = next(v for v in result['sources'] if v['type'] == 'registered_capability_catalog')
        self.assertEqual(catalog_source['status'], 'unavailable_no_workspace')
        self.assertEqual(self.data_snapshot(), before)
        self.assertFalse((self.data / 'capabilities.sqlite3').exists())

    def test_discovery_with_empty_managed_workspace_does_not_create_catalog_or_sidecars(self):
        before = self.data_snapshot()
        with patch.object(cap, '_known_skill_roots', return_value=[]):
            result = cap.execute(self.cfg, 'capability_discover', {})
        catalog_source = next(v for v in result['sources'] if v['type'] == 'registered_capability_catalog')
        self.assertEqual(catalog_source['status'], 'unavailable_no_catalog')
        self.assertEqual(self.data_snapshot(), before)
        self.assertFalse((self.data / 'capabilities.sqlite3').exists())

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

    def test_multiline_skill_descriptions_preserve_domain_discovery_without_body(self):
        root = self.base / 'multiline-skills'
        root.mkdir()
        for index, marker in enumerate(('>', '|', '>-', '|-', '>+', '|+')):
            folder = root / str(index)
            folder.mkdir()
            (folder / 'SKILL.md').write_text(
                f'---\nname: studio-{index}\ndescription: {marker}\n  Generate video and image\n  for a creative project.\nmetadata:\n  ignored: private\n---\nNEVER_READ_BODY', encoding='utf-8')
        result = discovery.discover({'capability_sources': [
            {'tool': 'custom', 'kind': 'skills_root', 'path': str(root)}]}, [], lambda _cfg: {}, environ={})
        self.assertEqual(len(result['suggestions']), 6)
        for item in result['suggestions']:
            self.assertEqual(item['domains'], ['image', 'video'])
            self.assertIn('creative project', item['description'])
            self.assertNotIn('private', item['description'])
            self.assertNotIn('NEVER_READ_BODY', str(item))

    def test_multiline_skill_metadata_keeps_secret_and_size_guards(self):
        path = self.base / 'SKILL.md'
        for text in ('Bearer ' + 'x' * 40, 'x' * 2001):
            path.write_text('---\nname: media\ndescription: >\n  ' + text + '\n---\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                discovery._read_skill(path, 'custom')

    def test_interface_record_limit_is_reported_in_source_and_discovery(self):
        manifest = self.base / 'overflow.capabilities.json'
        manifest.write_text(json.dumps({'capabilities': [
            {'name': f'video-{i}', 'domain': 'video'} for i in range(129)]}), encoding='utf-8')
        result = discovery.discover({'capability_definition_files': [str(manifest)]}, [], lambda _cfg: {}, environ={})
        self.assertEqual(len(result['interfaces']), 128)
        source = next(x for x in result['sources'] if x['type'] == 'explicit_capability_manifest')
        self.assertEqual(source['status'], 'truncated_budget')
        self.assertEqual(source['records_skipped'], 1)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['skipped'], 1)

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
        self.assertEqual(result['suggestions'][0]['classification_source'], 'metadata_keyword_heuristic')
        self.assertEqual(result['suggestions'][0]['classification_status'], 'unclassified')
        self.assertIn('不代表准确', result['suggestions'][0]['classification_note'])
        self.assertEqual(result['suggestions'][0]['tools'], ['codex'])
        self.assertEqual(result['suggestions'][0]['domains'], [])
        self.assertNotIn('正文不得回传', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['interfaces'], [])
        self.assertIn('credentials', result)
        self.assertIn('sources', result)
        self.assertFalse(result['published'])
        self.assertEqual(cap.catalog(self.cfg)['total'], 0)

    def test_discovery_includes_configured_custom_roots_and_public_interface_definitions(self):
        root = self.base / 'custom-skills'
        package = root / 'make-media'
        package.mkdir(parents=True)
        (package / 'SKILL.md').write_text(
            '---\nname: 图像与视频整理\ndescription: image and video workflow\n---\nprivate body\n',
            encoding='utf-8')
        manifest = self.base / 'public.capabilities.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'capabilities': [
            {'name': '语音合成接口', 'description': '生成语音', 'domains': ['audio'],
             'provider': 'Example', 'operation_id': 'speech.create', 'env_vars': ['MEDIA_API_KEY']},
            {'name': '代码助手接口', 'description': 'software development', 'domains': ['code'],
             'provider': 'Example', 'operation_id': 'code.assist', 'env_vars': ['CODE_HELPER_TOKEN']},
        ]}, ensure_ascii=False), encoding='utf-8')
        cfg = dict(self.cfg, capability_sources=[
            {'tool': 'new-codex-harness', 'kind': 'skills_root', 'path': str(root)},
            {'tool': 'second-harness', 'kind': 'skills_root', 'path': str(root)},
            {'tool': 'independent-media-tool', 'kind': 'capability_manifest', 'path': str(manifest)},
            {'tool': 'another-media-tool', 'kind': 'capability_manifest', 'path': str(manifest)},
        ])
        with patch.object(cap, '_known_skill_roots', return_value=[]):
            with patch.dict(os.environ, {'MEDIA_API_KEY': 'DO_NOT_RETURN_MEDIA_SECRET',
                                         'CODE_HELPER_TOKEN': 'DO_NOT_RETURN_CODE_SECRET'}, clear=False):
                result = cap.discover(cfg)
        skill = result['suggestions'][0]
        interface = next(i for i in result['interfaces'] if i['operation_id'] == 'speech.create')
        code_interface = next(i for i in result['interfaces'] if i['operation_id'] == 'code.assist')
        self.assertEqual(skill['tools'], ['new-codex-harness', 'second-harness'])
        self.assertEqual(skill['domains'], ['image', 'video'])
        self.assertTrue(skill['discovery_only'])
        self.assertEqual(interface['tools'], ['another-media-tool', 'independent-media-tool'])
        self.assertEqual(interface['domains'], ['audio'])
        self.assertEqual(interface['provider'], 'Example')
        self.assertEqual(code_interface['domains'], ['code'])
        self.assertEqual(code_interface['tools'], ['another-media-tool', 'independent-media-tool'])
        self.assertEqual(interface['evidence']['callability']['status'], 'definition_only')
        self.assertEqual(interface['evidence']['actual_invocation']['status'], 'not_observed')
        self.assertTrue(interface['discovery_only'])
        credential = next(c for c in result['credentials'] if c['name'] == 'MEDIA_API_KEY')
        self.assertEqual(credential['domains'], ['audio'])
        self.assertEqual(credential['tools'], ['another-media-tool', 'independent-media-tool'])
        self.assertTrue(credential['present'])
        self.assertFalse(credential['value_included'])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('DO_NOT_RETURN_MEDIA_SECRET', serialized)
        self.assertNotIn('DO_NOT_RETURN_CODE_SECRET', serialized)

    def test_credentials_are_separate_name_presence_evidence(self):
        secret = 'SENTINEL_SECRET_VALUE_MUST_NOT_RETURN'
        with patch.object(cap, '_known_skill_roots', return_value=[]), \
                patch.dict(os.environ, {'DASHSCOPE_API_KEY': secret}, clear=False):
            result = cap.discover(self.cfg)
        credential = next(v for v in result['credentials'] if v['name'] == 'DASHSCOPE_API_KEY')
        self.assertTrue(credential['present'])
        self.assertEqual(credential['tools'], [])
        self.assertEqual(credential['domains'], [])
        self.assertFalse(credential['value_included'])
        self.assertFalse(credential['capability_inferred'])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['interfaces'], [])

    def test_environment_inventory_preserves_runtime_presence_and_exposes_persistent_custom_names(self):
        sources = [
            {'id': 'windows_user', 'status': 'scanned', 'names': ['MY_PRIVATE_SERVICE_KEY', 'DASHSCOPE_API_KEY']},
            {'id': 'windows_system', 'status': 'scanned', 'names': ['MACHINE_TOOL_SETTING']},
        ]
        result = discovery.discover({}, [], lambda _cfg: {}, environ={'RUNTIME_ONLY': 'NEVER_RETURN_VALUE'},
                                    persistent_sources=sources)
        known = next(item for item in result['credentials'] if item['name'] == 'DASHSCOPE_API_KEY')
        self.assertFalse(known['present'])
        self.assertFalse(known['runtime_available'])
        self.assertTrue(known['configured_in_system'])
        self.assertTrue(known['needs_restart'])
        custom = next(item for item in result['credentials'] if item['name'] == 'MY_PRIVATE_SERVICE_KEY')
        self.assertEqual(custom['association_status'], 'unassociated')
        self.assertEqual(custom['provider'], '')
        self.assertEqual(custom['domains'], [])
        self.assertEqual(custom['tools'], [])
        self.assertEqual(custom['sources'], ['windows_user'])
        runtime = next(item for item in result['credentials'] if item['name'] == 'RUNTIME_ONLY')
        self.assertTrue(runtime['present'])
        self.assertFalse(runtime['configured_in_system'])
        self.assertEqual(result['environment_inventory']['total'], 4)
        self.assertEqual(result['environment_inventory']['returned'], 4)
        self.assertFalse(result['environment_inventory']['value_included'])
        self.assertNotIn('NEVER_RETURN_VALUE', json.dumps(result))
        self.assertEqual(result['interfaces'], [])

    def test_manifest_association_survives_persistent_only_name_and_failure_is_unknown(self):
        manifest = self.base / 'custom.capabilities.json'
        manifest.write_text(json.dumps({'capabilities': [{'name': 'Explicit audio interface',
            'domains': ['audio'], 'provider': 'Example', 'env_vars': ['CUSTOM_SPEECH_KEY']}]}), encoding='utf-8')
        cfg = {'capability_sources': [{'tool': 'custom-audio', 'kind': 'capability_manifest', 'path': str(manifest)}]}
        sources = [{'id': 'windows_user', 'status': 'scanned', 'names': ['custom_speech_key']},
                   {'id': 'windows_system', 'status': 'unavailable', 'names': [], 'error_code': 5}]
        result = discovery.discover(cfg, [], lambda _cfg: {}, environ={}, persistent_sources=sources)
        credential = next(item for item in result['credentials'] if item['name'] == 'CUSTOM_SPEECH_KEY')
        self.assertEqual(credential['association_status'], 'manifest_declared')
        self.assertEqual(credential['domains'], ['audio'])
        self.assertEqual(credential['tools'], ['custom-audio'])
        self.assertFalse(credential['present'])
        self.assertTrue(credential['configured_in_system'])
        self.assertTrue(credential['needs_restart'])
        absent = next(item for item in result['credentials'] if item['name'] == 'OPENAI_API_KEY')
        self.assertIsNone(absent['configured_in_system'])
        self.assertFalse(absent['needs_restart'])

    def test_runtime_snapshot_budget_remains_visible_in_discovery(self):
        with patch.object(discovery.environment_inventory, 'MAX_SOURCE_NAMES', 2):
            result = discovery.discover({}, [], lambda _cfg: {}, environ=['ONE', 'TWO', 'THREE'],
                                        persistent_sources=[])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['environment_inventory']['sources'][0]['status'], 'truncated_budget')

    def test_discovery_source_settings_accept_custom_tools_and_reject_unsafe_paths(self):
        root = self.base / 'skills-custom'
        root.mkdir()
        manifest = self.base / 'public.capabilities.json'
        manifest.write_text('{"capabilities":[]}', encoding='utf-8')
        sources = cap.capability_discovery.validate_source_settings({'sources': [
            {'tool': 'third-party_harness', 'kind': 'skills_root', 'path': str(root)},
            {'tool': 'third-party_harness', 'kind': 'capability_manifest', 'path': str(manifest)},
        ]})
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]['tool'], 'third-party_harness')
        for invalid in (
            {'sources': [{'tool': 'x', 'kind': 'skills_root', 'path': str(self.base / 'missing')}]},
            {'sources': [{'tool': 'x', 'kind': 'capability_manifest', 'path': str(self.base / 'secret.json')}]},
            {'sources': [{'tool': 'x', 'kind': 'skills_root', 'path': '\\\\server\\share'}]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, OSError)):
                cap.capability_discovery.validate_source_settings(invalid)

    def test_discovery_scans_seventeen_manifests_and_marks_budget_overflow(self):
        sources = []
        for index in range(17):
            manifest = self.base / f'capability-{index}.capabilities.json'
            manifest.write_text(json.dumps({'capabilities': [{
                'name': f'接口 {index}', 'description': 'image generation',
                'domains': ['image'], 'provider': 'Test', 'operation_id': f'image.{index}',
            }]}), encoding='utf-8')
            sources.append({'tool': f'tool-{index}', 'kind': 'capability_manifest', 'path': str(manifest)})

        result = discovery.discover({'capability_sources': sources}, [], lambda _cfg: {}, environ={})
        self.assertEqual(len(result['interfaces']), 17)
        self.assertFalse(result['truncated'])

        overflow = discovery.discover(
            {'capability_definition_files': [sources[0]['path']] * (discovery.MAX_DECLARATION_FILES + 1)},
            [], lambda _cfg: {}, environ={})
        overflow_source = next(source for source in overflow['sources'] if source['status'] == 'truncated_budget')
        self.assertEqual(overflow_source['files_skipped'], 1)
        self.assertTrue(overflow['truncated'])

    def test_codex_plugin_manifest_exposes_only_package_mcp_hint_and_skill_domains(self):
        cache = self.base / 'plugins' / 'cache'
        package = cache / 'marketplace' / 'media-plugin' / '1.2.3'
        plugin_meta = package / '.codex-plugin'
        plugin_meta.mkdir(parents=True)
        (plugin_meta / 'plugin.json').write_text(
            '{"name":"Media Plugin","description":"Image and video tools",'
            '"mcpServers":{"local":{"command":"NEVER_RETURN_THIS_COMMAND",'
            '"args":["NEVER_RETURN_ARGUMENTS"],"env":{"TOKEN":"NEVER_RETURN_SECRET"}}}}',
            encoding='utf-8')
        (package / 'private.mcp.json').write_text('NEVER_READ_MCP_CONFIG_CONTENT', encoding='utf-8')
        skill = package / 'skills' / 'generate'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(
            '---\nname: Image and video generation\ndescription: image video\n---\n', encoding='utf-8')
        with patch.object(cap, '_known_skill_roots', return_value=[('codex', cache)]):
            result = cap.execute({'ai_root': '', 'workspace_managed': False}, 'capability_discover', {})
        hint = next(i for i in result['interfaces'] if i['kind'] == 'plugin_mcp_hint')
        self.assertEqual(hint['plugin_name'], 'Media Plugin')
        self.assertTrue(hint['mcp_servers_declared'])
        self.assertEqual(hint['tools'], ['codex'])
        self.assertEqual(hint['domains'], ['image', 'video'])
        self.assertEqual(hint['classification_source'], 'inferred_from_plugin_skills')
        self.assertFalse(hint['mcp_details_included'])
        self.assertEqual(hint['evidence']['configuration']['status'], 'manifest_has_mcpServers')
        self.assertEqual(hint['evidence']['callability']['status'], 'not_tested')
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in ('NEVER_RETURN_THIS_COMMAND', 'NEVER_RETURN_ARGUMENTS', 'NEVER_RETURN_SECRET',
                       'NEVER_READ_MCP_CONFIG_CONTENT'):
            self.assertNotIn(secret, serialized)

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


class DispatchLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Workspace'
        self.root.mkdir()
        self.cfg = {'ai_root': str(self.root), 'workspace_managed': True}
        self.data = self.base / 'data'
        self.patcher = patch.object(config, 'DATA_DIR', str(self.data))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.request_id = str(uuid.uuid4())

    def snapshot(self):
        # Include directory identity, file bytes and timestamps: a new backup,
        # migration or same-size rewrite must not pass a read-only assertion.
        if not self.data.exists():
            return None
        return tuple(sorted((str(path.relative_to(self.base)), path.is_dir(),
                             path.stat().st_mtime_ns,
                             hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                            for path in [self.data, *self.data.rglob('*')]))

    def lookup(self, cfg=None, request_id=None):
        return cap.dispatch_receipt(cfg or self.cfg, {'request_id': request_id or self.request_id})

    def create_dispatch(self, cfg=None, actor='ui', request_id=None):
        cfg = cfg or self.cfg
        if 'codex' not in harnesses.allowed_ids(cfg, include_disabled=True):
            harnesses.save(cfg, {'id': 'codex', 'revision': 0, 'connection_mode': 'mcp_stdio'})
        collaboration.execute(cfg, 'client_heartbeat', {
            'client_id': 'lookup-client', 'tool': 'codex', 'name': 'Lookup fixture', 'protocol_version': 1})
        cap.publish(cfg, {'client_id': 'lookup-client', 'capabilities_json': json.dumps([{
            'key': 'lookup.fixture', 'name': '回查夹具', 'kind': 'mcp_tool',
            'provider': 'Fixture', 'server': 'fixture', 'domains': ['code'],
            'inputs': {'type': 'object'}}])})
        return cap.dispatch(cfg, {'capability_id': cap.catalog(cfg)['items'][0]['id'],
            'project': 'Lookup', 'title': 'Opaque receipt fixture', 'input_json': '{}',
            'client_id': 'lookup-client', 'request_id': request_id or self.request_id}, actor=actor)

    def test_lookup_missing_data_does_not_create_store_or_connect(self):
        self.assertFalse(self.data.exists())
        with patch.object(collaboration, 'store', side_effect=AssertionError('write store used')), \
                patch.object(collaboration.sqlite3, 'connect', side_effect=AssertionError('missing DB opened')):
            result = self.lookup()
        self.assertEqual(result, {'found': False, 'request_id': self.request_id,
                                  'deduplicated': True, 'task': None})
        self.assertFalse(self.data.exists())

    def test_lookup_legacy_schema_does_not_migrate_backup_or_change_bytes(self):
        self.data.mkdir()
        db = self.data / 'collaboration.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as con, con:
            con.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, root TEXT, title TEXT)')
            con.execute('INSERT INTO tasks VALUES (?, ?, ?)',
                        (str(uuid.uuid4()), config._key(str(self.root)), 'Preserve legacy row'))
            con.execute('PRAGMA user_version=1')
        before = self.snapshot()
        with patch.object(collaboration, 'store', side_effect=AssertionError('write store used')), \
                patch.object(collaboration, '_migrate_submission_schema',
                             side_effect=AssertionError('legacy schema migrated')):
            self.assertFalse(self.lookup()['found'])
            self.assertFalse(self.lookup()['found'])
        self.assertEqual(self.snapshot(), before)
        with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual([r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")], ['tasks'])
            self.assertEqual(con.execute('SELECT title FROM tasks').fetchone()[0], 'Preserve legacy row')

    def test_lookup_only_returns_ui_receipt_in_current_root(self):
        first = self.create_dispatch()
        other = self.base / 'Other'
        other.mkdir()
        other_cfg = dict(self.cfg, ai_root=str(other))
        self.assertFalse(self.lookup(other_cfg)['found'])
        # The same UUID is valid in another root and for an MCP caller. Neither
        # receipt may replace the UI receipt scoped to this workspace.
        other_task = self.create_dispatch(other_cfg)['task']
        mcp_task = self.create_dispatch(actor='mcp')['task']
        unknown_id = str(uuid.uuid4())
        before = self.snapshot()
        with patch.object(collaboration, 'store', side_effect=AssertionError('write store used')):
            current = self.lookup()
            other_result = self.lookup(other_cfg)
            missing = self.lookup(request_id=unknown_id)
        self.assertTrue(current['found'])
        self.assertEqual(current['task']['id'], first['task']['id'])
        self.assertEqual(other_result['task']['id'], other_task['id'])
        self.assertNotEqual(current['task']['id'], mcp_task['id'])
        self.assertFalse(missing['found'])
        self.assertIsNone(missing['task'])
        serialized = json.dumps(current)
        for private_field in ('payload_hash', 'lease_hash', 'caller', 'mcp:lookup-client'):
            self.assertNotIn(private_field, serialized)
        self.assertEqual(self.snapshot(), before)

    def test_lookup_mcp_only_receipt_is_not_exposed_to_ui(self):
        self.create_dispatch(actor='mcp')
        before = self.snapshot()
        result = self.lookup()
        self.assertEqual(result, {'found': False, 'request_id': self.request_id,
                                  'deduplicated': True, 'task': None})
        self.assertEqual(self.snapshot(), before)

    def test_lookup_rejects_invalid_uuid_and_extra_fields_without_creating_data(self):
        for invalid in ('not-a-uuid', '', 123, None, self.request_id.upper(),
                        self.request_id.replace('-', ''), '{' + self.request_id + '}'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'UUID'):
                cap.dispatch_receipt(self.cfg, {'request_id': invalid})
        for invalid_body in (None, [], {}, {'request_id': self.request_id, 'caller': 'mcp:lookup-client'},
                             {'request_id': self.request_id, 'client_id': 'lookup-client'}):
            with self.subTest(body=invalid_body), self.assertRaisesRegex(ValueError, '只接受 request_id'):
                cap.dispatch_receipt(self.cfg, invalid_body)
        self.assertFalse(self.data.exists())


if __name__ == '__main__':
    unittest.main()
