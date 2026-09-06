import pyautogui
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from pynput.keyboard import Key, Controller

import requests


def riot_get(session: requests.Session, url: str, timeout_seconds: int | float) -> requests.Response:
    try:
        response = session.get(url, timeout=timeout_seconds)
    except requests.exceptions.ConnectionError as e:
        print(f"Connection failed permanently: {e}")
        return None
    except requests.exceptions.ReadTimeout as e:
        print(f"Read timed out. {e}")
        return None
    return response


def print_json(label: str, payload: dict) -> None:
    print(label)
    print(json.dumps(payload, indent=2, sort_keys=True))


def local_api_get(session: requests.Session, path: str, timeout_seconds: int | float) -> requests.Response:
    return session.get(f"https://127.0.0.1:2999{path}", timeout=timeout_seconds, verify=False)


def local_api_post(session: requests.Session, path: str, body: dict, timeout_seconds: int | float) -> requests.Response:
    return session.post(f"https://127.0.0.1:2999{path}", json=body, timeout=timeout_seconds, verify=False)


def resolve_league_binary() -> tuple[Path, Path]:
    primary = Path("/Applications/League of Legends.app/Contents/LoL/Game/LeagueofLegends.app/Contents/MacOS/LeagueofLegends")
    fallback = Path("/Applications/League of Legends.app/Contents/LoL/RADS/solutions/lol_game_client_sln/releases")

    if primary.exists():
        return primary, primary.parents[3]

    if fallback.exists():
        releases = sorted(
            [path for path in fallback.iterdir() if path.is_dir()],
            reverse=True,
        )
        for release_dir in releases:
            candidate = release_dir / "deploy/LeagueofLegends.app/Contents/MacOS/LeagueofLegends"
            if candidate.exists():
                return candidate, release_dir / "deploy"

    raise FileNotFoundError("Could not find the League of Legends client binary.")


def launch_spectate(binary: Path, cwd: Path, game_id: int, platform_id: str, encryption_key: str) -> subprocess.Popen:
    spectator_host = f"spectator.{platform_id.lower()}.lol.pvp.net:8080"
    launch_args = [
        f"spectator {spectator_host} {encryption_key} {game_id} {platform_id}",
        "-UseRads",
        "-GameBaseDir=..",
    ]
    print()
    print("Launching League...")
    print("Binary:", binary)
    print("Args:", " ".join(launch_args))
    return subprocess.Popen([str(binary), *launch_args], cwd=str(cwd))


def wait_for_local_api(settings: dict) -> requests.Session:
    session = requests.Session()
    deadline = time.time() + settings["local_api_timeout_seconds"]

    while time.time() < deadline:
        try:
            response = local_api_get(session, "/swagger/v3/openapi.json", settings["request_timeout_seconds"])
            response1 = local_api_get(session, "/liveclientdata/playerlist", settings["request_timeout_seconds"])
            print(response, response1)
            if response.status_code == 200 and response1.status_code == 200:
                print(response1.json())
                if response1.json():
                    print("Found non-empty return in playerlist, proceeding...")
                    return session
        except requests.RequestException:
            pass
        time.sleep(settings["local_api_poll_seconds"])

    raise TimeoutError("Timed out waiting for the League replay API to become available.")


def find_target_participant(game_data: dict, target_puuid: str) -> dict | None:
    for participant in game_data.get("participants", []):
        if participant.get("puuid") == target_puuid:
            return participant
    return None


def normalize_champion_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_config_value(value: str):
    value = value.strip()
    if value in {"", "null", "None"}:
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value.strip('"').strip("'")


def parse_simple_config_yaml(path: Path) -> dict:
    config = {"settings": {}, "summoners": []}
    current_summoner = None
    current_section = None
    current_settings_group = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0 and line.endswith(":"):
            current_section = line[:-1]
            current_settings_group = None
            continue

        if current_section == "settings":
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if indent == 2 and not value:
                config["settings"][key] = {}
                current_settings_group = key
            elif indent >= 4 and current_settings_group:
                config["settings"][current_settings_group][key] = parse_config_value(value)
            else:
                config["settings"][key] = parse_config_value(value)
                current_settings_group = None
        elif current_section == "summoners" and line.startswith("- name:"):
            value = parse_config_value(line.split(":", 1)[1])
            current_summoner = {"name": value, "champions": []}
            config["summoners"].append(current_summoner)
        elif current_section == "summoners" and line.startswith("champions:") and current_summoner is not None:
            current_summoner["champions"] = parse_config_value(line.split(":", 1)[1])

    return config


def load_raw_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return parse_simple_config_yaml(path)

    with path.open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)

    return data if isinstance(data, dict) else {}


def config_string(settings: dict, key: str, fallback: str | None = None) -> str | None:
    value = settings.get(key, fallback)
    if value is None:
        return None
    return str(value).strip()


def config_bool(settings: dict, key: str, fallback: bool) -> bool:
    value = settings.get(key, fallback)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_recording_settings(settings: dict) -> dict:
    recording = settings.get("recording", {})
    if not isinstance(recording, dict):
        recording = {}
    return {
        "width": int(recording.get("width", 1920)),
        "height": int(recording.get("height", 1080)),
        "frames_per_second": int(recording.get("frames_per_second", 60)),
        "lossless": config_bool(recording, "lossless", False),
        "enforce_frame_rate": config_bool(recording, "enforce_frame_rate", True),
        "replay_speed": float(recording.get("replay_speed", 1.0)),
        "start_offset_seconds": float(recording.get("start_offset_seconds", 2.0)),
        "end_time": float(recording.get("end_time", -1.0)),
    }


def normalize_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        settings = {}

    api_key = config_string(settings, "api_key")
    api_key_env = config_string(settings, "api_key_env", "RIOT_API_KEY")
    if not api_key and api_key_env:
        api_key = os.getenv(api_key_env, "").strip()

    return {
        "platform": config_string(settings, "platform", "kr").lower(),
        "cluster": config_string(settings, "cluster", "asia").lower(),
        "api_key": api_key or "",
        "dry_run": config_bool(settings, "dry_run", False),
        "record": config_bool(settings, "record", True),
        "wait_for_match": config_bool(settings, "wait_for_match", True),
        "num_games": int(settings.get("num_games", 1)),
        "output": config_string(settings, "output"),
        "league_client_startup_attempts": int(settings.get("league_client_startup_attempts", 5)),
        "league_process_names": settings.get("league_process_names", ["LeagueofLegends", "League of Legends"]),
        "request_timeout_seconds": float(settings.get("request_timeout_seconds", 15)),
        "local_api_timeout_seconds": float(settings.get("local_api_timeout_seconds", 50)),
        "local_api_poll_seconds": float(settings.get("local_api_poll_seconds", 2)),
        "wait_poll_seconds": float(settings.get("wait_poll_seconds", 10)),
        "spectator_error_backoff_seconds": float(settings.get("spectator_error_backoff_seconds", 30)),
        "recording_check_interval_seconds": float(settings.get("recording_check_interval_seconds", 5)),
        "recording_timeout_seconds": float(settings.get("recording_timeout_seconds", 7200)),
        "playback_finish_threshold_seconds": float(settings.get("playback_finish_threshold_seconds", 1)),
        "playback_finish_confirmation_seconds": float(
            settings.get("playback_finish_confirmation_seconds", 5)
        ),
        "window_focus_delay_seconds": float(settings.get("window_focus_delay_seconds", 1.0)),
        "rune_tab_key": config_string(settings, "rune_tab_key", "c"),
        "rune_tab_key_hold_seconds": float(settings.get("rune_tab_key_hold_seconds", 0.1)),
        "fog_hotkey_hold_seconds": float(settings.get("fog_hotkey_hold_seconds", 0.10)),
        "camera_hotkey_hold_seconds": float(settings.get("camera_hotkey_hold_seconds", 0.08)),
        "camera_hotkey_between_presses_seconds": float(
            settings.get("camera_hotkey_between_presses_seconds", 0.12)
        ),
        "recording": normalize_recording_settings(settings),
    }


def normalize_targets(summoners: list) -> list[dict]:
    targets = []
    for entry in summoners:
        if not isinstance(entry, dict):
            continue
        riot_id = str(entry.get("name", "")).strip()
        if "#" not in riot_id:
            print(f"Skipping config entry without Name#Tag: {riot_id}")
            continue
        champions = entry.get("champions", [])
        if isinstance(champions, str):
            champions = [champions]
        champions = [str(champion).strip() for champion in champions if str(champion).strip()]
        if not champions:
            print(f"Skipping {riot_id}: no champions configured.")
            continue
        game_name, tag_line = riot_id.split("#", 1)
        targets.append(
            {
                "game_name": game_name.strip(),
                "tag_line": tag_line.strip(),
                "champions": champions,
                "champion_names": {normalize_champion_name(champion) for champion in champions},
                "champion_ids": {int(champion) for champion in champions if str(champion).isdigit()},
            }
        )

    return targets


def load_config(path: Path) -> dict:
    data = load_raw_config(path)
    summoners = data.get("summoners", []) if isinstance(data, dict) else []
    return {
        "settings": normalize_settings(data.get("settings", {})),
        "targets": normalize_targets(summoners),
    }


def fetch_champion_id_map(session: requests.Session, settings: dict) -> dict[str, int]:
    versions_res = session.get(
        "https://ddragon.leagueoflegends.com/api/versions.json",
        timeout=settings["request_timeout_seconds"],
    )
    versions_res.raise_for_status()
    latest_version = versions_res.json()[0]
    champions_res = session.get(
        f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/champion.json",
        timeout=settings["request_timeout_seconds"],
    )
    champions_res.raise_for_status()
    champion_data = champions_res.json()["data"]

    champion_ids = {}
    for champion in champion_data.values():
        champion_id = int(champion["key"])
        champion_ids[normalize_champion_name(champion["id"])] = champion_id
        champion_ids[normalize_champion_name(champion["name"])] = champion_id
    return champion_ids


def champion_allowed(participant: dict, target: dict, champion_id_map: dict[str, int]) -> bool:
    champion_id = participant.get("championId")
    if champion_id in target["champion_ids"]:
        return True

    allowed_ids = {
        champion_id_map[name]
        for name in target["champion_names"]
        if name in champion_id_map
    }
    if allowed_ids:
        return champion_id in allowed_ids

    champion_name = participant.get("championName") or participant.get("champion")
    return champion_name and normalize_champion_name(champion_name) in target["champion_names"]


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "recording"


def build_recording_output_path(game_name: str, tag_line: str, game_id: int, override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()

    recordings_dir = Path.cwd() / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    slug = sanitize_filename(f"{game_name}-{tag_line}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (recordings_dir / f"{slug}-{game_id}-{stamp}.webm").resolve()


def selection_name_for_participant(participant: dict) -> str:
    riot_id_game_name = participant.get("riotIdGameName")
    if riot_id_game_name:
        return riot_id_game_name

    riot_id = participant.get("riotId", "")
    if "#" in riot_id:
        return riot_id.split("#", 1)[0]

    return riot_id

def get_player_hotkey(session: requests.Session, target_selection_name: str, settings: dict) -> str | None:
    """
    Queries the local Live Client Data API.
    Sorts players strictly by position to guarantee exact hotkey assignment.
    
    Accepts target_selection_name (e.g., "Burdol" or "DK Solid").
    """
    try:
        # Request the live dataset directly from the running game engine
        response = local_api_get(session, "/liveclientdata/playerlist", settings["request_timeout_seconds"])
        if response.status_code != 200:
            print("Warning: Live Client Data API returned non-200 code.")
            return None
            
        player_list = response.json()
        
        # Standard tactical role priority layout
        role_order = {
            "TOP": 1,
            "JUNGLE": 2,
            "MIDDLE": 3,
            "BOTTOM": 4,
            "UTILITY": 5
        }
        red_team_overrides = ["q", "w", "e", "r", "t"]
        
        blue_team = []
        red_team = []
        
        for p in player_list:
            team_string = p.get("team", "").upper()
            role = p.get("position", "").upper()
            weight = role_order.get(role, 99)
            
            # Standardize name checking values
            riot_id_game_name = p.get("riotIdGameName", "")
            summoner_name_full = p.get("summonerName", "")
            summoner_name_base = summoner_name_full.split("#")[0] if "#" in summoner_name_full else summoner_name_full
            
            player_info = {
                "game_name": riot_id_game_name,
                "base_name": summoner_name_base,
                "weight": weight
            }
            
            # "ORDER" maps to Blue Side. "CHAOS" maps to Red Side.
            if team_string == "ORDER":
                blue_team.append(player_info)
            elif team_string == "CHAOS":
                red_team.append(player_info)
                
        # Sort both teams linearly to match the standard UI scoreboard layout
        blue_team.sort(key=lambda x: x["weight"])
        red_team.sort(key=lambda x: x["weight"])
        
        print(blue_team)
        print(red_team)        

        # 1. Search the sorted Blue side list
        for idx, player in enumerate(blue_team):
            if target_selection_name in (player["game_name"], player["base_name"]):
                return f"{idx + 1}", "blue"
                
        # 2. Search the sorted Red side list
        for idx, player in enumerate(red_team):
            if target_selection_name in (player["game_name"], player["base_name"]):
                return f"{red_team_overrides[idx]}", "red"
                
    except Exception as e:
        print(f"Error parsing local scoreboard payload: {e}")
        
    return None, None


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript_list(values: list[str]) -> str:
    return "{" + ", ".join(applescript_string(value) for value in values) + "}"


def focus_league_window(settings: dict) -> None:
    process_names = settings["league_process_names"]
    if isinstance(process_names, str):
        process_names = [process_names]

    script = f"""
    tell application "System Events"
        set procList to name of every process
        repeat with procName in {applescript_list(process_names)}
            if procList contains (procName as text) then
                set frontmost of process (procName as text) to true
                exit repeat
            end if
        end repeat
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False)
    time.sleep(settings["window_focus_delay_seconds"]) # Let the window focus fully


def apply_runes_tab(settings: dict) -> None:
    keyboard = Controller()

    focus_league_window(settings)
    
    # Inside your function where you want to press 'q':
    keyboard.press(settings["rune_tab_key"])
    time.sleep(settings["rune_tab_key_hold_seconds"])
    keyboard.release(settings["rune_tab_key"])


def apply_scoreboard(session: requests.Session, settings: dict) -> None:
    """
    Adds the scoreboard to the UI.
    """
    render_body = {
        "interfaceScoreboard": True
    }
    try:
        local_api_post(session, "/replay/render", render_body, settings["request_timeout_seconds"])
    except Exception as e:
        print(f"Warning: Render endpoint update failed: {e}")


def apply_fog_of_war_perspective(perspective: str, settings: dict, use_hotkey: bool = True) -> None:
    """
    Changes Fog of War perspective.
    Allowed perspectives: "Blue" (F1 hotkey) or "Red" (F2 hotkey).
    Uses Mac keys to set the team.
    """
    if perspective not in ["blue", "red"]:
        raise ValueError("Perspective must be either 'blue' or 'red'")

    # 2. Handle the team perspective via AppleScript and Mac key emulation
    if use_hotkey:
        # Determine the correct key mapping (F1 for Blue, F2 for Red)
        hotkey = "f1" if perspective == "blue" else "f2"
        print(f"Targeting active League window and pressing hotkey '{hotkey}' for {perspective} team POV...")
        
        focus_league_window(settings)
        
        # Execute a clean, single tap for the function key
        pyautogui.keyDown(hotkey)
        time.sleep(settings["fog_hotkey_hold_seconds"])
        pyautogui.keyUp(hotkey)
        
        print(f"Fog of War successfully switched to {perspective} team perspective via {hotkey}.")
    else:
        print("Hotkey simulation bypassed. Keeping default API Fog setting.")

def apply_camera_lock(session: requests.Session, selection_name: str, settings: dict, hotkey: str | None) -> None:
    # 1. Force the API engine into top isometric mode so fluid physics work
    render_body = {
        "cameraMode": "top",
        "selectionName": selection_name
    }
    try:
        local_api_post(session, "/replay/render", render_body, settings["request_timeout_seconds"])
    except Exception as e:
        print(f"Warning: Render endpoint update failed: {e}")

    # 2. Handle the fluid tracking system via AppleScript and Mac key emulation
    if hotkey:
        print(f"Targeting active League window and double-pressing hotkey '{hotkey}'...")
        
        focus_league_window(settings)
        
        # Execute the double-tap sequence that worked in testing
        pyautogui.keyDown(hotkey)
        time.sleep(settings["camera_hotkey_hold_seconds"])
        pyautogui.keyUp(hotkey)
        
        time.sleep(settings["camera_hotkey_between_presses_seconds"])
        
        pyautogui.keyDown(hotkey)
        time.sleep(settings["camera_hotkey_hold_seconds"])
        pyautogui.keyUp(hotkey)
        print(f"Camera fluidly locked to: {selection_name} via hotkey shortcut.")
    else:
        print(f"Could not camera-lock to {selection_name}: no hotkey available.")


def start_recording(session: requests.Session, output_path: Path, settings: dict) -> None:
    recording_state = local_api_get(session, "/replay/recording", settings["request_timeout_seconds"]).json()
    recording_settings = settings["recording"]
    body = {
        "codec": recording_state.get("codec", "webm"),
        "path": str(output_path),
        "width": recording_settings["width"],
        "height": recording_settings["height"],
        "framesPerSecond": recording_settings["frames_per_second"],
        "lossless": recording_settings["lossless"],
        "enforceFrameRate": recording_settings["enforce_frame_rate"],
        "replaySpeed": recording_settings["replay_speed"],
        "startTime": max(
            0.0,
            recording_state.get("currentTime", 0.0) - recording_settings["start_offset_seconds"],
        ),
        "endTime": recording_settings["end_time"],
    }
    response = local_api_post(session, "/replay/recording", body, settings["request_timeout_seconds"])
    response.raise_for_status()
    print(f"Recording started: {output_path}")

def is_playback_finished(session: requests.Session, settings: dict, finish_state: dict | None = None) -> bool:
    response_json = local_api_get(session, "/replay/playback", settings["request_timeout_seconds"]).json()
    end_time = response_json["length"]
    game_time = response_json["time"]

    is_near_end = abs(end_time - game_time) < settings["playback_finish_threshold_seconds"]
    if not is_near_end:
        if finish_state is not None:
            finish_state.pop("near_end_started_at", None)
        print(f"Game Length: {end_time}, Game Time: {game_time}")
        return False

    now = time.monotonic()
    confirmation_seconds = settings["playback_finish_confirmation_seconds"]
    if finish_state is None:
        near_end_seconds = confirmation_seconds
    else:
        near_end_started_at = finish_state.setdefault("near_end_started_at", now)
        near_end_seconds = now - near_end_started_at

    print(
        f"Game Length: {end_time}, Game Time: {game_time}, "
        f"Near End Seconds: {near_end_seconds:.1f}"
    )
    if near_end_seconds >= confirmation_seconds:
        return True
    return False

def wait_for_recording_to_finish(session: requests.Session, output_path: Path, settings: dict) -> None:
    deadline = time.time() + settings["recording_timeout_seconds"]
    last_state = None
    playback_finish_state = {}
    while time.time() < deadline:
        print("Waiting for playback to complete.")
        state = local_api_get(session, "/replay/recording", settings["request_timeout_seconds"]).json()
        last_state = state
        if state.get("recording") is False and state.get("path"):
            print(f"Recording finished: {state['path']}")
            return
        if is_playback_finished(session, settings, playback_finish_state):
            print("Playback API determined game is over.")
            return
        print(
            "Recording status: "
            f"recording={state.get('recording')} "
            f"currentTime={state.get('currentTime')} "
            f"path={state.get('path', '')}"
        )
        time.sleep(settings["recording_check_interval_seconds"])

    raise TimeoutError(f"Timed out waiting for recording to finish. Last state: {last_state}")

def wait_for_match_to_begin(session: requests.Session, headers: dict, spectator_url: str, settings: dict):
    # wait for a match if 
    while True:
        spec_res = riot_get(session, spectator_url, settings["request_timeout_seconds"])
        if spec_res is None:
            session = requests.Session()
            session.headers.update(headers)
        elif spec_res.status_code == 404:
            print("Player is not in a live game.")
            # Dont wait if not explicitly set
            if not settings["wait_for_match"]:
                break
            print("Waiting...")
            time.sleep(settings["wait_poll_seconds"])
        elif spec_res.status_code != 200:
            print(
                f"Spectator lookup failed: {spec_res.status_code}\n"
                f"{spec_res.text.strip()}"
            )
            time.sleep(settings["spectator_error_backoff_seconds"])
        elif spec_res.status_code == 200:
            print("Found a match.")
            break

    return spec_res


def lookup_target(
    session: requests.Session,
    settings: dict,
    cluster: str,
    platform: str,
    game_name: str,
    tag_line: str,
) -> dict | None:
    account_url = (
        f"https://{cluster}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(game_name)}/{quote(tag_line)}"
    )
    account_res = riot_get(session, account_url, settings["request_timeout_seconds"])
    if account_res is None or account_res.status_code != 200:
        status = getattr(account_res, "status_code", "no response")
        body = account_res.text.strip() if account_res is not None else ""
        print(f"Account lookup failed for {game_name}#{tag_line}: {status}\n{body}")
        return None

    account_data = account_res.json()
    puuid = account_data["puuid"]
    print(f"Found player: {account_data['gameName']}#{account_data['tagLine']}")
    print(f"PUUID: {puuid}")

    summoner_url = f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{quote(puuid)}"
    summoner_res = riot_get(session, summoner_url, settings["request_timeout_seconds"])
    if summoner_res is None or summoner_res.status_code != 200:
        status = getattr(summoner_res, "status_code", "no response")
        body = summoner_res.text.strip() if summoner_res is not None else ""
        print(f"Summoner lookup failed for {game_name}#{tag_line}: {status}\n{body}")
        return None

    summoner_data = summoner_res.json()
    summoner_id = summoner_data.get("id") or summoner_data.get("summonerId")

    if not summoner_id:
        print("Summoner lookup did not return an id or summonerId.")
        print("Using the PUUID directly for the spectator lookup instead.")
        summoner_id = puuid

    if summoner_data.get("id") is None and summoner_data.get("summonerId") is None:
        print_json("Summoner payload:", summoner_data)
    print(f"Summoner ID: {summoner_id}")

    return {
        "game_name": game_name,
        "tag_line": tag_line,
        "puuid": puuid,
        "summoner_id": summoner_id,
    }


def wait_for_configured_match(
    session: requests.Session,
    headers: dict,
    targets: list[dict],
    settings: dict,
    ignored_game_ids: set[int] | None = None,
) -> tuple[dict, dict, dict] | None:
    ignored_game_ids = ignored_game_ids or set()
    champion_id_map = fetch_champion_id_map(session, settings)

    unresolved = sorted(
        {
            champion
            for target in targets
            for champion in target["champion_names"]
            if champion not in champion_id_map and not champion.isdigit()
        }
    )
    if unresolved:
        print(f"Warning: could not resolve champion names: {', '.join(unresolved)}")

    while True:
        for target in targets:
            spectator_url = (
                f"https://{settings['platform']}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/"
                f"{quote(target['summoner_id'])}"
            )
            spec_res = riot_get(session, spectator_url, settings["request_timeout_seconds"])
            if spec_res is None:
                session = requests.Session()
                session.headers.update(headers)
                continue
            if spec_res.status_code == 404:
                print(f"{target['game_name']}#{target['tag_line']} is not in a live game.")
                continue
            if spec_res.status_code != 200:
                print(
                    f"Spectator lookup failed for {target['game_name']}#{target['tag_line']}: "
                    f"{spec_res.status_code}\n{spec_res.text.strip()}"
                )
                continue

            game_data = spec_res.json()
            game_id = game_data.get("gameId")
            if game_id in ignored_game_ids:
                print(
                    f"Skipping already watched game {game_id} for "
                    f"{target['game_name']}#{target['tag_line']}."
                )
                continue

            target_participant = find_target_participant(game_data, target["puuid"])
            if not target_participant:
                print(f"Found a match for {target['game_name']}#{target['tag_line']}, but not their participant data.")
                continue
            if not champion_allowed(target_participant, target, champion_id_map):
                print(
                    f"Found {target['game_name']}#{target['tag_line']} in a live game, "
                    f"but championId {target_participant.get('championId')} is not in their configured champions."
                )
                continue

            print(f"Found a matching live game for {target['game_name']}#{target['tag_line']}.")
            return game_data, target, target_participant

        if not settings["wait_for_match"]:
            return None

        print("No configured player is in a matching live game. Waiting...")
        time.sleep(settings["wait_poll_seconds"])


def spectate_match(
    game_data: dict,
    game_name: str,
    tag_line: str,
    puuid: str,
    settings: dict,
    target_participant: dict | None = None,
) -> None:
    print_json("Live game payload:", game_data)

    game_id = game_data["gameId"]
    encryption_key = game_data["observers"]["encryptionKey"]
    platform_id = game_data["platformId"]
    binary, cwd = resolve_league_binary()
    spectator_host = f"spectator.{platform_id.lower()}.lol.pvp.net:8080"
    launch_command = (
        f'"{binary}" '
        f'"spectator {spectator_host} {encryption_key} {game_id} {platform_id}" '
        f'"-UseRads" "-GameBaseDir=.."'
    )

    print()
    print("Spectate command:")
    print(launch_command)

    if settings["dry_run"]:
        return

    attempt = 0
    while attempt < settings["league_client_startup_attempts"]:
        process = launch_spectate(
            binary=binary,
            cwd=cwd,
            game_id=game_id,
            platform_id=platform_id,
            encryption_key=encryption_key,
        )

        try:
            local_session = wait_for_local_api(settings)
        except TimeoutError as exc:
            print(exc)
            process.terminate()
        else:
            break

        attempt += 1
    else:
        print("Unable to successfully start spectate. Quitting.")
        return

    if target_participant is None:
        target_participant = find_target_participant(game_data, puuid)

    target_selection_name = None
    player_hotkey = None
    team = None
    if target_participant:
        target_selection_name = selection_name_for_participant(target_participant)
        player_hotkey, team = get_player_hotkey(local_session, target_selection_name, settings)
    if team:
        apply_fog_of_war_perspective(team, settings)
    else:
        print("Could not determine team perspective for the target player.")
    apply_scoreboard(local_session, settings)
    if target_participant and target_participant.get("riotId") and target_selection_name:
        apply_camera_lock(
            local_session,
            target_selection_name,
            settings,
            player_hotkey,
        )
    else:
        print("Could not find a matching participant to lock the camera to.")
    apply_runes_tab(settings)

    if not settings["record"]:
        return

    output_path = build_recording_output_path(game_name, tag_line, game_id, settings["output"])
    start_recording(local_session, output_path, settings)
    if target_participant and target_participant.get("riotId") and target_selection_name:
        apply_camera_lock(
            local_session,
            target_selection_name,
            settings,
            player_hotkey,
        )
    else:
        print("Could not find a matching participant to lock the camera to.")
    wait_for_recording_to_finish(local_session, output_path, settings)
    process.terminate()


def run_from_config(config_path: Path) -> None:
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    settings = config["settings"]
    num_games = settings["num_games"]
    if num_games < 1:
        print("settings.num_games must be at least 1.")
        sys.exit(1)

    config_targets = config["targets"]
    if not config_targets:
        print(f"No valid summoners found in config: {config_path}")
        sys.exit(1)

    if not settings["api_key"]:
        print("Missing Riot API key. Set the configured api_key_env or api_key in config.yaml.")
        sys.exit(1)

    headers = {"X-Riot-Token": settings["api_key"]}
    session = requests.Session()
    session.headers.update(headers)

    targets = []
    for config_target in config_targets:
        target = lookup_target(
            session=session,
            settings=settings,
            cluster=settings["cluster"],
            platform=settings["platform"],
            game_name=config_target["game_name"],
            tag_line=config_target["tag_line"],
        )
        if not target:
            continue
        target.update(config_target)
        targets.append(target)

    if not targets:
        print("Could not resolve any configured summoners.")
        sys.exit(1)

    watched_game_ids = set()
    for game_number in range(1, num_games + 1):
        print(f"Waiting for matching game {game_number} of {num_games}.")
        match = wait_for_configured_match(
            session=session,
            headers=headers,
            targets=targets,
            settings=settings,
            ignored_game_ids=watched_game_ids,
        )
        if not match:
            print("Didn't find a matching configured live game, exit.")
            return

        game_data, target, target_participant = match
        watched_game_ids.add(game_data["gameId"])
        spectate_match(
            game_data=game_data,
            game_name=target["game_name"],
            tag_line=target["tag_line"],
            puuid=target["puuid"],
            settings=settings,
            target_participant=target_participant,
        )
if __name__ == "__main__":
    run_from_config(Path("config.yaml"))
