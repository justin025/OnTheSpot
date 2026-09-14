import copy
import json
import logging
import os
import re
import shutil
import uuid

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined

from .basemodels import AppSettings
from .credentials import CREDENTIAL_KEYS, CredentialStore

logger = logging.getLogger(__name__)


def _expanded_path(value: str) -> str:
    """Return an absolute path after expanding user and environment markers."""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def config_dir():
    """
    Returns the configuration directory path based on environment variables and operating system.

    :return: The configuration directory path as a string.
    """
    override = os.environ.get("ONTHESPOTDIR", "").strip()
    if override:
        return _expanded_path(override)
    if os.name == "nt" and os.environ.get("APPDATA"):
        base_dir = os.environ["APPDATA"]
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base_dir = os.environ["LOCALAPPDATA"]
    elif os.environ.get("XDG_CONFIG_HOME"):
        base_dir = os.environ["XDG_CONFIG_HOME"]
    else:
        base_dir = os.path.join(os.path.expanduser("~"), ".config")
    return _expanded_path(os.path.join(base_dir, "onthespot"))


def cache_dir():
    """
    Returns the cache directory path based on environment variables and operating system.

    :return: The cache directory path as a string.
    """
    override = os.environ.get("ONTHESPOTCACHEDIR", "").strip()
    if override:
        return _expanded_path(override)
    # Cache-backed state includes Spotify sessions, statistics, playlist
    # automation state, and uploaded YouTube cookies. Keep it beside the main
    # configuration so Docker/Unraid's config volume persists it.
    return os.path.join(config_dir(), "cache")


class Config:
    def __init__(self):
        """
        Initializes a new Config instance, setting up configuration paths,
        loading default and user configurations, and initializing session UUID.
        Also sets up download directories and determines the FFMPEG binary path.

        This method will:
        - Load template data from the external default configuration file.
        - Initialize session UUID.
        - Define file extension for cross-platform compatibility.
        - Load or create a user configuration file.
        - Create necessary download directories.
        - Determine the FFMPEG binary path.

        If any step fails, appropriate fallback mechanisms are used to ensure that the application can still run.
        """
        config_root = config_dir()

        self.__cfg_path = os.path.join(config_root, "otsconfig.json")
        self.__default_cfg_path = os.path.join(os.path.dirname(__file__), "otsconfig_default.json")
        self.__model_fields = AppSettings.model_fields
        self.session_uuid = str(uuid.uuid4())

        # Load default config
        try:
            with open(self.__default_cfg_path, "r", encoding="utf-8") as df:
                self.__template_data = json.load(df)
        except (json.JSONDecodeError, FileNotFoundError):
            print(f"Failed to load default config file: {self.__default_cfg_path}, using empty template")
            self.__template_data = {}

        # Load or create user config
        if os.path.isfile(self.__cfg_path):
            try:
                with open(self.__cfg_path, "r", encoding="utf-8") as cf:
                    self.__config = json.load(cf)
            except (json.JSONDecodeError, FileNotFoundError):
                print(f"Failed to load user config file: {self.__cfg_path}, using default template")
                self.__config = self.__template_data.copy()
        else:
            try:
                os.makedirs(os.path.dirname(self.__cfg_path), exist_ok=True)
                with open(self.__cfg_path, "w", encoding="utf-8") as cf:
                    json.dump(self.__template_data, cf, indent=4, ensure_ascii=False)
                self.__config = self.__template_data.copy()
            except (FileNotFoundError, PermissionError) as e:
                print(f"Failed to create config dir: {e}, attempting fallback path.")
                fallback_path = os.path.abspath(os.path.join(os.path.expanduser("~"), ".config", "otsconfig.json"))
                self.__cfg_path = fallback_path
                os.makedirs(os.path.dirname(self.__cfg_path), exist_ok=True)
                with open(self.__cfg_path, "w", encoding="utf-8") as cf:
                    json.dump(self.__template_data, cf, indent=4, ensure_ascii=False)
                self.__config = self.__template_data

        # Credentials live in their own encrypted file beside whichever config
        # path was actually used, including the fallback one. See #368.
        self.__credentials = CredentialStore(os.path.dirname(self.__cfg_path))
        self.__credential_values = self.__credentials.load()
        self.__adopt_plaintext_credentials()

        # Config validation
        validated_config = {}

        for key, value in list(self.__config.items()):
            validated_value = self.validate_value(value, key)
            if validated_value is not None:
                validated_config[key] = validated_value
            else:
                continue

        self.__config = validated_config

        # Keep existing configuration from pinning the UI to an older
        # release after the Docker image has been upgraded.
        if self.__template_data.get("version"):
            self.__config["version"] = self.__template_data["version"]

        # Download Folders Setup
        try:
            os.makedirs(self.get("audio_download_path"), exist_ok=True)
            os.makedirs(self.get("video_download_path"), exist_ok=True)
        except (FileNotFoundError, PermissionError) as e:
            print(f"Failed to create download dir: {e}, attempting fallback path.")
            self.set("audio_download_path", self.__template_data.get("audio_download_path"))
            self.set("video_download_path", self.__template_data.get("video_download_path"))
            os.makedirs(self.get("audio_download_path"), exist_ok=True)
            os.makedirs(self.get("video_download_path"), exist_ok=True)

        # FFMPEG Path Setup
        ffmpeg_env_path = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
        ffmpeg_path = "/usr/bin/ffmpeg" if not ffmpeg_env_path and os.name != "nt" else ffmpeg_env_path

        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            self._ffmpeg_bin_path = ffmpeg_path
            self.set("_ffmpeg_bin_path", self._ffmpeg_bin_path)
            print(f"FFMPEG Binary: {self._ffmpeg_bin_path}")
        else:
            print("Failed to find ffmpeg binary, please consider installing ffmpeg or defining its path.")
            self._ffmpeg_bin_path = ""

        # Cache Folder Setup
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            self.set("_cache_dir", cache_dir())
        except (FileNotFoundError, PermissionError):
            fallback_cachedir = os.path.abspath(".cache")
            os.makedirs(fallback_cachedir, exist_ok=True)
            self.set("_cache_dir", fallback_cachedir)
            print(f'Cache dir cannot be set up at "{self.get("_cache_dir")}"; Falling back to: {fallback_cachedir}')

        # Logs Folder Setup
        try:
            logs_dir = os.path.join(
                config_root,
                "logs",
                self.session_uuid,
                "onthespot.log",
            )
            os.makedirs(os.path.dirname(logs_dir), exist_ok=True)
            self.set("_log_file", logs_dir)
        except (FileNotFoundError, PermissionError):
            fallback_logdir = os.path.abspath(os.path.join(".logs", self.session_uuid, "onthespot.log"))
            self.set("_log_file", fallback_logdir)
            os.makedirs(os.path.dirname(self.get("_log_file")), exist_ok=True)
            print(f'Log dir cannot be set up at "{self.get("_log_file")}"; Falling back to: {fallback_logdir}')

    def validate_value(self, value, key):
        if key.startswith("_"):
            return None
        if key not in self.__model_fields:
            print(f"key: {key} not present in model config, skipping...")
            return None

        field_info = self.__model_fields[key]
        adapter = TypeAdapter(field_info.annotation)
        try:
            validated_value = adapter.validate_python(value)
            if isinstance(validated_value, list):
                validated_value = [
                    item.model_dump() if hasattr(item, "model_dump") else item for item in validated_value
                ]
            elif hasattr(validated_value, "model_dump"):
                validated_value = validated_value.model_dump()
        except ValidationError:
            if field_info.default is not PydanticUndefined:
                print(f"can't validate key: {key}, loading default value...")
                validated_value = field_info.default
            elif field_info.default_factory is not None:
                print(f"can't validate key: {key}, loading default value...")
                validated_value = field_info.default_factory()
            else:
                print(f"can't validate key or no default provided for: {key}, skipping...")
                return None

        return validated_value

    def __adopt_plaintext_credentials(self):
        """Move any plaintext credentials out of the config file.

        Leaving them in place would defeat the encrypted store, and simply
        dropping them would sign the user out without saying so, which is why
        they are carried across once rather than discarded. Only the credential
        keys move; nothing else from an old config is imported.
        """
        found = {key: self.__config.get(key) for key in list(self.__config) if key in CREDENTIAL_KEYS}
        if not found:
            return

        carried = {key: value for key, value in found.items() if value}
        if carried and not self.__credential_values:
            self.__credential_values.update(carried)
            if self.__credentials.save(self.__credential_values):
                for key in found:
                    self.__config.pop(key, None)
                self.save()
                print(
                    "Moved "
                    + ", ".join(sorted(carried))
                    + " out of otsconfig.json into the encrypted credential store."
                )
            else:
                print("Credentials Write failed, credentials are kept in config file.")
                return

    def get(self, key, default=None):
        """
        Retrieves the value of a configuration key.

        :param key: The configuration key to retrieve.
        :param default: The default value to return if the key is not found in either the user or template configurations.
        :return: The value associated with the key, or the default value if the key is not found.
        """
        if key in CREDENTIAL_KEYS:
            if key in self.__credential_values:
                return self.__credential_values[key]
            return self.__template_data.get(key, default)
        if key in self.__config:
            return self.__config[key]
        if key in self.__template_data:
            return self.__template_data[key]
        else:
            return default

    def as_dict(self, *, include_runtime=False, include_secrets=False):
        """Return a detached configuration snapshot for API responses.

        Historically FastAPI serialised the ``Config`` instance directly.
        That exposed Python implementation details, runtime filesystem paths,
        account login payloads, and the Spotify client secret.  Keep the
        public response flat and useful to the UI while never returning
        authentication material by default.
        """
        snapshot = copy.deepcopy(self.__template_data)
        snapshot.update(copy.deepcopy(self.__config))
        # Credentials no longer live in __config, so merge them back in or the
        # UI's account list would silently come back empty. They are reduced to
        # uuid/service/active below unless secrets were explicitly requested.
        snapshot.update(copy.deepcopy(self.__credential_values))

        if not include_runtime:
            snapshot = {key: value for key, value in snapshot.items() if not str(key).startswith("_")}

        if include_secrets:
            return snapshot

        accounts = []
        for account in snapshot.get("accounts", []) or []:
            if not isinstance(account, dict):
                continue
            accounts.append(
                {
                    "uuid": str(account.get("uuid") or ""),
                    "service": str(account.get("service") or ""),
                    "active": bool(account.get("active", True)),
                }
            )
        snapshot["accounts"] = accounts

        return snapshot

    def set(self, key, value):
        """
        Sets a configuration key to a given value.
        Performs validation against base model

        :param key: The configuration key to set.
        :param value: The value to associate with the key.
        :return: The value that was set.
        """
        if key in CREDENTIAL_KEYS:
            self.__credential_values[key] = value.copy() if isinstance(value, (list, dict)) else value
            self.__credentials.save(self.__credential_values)
            return value
        if key.startswith("_"):
            self.__config[key] = value
        elif self.validate_value(value, key) is not None:
            self.__config[key] = self.validate_value(value, key)
        else:
            print(f"Can't set {key}:{value}, validation against model failed.")

    def save(self):
        """
        Saves the current configuration to the user configuration file.

        This method will ensure that all necessary directories are created and then write the current configuration to the JSON file.
        If any step fails, appropriate fallback mechanisms are used to ensure that the application can still run.
        """
        os.makedirs(os.path.dirname(self.__cfg_path), exist_ok=True)
        # Never let a credential reach the plaintext config file.
        for key in CREDENTIAL_KEYS:
            self.__config.pop(key, None)
        try:
            with open(self.__cfg_path, "w", encoding="utf-8") as cf:
                json.dump(self.__config, cf, indent=4, ensure_ascii=False)
        except (IOError, OSError) as e:
            print(f"Failed to save config file: {e}")

    def reset(self):
        """
        Resets the configuration to its default values.

        This method will overwrite the user configuration file with the default template data.
        If any step fails, appropriate fallback mechanisms are used to ensure that the application can still run.
        """
        try:
            with open(self.__cfg_path, "w", encoding="utf-8") as cf:
                json.dump(self.__template_data, cf, indent=4, ensure_ascii=False)
        except (IOError, OSError) as e:
            print(f"Failed to reset config file: {e}")
        self.__config = self.__template_data.copy()


config = Config()
