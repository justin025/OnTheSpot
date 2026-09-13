import copy
import json
import logging
import os
import re
import shutil
import uuid

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
        self.__default_cfg_path = os.path.join(
            os.path.dirname(__file__), "otsconfig_default.json"
        )
        self.session_uuid = str(uuid.uuid4())

        # Load default config
        try:
            with open(self.__default_cfg_path, "r", encoding="utf-8") as df:
                self.__template_data = json.load(df)
        except (json.JSONDecodeError, FileNotFoundError):
            print(
                f"Failed to load default config file: {self.__default_cfg_path}, using empty template"
            )
            self.__template_data = {}

        # Load or create user config
        if os.path.isfile(self.__cfg_path):
            try:
                with open(self.__cfg_path, "r", encoding="utf-8") as cf:
                    self.__config = json.load(cf)
            except (json.JSONDecodeError, FileNotFoundError):
                print(
                    f"Failed to load user config file: {self.__cfg_path}, using default template"
                )
                self.__config = self.__template_data.copy()
        else:
            try:
                os.makedirs(os.path.dirname(self.__cfg_path), exist_ok=True)
                with open(self.__cfg_path, "w", encoding="utf-8") as cf:
                    json.dump(self.__template_data, cf, indent=4, ensure_ascii=False)
                self.__config = self.__template_data.copy()
            except (FileNotFoundError, PermissionError) as e:
                print(f"Failed to create config dir: {e}, attempting fallback path.")
                fallback_path = os.path.abspath(
                    os.path.join(os.path.expanduser("~"), ".config", "otsconfig.json")
                )
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

        # The settings endpoint stored whatever it received, so an existing or
        # hand-edited configuration file can hold text or booleans where the
        # template declares another type. Repair those values on load; the
        # healed configuration reaches disk on the next normal save.
        for key, value in list(self.__config.items()):
            # Credentials belong to the encrypted store, not to this file.
            if key in CREDENTIAL_KEYS or key not in self.__template_data:
                continue
            default = self.__template_data[key]
            if isinstance(default, str) and isinstance(value, (bool, int)):
                # The settings endpoint converted "true"/"false" to a boolean
                # and a run of digits to a number, text settings included.
                # Restore the user's text; the fallback below would throw the
                # setting away.
                if isinstance(value, bool):
                    self.__config[key] = "true" if value else "false"
                else:
                    self.__config[key] = str(value)
                continue
            try:
                self.__config[key] = self.coerce(key, value)
            except ValueError as e:
                print(f"{e}, restoring the default value.")
                self.__config[key] = copy.deepcopy(default)

        # Beta 1 changed a download profile's bitrate from text ("320k") to a
        # whole number; a file written by Alpha 2 still holds the old text.
        # The check above only sees ``download_profiles`` as a whole list, so
        # heal each profile's bitrate separately here.
        self.__heal_download_profiles()

        # Version identifies the bundled application build, not a user setting.
        # Keep existing configuration volumes from pinning the UI to an older
        # release after the Docker image has been upgraded.
        if self.__template_data.get("version"):
            self.__config["version"] = self.__template_data["version"]

        # ``cache_metadata_in_queue`` was the original UI key for the global
        # API-cache switch.  Preserve an existing user's choice while moving
        # to the accurately named setting.
        # if "cache_api_calls" not in self.__config:
        #    self.__config["cache_api_calls"] = bool(
        #        self.__config.get(
        #            "cache_metadata_in_queue",
        #            self.__template_data.get("cache_api_calls", True),
        #        )
        #    )

        # The bundled defaults are written for the Linux/Docker image. When
        # running the API directly on Windows, translate those container paths
        # to the user's normal Music/Videos folders instead of creating a
        # literal ``C:\\root`` directory.
        # if os.name == "nt":
        #     for path_key in ("audio_download_path", "video_download_path"):
        #         configured_path = self.__config.get(path_key)
        #         normalized_path = str(configured_path or "").replace("\\", "/")
        #         if normalized_path.startswith("/root/"):
        #             self.__config[path_key] = os.path.join(
        #                 os.path.expanduser("~"), normalized_path.removeprefix("/root/")
        #             )

        # Make Download Dirs
        try:
            os.makedirs(self.get("audio_download_path"), exist_ok=True)
            os.makedirs(self.get("video_download_path"), exist_ok=True)
        except (FileNotFoundError, PermissionError) as e:
            print(f"Failed to create download dir: {e}, attempting fallback path.")
            self.set(
                "audio_download_path", self.__template_data.get("audio_download_path")
            )
            self.set(
                "video_download_path", self.__template_data.get("video_download_path")
            )
            os.makedirs(self.get("audio_download_path"), exist_ok=True)
            os.makedirs(self.get("video_download_path"), exist_ok=True)

        # Set FFMPEG Path
        ffmpeg_path = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg_path and os.name != "nt":
            ffmpeg_path = "/usr/bin/ffmpeg"

        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            self._ffmpeg_bin_path = ffmpeg_path
        else:
            print(
                "Failed to find ffmpeg binary, please consider installing ffmpeg or defining its path."
            )
            self._ffmpeg_bin_path = ""

        print(f"FFMPEG Binary: {self._ffmpeg_bin_path}")
        self.set("_ffmpeg_bin_path", self._ffmpeg_bin_path)
        self.set(
            "_log_file",
            os.path.join(
                config_root,
                "logs",
                self.session_uuid,
                "onthespot.log",
            ),
        )
        self.set(
            "_cache_dir",
            cache_dir(),
        )
        try:
            os.makedirs(os.path.dirname(self.get("_log_file")), exist_ok=True)
            os.makedirs(self.get("_cache_dir"), exist_ok=True)
        except (FileNotFoundError, PermissionError):
            fallback_logdir = os.path.abspath(
                os.path.join(".logs", self.session_uuid, "onthespot.log")
            )
            print(
                f'Current logging dir cannot be set up at "{self.get("video_download_path")}"'
                f"; Falling back to: {fallback_logdir}"
            )
            self.set("_log_file", fallback_logdir)
            os.makedirs(os.path.dirname(self.get("_log_file")), exist_ok=True)

    def __adopt_plaintext_credentials(self):
        """Move any plaintext credentials out of the config file.

        Leaving them in place would defeat the encrypted store, and simply
        dropping them would sign the user out without saying so, which is why
        they are carried across once rather than discarded. Only the credential
        keys move; nothing else from an old config is imported.
        """
        found = {
            key: self.__config.get(key)
            for key in list(self.__config)
            if key in CREDENTIAL_KEYS
        }
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

    def __heal_download_profiles(self):
        """Repair an Alpha 2 profile's text bitrate to the number Beta 1 expects.

        Alpha 2 stored a profile's bitrate as text such as "320k". The Deezer
        quality selection compares the bitrate with a number, so leftover text
        raises instead of downloading. Numeric text, with or without the "k"
        suffix, becomes the number; anything else falls back to 320, the same
        default the ``/profiles`` endpoint uses for a missing bitrate.
        """
        profiles = self.__config.get("download_profiles")
        if not isinstance(profiles, list):
            return
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            bitrate = profile.get("bitrate")
            if isinstance(bitrate, int) and not isinstance(bitrate, bool):
                continue
            if isinstance(bitrate, str):
                match = re.fullmatch(r"\s*(\d+)[kK]?\s*", bitrate)
                if match:
                    profile["bitrate"] = int(match.group(1))
                    continue
            print(
                f"Download profile '{profile.get('id')}' has an invalid "
                f"bitrate {bitrate!r}, using the default of 320."
            )
            profile["bitrate"] = 320

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
            snapshot = {
                key: value
                for key, value in snapshot.items()
                if not str(key).startswith("_")
            }

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

        secret_keys = {
            "spotify_webapi_override_client_secret",
            "playlist_automation_client_secret",
            "webui_password",
        }
        for key in list(snapshot):
            if key in secret_keys or any(
                marker in key.casefold() for marker in ("password", "secret", "token")
            ):
                snapshot[f"{key}_configured"] = bool(snapshot.get(key))
                snapshot[key] = ""

        return snapshot

    def coerce(self, key, value):
        """
        Converts a value to the type the default template declares for the key.

        The bundled ``otsconfig_default.json`` is the type authority: the type of
        a key's default decides the type stored under that key. Only the text
        forms a query string or an imported document produce are converted; any
        other mismatch raises instead of hiding a programming error. Keys the
        template does not hold pass through unchanged. A default that is neither
        a bool, an int, a string nor a list raises, so a new template type cannot
        slip past the type authority.

        Healing on load calls this. ``set()``
        deliberately does not; it stores what it is given.

        :param key: The configuration key the value belongs to.
        :param value: The value to convert.
        :return: The value as the type of the key's default.
        :raises ValueError: If the value does not suit the type of the key's default.
        """
        if key not in self.__template_data:
            return value

        expected = type(self.__template_data[key])

        if expected is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.casefold() in ("true", "false"):
                return value.casefold() == "true"
            raise ValueError(f"Configuration key '{key}' needs a true or false value")

        if expected is int:
            # A bool is an int in Python, so exclude it from the number path.
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    pass
            raise ValueError(f"Configuration key '{key}' needs a whole number")

        if expected is str:
            if isinstance(value, str):
                return value
            raise ValueError(f"Configuration key '{key}' needs a text value")

        if expected is list:
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return parsed
            raise ValueError(f"Configuration key '{key}' needs a list value")

        raise ValueError(
            f"Configuration key '{key}' has the unsupported default type "
            f"'{expected.__name__}'"
        )

    def set(self, key, value):
        """
        Sets a configuration key to a given value.

        :param key: The configuration key to set.
        :param value: The value to associate with the key.
        :return: The value that was set.
        """
        if key in CREDENTIAL_KEYS:
            self.__credential_values[key] = (
                value.copy() if isinstance(value, (list, dict)) else value
            )
            self.__credentials.save(self.__credential_values)
            return value
        if type(value) in [list, dict]:
            self.__config[key] = value.copy()
        else:
            self.__config[key] = value
        return value

    def save(self):
        """
        Saves the current configuration to the user configuration file.

        This method will ensure that all necessary directories are created and then write the current configuration to the JSON file.
        If any step fails, appropriate fallback mechanisms are used to ensure that the application can still run.
        """
        os.makedirs(os.path.dirname(self.__cfg_path), exist_ok=True)
        # Merge template data into config for missing keys
        for key in list(set(self.__template_data).difference(set(self.__config))):
            # Credential keys are deliberately absent from __config. Merging the
            # template default back in would route through set() and overwrite
            # the stored credentials with an empty list.
            if not key.startswith("_") and key not in CREDENTIAL_KEYS:
                self.set(key, self.__template_data[key])
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
