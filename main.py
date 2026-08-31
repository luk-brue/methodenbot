"""Production entry point for the final Methodenbot."""

import logging
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import traceback
import uuid

from ai_service import AISummaryService
from configuration import Configuration
from control_state import ControlState
import exchangemail
from matrix_commands import MatrixCommandListener, MatrixCommandWorker
import matrixbot
from stats_table_manager import StatsTableManager


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",)


def controller_state_directory(base, user_id, room_id):
    identity = (user_id + '\0' + room_id).encode('utf-8')
    digest = hashlib.sha256(identity).hexdigest()[:32]
    return Path(base) / 'controllers' / digest


def control_bindings_digest(bindings):
    raw = json.dumps(list(bindings), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def clear_control_ready(base):
    path = Path(base) / 'ready.json'
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()):
        raise RuntimeError('Unsicherer Matrix-Bereitschaftsmarker')
    path.unlink()


def write_control_ready(base, bindings):
    directory = Path(base)
    path = directory / 'ready.json'
    data = {'version': 1, 'pid': os.getpid(),
            'controllers_sha256': control_bindings_digest(bindings)}
    raw = (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    temporary = directory / ('.ready.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main():
    os.umask(0o077)
    config = Configuration()
    config.validate_final_runtime()
    Path(config.state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parent)

    state = ControlState(config.control_state_dir, ai_default=config.ai_default_enabled)
    clear_control_ready(config.control_state_dir)
    ai_service = AISummaryService(config.ai)
    if config.ai.api_key or not config.ai.api_key_file:
        raise RuntimeError('Finaler Dienst verlangt eine lokale Credential-Datei, keinen direkten API-Key')
    ai_service.check_available()
    config.control_state = state
    config.ai_service = ai_service

    bot = matrixbot.MatrixBot(config)
    execution_lock = threading.Lock()
    listeners = []
    bindings = config.control_bindings()
    for index, (control_user, room_id) in enumerate(bindings):
        listener_state = (state if index == 0 else ControlState(
            controller_state_directory(config.control_state_dir, control_user, room_id),
            ai_default=state.snapshot()['ai_enabled']))
        listeners.append(MatrixCommandListener(
            bot, config, listener_state, ai_service,
            account_factory=lambda: exchangemail.init_exchange_connection(config),
            room_id=room_id, control_user=control_user, ai_state=state,
            execution_lock=execution_lock))
    # Bootstrap synchronously: old personal messages must be skipped before the
    # normal mail service begins. Security failure stops the whole startup.
    for listener in listeners:
        listener.state.acquire_process_lock()
        listener.bootstrap()
    write_control_ready(config.control_state_dir, bindings)

    account = exchangemail.init_exchange_connection(config)
    stats = StatsTableManager(config.stats_file)
    processed_emails = exchangemail.load_processed_emails(config.processed_file)

    work_event = threading.Event()
    worker = MatrixCommandWorker(listeners, work_event)
    control_threads = [threading.Thread(
        target=listener.run_poll_forever, args=(work_event,),
        name='matrix-control-' + str(index), daemon=True)
        for index, listener in enumerate(listeners, 1)]
    worker_thread = threading.Thread(target=worker.run_forever,
                                     name='matrix-control-worker', daemon=True)
    worker_thread.start()
    for control_thread in control_threads:
        control_thread.start()
    if any(listener.state.head() is not None for listener in listeners):
        work_event.set()
    try:
        logger.info("Lade E-Mails aus der INBOX (max. 100)...")
        messages = list(account.inbox.all().order_by('-datetime_received')[:100])
        logger.info("%d E-Mails aus der INBOX geholt.", len(messages))
        exchangemail.clean_up_processed_file(config.processed_file, messages, processed_emails)
        exchangemail.process_many_emails(messages, config, account, processed_emails, bot, stats)
    except Exception as exc:
        logger.error("Fehler beim ersten Abruf: %s", type(exc).__name__)
        logger.error(traceback.format_exc())
        raise
    try:
        exchangemail.maintain_notification_streaming(
            account, config, processed_emails, stats, bot, 29)
    except Exception as exc:
        logger.error("Fehler beim Notification Streaming: %s", type(exc).__name__)
        logger.error(traceback.format_exc())
        raise
    finally:
        for listener in listeners:
            listener.stop_event.set()
        worker.stop_event.set()
        work_event.set()
        for control_thread in control_threads:
            control_thread.join(timeout=5)
        worker_thread.join(timeout=5)


if __name__ == "__main__":
    main()
