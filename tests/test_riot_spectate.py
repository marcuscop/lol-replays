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
  num_games: 2
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
    assert settings["num_games"] == 2
    assert settings["request_timeout_seconds"] == 7
    assert settings["recording"]["width"] == 1280
    assert settings["recording"]["height"] == 720
    assert settings["recording"]["frames_per_second"] == 30
    assert not settings["display_mode"]["enabled"]
    assert settings["display_mode"]["resolution"] == "1920x1200"
    assert not settings["league_game_config"]["enabled"]
    assert settings["league_game_config"]["width"] == 1920
    assert settings["league_game_config"]["height"] == 1080
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


def test_normalize_settings_defaults_num_games_to_one():
    assert riot_spectate.normalize_settings({})["num_games"] == 1


def test_run_from_config_spectates_configured_number_of_games(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("settings: {}\nsummoners: []\n", encoding="utf-8")
    settings = riot_spectate.normalize_settings(
        {"api_key": "key", "wait_for_match": False, "num_games": 2}
    )
    config_target = {
        "game_name": "Rascal",
        "tag_line": "1231",
        "champions": ["Gnar"],
        "champion_names": {"gnar"},
        "champion_ids": set(),
    }
    resolved_target = {
        "game_name": "Rascal",
        "tag_line": "1231",
        "summoner_id": "target-id",
        "puuid": "target-puuid",
    }
    participant = {"puuid": "target-puuid", "championId": 150, "riotId": "Rascal#1231"}
    games = [
        {"gameId": 111, "participants": [participant]},
        {"gameId": 222, "participants": [participant]},
    ]
    ignored_game_ids_seen = []
    spectated_game_ids = []

    monkeypatch.setattr(
        riot_spectate,
        "load_config",
        lambda path: {"settings": settings, "targets": [config_target]},
    )
    monkeypatch.setattr(
        riot_spectate,
        "lookup_target",
        lambda **kwargs: dict(resolved_target),
    )

    def fake_wait_for_configured_match(
        session,
        headers,
        targets,
        settings,
        ignored_game_ids=None,
    ):
        ignored_game_ids_seen.append(set(ignored_game_ids or set()))
        return games[len(ignored_game_ids_seen) - 1], targets[0], participant

    def fake_spectate_match(**kwargs):
        spectated_game_ids.append(kwargs["game_data"]["gameId"])

    monkeypatch.setattr(
        riot_spectate,
        "wait_for_configured_match",
        fake_wait_for_configured_match,
    )
    monkeypatch.setattr(riot_spectate, "spectate_match", fake_spectate_match)

    riot_spectate.run_from_config(config_path)

    assert ignored_game_ids_seen == [set(), {111}]
    assert spectated_game_ids == [111, 222]


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


def test_run_betterdisplay_display_mode_uses_configured_resolution(monkeypatch, tmp_path):
    betterdisplay = tmp_path / "BetterDisplay"
    betterdisplay.write_text("", encoding="utf-8")
    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, check=False, capture_output=False, text=False):
        calls.append(
            {
                "args": args,
                "check": check,
                "capture_output": capture_output,
                "text": text,
            }
        )
        return FakeResult()

    monkeypatch.setattr(riot_spectate.subprocess, "run", fake_run)
    monkeypatch.setattr(riot_spectate.time, "sleep", lambda seconds: None)

    settings = riot_spectate.normalize_settings(
        {
            "display_mode": {
                "enabled": True,
                "betterdisplay_path": str(betterdisplay),
                "selector": "-displaywithmainstatus",
                "resolution": "1920x1200",
                "hi_dpi": "on",
                "settle_seconds": 0.5,
            },
        }
    )

    riot_spectate.run_betterdisplay_display_mode(settings["display_mode"])

    assert calls == [
        {
            "args": [
                str(betterdisplay),
                "set",
                "-displaywithmainstatus",
                "-resolution=1920x1200",
                "-hidpi=on",
            ],
            "check": False,
            "capture_output": True,
            "text": True,
        }
    ]


def test_run_betterdisplay_display_mode_can_use_mode_number(monkeypatch, tmp_path):
    betterdisplay = tmp_path / "BetterDisplay"
    betterdisplay.write_text("", encoding="utf-8")
    captured_args = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        riot_spectate.subprocess,
        "run",
        lambda args, **kwargs: captured_args.append(args) or FakeResult(),
    )

    settings = riot_spectate.normalize_settings(
        {
            "display_mode": {
                "enabled": True,
                "betterdisplay_path": str(betterdisplay),
                "display_mode_number": 556,
                "settle_seconds": 0,
            },
        }
    )

    riot_spectate.run_betterdisplay_display_mode(settings["display_mode"])

    assert captured_args == [
        [
            str(betterdisplay),
            "set",
            "-displaywithmainstatus",
            "-displaymodenumber=556",
        ]
    ]


def test_write_league_game_config_updates_general_section(tmp_path, monkeypatch):
    monkeypatch.setattr(riot_spectate.time, "strftime", lambda fmt: "20260906120000")
    game_cfg = tmp_path / "game.cfg"
    game_cfg.write_text(
        """[General]
WindowMode=0
Height=1200
Width=1920
Other=1

[Performance]
ShadowQuality=2
""",
        encoding="utf-8",
    )
    settings = riot_spectate.normalize_settings(
        {
            "league_game_config": {
                "enabled": True,
                "path": str(game_cfg),
                "window_mode": 1,
                "width": 1920,
                "height": 1080,
                "backup": True,
            },
        }
    )

    riot_spectate.write_league_game_config(settings["league_game_config"])

    assert game_cfg.read_text(encoding="utf-8") == """[General]
WindowMode=1
Height=1080
Width=1920
Other=1

[Performance]
ShadowQuality=2
"""
    assert (tmp_path / "game.cfg.bak.20260906120000").read_text(encoding="utf-8") == """[General]
WindowMode=0
Height=1200
Width=1920
Other=1

[Performance]
ShadowQuality=2
"""


def test_is_playback_finished_requires_consecutive_near_end_seconds(monkeypatch):
    settings = riot_spectate.normalize_settings(
        {
            "request_timeout_seconds": 9,
            "playback_finish_threshold_seconds": 1,
            "playback_finish_confirmation_seconds": 5,
        }
    )
    playback_responses = [
        {"length": 100.0, "time": 99.5},
        {"length": 100.0, "time": 99.6},
        {"length": 100.0, "time": 99.7},
    ]
    monotonic_times = iter([10.0, 14.0, 15.1])

    def fake_local_api_get(session, path, timeout_seconds):
        assert path == "/replay/playback"
        assert timeout_seconds == 9
        return FakeResponse(200, playback_responses.pop(0))

    monkeypatch.setattr(riot_spectate, "local_api_get", fake_local_api_get)
    monkeypatch.setattr(riot_spectate.time, "monotonic", lambda: next(monotonic_times))

    finish_state = {}

    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
    assert riot_spectate.is_playback_finished(object(), settings, finish_state)


def test_is_playback_finished_resets_when_playback_moves_away_from_end(monkeypatch):
    settings = riot_spectate.normalize_settings(
        {
            "request_timeout_seconds": 9,
            "playback_finish_threshold_seconds": 1,
            "playback_finish_confirmation_seconds": 5,
        }
    )
    playback_responses = [
        {"length": 100.0, "time": 99.5},
        {"length": 100.0, "time": 97.0},
        {"length": 100.0, "time": 99.5},
        {"length": 100.0, "time": 99.6},
    ]
    monotonic_times = iter([10.0, 20.0, 24.0])

    def fake_local_api_get(session, path, timeout_seconds):
        assert path == "/replay/playback"
        assert timeout_seconds == 9
        return FakeResponse(200, playback_responses.pop(0))

    monkeypatch.setattr(riot_spectate, "local_api_get", fake_local_api_get)
    monkeypatch.setattr(riot_spectate.time, "monotonic", lambda: next(monotonic_times))

    finish_state = {}

    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
    assert not riot_spectate.is_playback_finished(object(), settings, finish_state)
