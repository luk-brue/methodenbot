import os
from pathlib import Path
import pandas as pd
import logging
import uuid

logger = logging.getLogger(__name__)

def glimpse(df):
    print(f"Rows: {df.shape[0]}")
    print(f"Columns: {df.shape[1]}")
    for col in df.columns:
        print(f"$ {col} {df[col].head().values[:20]}")

class StatsTableManager:
    """
    Manages stats table stored in CSV files.

    - On init, checks for the files and creates them if missing.
    - Provides methods to append new records and update rows.
    
    Parameters:
    - logger: a logging object with .info and .error methods.
    """
    FILENAME1 = 'stats.csv'
    HEADERS = [
        'sender_name',
        'message_id', # email identifier
        'tmid',
        'received_date',
        'fachsemester',
        'art',
        'betreuung',
        'studiengang',
        'fachgebiet',
    ]

    def __init__(self, filename=None):
        self.FILENAME1 = filename or self.FILENAME1
        if not os.path.exists(self.FILENAME1):
            logger.info(f"StatsTableManager: {self.FILENAME1} nicht gefunden. Erstelle neue Datei.")
            pd.DataFrame(columns=self.HEADERS).to_csv(self.FILENAME1, index=False)
        self._load_df()

    def _load_df(self):
        logger.info(f"StatsTableManager: {self.FILENAME1} gefunden.")
        self.df = pd.read_csv(self.FILENAME1, dtype=str)
        logger.info(f"StatsTableManager: {self.FILENAME1} eingelesen.")
        self.df.fillna('', inplace=True)  # treat empty as ""

    def _save_df(self):
        target = Path(self.FILENAME1)
        temporary = target.parent / ('.' + target.name + '.new.' + uuid.uuid4().hex)
        try:
            with temporary.open('x', encoding='utf-8', newline='') as handle:
                self.df.to_csv(handle, index=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        logger.info("StatsTableManager: Daten in CSV auf Festplatte gespeichert.")

    def append_record(self, record_dict):
        """Add a new row. Missing fields default to empty string."""
        new_row = {h: record_dict.get(h, "") for h in self.HEADERS}
        message_id = new_row.get('message_id')
        if message_id and 'message_id' in self.df and (self.df['message_id'] == message_id).any():
            logger.info("StatsTableManager: Datensatz bereits vorhanden.")
            return False
        previous = self.df
        self.df = pd.concat([self.df, pd.DataFrame([new_row])], ignore_index=True)
        logger.info("StatsTableManager: Datensatz hinzugefügt.")
        try:
            self._save_df()
        except Exception:
            self.df = previous
            raise
        return True
