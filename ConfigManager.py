import json
import logging

class ConfigManager:
    _configs = {}

    @classmethod
    def load_config(cls, config_name, config_path):
        """
        Load a configuration file and store it in the manager.
        :param config_name: str - Name of the configuration.
        :param config_path: str - Path to the configuration file.
        """
        try:
            with open(config_path, 'r') as file:
                cls._configs[config_name] = json.load(file)
                logging.info(f"Loaded configuration '{config_name}' from {config_path}.")
        except Exception as e:
            logging.error(f"Error loading configuration '{config_name}' from {config_path}: {e}")
            raise

    @classmethod
    def get_config(cls, config_name):
        """
        Retrieve a configuration by name.
        :param config_name: str - Name of the configuration.
        :return: dict - Configuration data.
        """
        return cls._configs.get(config_name)

    @classmethod
    def reload_config(cls, config_name, config_path):
        """
        Reload a configuration file.
        :param config_name: str - Name of the configuration.
        :param config_path: str - Path to the configuration file.
        """
        cls.load_config(config_name, config_path)

