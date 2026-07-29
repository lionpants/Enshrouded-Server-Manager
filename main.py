"""Enshrouded Server Manager - web UI backend.

Run on the machine that hosts the Enshrouded dedicated server:
    pip install -r requirements.txt
    python main.py
Then open http://127.0.0.1:8555
"""
import json
import os
import re
import shutil
import urllib.request
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import a2s
import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
MANAGER_CONFIG_PATH = BASE_DIR / "manager_config.json"
STEAM_APP_ID = "2278520"  # Enshrouded Dedicated Server

DEFAULT_MANAGER_CONFIG = {
    "serverDir": "C:\\enshrouded_server",
    "serverExe": "enshrouded_server.exe",
    "steamcmdExe": "C:\\steamcmd\\steamcmd.exe",
    "host": "127.0.0.1",
    "port": 8555,
    "autoBackup": False,
    "autoBackupIntervalHours": 6,
    "backupKeepCount": 24,
    "autoRestart": False,
    "autoRestartTime": "05:00",
    "autoUpdate": False,
    "autoUpdateWhenEmpty": True,
}

# ---------------------------------------------------------------- manager config

def load_manager_config():
    if MANAGER_CONFIG_PATH.exists():
        cfg = DEFAULT_MANAGER_CONFIG.copy()
        cfg.update(json.loads(MANAGER_CONFIG_PATH.read_text(encoding="utf-8")))
        return cfg
    MANAGER_CONFIG_PATH.write_text(json.dumps(DEFAULT_MANAGER_CONFIG, indent=2), encoding="utf-8")
    return DEFAULT_MANAGER_CONFIG.copy()


def save_manager_config(cfg):
    MANAGER_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def server_exe_path():
    cfg = load_manager_config()
    return Path(cfg["serverDir"]) / cfg["serverExe"]


def server_json_path():
    return Path(load_manager_config()["serverDir"]) / "enshrouded_server.json"


# ---------------------------------------------------------------- settings schema
# Source: official Keen Games docs, enshrouded_server.json version 0.9.0.0

NS_PER_MIN = 60_000_000_000

SETTINGS_SCHEMA = {
    "groups": [
        {
            "name": "Player",
            "fields": [
                {"key": "playerHealthFactor", "label": "Player Health", "type": "factor", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "help": "Scales max player health."},
                {"key": "playerManaFactor", "label": "Player Mana", "type": "factor", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "help": "Scales max player mana."},
                {"key": "playerStaminaFactor", "label": "Player Stamina", "type": "factor", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "help": "Scales max player stamina."},
                {"key": "playerBodyHeatFactor", "label": "Body Heat vs Cold", "type": "enum-num", "options": [0.5, 1, 1.5, 2], "optionLabels": ["Low (50%)", "Default (100%)", "High (150%)", "Max (200%)"], "default": 1, "help": "How long players endure very cold areas before hypothermia."},
                {"key": "playerDivingTimeFactor", "label": "Diving Time", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "Scales oxygen / time available underwater."},
                {"key": "enableGliderTurbulences", "label": "Glider Air Turbulence", "type": "bool", "default": True, "help": "Off = glider unaffected by turbulence (pre-update behavior)."},
                {"key": "tombstoneMode", "label": "Tombstone Mode", "type": "enum", "options": ["AddBackpackMaterials", "Everything", "NoTombstone"], "optionLabels": ["Drop materials only", "Drop everything", "No tombstone (keep all)"], "default": "AddBackpackMaterials", "help": "What players lose on death."},
            ],
        },
        {
            "name": "Survival",
            "fields": [
                {"key": "enableDurability", "label": "Weapon Durability", "type": "bool", "default": True, "help": "Off = weapons never break."},
                {"key": "enableStarvingDebuff", "label": "Hunger & Starvation", "type": "bool", "default": False, "help": "On = players starve (lose health) without food/drink."},
                {"key": "foodBuffDurationFactor", "label": "Food Buff Duration", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "Scales food buff durations."},
                {"key": "fromHungerToStarving", "label": "Hungry State Duration", "type": "minutes", "min": 5, "max": 20, "default": 600_000_000_000, "help": "Minutes of 'hungry' before starving sets in."},
                {"key": "shroudTimeFactor", "label": "Shroud Time", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "Scales how long players can stay in the Shroud."},
                {"key": "curseModifier", "label": "Shroud Curse", "type": "enum", "options": ["Easy", "Normal", "Hard"], "optionLabels": ["Easy (off)", "Normal", "Hard (2x chance)"], "default": "Normal", "help": "Chance of receiving the Shroud curse from enemy attacks."},
            ],
        },
        {
            "name": "World",
            "fields": [
                {"key": "weatherFrequency", "label": "Weather Frequency", "type": "enum", "options": ["Disabled", "Rare", "Normal", "Often"], "default": "Normal", "help": "How often weather phenomena appear."},
                {"key": "fishingDifficulty", "label": "Fishing Difficulty", "type": "enum", "options": ["VeryEasy", "Easy", "Normal", "Hard", "VeryHard"], "default": "Normal", "help": "Fish strength in the fishing minigame."},
                {"key": "plantGrowthSpeedFactor", "label": "Plant Growth Speed", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Scales plant growth speed."},
                {"key": "dayTimeDuration", "label": "Daytime Length", "type": "minutes", "min": 2, "max": 60, "default": 1_800_000_000_000, "help": "Length of daytime in minutes."},
                {"key": "nightTimeDuration", "label": "Nighttime Length", "type": "minutes", "min": 2, "max": 60, "default": 720_000_000_000, "help": "Length of nighttime in minutes."},
                {"key": "tamingStartleRepercussion", "label": "Taming Failure Penalty", "type": "enum", "options": ["KeepProgress", "LoseSomeProgress", "LoseAllProgress"], "optionLabels": ["Keep progress", "Lose some progress", "Lose all progress"], "default": "LoseSomeProgress", "help": "What happens when wildlife is startled during taming."},
            ],
        },
        {
            "name": "Resources & Progression",
            "fields": [
                {"key": "miningDamageFactor", "label": "Mining Effectiveness", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "Higher = more terraforming and resource yield per hit."},
                {"key": "resourceDropStackAmountFactor", "label": "Resource Gain", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Scales materials per loot stack."},
                {"key": "factoryProductionSpeedFactor", "label": "Workstation Speed", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Scales workshop production times."},
                {"key": "perkUpgradeRecyclingFactor", "label": "Weapon Recycling Yield", "type": "factor", "min": 0, "max": 1, "step": 0.05, "default": 0.5, "help": "Runes returned when salvaging upgraded weapons."},
                {"key": "perkCostFactor", "label": "Weapon Upgrade Cost", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Runes required for upgrading weapons."},
                {"key": "experienceCombatFactor", "label": "Combat XP", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Scales XP from combat."},
                {"key": "experienceMiningFactor", "label": "Mining XP", "type": "factor", "min": 0, "max": 2, "step": 0.05, "default": 1, "help": "Scales XP from mining."},
                {"key": "experienceExplorationQuestsFactor", "label": "Exploration & Quest XP", "type": "factor", "min": 0.25, "max": 2, "step": 0.05, "default": 1, "help": "Scales XP from exploring and quests."},
            ],
        },
        {
            "name": "Enemies",
            "fields": [
                {"key": "randomSpawnerAmount", "label": "Enemy Amount", "type": "enum", "options": ["Few", "Normal", "Many", "Extreme"], "default": "Normal", "help": "Amount of enemies in the world."},
                {"key": "aggroPoolAmount", "label": "Simultaneous Attackers", "type": "enum", "options": ["Few", "Normal", "Many", "Extreme"], "default": "Normal", "help": "How many enemies may attack at the same time."},
                {"key": "enemyDamageFactor", "label": "Enemy Damage", "type": "factor", "min": 0.25, "max": 5, "step": 0.05, "default": 1, "help": "Scales enemy damage (excl. bosses)."},
                {"key": "enemyHealthFactor", "label": "Enemy Health", "type": "factor", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "help": "Scales enemy health (excl. bosses)."},
                {"key": "enemyStaminaFactor", "label": "Enemy Stun Resistance", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "Higher = enemies take longer to stun (excl. bosses)."},
                {"key": "enemyPerceptionRangeFactor", "label": "Enemy Perception Range", "type": "factor", "min": 0.5, "max": 2, "step": 0.05, "default": 1, "help": "How far enemies see/hear players (excl. bosses)."},
                {"key": "bossDamageFactor", "label": "Boss Damage", "type": "factor", "min": 0.2, "max": 5, "step": 0.05, "default": 1, "help": "Scales boss attack damage."},
                {"key": "bossHealthFactor", "label": "Boss Health", "type": "factor", "min": 0.2, "max": 5, "step": 0.05, "default": 1, "help": "Scales boss health."},
                {"key": "threatBonus", "label": "Enemy Attack Frequency", "type": "factor", "min": 0.25, "max": 4, "step": 0.05, "default": 1, "help": "Scales frequency of enemy attacks (excl. bosses)."},
                {"key": "pacifyAllEnemies", "label": "Pacify All Enemies", "type": "bool", "default": False, "help": "On = enemies won't attack until attacked (excl. bosses)."},
            ],
        },
    ]
}

GAME_SETTINGS_DEFAULTS = {
    f["key"]: f["default"] for g in SETTINGS_SCHEMA["groups"] for f in g["fields"]
}

DEFAULT_USER_GROUP = {
    "name": "NewGroup",
    "password": "",
    "canKickBan": False,
    "canAccessInventories": False,
    "canEditWorld": True,
    "canEditBase": False,
    "canExtendBase": False,
    "reservedSlots": 0,
}

DEFAULT_SERVER_CONFIG = {
    "name": "Enshrouded Server",
    "saveDirectory": "./savegame",
    "logDirectory": "./logs",
    "ip": "0.0.0.0",
    "queryPort": 15637,
    "slotCount": 16,
    "tags": [],
    "voiceChatMode": "Proximity",
    "enableVoiceChat": False,
    "enableTextChat": False,
    "gameSettingsPreset": "Default",
    "gameSettings": GAME_SETTINGS_DEFAULTS,
    "userGroups": [],
    "bans": [],
}

# ---------------------------------------------------------------- process management

update_state = {"running": False, "lines": [], "exit_code": None, "started": None}
update_lock = threading.Lock()
started_pid = None  # pid of a server we launched


def find_server_process():
    """Locate the running enshrouded server process."""
    exe = server_exe_path()
    exe_name = exe.name.lower()
    # fast path: the process we launched ourselves
    if started_pid and psutil.pid_exists(started_pid):
        try:
            p = psutil.Process(started_pid)
            if exe_name in " ".join(p.cmdline()).lower() or (p.name() or "").lower() == exe_name:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            pexe = p.info.get("exe")
            cmd = p.info.get("cmdline") or []
            if pexe and Path(pexe) == exe:
                return p
            if name == exe_name and (not pexe or Path(pexe).name.lower() == exe_name):
                return p
            if cmd and Path(cmd[0]).name.lower() == exe_name:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app):
    last_auto_backup["time"] = time.time()  # first auto backup one interval after boot
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    threading.Thread(target=auto_restart_loop, daemon=True).start()
    threading.Thread(target=player_poll_loop, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    yield


app = FastAPI(title="Enshrouded Server Manager", lifespan=lifespan)


@app.get("/api/status")
def status():
    proc = find_server_process()
    cfg = load_manager_config()
    exe = server_exe_path()
    result = {
        "installed": exe.exists(),
        "configExists": server_json_path().exists(),
        "running": proc is not None,
        "updating": update_state["running"],
        "serverDir": cfg["serverDir"],
        "installedBuild": get_installed_buildid(),
        "eternalNight": bool((cfg.get("eternalNight") or {}).get("active")),
        "autoRestart": {
            "enabled": bool(cfg.get("autoRestart")),
            "time": cfg.get("autoRestartTime"),
            "next": next_auto_restart().isoformat() if next_auto_restart() else None,
        },
        "autoUpdate": {
            "enabled": bool(cfg.get("autoUpdate")),
            "whenEmpty": bool(cfg.get("autoUpdateWhenEmpty", True)),
            "waiting": auto_update_state["waiting"],
            "last": auto_update_state["last"],
            "lastResult": auto_update_state["lastResult"],
        },
    }
    if proc:
        try:
            with proc.oneshot():
                result.update({
                    "pid": proc.pid,
                    "uptimeSeconds": int(time.time() - proc.create_time()),
                    "cpuPercent": proc.cpu_percent(interval=0.1),
                    "memoryMB": round(proc.memory_info().rss / 1024 / 1024, 1),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            result["running"] = False
    return result


@app.post("/api/server/start")
def start_server():
    if update_state["running"]:
        raise HTTPException(409, "An update is in progress.")
    if find_server_process():
        raise HTTPException(409, "Server is already running.")
    exe = server_exe_path()
    if not exe.exists():
        raise HTTPException(404, f"Server executable not found: {exe}. Check paths in Manager Settings, or run an update to install.")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    global started_pid
    started_pid = proc.pid
    return {"ok": True, "pid": proc.pid}


def _terminate(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    except psutil.NoSuchProcess:
        pass


@app.post("/api/server/stop")
def stop_server():
    proc = find_server_process()
    if not proc:
        raise HTTPException(409, "Server is not running.")
    _terminate(proc)
    return {"ok": True}


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
auto_restart_state = {"last_fired_date": None}


def next_auto_restart():
    cfg = load_manager_config()
    if not cfg.get("autoRestart") or not TIME_RE.match(cfg.get("autoRestartTime", "")):
        return None
    hh, mm = map(int, cfg["autoRestartTime"].split(":"))
    now = datetime.now()
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def auto_restart_loop():
    while True:
        time.sleep(30)
        try:
            cfg = load_manager_config()
            if not cfg.get("autoRestart") or not TIME_RE.match(cfg.get("autoRestartTime", "")):
                continue
            hh, mm = map(int, cfg["autoRestartTime"].split(":"))
            now = datetime.now()
            if auto_restart_state["last_fired_date"] == now.strftime("%Y-%m-%d"):
                continue
            sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not (sched <= now < sched + timedelta(minutes=5)):
                continue
            auto_restart_state["last_fired_date"] = now.strftime("%Y-%m-%d")
            if update_state["running"]:
                continue
            proc = find_server_process()
            if not proc:
                continue  # only restart a running server
            _terminate(proc)
            time.sleep(1)
            exe = server_exe_path()
            if exe.exists():
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                p = subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                global started_pid
                started_pid = p.pid
        except Exception:  # noqa: BLE001
            pass


@app.post("/api/server/restart")
def restart_server():
    if update_state["running"]:
        raise HTTPException(409, "An update is in progress.")
    proc = find_server_process()
    if proc:
        _terminate(proc)
        time.sleep(1)  # let the OS release ports/files
    return start_server()


def _run_update():
    cfg = load_manager_config()
    steamcmd = cfg["steamcmdExe"]
    cmd = [steamcmd, "+force_install_dir", cfg["serverDir"], "+login", "anonymous",
           "+app_update", STEAM_APP_ID, "validate", "+quit"]
    update_state["lines"].append(f"$ {' '.join(cmd)}")
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1, creationflags=flags)
        for line in proc.stdout:
            update_state["lines"].append(line.rstrip())
            if len(update_state["lines"]) > 2000:
                del update_state["lines"][:500]
        proc.wait()
        update_state["exit_code"] = proc.returncode
        update_state["lines"].append(f"[steamcmd exited with code {proc.returncode}]")
    except FileNotFoundError:
        update_state["exit_code"] = -1
        update_state["lines"].append(f"ERROR: steamcmd not found at '{steamcmd}'. Set the correct path in Manager Settings.")
    except Exception as e:  # noqa: BLE001
        update_state["exit_code"] = -1
        update_state["lines"].append(f"ERROR: {e}")
    finally:
        update_state["running"] = False
        check_cache["time"] = 0.0  # re-check builds after an update


@app.post("/api/server/update")
def update_server():
    with update_lock:
        if update_state["running"]:
            raise HTTPException(409, "Update already in progress.")
        if find_server_process():
            raise HTTPException(409, "Stop the server before updating.")
        update_state.update({"running": True, "lines": [], "exit_code": None,
                             "started": datetime.now().isoformat()})
        threading.Thread(target=_run_update, daemon=True).start()
    return {"ok": True}


def get_installed_buildid():
    cfg = load_manager_config()
    manifest = Path(cfg["serverDir"]) / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
    if not manifest.exists():
        return None
    m = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def get_latest_buildid_api():
    """Fast path: public SteamCMD info API."""
    url = f"https://api.steamcmd.net/v1/info/{STEAM_APP_ID}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    return str(data["data"][STEAM_APP_ID]["depots"]["branches"]["public"]["buildid"])


def get_latest_buildid_steamcmd():
    """Slow path: ask steamcmd directly (~15-30s)."""
    cfg = load_manager_config()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    out = subprocess.run(
        [cfg["steamcmdExe"], "+login", "anonymous", "+app_info_update", "1",
         "+app_info_print", STEAM_APP_ID, "+quit"],
        capture_output=True, text=True, errors="replace", timeout=120, creationflags=flags,
    ).stdout
    m = re.search(r'"public"\s*\{\s*"buildid"\s*"(\d+)"', out)
    return m.group(1) if m else None


CHECK_CACHE_TTL = 600  # seconds
check_cache = {"time": 0.0, "latest": None, "source": None, "error": None}


def refresh_latest_build():
    latest, source, error = None, None, None
    try:
        latest, source = get_latest_buildid_api(), "steamcmd.net API"
    except Exception as api_err:  # noqa: BLE001
        if update_state["running"]:
            error = f"API check failed ({api_err}); steamcmd busy with update."
        else:
            try:
                latest, source = get_latest_buildid_steamcmd(), "steamcmd"
            except Exception as cmd_err:  # noqa: BLE001
                error = f"API check failed ({api_err}); steamcmd check failed ({cmd_err})"
    if latest is None and error is None:
        error = "Could not determine the latest build ID."
    check_cache.update({"time": time.time(), "latest": latest, "source": source, "error": error})


auto_update_state = {"last": None, "lastResult": None, "waiting": False}


def auto_update_once():
    """One auto-update evaluation: returns a short status string (for tests/logging)."""
    cfg = load_manager_config()
    if not cfg.get("autoUpdate"):
        auto_update_state["waiting"] = False
        return "disabled"
    if update_state["running"]:
        return "update-in-progress"
    res = check_update()  # uses the 10-minute cache
    if not res["updateAvailable"]:
        auto_update_state["waiting"] = False
        return "up-to-date"
    proc = find_server_process()
    was_running = proc is not None
    if was_running and cfg.get("autoUpdateWhenEmpty", True) and live_players["players"]:
        auto_update_state["waiting"] = True
        return "waiting-for-empty"
    auto_update_state["waiting"] = False
    try:
        create_backup("auto")
    except Exception:  # noqa: BLE001
        pass  # missing/empty savegame shouldn't block an update
    if proc:
        _terminate(proc)
        time.sleep(1)
    with update_lock:
        if update_state["running"]:
            return "update-in-progress"
        update_state.update({"running": True, "exit_code": None,
                             "started": datetime.now().isoformat(),
                             "lines": [f"[auto-update] build {res['installedBuild']} -> {res['latestBuild']}"]})
    _run_update()  # synchronous; resets update_state['running'] when done
    ok = update_state["exit_code"] == 0
    if ok and was_running:
        exe = server_exe_path()
        if exe.exists():
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            p = subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            global started_pid
            started_pid = p.pid
    auto_update_state["last"] = datetime.now().isoformat(timespec="seconds")
    auto_update_state["lastResult"] = "ok" if ok else "failed"
    return "updated" if ok else "update-failed"


def auto_update_loop():
    while True:
        time.sleep(60)
        try:
            auto_update_once()
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/update/check")
def check_update(force: bool = False):
    if force or time.time() - check_cache["time"] > CHECK_CACHE_TTL:
        refresh_latest_build()
    installed = get_installed_buildid()
    latest = check_cache["latest"]
    return {
        "installedBuild": installed,
        "latestBuild": latest,
        "updateAvailable": bool(installed and latest and installed != latest),
        "source": check_cache["source"],
        "error": check_cache["error"],
        "checkedAgoSeconds": int(time.time() - check_cache["time"]),
        "note": None if installed else "No appmanifest found — server not installed via SteamCMD in this directory?",
    }


@app.get("/api/update/log")
def update_log(offset: int = 0):
    lines = update_state["lines"]
    return {"running": update_state["running"], "exitCode": update_state["exit_code"],
            "offset": len(lines), "lines": lines[offset:]}


# ---------------------------------------------------------------- server config

@app.get("/api/schema")
def schema():
    return SETTINGS_SCHEMA


@app.get("/api/config")
def get_config():
    path = server_json_path()
    if not path.exists():
        return JSONResponse({"exists": False, "config": DEFAULT_SERVER_CONFIG})
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"enshrouded_server.json is not valid JSON: {e}")
    # fill any missing gameSettings with defaults so the UI shows everything
    gs = cfg.setdefault("gameSettings", {})
    for k, v in GAME_SETTINGS_DEFAULTS.items():
        gs.setdefault(k, v)
    for k, v in DEFAULT_SERVER_CONFIG.items():
        cfg.setdefault(k, v)
    return {"exists": True, "config": cfg}


def validate_config(cfg):
    errors = []
    if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
        errors.append("Server name must not be empty.")
    if not isinstance(cfg.get("queryPort"), int) or not (1 <= cfg["queryPort"] <= 65535):
        errors.append("queryPort must be 1-65535.")
    if not isinstance(cfg.get("slotCount"), int) or not (1 <= cfg["slotCount"] <= 16):
        errors.append("slotCount must be 1-16.")
    if cfg.get("gameSettingsPreset") not in ("Default", "Relaxed", "Hard", "Survival", "Custom"):
        errors.append("Invalid gameSettingsPreset.")
    if cfg.get("voiceChatMode") not in ("Proximity", "Global"):
        errors.append("voiceChatMode must be Proximity or Global.")
    fields = {f["key"]: f for g in SETTINGS_SCHEMA["groups"] for f in g["fields"]}
    for key, val in cfg.get("gameSettings", {}).items():
        f = fields.get(key)
        if not f:
            continue  # unknown key: keep, game clamps/ignores
        t = f["type"]
        if t == "bool" and not isinstance(val, bool):
            errors.append(f"{key} must be true/false.")
        elif t == "factor" and (not isinstance(val, (int, float)) or not (f["min"] <= val <= f["max"])):
            errors.append(f"{key} must be between {f['min']} and {f['max']}.")
        elif t == "minutes":
            lo, hi = f["min"] * NS_PER_MIN, f["max"] * NS_PER_MIN
            if not isinstance(val, int) or not (lo <= val <= hi):
                errors.append(f"{key} must be {f['min']}-{f['max']} minutes.")
        elif t == "enum" and val not in f["options"]:
            errors.append(f"{key} must be one of {f['options']}.")
        elif t == "enum-num" and val not in f["options"]:
            errors.append(f"{key} must be one of {f['options']}.")
    for i, ug in enumerate(cfg.get("userGroups", [])):
        if not isinstance(ug.get("name"), str) or not ug["name"].strip():
            errors.append(f"User group #{i + 1}: name must not be empty.")
        if not isinstance(ug.get("reservedSlots", 0), int) or ug.get("reservedSlots", 0) < 0:
            errors.append(f"User group '{ug.get('name', i)}': reservedSlots must be >= 0.")
    return errors


def write_server_config(cfg):
    """Write enshrouded_server.json with an automatic timestamped backup."""
    path = server_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backups = path.parent / "config_backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, backups / f"enshrouded_server_{stamp}.json")
        old = sorted(backups.glob("enshrouded_server_*.json"))
        for f in old[:-20]:
            f.unlink()
    path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


@app.put("/api/config")
def put_config(cfg: dict):
    errors = validate_config(cfg)
    if errors:
        raise HTTPException(422, "; ".join(errors))
    write_server_config(cfg)
    restart_needed = find_server_process() is not None
    return {"ok": True, "restartNeeded": restart_needed}


NIGHT_MIN_DAY = 120_000_000_000     # 2 min (game minimum)
NIGHT_MAX_NIGHT = 3_600_000_000_000  # 60 min (game maximum)


@app.post("/api/eternal-night/toggle")
def toggle_eternal_night():
    if update_state["running"]:
        raise HTTPException(409, "An update is in progress.")
    path = server_json_path()
    if not path.exists():
        raise HTTPException(404, "enshrouded_server.json not found — save a configuration first.")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"enshrouded_server.json is not valid JSON: {e}")
    mcfg = load_manager_config()
    en = mcfg.get("eternalNight") or {"active": False}
    gs = cfg.setdefault("gameSettings", {})
    if not en.get("active"):
        mcfg["eternalNight"] = {"active": True, "prev": {
            "dayTimeDuration": gs.get("dayTimeDuration", 1_800_000_000_000),
            "nightTimeDuration": gs.get("nightTimeDuration", 720_000_000_000),
            "gameSettingsPreset": cfg.get("gameSettingsPreset", "Default"),
        }}
        gs["dayTimeDuration"] = NIGHT_MIN_DAY
        gs["nightTimeDuration"] = NIGHT_MAX_NIGHT
        cfg["gameSettingsPreset"] = "Custom"  # gameSettings only apply with Custom
        active = True
    else:
        prev = en.get("prev", {})
        gs["dayTimeDuration"] = prev.get("dayTimeDuration", 1_800_000_000_000)
        gs["nightTimeDuration"] = prev.get("nightTimeDuration", 720_000_000_000)
        cfg["gameSettingsPreset"] = prev.get("gameSettingsPreset", "Default")
        mcfg["eternalNight"] = {"active": False}
        active = False
    write_server_config(cfg)
    save_manager_config(mcfg)
    restarted = False
    if find_server_process():
        restart_server()
        restarted = True
    return {"ok": True, "active": active, "restarted": restarted}


# ---------------------------------------------------------------- players (A2S + lifetime tracking)

PLAYTIME_PATH = BASE_DIR / "playtime.json"
playtime_lock = threading.Lock()
POLL_INTERVAL = 20


def _load_playtime():
    data = {"players": {}, "pending": {}}
    if PLAYTIME_PATH.exists():
        try:
            data = json.loads(PLAYTIME_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # drop records accidentally keyed by internal handles like '1(1)' or '(unnamed)'
    for section in ("players", "pending"):
        data[section] = {k: v for k, v in data.get(section, {}).items()
                         if not re.match(r"^\d+(\(\d+\))?$", k) and k != "(unnamed)"}
    return data


playtime = _load_playtime()
live_players = {"players": [], "error": None, "time": 0.0}

# Enshrouded leaves player names empty in A2S responses, so names come from the
# server log. Patterns are configurable via "playerNamePatterns" in
# manager_config.json in case a game update changes the wording.
NAME_PATTERNS_DEFAULT = [
    r"\[server\] Player '(?P<name>[^']+)' logged in",  # confirmed format
    r"Player '(?P<name>[^']+)' (?:logged in|joined)",
]
LEAVE_PATTERNS_DEFAULT = [
    r"\[server\] Remove (?:Entity for )?Player '(?P<name>[^']+)'",  # confirmed format
    r"Player '(?P<name>[^']+)' (?:logged out|left|disconnected)",
]
LOG_STATE_WORDS = {"Reserve", "WaitForJoin", "HostOnline", "Host_Online", "Lobby",
                   "LoadingWorld", "Playing", "Login", "Session"}
HANDLE_RE = re.compile(r"^\d+(\(\d+\))?$")  # internal handles like 1(2), not real names


def is_real_name(name):
    return bool(name) and name not in LOG_STATE_WORDS and not HANDLE_RE.match(name)


def latest_log_file():
    cfg = load_manager_config()
    raw = {}
    if server_json_path().exists():
        try:
            raw = json.loads(server_json_path().read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    log_dir = Path(raw.get("logDirectory", "./logs"))
    if not log_dir.is_absolute():
        log_dir = Path(cfg["serverDir"]) / log_dir
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if log_dir.exists() else []
    return logs[0] if logs else None


def scan_log_names():
    """Ordered list of player names from join lines in the current log (oldest first)."""
    log = latest_log_file()
    if not log:
        return []
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2 * 1024 * 1024))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    mcfg = load_manager_config()
    join_pats = mcfg.get("playerNamePatterns") or NAME_PATTERNS_DEFAULT
    leave_pats = mcfg.get("playerLeavePatterns") or LEAVE_PATTERNS_DEFAULT
    save_re = re.compile(r"Sending Character Savegame '(?P<name>[^']+)'")
    roster = []  # currently-online names in join order (oldest first)
    saving = None  # names collected inside a Start Saving .. Saved block
    for line in text.splitlines():
        if "(up)!" in line or "(down)!" in line:
            continue  # session state-machine lines use quoted state names
        # the ~5-min autosave logs every connected player's name — use complete
        # save blocks as an authoritative roster baseline (survives log rotation
        # and joins that scrolled out of the tail window)
        if "[server] Start Saving" in line:
            saving = []
            continue
        if saving is not None:
            m = save_re.search(line)
            if m:
                name = m.group("name").strip()
                if is_real_name(name):
                    saving.append(name)
                continue
            if "[server] Saved" in line:
                roster = [n for n in roster if n in saving] + [n for n in saving if n not in roster]
                saving = None
                continue
        matched = False
        for pat in join_pats:
            try:
                m = re.search(pat, line)
            except re.error:
                continue
            if m:
                name = m.group("name").strip()
                if is_real_name(name):
                    if name in roster:
                        roster.remove(name)
                    roster.append(name)
                matched = True
                break
        if matched:
            continue
        for pat in leave_pats:
            try:
                m = re.search(pat, line)
            except re.error:
                continue
            if m:
                name = m.group("name").strip()
                if name in roster:
                    roster.remove(name)
                break
    return roster


def merge_names(a2s_names, durations):
    """Resolve player names: trust valid A2S names, fill the rest from the log roster.

    A2S sometimes reports internal handles like '1(1)' instead of names, so every
    name is validated. Missing slots are filled from the log roster (join order,
    oldest first); the newest session pairs with the newest unclaimed roster name.
    """
    n = len(durations)
    final = [nm if is_real_name((nm or "").strip()) else None for nm in a2s_names]
    missing = [i for i, v in enumerate(final) if v is None]
    if missing:
        used = {v for v in final if v}
        avail = [r for r in scan_log_names() if r not in used]
        for rank, idx in enumerate(sorted(missing, key=lambda i: durations[i])):
            pos = len(avail) - 1 - rank  # newest session ← newest roster name
            final[idx] = avail[pos] if pos >= 0 else None
    return [v or f"Player {i + 1}" for i, v in enumerate(final)]


def assign_names(durations):
    return merge_names([None] * len(durations), durations)


def _save_playtime():
    PLAYTIME_PATH.write_text(json.dumps(playtime, indent=2), encoding="utf-8")


def query_players():
    """Query the game server's Steam A2S endpoint for connected players."""
    port = 15637
    if server_json_path().exists():
        try:
            port = int(json.loads(server_json_path().read_text(encoding="utf-8-sig")).get("queryPort", 15637))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return a2s.players(("127.0.0.1", port), timeout=3.0)


def _commit_pending(name):
    p = playtime["pending"].pop(name, None)
    if not p:
        return
    rec = playtime["players"].setdefault(name, {"total": 0, "sessions": 0, "firstSeen": p.get("at")})
    rec["total"] += p.get("duration", 0)
    rec["sessions"] += 1
    rec["lastSeen"] = p.get("at")
    rec["longest"] = max(rec.get("longest", 0), p.get("duration", 0))


HISTORY_PATH = BASE_DIR / "history.json"


def _load_history():
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"hourly": {}, "peak": {"count": 0, "at": None}}


history = _load_history()


def record_history(count):
    """Track max concurrent players per hour + all-time peak. Called from the poll loop."""
    now = datetime.now()
    key = now.strftime("%Y-%m-%dT%H")
    changed = False
    if history["hourly"].get(key, -1) < count:
        history["hourly"][key] = count
        changed = True
    if count > history["peak"]["count"]:
        history["peak"] = {"count": count, "at": now.isoformat(timespec="seconds")}
        changed = True
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H")
    stale = [k for k in history["hourly"] if k < cutoff]
    for k in stale:
        del history["hourly"][k]
    if changed or stale:
        HISTORY_PATH.write_text(json.dumps(history), encoding="utf-8")


def player_poll_loop():
    while True:
        try:
            if not find_server_process():
                with playtime_lock:
                    for name in list(playtime["pending"]):
                        _commit_pending(name)
                    _save_playtime()
                live_players.update({"players": [], "error": None, "time": time.time()})
                time.sleep(POLL_INTERVAL)
                continue
            try:
                players = query_players()
                now_iso = datetime.now().isoformat(timespec="seconds")
                durations = [int(p.duration) for p in players]
                names = merge_names([p.name for p in players], durations)
                current = {}
                for name, dur in zip(names, durations):
                    current[name] = max(current.get(name, 0), dur)
                with playtime_lock:
                    for name in list(playtime["pending"]):
                        if name not in current:
                            _commit_pending(name)
                    for name, dur in current.items():
                        pend = playtime["pending"].setdefault(name, {"at": now_iso})
                        pend.update({"duration": dur, "at": now_iso})
                        playtime["players"].setdefault(name, {"total": 0, "sessions": 0, "firstSeen": now_iso})
                    _save_playtime()
                live_players.update({
                    "players": [{"name": n, "sessionSeconds": d} for n, d in sorted(current.items())],
                    "error": None, "time": time.time()})
                record_history(len(current))
            except Exception as e:  # noqa: BLE001
                live_players.update({"players": [], "error": f"Query failed: {e}", "time": time.time()})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(POLL_INTERVAL)


@app.get("/api/players")
def get_players():
    out, offline = [], []
    with playtime_lock:
        online_names = {p["name"] for p in live_players["players"]}
        for p in live_players["players"]:
            rec = playtime["players"].get(p["name"], {})
            out.append({
                "name": p["name"],
                "online": True,
                "sessionSeconds": p["sessionSeconds"],
                "lifetimeSeconds": rec.get("total", 0) + p["sessionSeconds"],
                "pastSessions": rec.get("sessions", 0),
                "firstSeen": rec.get("firstSeen"),
                "lastSeen": rec.get("lastSeen"),
            })
        for name, rec in playtime["players"].items():
            if name in online_names or re.match(r"^Player \d+$", name):
                continue  # skip anonymous fallback records
            offline.append({
                "name": name,
                "online": False,
                "sessionSeconds": None,
                "lifetimeSeconds": rec.get("total", 0),
                "pastSessions": rec.get("sessions", 0),
                "firstSeen": rec.get("firstSeen"),
                "lastSeen": rec.get("lastSeen"),
            })
    offline.sort(key=lambda r: r.get("lastSeen") or "", reverse=True)
    return {"players": out, "offline": offline, "error": live_players["error"],
            "ageSeconds": int(time.time() - live_players["time"]) if live_players["time"] else None}


@app.get("/api/stats")
def get_stats():
    with playtime_lock:
        online = {p["name"]: p["sessionSeconds"] for p in live_players["players"]}
        board = []
        for name, rec in playtime["players"].items():
            if re.match(r"^Player \d+$", name):
                continue
            live = online.get(name, 0)
            total = rec.get("total", 0) + live
            sessions = rec.get("sessions", 0) + (1 if name in online else 0)
            board.append({
                "name": name,
                "online": name in online,
                "totalSeconds": total,
                "sessions": sessions,
                "avgSession": total // sessions if sessions else 0,
                "longestSession": max(rec.get("longest", 0), live),
                "firstSeen": rec.get("firstSeen"),
                "lastSeen": "now" if name in online else rec.get("lastSeen"),
            })
        for name, live in online.items():  # online players with no committed record yet
            if not any(b["name"] == name for b in board) and not re.match(r"^Player \d+$", name):
                board.append({"name": name, "online": True, "totalSeconds": live, "sessions": 1,
                              "avgSession": live, "longestSession": live,
                              "firstSeen": None, "lastSeen": "now"})
    board.sort(key=lambda b: -b["totalSeconds"])
    now = datetime.now()
    daily = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        vals = [v for k, v in history["hourly"].items() if k.startswith(day)]
        daily.append({"day": day, "max": max(vals) if vals else 0})
    by_hour = [0] * 24
    for k, v in history["hourly"].items():
        try:
            by_hour[int(k[-2:])] = max(by_hour[int(k[-2:])], v)
        except (ValueError, IndexError):
            pass
    return {"leaderboard": board, "daily": daily, "byHour": by_hour, "peak": history["peak"]}


# ---------------------------------------------------------------- save backups

BACKUP_NAME_RE = re.compile(r"^save_(manual|auto|pre_restore)_\d{8}_\d{6}\.zip$")
last_auto_backup = {"time": 0.0}


def save_dir_path():
    cfg = load_manager_config()
    raw = {}
    if server_json_path().exists():
        try:
            raw = json.loads(server_json_path().read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    d = Path(raw.get("saveDirectory", "./savegame"))
    return d if d.is_absolute() else Path(cfg["serverDir"]) / d


def backups_dir():
    d = Path(load_manager_config()["serverDir"]) / "save_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_backup(tag="manual"):
    src = save_dir_path()
    if not src.exists() or not any(src.iterdir()):
        raise HTTPException(404, f"Save directory is missing or empty: {src}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"save_{tag}_{stamp}"
    shutil.make_archive(str(backups_dir() / name), "zip", str(src))
    prune_backups()
    return name + ".zip"


def prune_backups():
    keep = load_manager_config()["backupKeepCount"]
    files = sorted(backups_dir().glob("save_*.zip"), key=lambda p: p.stat().st_mtime)
    for f in files[:-keep] if len(files) > keep else []:
        f.unlink()


def backup_path_checked(name):
    if not BACKUP_NAME_RE.match(name):
        raise HTTPException(400, "Invalid backup name.")
    p = backups_dir() / name
    if not p.exists():
        raise HTTPException(404, "Backup not found.")
    return p


def auto_backup_loop():
    while True:
        time.sleep(60)
        try:
            cfg = load_manager_config()
            if not cfg.get("autoBackup"):
                continue
            interval = float(cfg.get("autoBackupIntervalHours", 6)) * 3600
            if time.time() - last_auto_backup["time"] < interval:
                continue
            if update_state["running"]:
                continue
            create_backup("auto")
            last_auto_backup["time"] = time.time()
        except Exception:  # noqa: BLE001
            last_auto_backup["time"] = time.time()  # don't retry every minute on failure


@app.get("/api/backups")
def list_backups():
    cfg = load_manager_config()
    items = []
    for p in sorted(backups_dir().glob("save_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({"name": p.name, "sizeMB": round(st.st_size / 1024 / 1024, 2),
                      "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return {"backups": items, "autoBackup": cfg["autoBackup"],
            "autoBackupIntervalHours": cfg["autoBackupIntervalHours"],
            "backupKeepCount": cfg["backupKeepCount"], "saveDir": str(save_dir_path())}


@app.post("/api/backups")
def make_backup():
    name = create_backup("manual")
    return {"ok": True, "name": name,
            "warning": "Backup taken while the server was running — for a guaranteed-consistent snapshot, stop the server first." if find_server_process() else None}


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    p = backup_path_checked(name)
    if find_server_process():
        raise HTTPException(409, "Stop the server before restoring a backup.")
    if update_state["running"]:
        raise HTTPException(409, "An update is in progress.")
    src = save_dir_path()
    if src.exists() and any(src.iterdir()):
        create_backup("pre_restore")  # safety copy of current world
        shutil.rmtree(src)
    src.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(str(p), str(src), "zip")
    return {"ok": True, "restored": name}


@app.get("/api/backups/{name}/download")
def download_backup(name: str):
    return FileResponse(backup_path_checked(name), filename=name, media_type="application/zip")


@app.delete("/api/backups/{name}")
def delete_backup(name: str):
    backup_path_checked(name).unlink()
    return {"ok": True}


# ---------------------------------------------------------------- manager config & logs

@app.get("/api/manager-config")
def get_manager_config():
    return load_manager_config()


@app.put("/api/manager-config")
def put_manager_config(cfg: dict):
    current = load_manager_config()
    for key in ("serverDir", "serverExe", "steamcmdExe", "host"):
        if key in cfg and isinstance(cfg[key], str) and cfg[key].strip():
            current[key] = cfg[key].strip()
    if "port" in cfg and isinstance(cfg["port"], int) and 1 <= cfg["port"] <= 65535:
        current["port"] = cfg["port"]
    if "autoBackup" in cfg and isinstance(cfg["autoBackup"], bool):
        current["autoBackup"] = cfg["autoBackup"]
    if "autoBackupIntervalHours" in cfg and isinstance(cfg["autoBackupIntervalHours"], (int, float)) and cfg["autoBackupIntervalHours"] >= 1:
        current["autoBackupIntervalHours"] = cfg["autoBackupIntervalHours"]
    if "backupKeepCount" in cfg and isinstance(cfg["backupKeepCount"], int) and cfg["backupKeepCount"] >= 1:
        current["backupKeepCount"] = cfg["backupKeepCount"]
    if "autoRestart" in cfg and isinstance(cfg["autoRestart"], bool):
        current["autoRestart"] = cfg["autoRestart"]
    if "autoUpdate" in cfg and isinstance(cfg["autoUpdate"], bool):
        current["autoUpdate"] = cfg["autoUpdate"]
    if "autoUpdateWhenEmpty" in cfg and isinstance(cfg["autoUpdateWhenEmpty"], bool):
        current["autoUpdateWhenEmpty"] = cfg["autoUpdateWhenEmpty"]
    if "autoRestartTime" in cfg:
        if not isinstance(cfg["autoRestartTime"], str) or not TIME_RE.match(cfg["autoRestartTime"]):
            raise HTTPException(422, "autoRestartTime must be HH:MM (24h).")
        current["autoRestartTime"] = cfg["autoRestartTime"]
    save_manager_config(current)
    return {"ok": True, "config": current,
            "note": "Host/port changes take effect after restarting the manager."}


@app.get("/api/logs")
def get_logs(lines: int = 200):
    cfg = load_manager_config()
    server_dir = Path(cfg["serverDir"])
    raw = json.loads(server_json_path().read_text(encoding="utf-8-sig")) if server_json_path().exists() else {}
    log_dir = Path(raw.get("logDirectory", "./logs"))
    if not log_dir.is_absolute():
        log_dir = server_dir / log_dir
    if not log_dir.exists():
        return {"file": None, "lines": [f"Log directory not found: {log_dir}"]}
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {"file": None, "lines": ["No log files found."]}
    latest = logs[0]
    try:
        content = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return {"file": str(latest), "lines": [f"Could not read log: {e}"]}
    return {"file": latest.name, "lines": content[-lines:]}


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    cfg = load_manager_config()
    print(f"Enshrouded Server Manager -> http://{cfg['host']}:{cfg['port']}")
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")
