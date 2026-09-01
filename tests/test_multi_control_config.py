import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


CONFIGURATION_PATH = Path(__file__).resolve().parents[1] / 'configuration.py'
SPEC = importlib.util.spec_from_file_location('multi_control_configuration_under_test',
                                              CONFIGURATION_PATH)
CONFIGURATION_MODULE = importlib.util.module_from_spec(SPEC)
FAKE_DOTENV = types.ModuleType('dotenv')
FAKE_DOTENV.load_dotenv = lambda *_args, **_kwargs: None
FAKE_AI_SUMMARY = types.ModuleType('ai_summary')


class FakeAISettings:
    @classmethod
    def from_environment(cls):
        return cls()


FAKE_AI_SUMMARY.AISettings = FakeAISettings
with patch.dict(sys.modules, {'dotenv': FAKE_DOTENV, 'ai_summary': FAKE_AI_SUMMARY}):
    SPEC.loader.exec_module(CONFIGURATION_MODULE)
Configuration = CONFIGURATION_MODULE.Configuration


BASE_ENV = {
    'UK_NUMMER': 'uk',
    'EMAIL_ADDRESS': 'mail@example.invalid',
    'EMAIL_PASSWORD': 'secret',
    'EWS_ENDPOINT': 'https://ews.example.invalid',
    'MATRIX_SERVER': 'https://matrix.example.invalid',
    'MATRIX_USER': '@bot:example.invalid',
    'MATRIX_PASSWORD': 'secret',
    'MATRIX_ROOM_ID': '!production:example.invalid',
    'MATRIX_CONSOLE_ROOM_ID': '!primary:example.invalid',
    'MATRIX_CONTROL_USER': '@primary:example.invalid',
    'MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM': 'true',
    'MATRIX_ALLOW_UNENCRYPTED_DIGEST_DM': 'true',
    'GOOGLE_FORM_LINK': 'https://form.example.invalid',
}


def configuration(additional=None):
    environment = dict(BASE_ENV)
    if additional is not None:
        environment['MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON'] = additional
    with (patch.dict(os.environ, environment, clear=True),
          patch.object(CONFIGURATION_MODULE, 'load_dotenv')):
        return Configuration()


class MultipleControlConfigurationTests(unittest.TestCase):
    def test_default_is_empty_and_primary_binding_is_first(self):
        config = configuration()
        config.validate_final_runtime()
        self.assertEqual(config.matrix_additional_control_rooms, {})
        self.assertEqual(config.control_bindings(), (
            ('@primary:example.invalid', '!primary:example.invalid'),))

    def test_valid_bindings_are_returned_primary_first_then_sorted(self):
        config = configuration(
            '{"@zeta:example.invalid":"!zeta:example.invalid",'
            '"@alpha:example.invalid":"!alpha:example.invalid"}')
        config.validate_final_runtime()
        self.assertEqual(config.control_bindings(), (
            ('@primary:example.invalid', '!primary:example.invalid'),
            ('@alpha:example.invalid', '!alpha:example.invalid'),
            ('@zeta:example.invalid', '!zeta:example.invalid')))

    def test_invalid_json_shape_ids_duplicates_and_limit_are_rejected(self):
        too_many = {f'@user{index}:example.invalid': f'!room{index}:example.invalid'
                    for index in range(9)}
        cases = (
            '[]',
            '{',
            '{"not-a-user":"!room:example.invalid"}',
            '{"@user:example.invalid":"not-a-room"}',
            ('{"@same:example.invalid":"!one:example.invalid",'
             '"@same:example.invalid":"!two:example.invalid"}'),
            ('{"@one:example.invalid":"!same:example.invalid",'
             '"@two:example.invalid":"!same:example.invalid"}'),
            json.dumps(too_many, separators=(',', ':')),
        )
        for index, raw in enumerate(cases):
            with self.subTest(index=index), self.assertRaisesRegex(
                    RuntimeError, 'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON ist ungültig'):
                configuration(raw)

    def test_primary_user_and_reserved_rooms_cannot_be_reused(self):
        cases = (
            '{"@primary:example.invalid":"!other:example.invalid"}',
            '{"@other:example.invalid":"!primary:example.invalid"}',
            '{"@other:example.invalid":"!production:example.invalid"}',
        )
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                config = configuration(raw)
                with self.assertRaisesRegex(RuntimeError, 'kollidiert mit einem Haupteintrag'):
                    config.validate_final_runtime()
