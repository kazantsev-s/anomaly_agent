import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    def __init__(self):
        self.tg_token = os.getenv('TG_TOKEN')
        self.postgres_host = os.getenv('POSTGRES_HOST')
        self.postgres_port = int(os.getenv('POSTGRES_PORT'))
        self.postgres_db = os.getenv('POSTGRES_DB')
        self.postgres_user = os.getenv('POSTGRES_USER')
        self.postgres_password = os.getenv('POSTGRES_PASSWORD')
        self.logging_enabled = os.getenv('LOGGING_ENABLED') == 'true'
        self.log_file = os.getenv('LOG_FILE')
        self.log_level = os.getenv('LOG_LEVEL')
        self.log_format = os.getenv('LOG_FORMAT')
        self.kolesa_table_sql_path = os.getenv('KOLESA_TABLE_SQL_PATH')
        self.kolesa_csv_path = os.getenv('KOLESA_CSV_PATH')
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.openai_model = os.getenv('OPENAI_MODEL', 'gpt-5.5')
        self.analyze_default_table = os.getenv('ANALYZE_DEFAULT_TABLE', 'kolesa')
        self.analyze_max_custom_check_iterations = int(os.getenv('ANALYZE_MAX_CUSTOM_CHECK_ITERATIONS'))
        self.analyze_custom_sql_per_iteration_limit = int(os.getenv('ANALYZE_CUSTOM_SQL_PER_ITERATION_LIMIT'))
        self.analyze_custom_sql_total_limit = int(os.getenv('ANALYZE_CUSTOM_SQL_TOTAL_LIMIT'))
        self.analyze_report_findings_limit = int(os.getenv('ANALYZE_REPORT_FINDINGS_LIMIT'))
        self.analyze_report_sample_rows_limit = int(os.getenv('ANALYZE_REPORT_SAMPLE_ROWS_LIMIT'))


def get_settings():
    return Settings()
