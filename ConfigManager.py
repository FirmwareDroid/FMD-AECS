# Python
import json
import logging


class ConfigManager:
    _configurations = {}  # Class-level dictionary to store configurations

    @staticmethod
    def load_config(name, path):
        """
        Loads a configuration file and stores it in the class-level dictionary.

        :param name: str - Name of the configuration.
        :param path: str - Path to the configuration file.
        """
        try:
            with open(path, 'r') as file:
                ConfigManager._configurations[name] = json.load(file)
        except Exception as e:
            logging.error(f"Failed to load config {name} from {path}: {e}")
            raise

    @staticmethod
    def get_config(name):
        """
        Retrieves a configuration by name.

        :param name: str - Name of the configuration.
        :return: dict - The configuration data.
        """
        return ConfigManager._configurations.get(name)

    @staticmethod
    def clear_config(name):
        """
        Clears a specific configuration.

        :param name: str - Name of the configuration to clear.
        """
        ConfigManager._configurations.pop(name, None)

    @staticmethod
    def clear_all_configs():
        """
        Clears all configurations.
        """
        ConfigManager._configurations.clear()
