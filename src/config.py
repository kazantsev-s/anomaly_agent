import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    def __init__(self):
        self.tg_token = os.getenv('TG_TOKEN')
        self.postgres_host = os.getenv('POSTGRES_HOST')
        self.postgres_port = int(os.getenv('POSTGRES_PORT', '5432'))
        self.postgres_db = os.getenv('POSTGRES_DB')
        self.postgres_user = os.getenv('POSTGRES_USER')
        self.postgres_password = os.getenv('POSTGRES_PASSWORD')
        self.log_level = os.getenv('LOG_LEVEL')
        self.log_format = os.getenv('LOG_FORMAT')
        self.kolesa_table_sql_path = os.getenv('KOLESA_TABLE_SQL_PATH')
        self.kolesa_csv_path = os.getenv('KOLESA_CSV_PATH')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.openai_model = os.getenv('OPENAI_MODEL', 'gpt-5.5')


def get_settings():
    return Settings()
