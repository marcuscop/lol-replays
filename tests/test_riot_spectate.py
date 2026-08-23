import importlib.util
from pathlib import Path


def load_script_module():
    script_path = Path(__file__).resolve().parents[1] / "riot-spectate.py"
    spec = importlib.util.spec_from_file_location("riot_spectate", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


riot_spectate = load_script_module()


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses, expected_timeout=15):
        self.responses = list(responses)
        self.expected_timeout = expected_timeout
        self.headers = {}

    def get(self, url, timeout=15):
        assert timeout == self.expected_timeout
        assert "active-games/by-summoner" in url
        return self.responses.pop(0)


def test_load_config_reads_settings_summoners_and_champion_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RIOT_KEY", "from-env")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  platform: "kr"
  cluster: "asia"
  api_key_env: "TEST_RIOT_KEY"
  record: true
  wait_for_match: false
  request_timeout_seconds: 7
  recording:
    width: 1280
    height: 720
    frames_per_second: 30
summoners:
  - name: "Rascal#1231"
    champions: ["Gnar", "Aatrox"]
  - name: "Hide on bush#KR1"
    champions: ["Locke"]
  - name: "NoChampions#NA1"
    champions: []
  - name: "MissingTag"
    champions: ["Gnar"]
""",
        encoding="utf-8",
    )

    config = riot_spectate.load_config(config_path)
    settings = config["settings"]
    targets = config["targets"]

    assert settings["platform"] == "kr"
    assert settings["cluster"] == "asia"
    assert settings["api_key"] == "from-env"
    assert settings["record"]
    assert not settings["wait_for_match"]
    assert settings["request_timeout_seconds"] == 7
    assert settings["recording"]["width"] == 1280
    assert settings["recording"]["height"] == 720
    assert settings["recording"]["frames_per_second"] == 30
    assert [
        (target["game_name"], target["tag_line"], target["champions"])
        for target in targets
    ] == [
        ("Rascal", "1231", ["Gnar", "Aatrox"]),
        ("Hide on bush", "KR1", ["Locke"]),
    ]
    assert targets[0]["champion_names"] == {"gnar", "aatrox"}


def test_champion_allowed_matches_by_configured_champion_name():
    target = {"champion_ids": set(), "champion_names": {"gnar"}}

    assert riot_spectate.champion_allowed(
        {"championId": 150},
        target,
        {"gnar": 150},
    )
    assert not riot_spectate.champion_allowed(
        {"championId": 266},
        target,
        {"gnar": 150},
    )


def test_champion_allowed_matches_numeric_configured_champion_id():
    target = {"champion_ids": {150}, "champion_names": {"150"}}

    assert riot_spectate.champion_allowed({"championId": 150}, target, {})
    assert not riot_spectate.champion_allowed({"championId": 266}, target, {})


def test_find_target_participant_returns_matching_puuid():
    game_data = {
        "participants": [
            {"puuid": "other", "championId": 266},
            {"puuid": "target", "championId": 150},
        ]
    }

    assert riot_spectate.find_target_participant(game_data, "target") == {
        "puuid": "target",
        "championId": 150,
    }
    assert riot_spectate.find_target_participant(game_data, "missing") is None


def test_applescript_list_quotes_process_names():
    assert (
        riot_spectate.applescript_list(["LeagueofLegends", "League of Legends"])
        == '{"LeagueofLegends", "League of Legends"}'
    )


def test_wait_for_configured_match_returns_first_live_allowed_champion(monkeypatch):
    settings = riot_spectate.normalize_settings({"request_timeout_seconds": 7, "wait_for_match": False})
    monkeypatch.setattr(riot_spectate, "fetch_champion_id_map", lambda session, settings: {"gnar": 150})

    game_data = {
        "gameId": 123,
        "participants": [
            {"puuid": "target-puuid", "championId": 150, "riotId": "Rascal#1231"}
        ],
    }
    session = FakeSession(
        [
            FakeResponse(404),
            FakeResponse(200, game_data),
        ],
        expected_timeout=7,
    )
    targets = [
        {
            "game_name": "Offline",
            "tag_line": "NA1",
            "summoner_id": "offline-id",
            "puuid": "offline-puuid",
            "champion_names": {"gnar"},
            "champion_ids": set(),
        },
        {
            "game_name": "Rascal",
            "tag_line": "1231",
            "summoner_id": "target-id",
            "puuid": "target-puuid",
            "champion_names": {"gnar"},
            "champion_ids": set(),
        },
    ]

    match = riot_spectate.wait_for_configured_match(
        session=session,
        headers={"X-Riot-Token": "key"},
        targets=targets,
        settings=settings,
    )

    assert match == (game_data, targets[1], game_data["participants"][0])


def test_wait_for_configured_match_skips_live_disallowed_champion(monkeypatch):
    settings = riot_spectate.normalize_settings({"request_timeout_seconds": 7, "wait_for_match": False})
    monkeypatch.setattr(riot_spectate, "fetch_champion_id_map", lambda session, settings: {"gnar": 150})

    game_data = {
        "gameId": 123,
        "participants": [
            {"puuid": "target-puuid", "championId": 266, "riotId": "Rascal#1231"}
        ],
    }
    session = FakeSession([FakeResponse(200, game_data)], expected_timeout=7)
    targets = [
        {
            "game_name": "Rascal",
            "tag_line": "1231",
            "summoner_id": "target-id",
            "puuid": "target-puuid",
            "champion_names": {"gnar"},
            "champion_ids": set(),
        }
    ]

    assert (
        riot_spectate.wait_for_configured_match(
            session=session,
            headers={"X-Riot-Token": "key"},
            targets=targets,
            settings=settings,
        )
        is None
    )


def test_start_recording_uses_configured_recording_settings(monkeypatch, tmp_path):
    captured = {}
    settings = riot_spectate.normalize_settings(
        {
            "request_timeout_seconds": 9,
            "recording": {
                "width": 1280,
                "height": 720,
                "frames_per_second": 30,
                "lossless": True,
                "enforce_frame_rate": False,
                "replay_speed": 0.5,
                "start_offset_seconds": 4.0,
                "end_time": 123.0,
            },
        }
    )

    def fake_local_api_get(session, path, timeout_seconds):
        assert path == "/replay/recording"
        assert timeout_seconds == 9
        return FakeResponse(200, {"codec": "webm", "currentTime": 10.0})

    def fake_local_api_post(session, path, body, timeout_seconds):
        assert path == "/replay/recording"
        assert timeout_seconds == 9
        captured.update(body)
        response = FakeResponse(200)
        response.raise_for_status = lambda: None
        return response

    monkeypatch.setattr(riot_spectate, "local_api_get", fake_local_api_get)
    monkeypatch.setattr(riot_spectate, "local_api_post", fake_local_api_post)

    output_path = tmp_path / "recording.webm"
    riot_spectate.start_recording(object(), output_path, settings)

    assert captured == {
        "codec": "webm",
        "path": str(output_path),
        "width": 1280,
        "height": 720,
        "framesPerSecond": 30,
        "lossless": True,
        "enforceFrameRate": False,
        "replaySpeed": 0.5,
        "startTime": 6.0,
        "endTime": 123.0,
    }
