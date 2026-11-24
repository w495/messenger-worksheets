"""Ограниченная память одного чата с независимым контекстом каждого треда."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Callable


class ThreadContextStore:
    """Хранить очищенные сообщения и собственные приватные копии вложений."""

    TURN_BYTES = 16 * 1024
    MESSAGE_BYTES = 64 * 1024
    TURNS = 20
    THREAD_BYTES = 50 * 1024 * 1024
    THREAD_FILES = 32
    GLOBAL_BYTES = 512 * 1024 * 1024
    THREADS = 128
    RECOVERY_ENTRIES = 16384
    MANAGED_DIRECTORY = re.compile(r"thread-(0|[1-9][0-9]*)\Z", re.ASCII)
    MANAGED_FILE = re.compile(r"[0-9]+-[a-f0-9]{24}-[a-f0-9]{12}\Z", re.ASCII)

    def __init__(self, db: sqlite3.Connection, root: Path, sanitize_user: Callable[[str], str]):
        """Открыть SQLite-таблицы и приватный каталог файлов, не следуя симлинкам.

        Параметры: db — принадлежащее одному чату соединение SQLite; root —
        отдельный корень копий gateway; sanitize_user — очистка пользовательского
        текста до записи. Соединение остаётся во владении вызывающего кода.
        Результат: настроенный объект; создаются таблицы и отсутствующие каталоги,
        корень получает права 0700, выполняется ограниченная очистка orphan-копий
        и применяется retention с commit соединения. Gateway обязан уже держать
        exclusive flock: восстановление рассчитано на единственного писателя.
        Ошибки: ValueError для симлинков, sqlite3.Error и OSError при ошибках БД
        или файловой системы. Обход компонентов каталогов использует NOFOLLOW;
        существующие данные других чатов нельзя передавать через тот же db/root."""
        self.db, self.root, self.sanitize_user = db, Path(root).absolute(), sanitize_user
        self._check_ancestors(self.root)
        root_fd = self._ensure_directory(self.root)
        try:
            os.fchmod(root_fd, 0o700)
        finally:
            os.close(root_fd)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS thread_context_resets (thread INTEGER PRIMARY KEY, cutoff INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS thread_context_threads (
                thread INTEGER PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
                task INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0,
                touched INTEGER NOT NULL, cutoff INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS thread_context_messages (
                thread INTEGER NOT NULL, stamp INTEGER NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, task INTEGER NOT NULL, PRIMARY KEY(thread,stamp,role));
            CREATE TABLE IF NOT EXISTS thread_context_files (
                thread INTEGER NOT NULL, stamp INTEGER NOT NULL, name TEXT NOT NULL,
                original_name TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(thread,name));
        ''')
        if "cutoff" not in {r[1] for r in db.execute("PRAGMA table_info(thread_context_threads)")}:
            db.execute("ALTER TABLE thread_context_threads ADD COLUMN cutoff INTEGER NOT NULL DEFAULT 0")
        self._recover_orphans()
        self._retention()

    @staticmethod
    def _ensure_directory(path: Path) -> int:
        """Создать каталог покомпонентно без следования симлинкам даже при подмене пути.

        Параметр path — путь требуемого каталога, преобразуемый в абсолютный.
        Результат: открытый дескриптор конечного каталога; вызывающий обязан
        закрыть его. Недостающие компоненты создаются с правами 0700.
        Ошибки: OSError для симлинка, некаталога или отказа доступа. Каждый
        компонент открывается относительно уже открытого родителя с NOFOLLOW;
        промежуточные дескрипторы закрываются и при исключении."""
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.absolute().parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _open_directory(path: Path) -> int:
        """Открыть абсолютный каталог по компонентам с NOFOLLOW против подмены предков.

        Параметр path — существующий каталог, преобразуемый в абсолютный путь.
        Результат: открытый дескриптор конечного каталога, который обязан закрыть
        вызывающий код. Каталоги и права не изменяются; содержимое не читается.
        Ошибки: OSError для отсутствующего компонента, симлинка, некаталога или
        отказа доступа. Каждый переход использует dir_fd уже открытого родителя;
        при неуспехе все открытые этим методом дескрипторы закрываются."""
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.absolute().parts[1:]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _check_ancestors(path: Path) -> None:
        """Отклонить симлинки во всех существующих компонентах абсолютного пути.

        Параметр path — проверяемый путь вместе со всеми его родителями.
        Результат: None при отсутствии обнаруженных симлинков.
        Ошибки: ValueError при обнаружении симлинка. Выполняет только проверки
        файловых метаданных; не создаёт и не удаляет объектов. Это предварительная
        проверка: окончательная защита открытия обеспечивается NOFOLLOW/dir_fd."""
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError("Symlink path component is forbidden")

    @staticmethod
    def _check_dir(path: Path) -> None:
        """Отклонить каталог, который является симлинком или иным типом объекта.

        Параметр path — путь существующего каталога.
        Результат: None, если lstat подтверждает настоящий каталог.
        Ошибки: ValueError для другого типа, OSError при ошибке lstat.
        Не следует конечному симлинку, не читает содержимое и не меняет права;
        проверка не заменяет безопасное открытие дескриптора."""
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("Context directory must be a real directory")

    @staticmethod
    def _id(thread_id: int) -> int:
        """Проверить целочисленный идентификатор и исключить компоненты пути.

        Параметр thread_id — неотрицательный int, используемый также для stamp.
        Результат: исходное число без преобразования строк или bool.
        Ошибки: ValueError для отрицательных чисел, bool и иных типов.
        Побочные эффекты отсутствуют; проверенный идентификатор применяется
        в параметризованном SQL и в фиксированном имени каталога thread-<число>."""
        if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id < 0:
            raise ValueError("Invalid thread timestamp")
        return thread_id

    def _touch(self, thread_id: int) -> None:
        """Создать метаданные треда и обновить время последнего обращения.

        Параметр thread_id — проверяемый идентификатор треда внутри текущего чата.
        Результат: None; выполняется один SQLite upsert с time.time_ns().
        Ошибки: ValueError для неверного ID, sqlite3.Error при ошибке записи.
        Самостоятельный commit не выполняется; timestamp используется retention
        для выбора старейшего треда и не заменяет stamp сообщений."""
        self._id(thread_id)
        self.db.execute("INSERT INTO thread_context_threads(thread,touched) VALUES (?,?) ON CONFLICT(thread) DO UPDATE SET touched=excluded.touched", (thread_id, time.time_ns()))

    def _directory(self, thread_id: int) -> Path:
        """Вернуть проверенный приватный каталог конкретного треда.

        Параметр thread_id — неотрицательный целочисленный ID.
        Результат: Path внутри root с фиксированным именем thread-<ID>.
        При необходимости создаёт каталог и устанавливает 0700 через дескриптор;
        root и каталог открываются без следования симлинкам.
        Ошибки: ValueError для неверного ID/root, OSError при отказе доступа,
        симлинке или другом типе объекта. Все служебные дескрипторы закрываются."""
        self._check_dir(self.root)
        directory = self.root / f"thread-{self._id(thread_id)}"
        root_fd = self._open_directory(self.root)
        try:
            try:
                os.mkdir(directory.name, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)
        return directory

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """Ограничить текст числом UTF-8 байтов без повреждения символов.

        Параметры: text — исходная строка; limit — неотрицательный байтовый предел.
        Результат: префикс строки, чей UTF-8 размер не превышает limit;
        незавершённый многобайтовый символ на границе отбрасывается целиком.
        Ошибки: UnicodeEncodeError для некорректных суррогатов Python-строки.
        Ввод-вывод не выполняется; исходная строка не изменяется."""
        return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")

    def is_initialized(self, thread_id: int) -> bool:
        """Проверить сохранённый флаг инициализации только указанного треда.

        Параметр thread_id — ID треда в принадлежащем этому объекту чате.
        Результат: bool; True также для сохранённого reset-барьера, даже если
        retention уже удалил тяжёлые метаданные треда. Это запрещает повторный
        bootstrap старой истории после reset.
        Ошибки: ValueError для неверного ID, sqlite3.Error при чтении БД.
        Файлы и записи не изменяются; commit не выполняется."""
        row = self.db.execute("SELECT initialized FROM thread_context_threads WHERE thread=?", (self._id(thread_id),)).fetchone()
        return bool((row and row[0]) or self.db.execute("SELECT 1 FROM thread_context_resets WHERE thread=?", (thread_id,)).fetchone())

    def mark_initialized(self, thread_id: int) -> None:
        """Записать флаг завершённой инициализации треда и применить retention.

        Параметр thread_id — ID треда, чей первоначальный bootstrap завершён.
        Результат: None; создаёт/обновляет метаданные, ставит initialized=1
        и фиксирует изменения через retention.
        Ошибки: ValueError для ID; sqlite3.Error/OSError при записи БД или
        удалении старых копий. Retention может удалить старые данные других
        тредов этого чата, но не сохранённые reset-барьеры."""
        self._touch(thread_id)
        self.db.execute("UPDATE thread_context_threads SET initialized=1 WHERE thread=?", (thread_id,))
        self._retention()

    def cutoff(self, thread_id: int) -> int:
        """Вернуть включительную границу reset, исключающую старую историю при bootstrap.

        Параметр thread_id — ID треда в текущем чате.
        Результат: stamp последнего reset либо 0 при отсутствии барьера.
        Вызывающий bootstrap должен исключить все сообщения с stamp <= cutoff;
        барьер хранится независимо от ограниченных тяжёлых данных треда.
        Ошибки: ValueError для ID, sqlite3.Error при чтении. Записей,
        файловых операций и commit метод не выполняет."""
        row = self.db.execute("SELECT cutoff FROM thread_context_resets WHERE thread=?", (self._id(thread_id),)).fetchone()
        return row[0] if row else 0

    def reset(self, thread_id: int, before: int | None = None) -> None:
        """Удалить контекст треда и сохранить барьер, чтобы bootstrap не вернул старые данные.

        Параметры: thread_id — очищаемый тред; before — включительный stamp
        reset. При None используется максимум прежнего барьера и сохранённых
        stamp сообщений/файлов. Ранее установленный барьер не уменьшается.
        Результат: None; удаляются известные копии и сообщения, сбрасываются
        task/truncated, initialized становится True, изменения фиксируются.
        Ошибки: ValueError для ID/stamp или исчерпания 16384 reset-барьеров;
        sqlite3.Error/OSError при записи/удалении. Барьеры retention не удаляет:
        исчерпание ёмкости останавливает новый reset без обхода защиты истории.
        Файлы удаляются только через привязанный к root дескриптор каталога."""
        self._touch(thread_id)
        if before is None:
            before = max(self.cutoff(thread_id), self.db.execute("SELECT COALESCE(MAX(stamp),0) FROM thread_context_messages WHERE thread=?", (thread_id,)).fetchone()[0], self.db.execute("SELECT COALESCE(MAX(stamp),0) FROM thread_context_files WHERE thread=?", (thread_id,)).fetchone()[0])
        self._id(before)
        if not self.db.execute("SELECT 1 FROM thread_context_resets WHERE thread=?", (thread_id,)).fetchone() and self.db.execute("SELECT COUNT(*) FROM thread_context_resets").fetchone()[0] >= 16384:
            raise ValueError("Reset barrier capacity exhausted")
        self.db.execute("INSERT INTO thread_context_resets VALUES (?,?) ON CONFLICT(thread) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff)", (thread_id, before))
        for (name,) in self.db.execute("SELECT name FROM thread_context_files WHERE thread=?", (thread_id,)).fetchall():
            self._drop_file(thread_id, name)
        self.db.execute("DELETE FROM thread_context_messages WHERE thread=?", (thread_id,))
        self.db.execute("UPDATE thread_context_threads SET initialized=1,task=0,truncated=0,cutoff=MAX(cutoff,?) WHERE thread=?", (before, thread_id))
        self._retention()

    def remember(self, thread_id: int, stamp: int, role: str, text: str, *, task: bool = False) -> None:
        """Сохранить очищенный user либо текст assistant в пределах лимитов.

        Параметры: thread_id — тред; stamp — timestamp входящего сообщения;
        role — user/assistant; text — текст; task — явный признак файлового хода.
        Результат: None. Записи до reset-барьера и пустой после очистки текст
        пропускаются. User проходит sanitize_user до записи; assistant сохраняет
        HTML/код без фильтрации, но оба текста ограничиваются 16 KiB UTF-8.
        Закрепляется root user (stamp == thread_id), сохраняются до 20 последних
        остальных сообщений в общем бюджете 64 KiB. Флаг task сохраняет ранний
        stamp до reset; потеря текста отмечается truncated. Применяется retention
        и commit соединения. Внутри одинакового stamp роли хранятся отдельно.
        Ошибки: ValueError для ID/роли; ошибки sanitize_user, кодирования UTF-8,
        SQLite или retention передаются вызывающему. Содержимое assistant не
        нормализуется и пути внутри него произвольно не переписываются."""
        self._id(thread_id)
        self._id(stamp)
        if stamp <= self.cutoff(thread_id):
            return
        if role not in ("user", "assistant"):
            raise ValueError("Invalid context role")
        if role == "user":
            text = self.sanitize_user(text)
        if not text.strip():
            return
        self._touch(thread_id)
        clipped = self._clip(text, self.TURN_BYTES)
        self.db.execute("INSERT OR REPLACE INTO thread_context_messages VALUES (?,?,?,?,?)", (thread_id, stamp, role, clipped, int(task)))
        if task:
            self.db.execute("UPDATE thread_context_threads SET task=CASE WHEN task=0 THEN ? ELSE MIN(task,?) END WHERE thread=?", (stamp, stamp, thread_id))
        if clipped != text:
            self.db.execute("UPDATE thread_context_threads SET truncated=1 WHERE thread=?", (thread_id,))
        rows = self.db.execute("SELECT stamp,role,text FROM thread_context_messages WHERE thread=? ORDER BY stamp DESC,role DESC", (thread_id,)).fetchall()
        root = next((r for r in rows if r[0] == thread_id and r[1] == "user"), None)
        chosen = ([root] if root else []) + [r for r in rows if r != root][:self.TURNS]
        budget = self.MESSAGE_BYTES
        keep = set()
        for timestamp, sender, body in chosen:
            size = len(body.encode("utf-8"))
            if size <= budget:
                keep.add((timestamp, sender))
                budget -= size
        for timestamp, sender, _ in rows:
            if (timestamp, sender) not in keep:
                self.db.execute("DELETE FROM thread_context_messages WHERE thread=? AND stamp=? AND role=?", (thread_id, timestamp, sender))
                self.db.execute("UPDATE thread_context_threads SET truncated=1 WHERE thread=?", (thread_id,))
        self._retention()

    @staticmethod
    def _read_file(fd: int, limit: int) -> tuple[int, str]:
        """Посчитать размер и SHA-256 файла с жёстким пределом чтения.

        Параметры: fd — уже безопасно открытый и проверенный дескриптор;
        limit — максимальное разрешённое число байтов от текущей позиции.
        Результат: пара (прочитанный размер, SHA-256 в hex) при достижении EOF.
        Ошибки: ValueError, если прочитано больше limit, OSError при сбое чтения.
        Сдвигает позицию fd и читает блоками до 1 MiB; тип/число ссылок проверяет
        вызывающий. Байты не интерпретируются, не исполняются и не журналируются."""
        size, digest = 0, hashlib.sha256()
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit - size + 1))
            if not chunk:
                return size, digest.hexdigest()
            size += len(chunk)
            if size > limit:
                raise ValueError("Context file exceeds size limit")
            digest.update(chunk)

    def remember_files(self, thread_id: int, stamp: int, paths: tuple[Path, ...]) -> None:
        """Скопировать обычные односсылочные файлы с проверкой inode, размера и хеша.

        Параметры: thread_id — тред; stamp — timestamp исходного хода;
        paths — кортеж существующих путей к результатам gateway/worker.
        Результат: None; создаются собственные копии 0600 внутри thread-<ID>,
        сохраняются исходное basename, размер и SHA-256. Стабильное безопасное
        имя зависит от stamp, содержимого и basename. Повтор известной записи
        не создаёт копию; source после успешной записи больше не нужен.
        Данные до reset пропускаются. Содержимое читается как bytes дважды для
        проверки hash, проверяются inode/размер/времена/nlink до и после копии,
        затем выполняется fsync. Код и форматы файлов не разбираются.
        Ошибки: ValueError для traversal, симлинков, небезопасного типа/nlink,
        размера или изменения источника; OSError/SQLite-ошибки передаются.
        Незавершённая собственная копия удаляется при обычном исключении;
        уже записанные предыдущие файлы остаются, retention/commit выполняется
        в finally. Метод не является атомарной транзакцией всего кортежа."""
        self._id(thread_id)
        self._id(stamp)
        self._touch(thread_id)
        if stamp <= self.cutoff(thread_id):
            return
        directory = self._directory(thread_id)
        dir_fd = self._open_directory(directory)
        try:
            for source in paths:
                source = Path(source)
                if ".." in source.parts:
                    raise ValueError("Parent traversal is forbidden")
                self._check_ancestors(source.absolute())
                parent_fd = self._open_directory(source.absolute().parent)
                try:
                    source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                destination_fd = None
                name = None
                try:
                    before = os.fstat(source_fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > self.THREAD_BYTES:
                        raise ValueError("Unsafe context source file")
                    size, digest = self._read_file(source_fd, self.THREAD_BYTES)
                    name = f"{stamp}-{digest[:24]}-{hashlib.sha256(source.name.encode()).hexdigest()[:12]}"
                    existing = self.db.execute("SELECT 1 FROM thread_context_files WHERE thread=? AND name=?", (thread_id, name)).fetchone()
                    if existing:
                        continue
                    destination_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    copied, check = 0, hashlib.sha256()
                    while True:
                        chunk = os.read(source_fd, min(1024 * 1024, self.THREAD_BYTES - copied + 1))
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > self.THREAD_BYTES:
                            raise ValueError("Context source grew during copying")
                        check.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(destination_fd, view):]
                    after = os.fstat(source_fd)
                    current = source.lstat()
                    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
                    if identity(before) != identity(after) or identity(after) != identity(current) or copied != size or check.hexdigest() != digest:
                        raise ValueError("Context source changed during copying")
                    os.fsync(destination_fd)
                    self.db.execute("INSERT INTO thread_context_files VALUES (?,?,?,?,?,?)", (thread_id, stamp, name, source.name, size, digest))
                    self.db.execute("UPDATE thread_context_threads SET task=CASE WHEN task=0 THEN ? ELSE MIN(task,?) END WHERE thread=?", (stamp, stamp, thread_id))
                    name = None
                finally:
                    os.close(source_fd)
                    if destination_fd is not None:
                        os.close(destination_fd)
                    if name is not None:
                        # Только файл, созданный этим вызовом, может быть удалён.
                        if destination_fd is not None:
                            os.unlink(name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
            self._retention()

    def _drop_file(self, thread_id: int, name: str) -> None:
        """Удалить одну известную копию внутри корня и соответствующую запись SQLite.

        Параметры: thread_id — владеющий тред; name — единственный basename
        из метаданных хранилища, без компонентов пути.
        Результат: None; unlink выполняется относительно NOFOLLOW-дескриптора
        thread-каталога, отсутствующая копия допустима. SQLite-запись удаляется,
        truncated треда становится True; самостоятельного commit нет.
        Ошибки: ValueError для имени/ID/root, OSError и sqlite3.Error при сбое.
        Конечный симлинк удаляется как ссылка, его цель не открывается;
        каталоги вне root и исходные worker-файлы метод не удаляет."""
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid stored filename")
        self._check_dir(self.root)
        directory = self.root / f"thread-{self._id(thread_id)}"
        try:
            fd = self._open_directory(directory)
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)
        except FileNotFoundError:
            pass
        self.db.execute("DELETE FROM thread_context_files WHERE thread=? AND name=?", (thread_id, name))
        self.db.execute("UPDATE thread_context_threads SET truncated=1 WHERE thread=?", (thread_id,))

    def _recover_orphans(self) -> None:
        """Удалить распознаваемые незарегистрированные копии после аварии gateway.

        Параметров нет. Результат: None; выполняется только при открытии store,
        когда вызывающий gateway уже держит свой exclusive flock. Обход имеет
        два уровня root/thread-<integer> и общий предел RECOVERY_ENTRIES записей;
        превышение предела вызывает ValueError, не разрешая дальнейшую запись.
        Проверяются владелец каталога/файла, режимы 0700/0600, regular/nlink=1
        и точная грамматика имён, сгенерированных remember_files. Удаляются
        только отсутствующие в SQLite копии; известные копии, неизвестные имена,
        симлинки и hardlinks не открываются для чтения и не удаляются.
        Каталоги открываются с NOFOLLOW относительно проверенного root_fd;
        перед unlink повторно сверяется inode. Источники worker не затрагиваются.
        Ошибки: ValueError при чрезмерном числе записей; OSError/SQLite-ошибки
        передаются вызывающему. Содержимое файлов не читается и не исполняется;
        commit не выполняется, пустые распознанные каталоги могут быть удалены.
        """
        remaining = self.RECOVERY_ENTRIES
        root_fd = self._open_directory(self.root)
        try:
            with os.scandir(root_fd) as directories:
                for entry in directories:
                    remaining -= 1
                    if remaining < 0:
                        raise ValueError("Context recovery entry budget exhausted")
                    match = self.MANAGED_DIRECTORY.fullmatch(entry.name)
                    if match is None:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        continue
                    thread = int(match[1])
                    if thread > 2**63 - 1:
                        continue
                    directory_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    try:
                        opened = os.fstat(directory_fd)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise ValueError("Context directory changed during recovery")
                        with os.scandir(directory_fd) as files:
                            for item in files:
                                remaining -= 1
                                if remaining < 0:
                                    raise ValueError("Context recovery entry budget exhausted")
                                if self.MANAGED_FILE.fullmatch(item.name) is None:
                                    continue
                                if self.db.execute("SELECT 1 FROM thread_context_files WHERE thread=? AND name=?", (thread, item.name)).fetchone():
                                    continue
                                before = item.stat(follow_symlinks=False)
                                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                                        or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
                                    continue
                                orphan_fd = os.open(item.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                                try:
                                    opened_file = os.fstat(orphan_fd)
                                    current = os.stat(item.name, dir_fd=directory_fd, follow_symlinks=False)
                                    identity = lambda value: (value.st_dev, value.st_ino, value.st_nlink, value.st_mode, value.st_uid)
                                    if identity(opened_file) != identity(before) or identity(current) != identity(opened_file):
                                        raise ValueError("Context orphan changed during recovery")
                                    os.unlink(item.name, dir_fd=directory_fd)
                                finally:
                                    os.close(orphan_fd)
                    finally:
                        os.close(directory_fd)
                    if not self.db.execute("SELECT 1 FROM thread_context_threads WHERE thread=?", (thread,)).fetchone():
                        try:
                            os.rmdir(entry.name, dir_fd=root_fd)
                        except OSError:
                            # Неизвестное содержимое и непустые каталоги сохраняются.
                            pass
        finally:
            os.close(root_fd)

    def _retention(self) -> None:
        """Ограничить число тредов, файлов и байтов, удаляя самые старые записи.

        Параметров нет; используются лимиты класса и текущее SQLite-соединение.
        Результат: None; остаются до 128 недавно использованных тяжёлых тредов,
        до 32 файлов/50 MiB на тред и до 512 MiB зарегистрированных копий всего.
        Треды сортируются по touched, файлы — по stamp с устойчивым разрешением
        совпадений. Вместе с вытесненным тредом удаляются messages и file metadata;
        reset-барьеры сохраняются отдельно. Завершает commit соединения.
        Ошибки: sqlite3.Error, ValueError и OSError удаления копий передаются;
        невозможность удалить пустой каталог не блокирует очистку метаданных.
        Удаляются только известные SQLite файлы, рекурсивного обхода root нет."""
        for (thread,) in self.db.execute("SELECT thread FROM thread_context_threads ORDER BY touched DESC,thread DESC LIMIT -1 OFFSET ?", (self.THREADS,)).fetchall():
            for (name,) in self.db.execute("SELECT name FROM thread_context_files WHERE thread=?", (thread,)).fetchall():
                self._drop_file(thread, name)
            self.db.execute("DELETE FROM thread_context_messages WHERE thread=?", (thread,))
            self.db.execute("DELETE FROM thread_context_threads WHERE thread=?", (thread,))
            try:
                root_fd = self._open_directory(self.root)
                try:
                    os.rmdir(f"thread-{thread}", dir_fd=root_fd)
                finally:
                    os.close(root_fd)
            except (FileNotFoundError, OSError):
                pass
        for (thread,) in self.db.execute("SELECT thread FROM thread_context_threads").fetchall():
            rows = self.db.execute("SELECT name,size FROM thread_context_files WHERE thread=? ORDER BY stamp DESC,name DESC", (thread,)).fetchall()
            total = 0
            for index, (name, size) in enumerate(rows):
                total += size
                if index >= self.THREAD_FILES or total > self.THREAD_BYTES:
                    self._drop_file(thread, name)
        total = self.db.execute("SELECT COALESCE(SUM(size),0) FROM thread_context_files").fetchone()[0]
        for thread, name, size in self.db.execute("SELECT thread,name,size FROM thread_context_files ORDER BY stamp,thread,name").fetchall():
            if total <= self.GLOBAL_BYTES:
                break
            self._drop_file(thread, name)
            total -= size
        self.db.commit()

    def snapshot(self, thread_id: int, before: int) -> dict:
        """Вернуть только более ранний контекст треда и проверенные собственные файлы.

        Параметры: thread_id — изолированный ID; before — исключительная
        верхняя граница stamp. Все messages и files удовлетворяют stamp < before;
        user идёт перед assistant при одинаковом stamp.
        Результат: dict с messages=[{role,text}], files=[{name,path,original_name}],
        task и truncated. Task учитывает ранний явный файловый ход, даже если его
        сообщение удалено retention; truncated описывает потерю контекста треда.
        Path ведёт только к собственной gateway-копии и не передаётся worker:
        транспорт должен отправлять имя/bytes и безопасно открыть путь заново.
        Ошибки: ValueError для ID или нарушения size/hash/regular/nlink проверки;
        OSError при отсутствующей/небезопасной копии, sqlite3.Error при чтении.
        Файлы читаются блоками для SHA-256 через NOFOLLOW и не интерпретируются;
        записи, флаги, доступы и commit не изменяются."""
        self._id(thread_id)
        self._id(before)
        row = self.db.execute("SELECT truncated,task FROM thread_context_threads WHERE thread=?", (thread_id,)).fetchone()
        rows = self.db.execute("SELECT role,text,task FROM thread_context_messages WHERE thread=? AND stamp<? ORDER BY stamp,role DESC", (thread_id, before)).fetchall()
        files = []
        for name, original, size, digest in self.db.execute("SELECT name,original_name,size,digest FROM thread_context_files WHERE thread=? AND stamp<? ORDER BY stamp,name", (thread_id, before)).fetchall():
            path = self.root / f"thread-{thread_id}" / name
            directory_fd = self._open_directory(path.parent)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size or self._read_file(fd, self.THREAD_BYTES) != (size, digest):
                        raise ValueError("Context copy failed integrity check")
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
            files.append({"name": name, "path": str(path), "original_name": original})
        return {"messages": [{"role": role, "text": body} for role, body, _ in rows], "files": files, "task": bool(files or (row and 0 < row[1] < before) or any(task for _, _, task in rows)), "truncated": bool(row and row[0])}
