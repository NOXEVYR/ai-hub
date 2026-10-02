import json
import unittest
from unittest.mock import patch

from aihub import environment_inventory as inventory


def persistent(user=(), system=(), user_status='scanned', system_status='scanned'):
    return [{'id': 'windows_user', 'status': user_status, 'names': user},
            {'id': 'windows_system', 'status': system_status, 'names': system}]


class KeysOnly:
    def __iter__(self):
        return iter(['MY_CUSTOM_KEY', 'EMPTY_KEY'])

    def __getitem__(self, name):
        raise AssertionError('Environment values must never be accessed')

    def values(self):
        raise AssertionError('Environment values must never be accessed')


class FakeRegistry:
    def __init__(self, names=(), open_code=0, enum_code=None):
        self.names = names
        self.open_code = open_code
        self.enum_code = enum_code
        self.closed = 0
        self.enum_calls = 0

    def RegOpenKeyExW(self, root, subkey, options, access, handle):
        self.assertions = (options, access)
        handle._obj.value = 123
        return self.open_code

    def RegEnumValueW(self, handle, index, buffer, length, reserved, kind, data, size):
        if any(value is not None for value in (reserved, kind, data, size)):
            raise AssertionError('Registry must receive NULL data, type and size pointers')
        self.enum_calls += 1
        if self.enum_code is not None:
            return self.enum_code
        if index >= len(self.names):
            return 259
        name = self.names[index]
        if len(name) >= length._obj.value:
            return 234
        buffer.value = name
        length._obj.value = len(name)
        return 0

    def RegCloseKey(self, handle):
        self.closed += 1
        return 0


class EnvironmentInventoryTests(unittest.TestCase):
    def test_keys_only_and_custom_names_have_no_inferred_association(self):
        result = inventory.build_inventory(KeysOnly(), persistent())
        self.assertEqual(result['total'], 2)
        item = next(item for item in result['items'] if item['name'] == 'MY_CUSTOM_KEY')
        self.assertEqual(item['association_status'], 'unassociated')
        self.assertTrue(item['runtime_available'])
        self.assertFalse(item['configured_in_system'])
        self.assertFalse(item['needs_restart'])
        for field in ('provider', 'provider_hint'):
            self.assertEqual(item[field], '')
        for field in ('domains', 'tools'):
            self.assertEqual(item[field], [])
        self.assertFalse(item['value_included'])
        self.assertFalse(item['capability_inferred'])
        self.assertEqual(item['evidence']['callability']['status'], 'not_established')
        self.assertEqual(item['evidence']['actual_invocation']['status'], 'not_observed')

    def test_persistent_only_requires_restart_and_refresh_does_not_inject_values(self):
        runtime = {'INHERITED': 'SENTINEL_RUNTIME_VALUE'}
        before = dict(runtime)
        first = inventory.build_inventory(runtime, persistent())
        second = inventory.build_inventory(runtime, persistent(user=['INHERITED', 'NEW_CUSTOM_NAME']))
        self.assertEqual(first['total'], 1)
        item = next(item for item in second['items'] if item['name'] == 'NEW_CUSTOM_NAME')
        self.assertEqual(item['sources'], ['windows_user'])
        self.assertTrue(item['configured_in_system'])
        self.assertFalse(item['runtime_available'])
        self.assertTrue(item['needs_restart'])
        inherited = next(item for item in second['items'] if item['name'] == 'INHERITED')
        self.assertFalse(inherited['needs_restart'])
        self.assertEqual(runtime, before)
        self.assertNotIn('SENTINEL_RUNTIME_VALUE', json.dumps(second))

    def test_name_case_deduplicates_sources_and_manifest_is_explicit(self):
        associations = [{'name': 'CUSTOM_TOKEN', 'provider': 'Explicit Provider',
                         'domains': ['audio'], 'tools': ['media-tool']}]
        result = inventory.build_inventory(['custom_token'], persistent(['CUSTOM_TOKEN'], ['Custom_Token']),
                                           associations, {'CUSTOM_TOKEN': 'legacy-hint'})
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['sources'], ['process', 'windows_user', 'windows_system'])
        self.assertEqual(item['association_status'], 'manifest_declared')
        self.assertEqual(item['provider'], 'Explicit Provider')
        self.assertEqual(item['domains'], ['audio'])
        self.assertEqual(item['tools'], ['media-tool'])

    def test_failure_and_unsupported_are_unknown_not_absent(self):
        for status in ('unavailable', 'not_supported', 'truncated_budget'):
            result = inventory.build_inventory(['PROCESS_ONLY'], persistent(user_status=status))
            self.assertIsNone(result['items'][0]['configured_in_system'])
            self.assertFalse(result['items'][0]['needs_restart'])
            self.assertEqual(result['sources'][1]['status'], status)
        ignored = inventory.build_inventory([], persistent(['DO_NOT_TRUST'], user_status='unavailable'))
        self.assertEqual(ignored['items'], [])

    def test_cross_platform_never_loads_registry(self):
        with patch.object(inventory, '_registry_functions', side_effect=AssertionError('must not load')):
            sources = inventory.persistent_name_sources(platform='posix')
        self.assertTrue(all(source['status'] == 'not_supported' for source in sources))

    def test_name_filter_limits_and_search_before_pagination(self):
        names = ['PATH', '=C:', '', 'has-hyphen', 'TOKEN=secret', 'x' * 129, 123, 'NAME_1', 'name_1']
        result = inventory.build_inventory(names, persistent())
        self.assertEqual({item['name'] for item in result['items']}, {'PATH', 'NAME_1'})
        self.assertEqual(result['sources'][0]['names_rejected'], 6)
        many = [f'ENV_{i:04}' for i in range(20)]
        result = inventory.build_inventory(many, persistent(), query='ENV_0019', limit=1)
        self.assertEqual([item['name'] for item in result['items']], ['ENV_0019'])
        self.assertFalse(result['truncated'])
        self.assertTrue(inventory.build_inventory(many, persistent(), limit=1)['truncated'])
        with patch.object(inventory, 'MAX_SOURCE_NAMES', 3):
            result = inventory.build_inventory(many, persistent())
        self.assertEqual(result['sources'][0]['status'], 'truncated_budget')
        self.assertEqual(result['total'], 3)
        for args in ({'query': 1}, {'query': 'x' * 2001}, {'limit': True}, {'limit': 0}, {'limit': 1025}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                inventory.build_inventory([], persistent(), **args)

    def test_native_registry_enumerates_names_only_and_closes_handle(self):
        api = FakeRegistry(['CUSTOM_TOKEN', 'x' * 200, 'PATH'])
        result = inventory._windows_source('windows_user', api)
        self.assertEqual(result['status'], 'scanned')
        self.assertEqual(result['names'], ['CUSTOM_TOKEN', 'PATH'])
        self.assertEqual(result['names_rejected'], 1)
        self.assertEqual(api.assertions, (0, 1))  # KEY_QUERY_VALUE only
        self.assertEqual(api.closed, 1)
        self.assertEqual(api.enum_calls, 4)

    def test_registry_failures_are_unavailable_and_error_text_not_returned(self):
        api = FakeRegistry(open_code=5)
        result = inventory._windows_source('windows_system', api)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['error_code'], 5)
        self.assertEqual(api.enum_calls, 0)
        self.assertEqual(api.closed, 0)
        api = FakeRegistry(enum_code=5)
        result = inventory._windows_source('windows_user', api)
        self.assertEqual(result['names'], [])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(api.closed, 1)
        with patch.object(inventory, '_registry_functions', side_effect=OSError('SECRET_ERROR_TEXT')):
            result = inventory._windows_source('windows_user')
        self.assertNotIn('SECRET_ERROR_TEXT', json.dumps(result))

    def test_registry_budget_cannot_loop_forever(self):
        api = FakeRegistry(enum_code=234)
        with patch.object(inventory, 'MAX_SOURCE_NAMES', 3):
            result = inventory._windows_source('windows_user', api)
        self.assertEqual(result['status'], 'truncated_budget')
        self.assertEqual(api.enum_calls, 3)
        self.assertEqual(api.closed, 1)


if __name__ == '__main__':
    unittest.main()
