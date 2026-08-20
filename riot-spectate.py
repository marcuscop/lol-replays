import pyautogui
import argparse
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


DEFAULT_RIOT_ID = "Zven#S16XD"
DEFAULT_PLATFORM = "na1"
DEFAULT_CLUSTER = "americas"
DEFAULT_CAMERA_HEIGHT = 1500.0

MAX_LEAGUE_CLIENT_STARTUP_ATTEMPTS = 5


# GNAR ONE TRICK summoner = 유키라#키뭉이

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Look up Riot match data and, if the player is live, print a spectate command."
    )
    parser.add_argument(
        "riot_id",
        nargs="?",
        default=DEFAULT_RIOT_ID,
        help="Riot ID in the form GameName#TAG (example: Zven#S16XD)",
    )
    parser.add_argument(
        "--game-name",
        help="Game name part of the Riot ID. Overrides the positional riot_id if set.",
    )
    parser.add_argument(
        "--tag-line",
        help="Tag line part of the Riot ID. Overrides the positional riot_id if set.",
    )
    parser.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        help="LoL platform routing value, e.g. na1, euw1, kr",
    )
    parser.add_argument(
        "--cluster",
        default=DEFAULT_CLUSTER,
        help="Regional routing value for account lookup, e.g. americas, europe, asia",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("RIOT_API_KEY", "").strip(),
        help="Riot API key. Prefer setting RIOT_API_KEY in your shell.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the spectate command but do not launch League.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Start replay recording after spectate attaches and wait for the file to finish writing.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for a game to start from this summoner to record.",

    )
    parser.add_argument(
        "--output",
        help="Output path for --record. Defaults to ./recordings/<riot-id>-<game-id>.webm",
    )
    parser.add_argument(
        "--camera-height",
        type=float,
        default=DEFAULT_CAMERA_HEIGHT,
        help="Vertical offset for the follow camera when recording. Higher values look more top-down.",
    )
    parser.add_argument(
        "--summoner-id",
        help="Override the live-game lookup with a known encrypted summoner id.",
    )
    return parser


def riot_get(session: requests.Session, url: str) -> requests.Response:
    try:
        response = session.get(url, timeout=15)
    except requests.exceptions.ConnectionError as e:
        print(f"Connection failed permanently: {e}")
        return None
    return response


def print_json(label: str, payload: dict) -> None:
    print(label)
    print(json.dumps(payload, indent=2, sort_keys=True))


def local_api_get(session: requests.Session, path: str) -> requests.Response:
    return session.get(f"https://127.0.0.1:2999{path}", timeout=15, verify=False)


def local_api_post(session: requests.Session, path: str, body: dict) -> requests.Response:
    return session.post(f"https://127.0.0.1:2999{path}", json=body, timeout=15, verify=False)


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


def wait_for_local_api(timeout_seconds: int = 50) -> requests.Session:
    session = requests.Session()
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            response = local_api_get(session, "/swagger/v3/openapi.json")
            response1 = local_api_get(session, "/liveclientdata/playerlist")
            print(response, response1)
            if response.status_code == 200 and response1.status_code == 200:
                print(response1.json())
                if response1.json():
                    print("Found non-empty return in playerlist, proceeding...")
                    return session
        except requests.RequestException:
            pass
        time.sleep(2)

    raise TimeoutError("Timed out waiting for the League replay API to become available.")


def find_target_participant(game_data: dict, target_puuid: str) -> dict | None:
    for participant in game_data.get("participants", []):
        if participant.get("puuid") == target_puuid:
            return participant
    return None


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

def get_player_hotkey(session: requests.Session, target_selection_name: str) -> str | None:
    """
    Queries the local Live Client Data API.
    Sorts players strictly by position to guarantee exact hotkey assignment.
    
    Accepts target_selection_name (e.g., "Burdol" or "DK Solid").
    """
    try:
        # Request the live dataset directly from the running game engine
        response = local_api_get(session, "/liveclientdata/playerlist")
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

def apply_runes_tab() -> None:
    keyboard = Controller()

    # AppleScript to target the correct process
    applescript = """
    osascript -e '
    tell application "System Events"
        set procList to name of every process
        if procList contains "LeagueofLegends" then
            set frontmost of process "LeagueofLegends" to true
        else if procList contains "League of Legends" then
            set frontmost of process "League of Legends" to true
        end if
    end tell'
    """
    os.system(applescript)
    time.sleep(1.0) # Let the window focus fully
    
    # Inside your function where you want to press 'q':
    keyboard.press("c")
    time.sleep(0.1)
    keyboard.release("c")


def apply_scoreboard(session: requests.Session) -> None:
    """
    Adds the scoreboard to the UI.
    """
    render_body = {
        "interfaceScoreboard": True
    }
    try:
        local_api_post(session, "/replay/render", render_body)
    except Exception as e:
        print(f"Warning: Render endpoint update failed: {e}")


def apply_fog_of_war_perspective(perspective: str, use_hotkey: bool = True) -> None:
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
        
        # AppleScript to target the correct macOS process
        applescript = """
        osascript -e '
        tell application "System Events"
            set procList to name of every process
            if procList contains "LeagueofLegends" then
                set frontmost of process "LeagueofLegends" to true
            else if procList contains "League of Legends" then
                set frontmost of process "League of Legends" to true
            end if
        end tell'
        """
        os.system(applescript)
        time.sleep(1.0) # Let the window focus fully
        
        # Execute a clean, single tap for the function key
        pyautogui.keyDown(hotkey)
        time.sleep(0.10)
        pyautogui.keyUp(hotkey)
        
        print(f"Fog of War successfully switched to {perspective} team perspective via {hotkey}.")
    else:
        print("Hotkey simulation bypassed. Keeping default API Fog setting.")

def apply_camera_lock(session: requests.Session, selection_name: str, camera_height: float, hotkey: str | None) -> None:
    # 1. Force the API engine into top isometric mode so fluid physics work
    render_body = {
        "cameraMode": "top",
        "selectionName": selection_name
    }
    try:
        local_api_post(session, "/replay/render", render_body)
    except Exception as e:
        print(f"Warning: Render endpoint update failed: {e}")

    # 2. Handle the fluid tracking system via AppleScript and Mac key emulation
    if hotkey:
        print(f"Targeting active League window and double-pressing hotkey '{hotkey}'...")
        
        # AppleScript to target the correct process
        applescript = """
        osascript -e '
        tell application "System Events"
            set procList to name of every process
            if procList contains "LeagueofLegends" then
                set frontmost of process "LeagueofLegends" to true
            else if procList contains "League of Legends" then
                set frontmost of process "League of Legends" to true
            end if
        end tell'
        """
        os.system(applescript)
        time.sleep(1.0) # Let the window focus fully
        
        # Execute the double-tap sequence that worked in testing
        pyautogui.keyDown(hotkey)
        time.sleep(0.08)
        pyautogui.keyUp(hotkey)
        
        time.sleep(0.12)
        
        pyautogui.keyDown(hotkey)
        time.sleep(0.08)
        pyautogui.keyUp(hotkey)
        print(f"Camera fluidly locked to: {selection_name} via hotkey shortcut.")
    else:
        # Fallback to the sequence method if hotkey parsing fails
        playback_state = local_api_get(session, "/replay/playback").json()
        current_time = float(playback_state.get("time", 0.0))
        sequence_state = {
            "selectionName": [
                {
                    "time": current_time,
                    "value": selection_name,
                    "blend": "snap",
                }
            ],
            "selectionOffset": [
                {
                    "time": current_time,
                    "value": {"x": 0.0, "y": camera_height, "z": 0.0},
                    "blend": "snap",
                }
            ],
        }
        local_api_post(session, "/replay/sequence", sequence_state)
        print(f"Camera hard-locked to: {selection_name} via Sequence API (Fallback).")


def start_recording(session: requests.Session, output_path: Path) -> None:
    recording_state = local_api_get(session, "/replay/recording").json()
    body = {
        "codec": recording_state.get("codec", "webm"),
        "path": str(output_path),
        "width": recording_state.get("width", 1920),
        "height": recording_state.get("height", 1080),
        "framesPerSecond": 60, # recording_state.get("framesPerSecond", 60),
        "lossless": False, # recording_state.get("lossless", False),
        "enforceFrameRate": True,
        "replaySpeed": 1.0, # recording_state.get("replaySpeed", 1.0),
        "startTime": max(0.0, recording_state.get("currentTime", 0.0) - 2.0), # recording_state.get("currentTime", 0.0),
        "endTime": -1.0,
    }
    response = local_api_post(session, "/replay/recording", body)
    response.raise_for_status()
    print(f"Recording started: {output_path}")

def is_playback_finished(session: requests.Session) -> bool:
    response_json = local_api_get(session, "/replay/playback").json()
    # if match time is near total playback length
    if abs(response_json["length"] - response_json["time"]) < 2:
        return True
    return False

def wait_for_recording_to_finish(session: requests.Session, output_path: Path, timeout_seconds: int = 7200) -> None:
    deadline = time.time() + timeout_seconds
    last_state = None
    while time.time() < deadline:
        print("Waiting for playback to complete.")
        state = local_api_get(session, "/replay/recording").json()
        last_state = state
        if state.get("recording") is False and state.get("path"):
            print(f"Recording finished: {state['path']}")
            return
        if is_playback_finished(session):
            print("Playback API determined game is over.")
            return
        print(
            "Recording status: "
            f"recording={state.get('recording')} "
            f"currentTime={state.get('currentTime')} "
            f"path={state.get('path', '')}"
        )
        time.sleep(5)

    raise TimeoutError(f"Timed out waiting for recording to finish. Last state: {last_state}")

def wait_for_match_to_begin(session: requests.Session, headers: dict, spectator_url: str, wait_for_match: str):
    # wait for a match if 
    while True:
        spec_res = riot_get(session, spectator_url)
        if spec_res is None:
            session = requests.Session()
            session.headers.update(headers)
        elif spec_res.status_code == 404:
            print("Player is not in a live game.")
            # Dont wait if not explicitly set
            if not wait_for_match:
                break
            print("Waiting...")
            time.sleep(10)
        elif spec_res.status_code != 200:
            print(
                f"Spectator lookup failed: {spec_res.status_code}\n"
                f"{spec_res.text.strip()}"
            )
            sys.exit(1)
        elif spec_res.status_code == 200:
            print("Found a match.")
            break

    return spec_res


def get_match_data(
    game_name: str,
    tag_line: str,
    platform: str,
    cluster: str,
    api_key: str,
    dry_run: bool,
    record: bool,
    output_override: str | None,
    summoner_id_override: str | None,
    camera_height: float,
    wait_for_match: bool,
) -> None:
    if not api_key:
        print("Missing Riot API key. Set RIOT_API_KEY or pass --api-key.")
        sys.exit(1)

    headers = {"X-Riot-Token": api_key}
    session = requests.Session()
    session.headers.update(headers)

    account_url = (
        f"https://{cluster}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(game_name)}/{quote(tag_line)}"
    )
    account_res = riot_get(session, account_url)
    if account_res.status_code != 200:
        print(
            f"Account lookup failed: {account_res.status_code}\n"
            f"{account_res.text.strip()}"
        )
        sys.exit(1)

    account_data = account_res.json()
    puuid = account_data["puuid"]
    print(f"Found player: {account_data['gameName']}#{account_data['tagLine']}")
    print(f"PUUID: {puuid}")

    summoner_url = f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{quote(puuid)}"
    summoner_res = riot_get(session, summoner_url)
    if summoner_res.status_code != 200:
        print(
            f"Summoner lookup failed: {summoner_res.status_code}\n"
            f"{summoner_res.text.strip()}"
        )
        sys.exit(1)

    summoner_data = summoner_res.json()
    summoner_id = summoner_data.get("id") or summoner_data.get("summonerId")
    if summoner_id_override:
        summoner_id = summoner_id_override

    if not summoner_id:
        print("Summoner lookup did not return an id or summonerId.")
        print("Using the PUUID directly for the spectator lookup instead.")
        summoner_id = puuid

    if summoner_data.get("id") is None and summoner_data.get("summonerId") is None:
        print_json("Summoner payload:", summoner_data)
    print(f"Summoner ID: {summoner_id}")

    spectator_url = f"https://{platform}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{quote(summoner_id)}"
    spec_res = wait_for_match_to_begin(session, headers, spectator_url, wait_for_match)
    if not hasattr(spec_res, "status_code") or spec_res.status_code != 200:
        print("Didn't find a match, exit.")
        return
    game_data = spec_res.json()
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

    if dry_run:
        return

    attempt = 0
    while attempt < MAX_LEAGUE_CLIENT_STARTUP_ATTEMPTS:
        process = launch_spectate(
            binary=binary,
            cwd=cwd,
            game_id=game_id,
            platform_id=platform_id,
            encryption_key=encryption_key,
        )

        try:
            local_session = wait_for_local_api()
        except TimeoutError as exc:
            print(exc)
            process.terminate()
        else:
            break

        attempt += 1
    else:
        print("Unable to successfully start spectate. Quitting.")
        return

    target_participant = find_target_participant(game_data, puuid)
    target_selection_name = selection_name_for_participant(target_participant)
    player_hotkey, team = get_player_hotkey(local_session, target_selection_name)
    apply_fog_of_war_perspective(team)
    apply_scoreboard(local_session)
    if target_participant and target_participant.get("riotId"):
        apply_camera_lock(
            local_session,
            target_selection_name,
            camera_height,
            player_hotkey,
        )
    else:
        print("Could not find a matching participant to lock the camera to.")
    apply_runes_tab()

    if not record:
        return

    output_path = build_recording_output_path(game_name, tag_line, game_id, output_override)
    start_recording(local_session, output_path)
    if target_participant and target_participant.get("riotId"):
        apply_camera_lock(
            local_session,
            target_selection_name,
            camera_height,  
            player_hotkey,
        )
    else:
        print("Could not find a matching participant to lock the camera to.")
    wait_for_recording_to_finish(local_session, output_path)
    process.terminate()

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.game_name or args.tag_line:
        if not (args.game_name and args.tag_line):
            print("Both --game-name and --tag-line are required when using the explicit Riot ID flags.")
            sys.exit(1)
        game_name = args.game_name.strip()
        tag_line = args.tag_line.strip()
    else:
        if "#" not in args.riot_id:
            print("Riot ID must be in the form GameName#TAG.")
            sys.exit(1)
        game_name, tag_line = args.riot_id.split("#", 1)
        game_name = game_name.strip()
        tag_line = tag_line.strip()

    get_match_data(
        game_name=game_name,
        tag_line=tag_line,
        platform=args.platform.strip().lower(),
        cluster=args.cluster.strip().lower(),
        api_key=args.api_key,
        dry_run=args.dry_run,
        record=args.record,
        output_override=args.output,
        summoner_id_override=args.summoner_id.strip() if args.summoner_id else None,
        camera_height=args.camera_height,
        wait_for_match=args.wait,
    )
