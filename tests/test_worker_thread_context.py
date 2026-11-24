"""Проверки data-only контекста worker без реальных процессов и сетевых запросов."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from messenger_worksheets import codex_rpc, codex_worker as worker


class WorkerThreadContextTest(unittest.TestCase):
    """Проверить совместимость schema, изоляцию истории и материализацию файлов."""

    def payload(self, **fields):
        """Создать минимальный прежний четырёхпольный запрос с явными overrides."""
        return {'text': 'current', 'task': False, 'session': None, 'images': [], **fields}

    def test_schema_limits_and_legacy_compatibility(self):
        """Принять старую schema и отклонить неверные роли, пути, дубликаты и квоты."""
        self.assertEqual(worker.Request.decode(self.payload()).options, {})
        request = worker.Request.decode(self.payload(session=str(uuid.uuid4()), context=[{'role': 'assistant', 'text': '<!-- html -->'}]))
        self.assertIsNone(request.session)
        invalid = [
            {'context': [{'role': 'system', 'text': 'bad'}]},
            {'context': [{'role': [], 'text': 'bad'}]},
            {'context': [{'role': 'user', 'text': 'Ж' * 8193}]},
            {'context': [{'role': 'user', 'text': 'a' * 16384}] * 5},
            {'context': [{'role': 'user', 'text': ''}] * 22},
            {'history_files': [{'name': '../outside', 'data': ''}]},
            {'history_files': [{'name': 'same', 'data': ''}] * 2},
            {'options': {'model': 'shell;bad'}},
            {'options': {'unknown': 'bad'}},
        ]
        for extra in invalid:
            with self.subTest(extra=list(extra)), self.assertRaises(ValueError):
                worker.Request.decode(self.payload(**extra))
        self.assertGreater(codex_rpc.MAX_INPUT_BYTES, (worker.MAX_IMAGE_BYTES + worker.MAX_HISTORY_BYTES) * 4 // 3)

    def test_native_files_context_options(self):
        """Передать копии и manifest, сохранить контекст/options при fallback и не отдавать readonly файлы."""
        request = worker.Request.decode(self.payload(task=True,
            context=[{'role': 'user', 'text': 'previous'}],
            history_files=[{'name': 'stable.txt', 'original_name': 'исходный.txt', 'data': base64.b64encode(b'old data').decode()}],
            options={'model': 'override-model', 'mode': 'review'}))
        seen = []
        def invoke(argv, *, workspace, environment, text, timeout):
            """Проверить временные копии непосредственно во время вызова native worker."""
            seen.append(text)
            self.assertEqual((workspace / 'input/history/stable.txt').read_bytes(), b'old data')
            manifest = json.loads((workspace / 'input/history-manifest.json').read_text())
            self.assertEqual(manifest[0]['original_name'], 'исходный.txt')
            self.assertIn('previous', text)
            self.assertEqual(text.count('current'), 1)
            self.assertIn('read-only', argv)
            self.assertIn('override-model', argv)
            (workspace / 'out/ignored.txt').write_text('cannot publish readonly')
            return subprocess.CompletedProcess(argv, 0, '\n'.join(json.dumps(event) for event in [{'type': 'thread.started', 'thread_id': str(uuid.uuid4())}, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}}, {'type': 'turn.completed'}]), '')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'WORKSHEET_CODEX_PROVIDER': 'local_user', 'WORKSHEET_CODEX_BINARY': '/bin/true'}, clear=True), patch.object(worker, 'run_codex_process', side_effect=invoke):
            result = worker.execute(request, Path(directory))
        self.assertEqual(result['files'], [])
        self.assertEqual(len(seen), 1)

    def test_direct_context_once_and_no_history_copy_in_database(self):
        """Включить gateway-историю один раз и сохранять только текущий ход и ответ модели."""
        request = worker.Request.decode(self.payload(context=[{'role': 'user', 'text': 'UNIQUE_PREVIOUS'}], options={'model': 'override', 'reasoning_effort': 'low', 'mode': 'plan'}))
        responses = []
        def transport(http_request, **kwargs):
            """Перехватить JSON запроса модели и вернуть минимальный успешный ответ."""
            responses.append(json.loads(http_request.data))
            return json.dumps({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}).encode()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'WORKSHEET_CODEX_PROVIDER': 'responses_direct', 'WORKSHEET_CODEX_MODEL': 'default', 'WORKSHEET_CODEX_API_KEY': 'test-only'}, clear=True), patch.object(worker, 'post_model_response', side_effect=transport):
            worker.execute(request, Path(directory))
            with sqlite3.connect(Path(directory) / 'direct-sessions.sqlite3') as db:
                stored = db.execute('SELECT history_json FROM sessions').fetchone()[0]
            self.assertNotIn('UNIQUE_PREVIOUS', stored)
            self.assertIn('current', stored)
        body = responses[0]
        self.assertEqual(body['model'], 'override')
        self.assertEqual(body['reasoning'], {'effort': 'low'})
        self.assertEqual(body['input'][0]['content'][0]['text'].count('UNIQUE_PREVIOUS'), 1)
        self.assertIn('/plan', body['instructions'])
        self.assertEqual(body['tools'], [])
