"""Проверки приватной, ограниченной памяти тредов без сетевых сервисов."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from messenger_worksheets.thread_context import ThreadContextStore


class ThreadContextStoreTest(unittest.TestCase):
    """Проверить сохранность, границы истории и безопасное владение файлами."""

    def setUp(self):
        """Создать временную SQLite и приватный корень контекста."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = sqlite3.connect(self.base / 'state.sqlite')
        self.addCleanup(lambda: self.db.close())
        self.store = ThreadContextStore(self.db, self.base / 'context', lambda t: '\n'.join(x for x in t.splitlines() if not x.startswith('#')))

    def test_reopen_isolation_cutoff_and_roles(self):
        """Сохранить состояние, разделить треды и исключить текущий stamp с правильным порядком ролей."""
        self.store.mark_initialized(10)
        self.store.remember(10, 10, 'user', '# secret\nroot')
        self.store.remember(10, 10, 'assistant', '<!-- code -->\n```html\n# keep\n```')
        self.store.remember(20, 10, 'user', 'other')
        self.store.remember(10, 11, 'user', 'current', task=True)
        self.db.close()
        self.db = sqlite3.connect(self.base / 'state.sqlite')
        self.store = ThreadContextStore(self.db, self.base / 'context', str)
        self.assertTrue(self.store.is_initialized(10))
        snap = self.store.snapshot(10, 11)
        self.assertEqual([m['role'] for m in snap['messages']], ['user', 'assistant'])
        self.assertEqual(snap['messages'][0]['text'], 'root')
        self.assertIn('<!-- code -->', snap['messages'][1]['text'])
        self.assertFalse(snap['task'])
        self.assertEqual(self.store.snapshot(20, 11)['messages'][0]['text'], 'other')

    def test_comments_pinned_root_and_bytes(self):
        """Не хранить комментарии, закрепить root и ограничить UTF-8 без повреждения символов."""
        self.store.remember(1, 1, 'user', 'root')
        self.store.remember(1, 2, 'user', '# private', task=True)
        self.assertFalse(self.store.snapshot(1, 3)['task'])
        for stamp in range(3, 40):
            self.store.remember(1, stamp, 'assistant', 'Ж' * 10000)
        snap = self.store.snapshot(1, 100)
        self.assertEqual(snap['messages'][0]['text'], 'root')
        self.assertTrue(snap['truncated'])
        self.assertLessEqual(len(snap['messages']), 21)
        self.assertLessEqual(sum(len(m['text'].encode()) for m in snap['messages']), 65536)
        self.assertTrue(all(len(m['text'].encode()) <= 16384 for m in snap['messages']))

    def test_copy_survives_unlink_and_names_are_isolated(self):
        """Скопировать одноимённые файлы, пережить удаление источника и исключить текущие файлы."""
        sources = []
        for index in (1, 2):
            folder = self.base / str(index)
            folder.mkdir()
            path = folder / 'same.txt'
            path.write_bytes(str(index).encode())
            sources.append(path)
        self.store.remember_files(10, 11, tuple(sources))
        self.store.remember_files(20, 11, (sources[0],))
        for path in sources:
            path.unlink()
        self.assertFalse(self.store.snapshot(10, 11)['files'])
        files = self.store.snapshot(10, 12)['files']
        self.assertEqual(len(files), 2)
        self.assertEqual({Path(f['path']).read_bytes() for f in files}, {b'1', b'2'})
        self.assertTrue(all(f['original_name'] == 'same.txt' for f in files))
        self.assertTrue(all(len(f['name'].encode()) <= 200 for f in files))
        self.assertTrue(all(Path(f['path']).stat().st_mode & 0o777 == 0o600 for f in files))
        self.assertNotEqual(files[0]['path'], self.store.snapshot(20, 12)['files'][0]['path'])

    def test_file_and_thread_budgets(self):
        """Удалять старые копии и треды при малых тестовых квотах и сохранять bounded SQLite."""
        self.store.THREAD_BYTES = 8
        self.store.THREAD_FILES = 2
        self.store.GLOBAL_BYTES = 10
        self.store.THREADS = 2
        source = self.base / 'data'
        for stamp in (11, 12, 13):
            source.write_bytes(bytes([stamp]) * 4)
            self.store.remember_files(10, stamp, (source,))
        self.assertEqual(len(self.store.snapshot(10, 14)['files']), 2)
        for thread in (20, 30):
            self.store.mark_initialized(thread)
            source.write_bytes(b'abcd')
            self.store.remember_files(thread, 40, (source,))
        self.assertFalse(self.store.is_initialized(10))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM thread_context_threads').fetchone()[0], 2)
        self.assertLessEqual(self.db.execute('SELECT SUM(size) FROM thread_context_files').fetchone()[0], 10)

    def test_unsafe_paths_and_tampered_copies_are_rejected(self):
        """Отклонить симлинки, traversal, hardlink и изменённые копии без удаления внешних файлов."""
        source = self.base / 'safe'
        source.write_bytes(b'data')
        link = self.base / 'link'
        link.symlink_to(source)
        for path in (link, self.base / 'x' / '..' / 'safe'):
            with self.assertRaises((ValueError, OSError)):
                self.store.remember_files(1, 2, (path,))
        hard = self.base / 'hard'
        os.link(source, hard)
        with self.assertRaises(ValueError):
            self.store.remember_files(1, 2, (hard,))
        hard.unlink()
        self.store.remember_files(1, 2, (source,))
        copy = Path(self.store.snapshot(1, 3)['files'][0]['path'])
        copy.write_bytes(b'evil')
        with self.assertRaises(ValueError):
            self.store.snapshot(1, 3)
        self.assertEqual(source.read_bytes(), b'data')
        with self.assertRaises(ValueError):
            self.store.remember('../outside', 2, 'user', 'bad')

    def test_reset_barrier_survives_reopen(self):
        """Очистить сообщения и файлы, удержать барьер и принять только новые записи."""
        source = self.base / 'file'
        source.write_bytes(b'data')
        self.store.remember(10, 10, 'user', 'old', task=True)
        self.store.remember_files(10, 11, (source,))
        self.store.reset(10, 15)
        self.store.remember(10, 14, 'user', 'resurrect')
        self.store.remember_files(10, 14, (source,))
        self.assertEqual(self.store.cutoff(10), 15)
        self.assertEqual(self.store.snapshot(10, 20), {'messages': [], 'files': [], 'task': False, 'truncated': False})
        self.assertTrue(self.store.is_initialized(10))
        self.store.remember(10, 16, 'user', 'new')
        self.assertEqual(self.store.snapshot(10, 20)['messages'], [{'role': 'user', 'text': 'new'}])

    def test_reset_barrier_survives_thread_retention(self):
        """Не восстанавливать старую историю после вытеснения тяжёлых данных треда."""
        self.store.THREADS = 1
        self.store.reset(10, 100)
        self.store.mark_initialized(20)
        self.assertEqual(self.store.cutoff(10), 100)
        self.assertTrue(self.store.is_initialized(10))
        self.store.remember(10, 99, 'user', 'old')
        self.assertEqual(self.store.snapshot(10, 101)['messages'], [])

    def test_task_flag_survives_message_trimming(self):
        """Сохранить task-режим после удаления самого старого task-сообщения."""
        self.store.remember(1, 2, 'user', 'task', task=True)
        for stamp in range(3, 40):
            self.store.remember(1, stamp, 'assistant', 'reply')
        self.assertTrue(self.store.snapshot(1, 50)['task'])
        self.assertFalse(self.store.snapshot(1, 2)['task'])

    def test_recovery_removes_orphans_and_allows_stable_name_retry(self):
        """После имитации аварии удалить копии без SQLite и повторить запись в пределах квот."""
        self.store.THREAD_BYTES = 8
        self.store.GLOBAL_BYTES = 8
        source = self.base / 'result.txt'
        source.write_bytes(b'data')
        self.store.remember_files(10, 11, (source,))
        original = self.store.snapshot(10, 12)['files'][0]
        copy = Path(original['path'])
        self.db.execute('DELETE FROM thread_context_files')
        self.db.commit()
        # Частичная копия второго файла также могла остаться до INSERT.
        partial = copy.parent / ('12-' + 'a' * 24 + '-' + 'b' * 12)
        partial.write_bytes(b'partial content exceeding quota')
        partial.chmod(0o600)
        self.assertGreater(sum(p.stat().st_size for p in copy.parent.iterdir()), 8)
        self.store = ThreadContextStore(self.db, self.base / 'context', str)
        self.store.THREAD_BYTES = 8
        self.store.GLOBAL_BYTES = 8
        self.assertFalse(copy.exists())
        self.assertFalse(partial.exists())
        self.store.remember_files(10, 11, (source,))
        retried = self.store.snapshot(10, 12)['files'][0]
        self.assertEqual(retried['name'], original['name'])
        self.assertEqual(Path(retried['path']).read_bytes(), b'data')
        self.assertLessEqual(sum(p.stat().st_size for p in copy.parent.iterdir()), 8)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM thread_context_files').fetchone()[0], 1)

    def test_recovery_preserves_unknown_files_symlinks_and_hardlinks(self):
        """Не удалять неизвестные объекты и не следовать ссылкам при восстановлении."""
        directory = self.base / 'context/thread-10'
        directory.mkdir(mode=0o700)
        outside = self.base / 'outside'
        outside.write_bytes(b'must survive')
        unknown = directory / 'notes.txt'
        unknown.write_bytes(b'unknown')
        symlink = directory / ('11-' + 'a' * 24 + '-' + 'b' * 12)
        symlink.symlink_to(outside)
        hardlink = directory / ('12-' + 'c' * 24 + '-' + 'd' * 12)
        os.link(outside, hardlink)
        directory_link = self.base / 'context/thread-20'
        directory_link.symlink_to(self.base, target_is_directory=True)
        self.store = ThreadContextStore(self.db, self.base / 'context', str)
        self.assertEqual(outside.read_bytes(), b'must survive')
        self.assertEqual(unknown.read_bytes(), b'unknown')
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(directory_link.is_symlink())
        self.assertTrue(hardlink.exists())

    def test_recovery_fails_closed_on_excessive_entry_count(self):
        """Остановить ограниченный обход, не удаляя неизвестные записи и внешние данные."""
        for name in ('unknown-one', 'unknown-two'):
            (self.base / 'context' / name).write_bytes(b'preserve')
        class SmallRecoveryStore(ThreadContextStore):
            """Ограничить восстановление одной записью для проверки предела обхода."""
            RECOVERY_ENTRIES = 1
        with self.assertRaisesRegex(ValueError, 'entry budget'):
            SmallRecoveryStore(self.db, self.base / 'context', str)
        self.assertEqual((self.base / 'context/unknown-one').read_bytes(), b'preserve')
        self.assertEqual((self.base / 'context/unknown-two').read_bytes(), b'preserve')
