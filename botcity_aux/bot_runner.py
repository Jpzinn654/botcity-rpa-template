import time
import warnings
from typing import List, Optional, Tuple

import GPUtil
import psutil
from botcity.maestro import (AutomationTaskFinishStatus, BotExecution,
                             BotMaestroSDK, ServerMessage)
from loguru import logger
from urllib3.exceptions import InsecureRequestWarning

from src.main import main

from .logger_config import LoggerConfig
from .sharepoint_config import SharePointApi
from .sql.sql_database_connector import SQLDatabaseConnector
from .telegram_plugin import TelegramBot


class BotRunnerMaestro:
    def __init__(
        self,
        bot_name: str,
        dev: str,
        sector: str,
        stakeholder: str,
        recurrence: str,
        log_folder: str,
        bot_maestro_sdk_raise: bool = False,
        log_dir: str = "logs",
        use_telegram: bool = False,
        telegram_group: Optional[str] = None,
        max_retries: int = 0,
    ) -> None:
        """
        Initializes the BotRunnerMaestro with the provided configuration.

        Args:
            bot_name (str): The name of the bot, used for logging and notifications.
            bot_maestro_sdk_raise (bool, optional): Whether to raise exceptions when
                BotMaestroSDK encounters connection issues (default: False).
            log_dir (str, optional): Directory where log files will be stored (default: "logs").
            use_telegram (bool, optional): Whether to enable Telegram integration (default: False).
            telegram_group (Optional[str]): The Telegram group name or ID for sending notifications.

        Raises:
            ValueError: If 'telegram_group' is not provided.
        """
        # initial config
        self.bot_name: str = bot_name
        self.logger: LoggerConfig = LoggerConfig(bot_name, log_dir)

        # maestro config
        self.bot_maestro_sdk_raise: bool = bot_maestro_sdk_raise
        self.maestro, self.execution = self._setup_maestro()

        # Save in prod database
        self.dev: str = dev
        self.sector: str = sector
        self.stakeholder: str = stakeholder
        self.recurrence: str = recurrence
        self.max_retries = max_retries

        # Sharepoint credentials
        site_url_sharepoint = (
            self.maestro.get_credential(
                label="Your_Sharepoint_Credentials", key="site_url"
            )
            + "YourGroup"
        )
        username_sharepoint = self.maestro.get_credential(
            label="Your_Sharepoint_Credentials", key="username"
        )
        password_sharepoint = self.maestro.get_credential(
            label="Your_Sharepoint_Credentials", key="password"
        )
        self.sharepoint = SharePointApi(
            site_url_sharepoint,
            username_sharepoint,
            password_sharepoint,
            log_folder,
            self.bot_name,
        )

        # telegram config
        self.use_telegram: bool = use_telegram
        self.telegram_bot: Optional[TelegramBot] = None

        if self.use_telegram:
            if not telegram_group:
                raise ValueError(
                    "Telegram group must be provided when use_telegram is True."
                )
            self.telegram_group: str = telegram_group
            self.telegram_token: str = self._get_telegram_token()
            self.telegram_bot = TelegramBot(token=self.telegram_token)

        # time config
        self.start_time: Optional[float] = None

    def _setup_maestro(self) -> Tuple[BotMaestroSDK, BotExecution]:
        """
        Sets up the BotMaestroSDK and retrieves the current task execution.

        Returns:
            Tuple[BotMaestroSDK, BotExecution]:
                - BotMaestroSDK: Instance configured with system arguments.
                - BotExecution: Current task execution details.

        Raises:
            Exception: If the SDK initialization or execution retrieval fails.
        """
        try:
            # Set up the BotMaestroSDK with custom configuration
            BotMaestroSDK.RAISE_NOT_CONNECTED = self.bot_maestro_sdk_raise
            maestro = BotMaestroSDK.from_sys_args()
            execution: BotExecution = maestro.get_execution()

            # Log the task details
            logger.info(f"Task ID is: {execution.task_id}")
            logger.info(f"Task Parameters are: {execution.parameters}")

            return maestro, execution
        except Exception as e:
            logger.error(f"Failed to initialize BotMaestroSDK: {e}")
            raise e

    def _get_telegram_token(self) -> Optional[str]:
        """
        Retrieves the Telegram bot token from the BotMaestro server.

        Returns:
            Optional[str]: The Telegram bot token, or None if `use_telegram` is False.

        Raises:
            ValueError: If the token is empty or not provided in the Maestro credentials.
            Exception: If an error occurs during token retrieval.
        """
        if not self.use_telegram:
            return None
        try:
            token = self.maestro.get_credential(label="Telegram", key="token")
            if not token or token == "":
                raise ValueError(
                    "Telegram Token must be provided in Maestro credentials"
                )
            logger.info("Telegram token retrieved successfully.")
            return token
        except Exception as e:
            logger.error(f"Failed to retrieve telegram token: {e}")
            raise e

    def _add_log_file_into_maestro(self) -> ServerMessage:
        """
        Uploads the bot's log file as an artifact to the BotMaestro server.

        Returns:
            ServerMessage: Response message from the BotMaestro server.

        Raises:
            Exception: If the file upload fails.
        """
        try:
            response: ServerMessage = self.maestro.post_artifact(
                task_id=self.execution.task_id,
                artifact_name=self.logger.log_filename,
                filepath=self.logger.log_path,
            )
            logger.info(
                f"Log file '{self.logger.log_filename}' uploaded successfully to BotCity Maestro."
            )
            return response
        except Exception as e:
            logger.error(
                f"Failed to upload log file '{self.logger.log_filename}' into BotCity Maestro: {e}"
            )
            raise e

    def _get_execution_time(self) -> str:
        """
        Computes the execution duration since the bot's start time.

        Returns:
            str: Execution time formatted as 'DD:HH:MM:SS'.
        """
        if self.start_time is None:
            return "Execution time not available"

        end_time = time.time()
        elapsed_seconds = int(end_time - self.start_time)

        days, remainder = divmod(elapsed_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        execution_time = f"{days:02}:{hours:02}:{minutes:02}:{seconds:02}"
        return execution_time

    def _get_resource_usage(self) -> str:
        """
        Retrieves current resource usage (CPU, RAM, and GPU).

        Returns:
            str: Formatted string with CPU, RAM, and GPU usage.
        """
        # CPU and RAM usage
        cpu_percent = psutil.cpu_percent(interval=1)
        ram_usage = psutil.virtual_memory()
        ram_percent = ram_usage.percent
        ram_used_mb = ram_usage.used / (1024 * 1024)

        # GPU usage (if GPU is available)
        gpu_stats = []
        gpus: List = GPUtil.getGPUs()
        if gpus:
            for gpu in gpus:
                gpu_stats.append(
                    f"GPU {gpu.id}: {gpu.name}, Load: {gpu.load * 100:.1f}%, "
                    f"Memory: {gpu.memoryUsed}MB/{gpu.memoryTotal}MB"
                )
            gpu_usage_str = "; ".join(gpu_stats)
        else:
            gpu_usage_str = "No GPU found."

        # Format the usage information
        usage_info = f"CPU Usage: {cpu_percent}%, RAM Usage: {ram_percent}% ({ram_used_mb:.1f} MB), GPU Usage: {gpu_usage_str}"
        return usage_info

    def _insert_database_log_execution(self):
        """
        Inserts an execution log entry into the automation logs database.

        This function retrieves the necessary SQL credentials from BotMaestro, establishes a
        connection with the production SQL database, and inserts a log record with information
        about the bot execution (such as bot name, developer, sector, stakeholder, recurrence, and execution time).

        Raises:
            Exception: If there is an error connecting to the database or executing the query.
        """
        time = self._get_execution_time()

        sql_server = self.maestro.get_credential(
            label="Your_SQL_Credentials", key="your_sql_server"
        )
        sql_database = self.maestro.get_credential(
            label="Your_SQL_Credentials", key="your_sql_database"
        )
        sql_username = self.maestro.get_credential(
            label="Your_SQL_Credentials", key="your_sql_username"
        )
        sql_password = self.maestro.get_credential(
            label="Your_SQL_Credentials", key="your_sql_key"
        )

        sql_connector = SQLDatabaseConnector(
            server=sql_server,
            database=sql_database,
            use_windows_auth=False,
            username=sql_username,
            password=sql_password,
        )

        sql_connector.connect()

        params = (
            self.bot_name,
            self.dev,
            self.sector,
            self.stakeholder,
            self.recurrence,
            time,
        )

        query = r"botcity_aux\sql\query\insert_log.sql"

        sql_connector.execute_query_from_file(query, params)

        sql_connector.disconnect()

    def _execute_bot_task(self) -> None:
        """
        Executes the main bot task logic.

        Note:
            This method should be extended with the actual task logic for your bot.
        """
        main()

    def run(self) -> None:
        """
        Starts the bot execution process with retry logic and logs the results.

        This method attempts to execute the bot task up to the maximum number of retries
        defined by `self.max_retries`. For each attempt, it logs the start time, execution time,
        resource usage, and final status.

        Logs:
            - Execution start and completion per attempt.
            - Execution time and system resource usage.
            - Any errors encountered during execution.

        Raises:
            Exception: If the bot fails to execute successfully after all retry attempts.
        """
        attempts = 0
        while attempts <= self.max_retries:
            try:
                self.start_time = time.time()
                logger.info(f"Bot execution started. Attempt {attempts}")

                self._execute_bot_task()

                execution_time = self._get_execution_time()
                resource_usage = self._get_resource_usage()

                logger.info(
                    f"{self.bot_name} Bot execution completed on attempt {attempts}."
                )
                logger.info(f"Execution time: {execution_time}")
                logger.info(f"Resource usage at end of execution: {resource_usage}")

                self.sharepoint.list_folders_by_number()
                self.sharepoint.upload_files([rf"{self.logger.log_path}"])
                self._insert_database_log_execution()

                success_message = f"""Execution time: {execution_time}\nResource usage at end of execution: {resource_usage}"""

                self.maestro.finish_task(
                    self.execution.task_id,
                    AutomationTaskFinishStatus.SUCCESS,
                    success_message,
                )

                if self.use_telegram:
                    self.telegram_bot.send_message(
                        f"{self.bot_name} Bot execution completed.",
                        group=self.telegram_group,
                    )
                    self.telegram_bot.upload_document(
                        document=self.logger.log_path,
                        group=self.telegram_group,
                        caption=self.bot_name,
                    )

                break

            except Exception as e:
                attempts += 1
                logger.error(
                    f"An error occurred during bot '{self.bot_name}' execution: {e}"
                )

                self.maestro.error(
                    self.execution.task_id, e, attachments=[self.logger.log_path]
                )

                self.maestro.finish_task(
                    self.execution.task_id,
                    AutomationTaskFinishStatus.FAILED,
                    f"An error occurred during bot execution: {e}",
                )

                if self.use_telegram:
                    self.telegram_bot.send_message(
                        f"An error occurred during bot '{self.bot_name}' execution: {e}",
                        self.telegram_group,
                    )
                    self.telegram_bot.upload_document(
                        document=self.logger.log_path,
                        group=self.telegram_group,
                        caption=self.bot_name,
                    )

                if attempts > self.max_retries:
                    logger.error(
                        f"Max retries reached ({self.max_retries}). Giving up."
                    )
                    self.sharepoint.list_folders_by_number()
                    self.sharepoint.upload_files([rf"{self.logger.log_path}"])
                    raise e

                else:
                    logger.info(f"Retrying bot execution (attempt {attempts})...")

            finally:
                self._add_log_file_into_maestro()


class BotRunnerLocal(BotMaestroSDK):
    def __init__(
        self,
        bot_name: str,
        dev: str,
        sector: str,
        stakeholder: str,
        recurrence: str,
        log_folder: str,
        server: str,
        login: str,
        key: str,
        log_dir: str = "logs",
        use_telegram: bool = False,
        telegram_group: Optional[str] = None,
        max_retries: int = 0,
    ) -> None:
        """
        Initializes the BotRunnerLocal instance with the specified configuration.

        Args:
            bot_name (str): The bot's name, used for logging purposes.
            server (str): BotMaestro server URL.
            login (str): BotMaestro login credential.
            key (str): BotMaestro authentication key.
            log_dir (str, optional): Directory for log files (default: 'logs').
            use_telegram (bool, optional): Whether to enable Telegram integration (default: False).
            telegram_group (Optional[str]): Telegram group name or ID for sending notifications.

        Raises:
            ValueError: If 'telegram_group' is not provided.
        """
        # BotMaestroSDK config
        super().__init__(server, login, key)
        super().login()
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)

        # initial config
        self.bot_name: str = bot_name
        self.logger: LoggerConfig = LoggerConfig(bot_name, log_dir)

        # Save in homol database
        self.dev: str = dev
        self.sector: str = sector
        self.stakeholder: str = stakeholder
        self.recurrence: str = recurrence
        self.max_retries = max_retries
        self.log_folder: str = log_folder

        # Sharepoint credentials
        site_url_sharepoint = (
            super().get_credential(label="Your_Sharepoint_Credentials", key="site_url")
            + "YourGroup"
        )
        username_sharepoint = super().get_credential(
            label="Your_Sharepoint_Credentials", key="username"
        )
        password_sharepoint = super().get_credential(
            label="Your_Sharepoint_Credentials", key="password"
        )
        self.sharepoint = SharePointApi(
            site_url_sharepoint,
            username_sharepoint,
            password_sharepoint,
            log_folder,
            self.bot_name,
        )

        # maestro config
        self.RAISE_NOT_CONNECTED: bool = False
        self.VERIFY_SSL_CERT = False

        # telegram config
        self.use_telegram: bool = use_telegram
        self.telegram_bot: Optional[TelegramBot] = None

        if self.use_telegram:
            if not telegram_group:
                raise ValueError(
                    "Telegram group must be provided when use_telegram is True."
                )
            self.telegram_group: str = telegram_group
            self.telegram_token: str = self._get_telegram_token()
            self.telegram_bot = TelegramBot(token=self.telegram_token)

        # time config
        self.start_time: Optional[float] = None

    def _get_telegram_token(self) -> Optional[str]:
        """
        Retrieves the Telegram bot token from the BotMaestro server.

        Returns:
            Optional[str]: The Telegram bot token, or None if `use_telegram` is False.

        Raises:
            ValueError: If the token is empty or not provided in the Maestro credentials.
            Exception: If an error occurs during token retrieval.
        """
        if not self.use_telegram:
            return None
        try:
            token = super().get_credential(label="Telegram", key="token")
            if not token or token == "":
                raise ValueError(
                    "Telegram Token must be provided in Maestro credentials"
                )
            logger.info("Telegram token retrieved successfully.")
            return token
        except Exception as e:
            logger.error(f"Failed to retrieve telegram token: {e}")
            raise e

    def _get_execution_time(self) -> str:
        """
        Computes the execution duration since the bot's start time.

        Returns:
            str: Execution time formatted as 'DD:HH:MM:SS'.
        """
        if self.start_time is None:
            return "Execution time not available"

        end_time = time.time()
        elapsed_seconds = int(end_time - self.start_time)

        days, remainder = divmod(elapsed_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        execution_time = f"{days:02}:{hours:02}:{minutes:02}:{seconds:02}"
        return execution_time

    def _get_resource_usage(self) -> str:
        """
        Retrieves current resource usage (CPU, RAM, and GPU).

        Returns:
            str: Formatted string with CPU, RAM, and GPU usage.
        """
        # CPU and RAM usage
        cpu_percent = psutil.cpu_percent(interval=1)
        ram_usage = psutil.virtual_memory()
        ram_percent = ram_usage.percent
        ram_used_mb = ram_usage.used / (1024 * 1024)

        # GPU usage (if GPU is available)
        gpu_stats = []
        gpus = GPUtil.getGPUs()
        if gpus:
            for gpu in gpus:
                gpu_stats.append(
                    f"GPU {gpu.id}: {gpu.name}, Load: {gpu.load * 100:.1f}%, "
                    f"Memory: {gpu.memoryUsed}MB/{gpu.memoryTotal}MB"
                )
            gpu_usage_str = "; ".join(gpu_stats)
        else:
            gpu_usage_str = "No GPU found."

        # Format the usage information
        usage_info = f"CPU Usage: {cpu_percent}%, RAM Usage: {ram_percent}% ({ram_used_mb:.1f} MB), GPU Usage: {gpu_usage_str}"
        return usage_info

    def _insert_database_log_execution(self):
        """
        Inserts an execution log entry into the automation logs database.

        This function retrieves the necessary SQL credentials from BotMaestro, establishes a
        connection with the production SQL database, and inserts a log record with information
        about the bot execution (such as bot name, developer, sector, stakeholder, recurrence, and execution time).

        Raises:
            Exception: If there is an error connecting to the database or executing the query.
        """
        time = self._get_execution_time()

        sql_connector = SQLDatabaseConnector(
            server="srv-homologation",
            database="your_database",
            use_windows_auth=True,
        )

        sql_connector.connect()

        params = (
            self.bot_name,
            self.dev,
            self.sector,
            self.stakeholder,
            self.recurrence,
            time,
        )

        query = r"botcity_aux\sql\query\insert_log.sql"

        sql_connector.execute_query_from_file(query, params)

        sql_connector.disconnect()

    def _execute_bot_task(self) -> None:
        """
        Executes the main bot task logic.

        Note:
            This method should be extended with the actual task logic for your bot.
        """
        main()

    def run(self) -> None:
        """
        Starts the bot execution process with retry logic and logs the results.

        This method attempts to execute the bot task up to the maximum number of retries
        defined by `self.max_retries`. For each attempt, it logs the start time, execution time,
        resource usage, and final status.

        Logs:
            - Execution start and completion per attempt.
            - Execution time and system resource usage.
            - Any errors encountered during execution.

        Raises:
            Exception: If the bot fails to execute successfully after all retry attempts.
        """
        attempts = 0
        while attempts <= self.max_retries:
            try:
                self.start_time = time.time()
                logger.info(f"Bot execution started. Attempt {attempts}")

                self._execute_bot_task()

                logger.info(
                    f"{self.bot_name} Bot execution completed on attempt {attempts}."
                )
                logger.info(f"Execution time: {self._get_execution_time()}")
                logger.info(
                    f"Resource usage at end of execution: {self._get_resource_usage()}"
                )
                self.sharepoint.list_folders_by_number()
                self.sharepoint.upload_files([rf"{self.logger.log_path}"])
                self._insert_database_log_execution()

                if self.use_telegram:
                    self.telegram_bot.send_message(
                        f"{self.bot_name} Bot execution completed.",
                        group=self.telegram_group,
                    )
                    self.telegram_bot.upload_document(
                        document=self.logger.log_path,
                        group=self.telegram_group,
                        caption=self.bot_name,
                    )

                break

            except Exception as e:
                attempts += 1
                logger.error(
                    f"An error occurred during bot '{self.bot_name}' attempt: {attempts}, execution: {e}"
                )

                if self.use_telegram:
                    self.telegram_bot.send_message(
                        f"An error occurred during bot '{self.bot_name}' execution: {e}",
                        self.telegram_group,
                    )
                    self.telegram_bot.upload_document(
                        document=self.logger.log_path,
                        group=self.telegram_group,
                        caption=self.bot_name,
                    )

                if attempts > self.max_retries:
                    logger.error(
                        f"Max retries reached ({self.max_retries}). Giving up."
                    )
                    raise e

                else:
                    logger.info(f"Retrying bot execution (attempt {attempts})...")
