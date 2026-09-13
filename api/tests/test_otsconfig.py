import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import TEST_ROOT

from onthespot import otsconfig
from onthespot.otsconfig import Config, cache_dir, config_dir  # noqa: E402

DEFAULT_CONFIG = json.loads(
    Path(otsconfig.__file__)
    .with_name("otsconfig_default.json")
    .read_text(encoding="utf-8")
)


class ConfigPathTests(unittest.TestCase):
    def test_nonexistent_config_override_is_honoured(self):
        override = TEST_ROOT / "new-config-root"
        self.assertFalse(override.exists())
        with patch.dict(os.environ, {"ONTHESPOTDIR": str(override)}):
            self.assertEqual(Path(config_dir()), override.resolve())

    def test_cache_override_is_honoured(self):
        override = TEST_ROOT / "new-cache-root"
        with patch.dict(os.environ, {"ONTHESPOTCACHEDIR": str(override)}):
            self.assertEqual(Path(cache_dir()), override.resolve())

    def test_config_saves_inside_configured_app_data(self):
        config_root = TEST_ROOT / "isolated-config"
        cache_root = TEST_ROOT / "isolated-cache"
        with patch.dict(
            os.environ,
            {
                "ONTHESPOTDIR": str(config_root),
                "ONTHESPOTCACHEDIR": str(cache_root),
            },
        ):
            instance = Config()
            instance.set("release_readiness_probe", "saved")
            instance.save()

        config_file = config_root / "otsconfig.json"
        self.assertTrue(config_file.is_file())
        self.assertEqual(json.loads(config_file.read_text(encoding="utf-8"))["release_readiness_probe"], "saved")
        self.assertTrue(cache_root.is_dir())

    def test_public_snapshot_is_flat_detached_and_redacts_secrets(self):
        config_root = TEST_ROOT / "public-snapshot-config"
        cache_root = TEST_ROOT / "public-snapshot-cache"
        with patch.dict(
            os.environ,
            {
                "ONTHESPOTDIR": str(config_root),
                "ONTHESPOTCACHEDIR": str(cache_root),
            },
        ):
            instance = Config()
            instance.set("spotify_webapi_override_client_secret", "do-not-expose")
            instance.set(
                "accounts",
                [
                    {
                        "uuid": "worker-1",
                        "service": "spotify",
                        "active": True,
                        "login": {"credentials": "also-secret"},
                    }
                ],
            )
            snapshot = instance.as_dict()

        self.assertEqual(snapshot["spotify_webapi_override_client_secret"], "")
        self.assertTrue(snapshot["spotify_webapi_override_client_secret_configured"])
        self.assertEqual(
            snapshot["accounts"],
            [{"uuid": "worker-1", "service": "spotify", "active": True}],
        )
        self.assertNotIn("_Config__config", snapshot)
        snapshot["accounts"][0]["service"] = "changed"
        self.assertEqual(instance.get("accounts")[0]["service"], "spotify")


class ConfigCoercionTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(
            os.environ,
            {
                "ONTHESPOTDIR": str(TEST_ROOT / "coercion-config"),
                "ONTHESPOTCACHEDIR": str(TEST_ROOT / "coercion-cache"),
            },
        ):
            self.config = Config()

    def test_numeric_text_becomes_a_whole_number(self):
        self.assertEqual(self.config.coerce("download_chunk_size", "50000"), 50000)

    def test_boolean_keys_accept_text_and_real_booleans(self):
        self.assertIs(self.config.coerce("debug_mode", "false"), False)
        self.assertIs(self.config.coerce("debug_mode", "True"), True)
        self.assertIs(self.config.coerce("debug_mode", True), True)

    def test_text_key_keeps_a_value_that_reads_as_a_boolean(self):
        self.assertEqual(self.config.coerce("search_prefix", "true"), "true")

    def test_values_of_the_wrong_type_are_rejected(self):
        with self.assertRaises(ValueError):
            self.config.coerce("search_prefix", 123)
        with self.assertRaises(ValueError):
            self.config.coerce("download_chunk_size", True)
        with self.assertRaises(ValueError):
            self.config.coerce("download_chunk_size", "abc")

    def test_list_key_takes_json_text_that_holds_a_list(self):
        self.assertEqual(
            self.config.coerce("ffmpeg_args", '["-hide_banner"]'), ["-hide_banner"]
        )
        with self.assertRaises(ValueError):
            self.config.coerce("ffmpeg_args", '{"loglevel": "quiet"}')

    def test_keys_outside_the_template_pass_through(self):
        self.assertEqual(self.config.coerce("release_readiness_probe", 7), 7)


class ConfigHealingTests(unittest.TestCase):
    def _load(self, name, stored):
        config_root = TEST_ROOT / name
        config_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "audio_download_path": str(TEST_ROOT / f"{name}-audio"),
            "video_download_path": str(TEST_ROOT / f"{name}-video"),
        }
        payload.update(stored)
        (config_root / "otsconfig.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        with patch.dict(
            os.environ,
            {
                "ONTHESPOTDIR": str(config_root),
                "ONTHESPOTCACHEDIR": str(TEST_ROOT / f"{name}-cache"),
            },
        ):
            return Config()

    def test_corrupt_config_file_is_healed_on_load(self):
        instance = self._load(
            "healing-config",
            {
                "download_chunk_size": "50000",
                "debug_mode": "false",
                "search_prefix": True,
                "m3u_format": 320,
                "extinf_separator": 3.5,
                "download_delay": "garbage",
                "download_profiles": "not-json",
            },
        )

        self.assertEqual(instance.get("download_chunk_size"), 50000)
        self.assertIs(instance.get("debug_mode"), False)
        # A boolean or a number in a text slot keeps the user's choice as
        # text. The settings endpoint converted both, whatever the slot.
        self.assertEqual(instance.get("search_prefix"), "true")
        self.assertEqual(instance.get("m3u_format"), "320")
        # The endpoint cannot produce a fraction, so a float in a text slot is
        # a broken file rather than damage to repair. It takes the default.
        self.assertEqual(
            instance.get("extinf_separator"), DEFAULT_CONFIG["extinf_separator"]
        )
        self.assertEqual(
            instance.get("download_delay"), DEFAULT_CONFIG["download_delay"]
        )

        healed_profiles = instance.get("download_profiles")
        self.assertEqual(healed_profiles, DEFAULT_CONFIG["download_profiles"])
        self.assertIsNot(
            healed_profiles[0],
            instance._Config__template_data["download_profiles"][0],
        )

    def test_healing_leaves_credentials_alone(self):
        # An empty string is falsy, so the one-shot migration leaves it in the
        # plaintext file rather than moving it to the encrypted store, and the
        # heal loop is the next thing to see it. Skipping credentials keeps the
        # repair independent of that migration's ordering. Both get() and
        # as_dict() mask the stored value here, so assert on it directly.
        instance = self._load("healing-credentials", {"accounts": ""})

        self.assertEqual(instance._Config__config["accounts"], "")

    def test_alpha2_profile_bitrates_are_healed_to_numbers(self):
        # Alpha 2 wrote a profile's bitrate as text like "320k"; Beta 1 wants
        # a whole number. A value that is neither known text nor a number
        # (the "odd" profile here) falls back to 320, the same default the
        # /profiles endpoint uses for a missing bitrate.
        stored_profiles = [
            {
                "id": "mp3-320",
                "name": "MP3 · 320 kbps",
                "format": "mp3",
                "bitrate": "320k",
                "download_path": "",
            },
            {
                "id": "flac",
                "name": "FLAC · lossless",
                "format": "flac",
                "bitrate": "1411k",
                "download_path": "",
            },
            {
                "id": "odd",
                "name": "Odd",
                "format": "mp3",
                "bitrate": "high",
                "download_path": "",
            },
        ]
        instance = self._load(
            "healing-profile-bitrates",
            {
                "download_profiles": stored_profiles,
                "active_download_profile": "flac",
            },
        )

        # Only the bitrates change; every other field survives, in order.
        profiles = instance.get("download_profiles")
        self.assertEqual(
            profiles,
            [
                {**profile, "bitrate": bitrate}
                for profile, bitrate in zip(stored_profiles, (320, 1411, 320))
            ],
        )
        # Equality alone would accept 320.0.
        for profile in profiles:
            self.assertIsInstance(profile["bitrate"], int)
        self.assertEqual(instance.get("active_download_profile"), "flac")

    def test_profile_bitrate_that_is_already_a_number_is_left_alone(self):
        # A file already on the new format should pass through unchanged.
        profiles = [
            {
                "id": "mp3-320",
                "name": "MP3 · 320 kbps",
                "format": "mp3",
                "bitrate": 320,
                "download_path": "",
            },
            {
                "id": "flac",
                "name": "FLAC · lossless",
                "format": "flac",
                "bitrate": 1411,
                "download_path": "",
            },
        ]
        instance = self._load(
            "healing-profile-bitrates-untouched",
            {"download_profiles": profiles},
        )

        self.assertEqual(instance.get("download_profiles"), profiles)


if __name__ == "__main__":
    unittest.main()
