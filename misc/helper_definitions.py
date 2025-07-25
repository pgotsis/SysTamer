import json

from .output_manager import *
from typing import Any, Dict, List
from pathlib import Path

COMMANDS_DICT = {
    "start": "Get the list of all commands",
    "login": "Authenticate the session",
    "browse": "Navigate file system",
    "upload": "Upload a file to the server",
    "list_uploads": "Uploads directory contents",
    "system": "Get system resource usage",
    "processes": "Active processes <F=FILTER>",
    "kill": "Kill a process by its PID",
    "systemctl": "Handle systemd services",
    "screenshot": "Take & send a screenshot",
    "logout": "De-authenticate the session",
    "help": "Refers to /start"
}

PARAMS_DICT = {
    "login": ["PASS"],
    "processes": ["F"],
    "kill": ["PID"],
    "systemctl": ["ACT", "SRVC"]
}


def generate_cmd_dict_msg(description, commands: dict) -> str:
    header = f"{description}:\n| Command                  | Description                    |\n"
    separator = "|--------------------------|--------------------------------|\n"

    # Create the table with the header and separator
    table = header + separator

    # Loop through the dictionary to create each row
    for command, description in commands.items():
        # Formatting each row to have aligned columns
        if command in PARAMS_DICT:
            command += "\t" + ','.join([f"<{arg}>" for arg in PARAMS_DICT[command]])
        command = "/" + command
        table += f"| {command:<24} | {description:<30} |\n"

    return f"```{table}```"


def generate_machine_stats_msg(description, cpu_usage, memory_info, disk_usage) -> str:
    header = f"{description}\n| Resource   | Usage                     |\n"
    separator = "|------------|---------------------------|\n"

    table = header + separator
    table += f"| {'CPU':<10} | {f'{cpu_usage}%':<25} |\n"
    table += f"| {'Memory':<10} | {f'{memory_info.percent}% ({memory_info.used / (1024 ** 3):.1f}/{memory_info.total / (1024 ** 3):.1f} GB)':<25} |\n"
    table += f"| {'Disk':<10} | {f'{disk_usage.percent}% ({disk_usage.used / (1024 ** 3):.1f}/{disk_usage.total / (1024 ** 3):.1f} GB)':<25} |\n"

    return f"```{table}```"


def generate_proc_stats_msg(description, processes: list) -> List[str]:
    table_header = f"{description}\n| PID   | Name                 | CPU (%) | Mem (%)  |\n"
    separator = "|-------|----------------------|---------|----------|\n"
    table = table_header + separator
    chunks = list()

    for proc in processes:
        pid = str(proc['pid']).ljust(5)
        name = (proc['name'] or "N/A")[:20].ljust(20)
        cpu = f"{proc['cpu_percent']:.1f}".ljust(7)
        mem = f"{round(proc['memory_percent'], 1):.1f}".ljust(8)

        table += f"| {pid} | {name} | {cpu} | {mem} |\n"
        if len(table) > 3500:  # Telegram's max message size is about 4096 bytes
            chunks.append(table)
            table = table_header + separator

    chunks.append(table)
    return chunks


def load_config(conf_path: Path) -> Dict[str, Any]:
    try:
        with open(conf_path, 'r') as file:
            data = json.load(file)
        return data
    except FileNotFoundError as e:
        print_error(f"Config path not found -> {conf_path}")
        raise e
    except json.JSONDecodeError as e:
        print_error(f"Error decoding config -> {conf_path}.")
        raise e
