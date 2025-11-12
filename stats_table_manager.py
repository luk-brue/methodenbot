import os
import pandas as pd
import logging

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

    def __init__(self):
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
        self.df.to_csv(self.FILENAME1, index=False)
        logger.info("StatsTableManager: Daten in CSV auf Festplatte gespeichert.")

    def append_record(self, record_dict):
        """Add a new row. Missing fields default to empty string."""
        new_row = {h: record_dict.get(h, "") for h in self.HEADERS}
        self.df = pd.concat([self.df, pd.DataFrame([new_row])], ignore_index=True)
        logger.info(f"StatsTableManager: Daten hinzugefügt von {new_row['sender_name']}.")
        self._save_df()
    