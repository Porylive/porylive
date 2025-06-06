import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import difflib
import sys
import socket
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict, List, Dict, TypeVar, Optional

class ScriptParams(TypedDict):
    name: str
    params: List[str]
    data: bytearray

class ChildLabel(TypedDict):
    name: str
    offset: int

class RoutineData(TypedDict):
    scripts: List[ScriptParams]
    starting_offset: int
    child_labels: List[ChildLabel]

PROFILING = False

# Get the directory containing this script
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent

CONFIG_FILE = PROJECT_DIR / "build" / "porylive_config.lua"

# Define paths relative to the script directory
MACRO_DATA_FILE = SCRIPT_DIR / "porylive_macro_data.json"

# List of supported files
supported_files = [
    'data/battle_anim_scripts.s',
    'data/event_scripts.s'
]

map_file_lines = None
map_file_address_cache = {}
macro_data_cache = None
new_script_labels = set()
new_script_globals = set()
used_global_labels = set()
child_to_label_map = {}

SECTION_PATTERN = re.compile(r'\.section script_data,"aw",%progbits')

def load_map_file():
    global map_file_lines
    with open(MAP_FILE, "r") as f:
        map_file_lines = f.readlines()

def get_map_file_address(variable_name) -> int | None:
    if map_file_lines is None:
        load_map_file()
    for line in map_file_lines:
        # Find the line that contains the variable name as a whole word
        if variable_name in line.split():
            # Extract the address from the line
            address = line.split()[0].split("x")[1]
            map_file_address_cache[variable_name] = int(address, 16)
            return map_file_address_cache[variable_name]
    return None

def log_message(*args):
    """Log a message to porylive_watchman.log with timestamp. The file should be created by the Makefile"""
    log_file_path = SCRIPT_DIR.parent.parent / ".porylive" / "porylive_on_change.log"
    timestamp = datetime.now().strftime("%H:%M:%S")
    with open(log_file_path, "a") as f:
        if args:
            # First argument gets timestamp, rest are joined with newline + 18 spaces
            if len(args) == 1:
                f.write(f"[{timestamp}] {args[0]}\n")
            else:
                _spaces = " " * 11
                additional_args = f"\n{_spaces}".join(str(arg) for arg in args[1:])
                f.write(f"[{timestamp}] {args[0]}\n{_spaces}{additional_args}\n")
        else:
            f.write(f"[{timestamp}] \n")

def log_profiling(message: str):
    """Log a profiling message with [PROFILE] prefix"""
    if not PROFILING:
        return
    log_message(f"[PROFILE] {message}")

def load_macro_data():
    """Load macro adjustment data from JSON file"""
    global macro_data_cache
    if macro_data_cache is None:
        with open(MACRO_DATA_FILE, "r") as f:
            macro_data_cache = json.load(f)
    return macro_data_cache

def load_porylive_config():
    """Load porylive_config.lua from the build directory"""
    with open(CONFIG_FILE, "r") as f:
        content = f.read()
        # Extract the current_build_dir value from the Lua file
        # Look for the pattern: current_build_dir = 'path'
        import re
        match = re.search(r"current_build_dir\s*=\s*['\"]([^'\"]+)['\"]", content)
        if match:
            global BUILD_DIR
            BUILD_DIR = PROJECT_DIR / match.group(1)
            return BUILD_DIR
        else:
            raise ValueError("Could not find current_build_dir in porylive_config.lua")

def get_macros_to_adjust(src_file: str) -> Dict:
    """Get macro adjustment data for a specific file"""
    macro_data = load_macro_data()
    return macro_data.get(src_file, {})

def adjust_data_from_macro(routines: Dict[str, RoutineData], script: ScriptParams, src_file: str) -> tuple[bytearray, List[Dict[str, any]]]:

    def adjust_by_address(script: ScriptParams, info: Dict[str, any]):
        if "name" in info.keys():
            _name = info["name"]
        else:
            _name = script["params"][info["index"]]
        # Do not process if _name is a hex number (e.g. 0x8000000)
        if _name.startswith("0x"):
            return script["data"], []

        address = get_map_file_address(_name)
        if address is not None:
            if "add" in info:
                address += info["add"]
            script["data"][info["offset"]:info["offset"]+4] = address.to_bytes(4, "little")
        elif _name in new_script_labels or _name in new_script_globals:
            # Needs to be adjusted in Lua
            lua_adjustments.append({
                "label": _name,
                "offset": info["offset"],
                "address_offset": 0,
            })
        else:
            # Exit with error
            log_message(f"[address] Unknown symbol for {script['name']}: {_name}")
            sys.exit(1)

    def adjust_by_offset(script: ScriptParams, info: Dict[str, any]):
        _name = script["params"][info["index"]]
        # Do not process if _name is a hex number (e.g. 0x8000000)
        if _name.startswith("0x"):
            return script["data"], []

        found = False
        for label, routine in routines.items():
            if routine["child_labels"]:
                for child_label in routine["child_labels"]:
                    if child_label["name"] == _name:
                        if child_label["name"] in new_script_labels:
                            # Needs to be adjusted in Lua
                            lua_adjustments.append({
                                "label": label,
                                "offset": info["offset"],
                                "address_offset": child_label["offset"],
                            })
                            found = True
                            break
                        # Replace 32 bits starting at info["offset"] with the address from the map file of label + child_label["offset"]
                        address = get_map_file_address(label)
                        if address is not None:
                            script["data"][info["offset"]:info["offset"]+4] = (address + child_label["offset"]).to_bytes(4, "little")
                            found = True
                            break
                if found:
                    break
        if not found:
            # Exit with error
            log_message(f"[offset] Unknown symbol for {script['name']}: {script['params'][info['index']]}")
            sys.exit(1)

    def handle_condition(script: ScriptParams, macro_info: Dict[str, any]) -> List[Dict[str, any]]:
        """Handle conditional macro adjustments based on conditions like num_args"""
        condition_type = macro_info.get("condition")
        if_conditions = macro_info.get("if", {})
        
        if condition_type == "num_args":
            num_params = len(script["params"])
            
            # Find matching condition
            for condition_key, adjustments in if_conditions.items():
                # Handle single number or comma-separated numbers
                if "," in condition_key:
                    valid_counts = [int(x.strip()) for x in condition_key.split(",")]
                else:
                    valid_counts = [int(condition_key)]
                
                if num_params in valid_counts:
                    return adjustments
        
        # Return empty list if no condition matches
        return []

    macros_to_adjust = get_macros_to_adjust(src_file)
    macro_info = macros_to_adjust.get(script["name"])
    lua_adjustments = []
    if not macro_info:
        return script["data"], []
    
    # Determine which adjustments to apply
    if isinstance(macro_info, dict) and "condition" in macro_info:
        # Handle conditional macros (when macro_info is a dict with condition)
        adjustments = handle_condition(script, macro_info)
    elif isinstance(macro_info, list):
        # Handle non-conditional macros (when macro_info is a list)
        adjustments = macro_info
    else:
        # Exit with error
        log_message(f"[adjust_data_from_macro] Unknown macro_info type for {script['name']}: {macro_info}")
        sys.exit(1)
    
    # Process all adjustments
    for info in adjustments:
        if info["type"] == "macro":
            script["data"], _lua_adjustments = adjust_data_from_macro(routines, {
                "name": info["name"],
                "params": info["params"],
                "data": script["data"],
            }, src_file)
            lua_adjustments.extend(_lua_adjustments)
        elif "index" in info.keys() and info["index"] >= len(script["params"]):
            continue
        elif info["type"] == "address":
            # Replace 32 bits starting at info["offset"] with the address from the map file
            adjust_by_address(script, info)
        elif info["type"] == "offset":
            # Find a routine with a child label that matches script["params"][info["index"]]
            adjust_by_offset(script, info)
        elif info["type"] == "dynamic":
            # Check if the 4 bytes to overwrite are all 0
            if script["data"][info["offset"]:info["offset"]+4] == b"\x00\x00\x00\x00":
                # Replace with the address of the script
                adjust_by_address(script, info)
            else:
                adjust_by_offset(script, info)

    return script["data"], lua_adjustments

def get_updated_scripts(lst_path_old, lst_path_new, src_file: str):
    """Strip file down to just labels and script calls, then diff the two files"""
    
    start_time = time.perf_counter()
    log_profiling("Starting get_updated_scripts")
    
    def strip_lst_file(lst_path):
        """Strip an LST file down to just labels and script calls"""
        strip_start = time.perf_counter()
        stripped_lines = []
        started_parsing = False
        
        with open(lst_path, 'r') as f:
            for line in f:
                line = line.rstrip()
                
                # Wait for the .section line before starting to parse
                if not started_parsing:
                    if SECTION_PATTERN.search(line):
                        started_parsing = True
                    continue
                
                # Replace empty lines, form feed characters, and page headers with newlines
                if not line or line.startswith("\x0c") or "ARM GAS" in line:
                    continue
                
                # Check for label line - it will have a colon and start with spaces followed by a number
                if ':' in line and line.lstrip().split()[0].isdigit():
                    # Extract just the label part (everything after the number and before any comments)
                    parts = line.split(';')
                    if len(parts) >= 2:
                        # Get the label part, remove the line number prefix
                        label_part = parts[1].strip()
                        stripped_lines.append(label_part)
                    else:
                        # Handle child labels that don't have "; .global"
                        colon_part = line.split(':')[0]
                        if colon_part.strip().split():
                            label_name = colon_part.strip().split()[-1]
                            stripped_lines.append(f"{label_name}:")
                
                # Check for script/macro calls (lines with hex data and macro names)
                elif started_parsing and line.strip():
                    parts = line.split(';')[0].split()
                    # Look for lines that have at least 4 parts (line_num, address, hex_data, macro_name)
                    if len(parts) >= 4:
                        macro_name = parts[3]
                        if macro_name == ".align":
                            continue
                        params = ",".join(parts[4:]) if len(parts) > 4 else ""
                        if params:
                            stripped_lines.append(f" {macro_name} {params}")
                        else:
                            stripped_lines.append(f" {macro_name}")
        
        strip_end = time.perf_counter()
        log_profiling(f"strip_lst_file({lst_path}) took {strip_end - strip_start:.4f}s, {len(stripped_lines)} lines")
        return stripped_lines
    
    if not Path(lst_path_old).exists():
        log_message(f"File not found: {lst_path_old}")
        sys.exit(1)
    if not Path(lst_path_new).exists():
        log_message(f"File not found: {lst_path_new}")
        sys.exit(1)

    # Strip both files in parallel
    parallel_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        old_future = executor.submit(strip_lst_file, lst_path_old)
        new_future = executor.submit(strip_lst_file, lst_path_new)
        
        old_stripped = old_future.result()
        new_stripped = new_future.result()
    parallel_end = time.perf_counter()
    log_profiling(f"Parallel file stripping took {parallel_end - parallel_start:.4f}s")
    
    # Generate diff using unified_diff (more efficient than Differ)
    diff_start = time.perf_counter()
    unified_diff = list(difflib.unified_diff(
        old_stripped,
        new_stripped,
        lineterm='',
        n=0  # No context lines needed
    ))
    diff_end = time.perf_counter()
    log_profiling(f"Unified diff generation took {diff_end - diff_start:.4f}s, {len(unified_diff)} diff lines")

    updated_scripts = set()
    updated_script_params = set()

    # Parse unified diff format more efficiently
    analysis_start = time.perf_counter()
    current_label = None
    current_script_names = set()
    current_script_params = set()
    current_child_labels = set()
    needs_macro_adjustment = False
    
    # Convert unified diff to a simpler format for processing, including unchanged lines
    diff_lines = []
    new_line_idx = 0
    
    i = 0
    while i < len(unified_diff):
        line = unified_diff[i]
        
        if line.startswith('@@'):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            hunk_match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if hunk_match:
                new_start = int(hunk_match.group(3)) - 1  # Convert to 0-based index
                
                # Add any unchanged lines before this hunk
                while new_line_idx < new_start:
                    if new_line_idx < len(new_stripped):
                        diff_lines.append(('unchanged', new_stripped[new_line_idx]))
                    new_line_idx += 1
                
                # Process the hunk
                i += 1
                while i < len(unified_diff) and not unified_diff[i].startswith('@@'):
                    hunk_line = unified_diff[i]
                    if hunk_line.startswith('---') or hunk_line.startswith('+++'):
                        i += 1
                        continue
                    elif hunk_line.startswith('-'):
                        diff_lines.append(('removed', hunk_line[1:]))
                    elif hunk_line.startswith('+'):
                        diff_lines.append(('added', hunk_line[1:]))
                        new_line_idx += 1
                    else:
                        # Unchanged line within hunk (shouldn't happen with n=0, but just in case)
                        diff_lines.append(('unchanged', hunk_line))
                        new_line_idx += 1
                    i += 1
                continue
        elif line.startswith('---') or line.startswith('+++'):
            i += 1
            continue
        
        i += 1
    
    # Add any remaining unchanged lines at the end
    while new_line_idx < len(new_stripped):
        diff_lines.append(('unchanged', new_stripped[new_line_idx]))
        new_line_idx += 1
    
    # Process the complete diff sequence
    for action, line in diff_lines:
        if ".global" in line:
            if current_label and len(current_script_names) > 0 and (current_label in updated_scripts or current_label in new_script_globals):
                # Edge case where a new label has the same scripts as an old label
                if current_label in new_script_globals and current_label not in updated_scripts:
                    updated_scripts.add(current_label)
                # Check if any of the script names are in the keys of the macros_to_adjust list under the current src_file
                if not needs_macro_adjustment:
                    macros_to_adjust = get_macros_to_adjust(src_file)
                    keys = list(macros_to_adjust.keys())
                    for script_name in current_script_names:
                        if script_name in keys:
                            needs_macro_adjustment = True
                            break
            if current_label in updated_scripts:
                updated_script_params.update(current_script_params)
            else:
                for child_label in current_child_labels:
                    child_to_label_map[child_label] = current_label
            
            if action == 'added':
                current_label = line.lstrip(".global").strip().split()[0]
            else:
                current_label = line.split()[1] if len(line.split()) > 1 else None
            current_script_names = set()
            current_script_params = set()
            current_child_labels = set()
        elif current_label and action != 'removed':
            if ":" in line:
                current_child_labels.add(line.split(":")[0].strip())
            _script = line.lstrip().strip().split()
            if _script:
                current_script_names.add(_script[0])
                if len(_script) > 1:
                    current_script_params.update(",".join(_script[1:]).split(","))
        
        if action in ('added', 'removed'):
            if action == 'added' and ".global" in line:
                new_script_globals.add(line.lstrip(".global").strip().split()[0])
            elif action == 'added' and ":" in line:
                new_script_labels.add(line.rstrip(":").lstrip())
            elif current_label not in updated_scripts and line.split() and len(line.split()) > 1 and line.split()[1] != ".align":
                updated_scripts.add(current_label)

    # Check if any of the updated_script_params are in other_labels_in_file
    # If so, those are the only other scripts that need to be processed for address offsets
    for script_param in updated_script_params:
        if script_param in child_to_label_map.keys():
            used_global_labels.add(child_to_label_map[script_param])
    
    analysis_end = time.perf_counter()
    log_profiling(f"Diff analysis took {analysis_end - analysis_start:.4f}s")

    total_time = time.perf_counter() - start_time
    log_profiling(f"get_updated_scripts total time: {total_time:.4f}s")
    log_profiling(f"Found {len(updated_scripts)} updated scripts, needs_macro_adjustment: {needs_macro_adjustment}")

    return updated_scripts, needs_macro_adjustment

def parse_lst(lst_path, updated_scripts, src_file: str, needs_macro_adjustment: bool) -> Dict[str, RoutineData]:
    routines = {}
    current_script = {
        "label": "",
        "offset": 0,
        "scripts": [],
        "child_labels": [],
    }
    started_parsing = False
    found_label = False  # Flag to track if we just found a label
    current_child_label = None  # Name of the child label we just found
    is_updated = False

    # All scripts that need to be processed
    scripts_to_process = new_script_globals.union(used_global_labels).union(updated_scripts)

    with open(lst_path) as f:
        for line in f:
            line = line.rstrip()

            # Wait for the .section line before starting to parse
            if not started_parsing:
                if '.section script_data,"aw",%progbits' in line:
                    started_parsing = True
                continue

            # Skip empty lines and form feed characters
            if not line or line.startswith("\x0c"):
                continue

            # Skip . operations like .macro and .set
            line_parts = line.split()
            if len(line_parts) > 1 and line_parts[1].startswith('.'):
                continue

            # Check for label line - it will have a colon and start with spaces followed by a number
            if ':' in line and line.lstrip().split()[0].isdigit():
                if '; .global' in line:
                    # Extract label name - it's the part before the colon, after the number
                    _label = line.split(':')[0].split()[-1]

                    if current_script["label"] and current_script["label"] in scripts_to_process:
                        # Store both the hex data and the starting offset
                        routines[current_script["label"]] = {
                            'scripts': current_script["scripts"],
                            'starting_offset': current_script["offset"],
                            'child_labels': current_script["child_labels"],
                            'original_address': get_map_file_address(current_script["label"]),
                        }

                    if _label not in scripts_to_process:
                        current_script["label"] = None
                        continue

                    current_script["label"] = _label  # Last part before the colon
                    current_script["scripts"] = []
                    current_script["child_labels"] = []
                    found_label = True  # Set flag that we found a label
                    if _label in updated_scripts:
                        current_script["data"] = bytearray()  # Reset to empty bytearray
                        is_updated = True
                    else:
                        current_script["data"] = None
                        is_updated = False
                elif current_script["label"]:
                    if "SandAttackStorm" in line:
                        print(line)
                    current_child_label = line.split(':')[0].split()[1]
            elif current_script["label"] and line:
                if not needs_macro_adjustment and not is_updated:
                    continue
                parts = line.split(';')[0].split()
                if len(parts) >= 4 and is_updated:
                    _name = parts[3]
                    _params_joined = ",".join(parts[4:])
                    _params = _params_joined.split(",") if len(parts) >= 5 else []
                    current_script["scripts"].append({
                        "name": _name,
                        "params": _params,
                        "data": bytearray(),
                    })
                if len(parts) >= 2:
                    if found_label:
                        try:
                            current_script["offset"] = int(parts[1], 16)
                        except ValueError:
                            continue
                        found_label = False  # Reset the flag
                    elif current_child_label:
                        try:
                            _offset = int(parts[1], 16) - current_script["offset"]
                        except ValueError:
                            continue
                        current_script["child_labels"].append({
                            "name": current_child_label,
                            "offset": _offset,
                        })
                        current_child_label = None
                    if not is_updated:
                        continue
                    # For lines with 2 parts, hex data is in part 2
                    # For lines with 3+ parts, hex data is in part 3
                    hex_data = parts[2] if len(parts) >= 3 else parts[1]
                    # Only add if it contains valid hex characters
                    if all(c in "0123456789abcdef" for c in hex_data.lower()):
                        # Convert hex pairs to bytes and add to current script's data
                        for i in range(0, len(hex_data), 2):
                            byte_value = int(hex_data[i:i+2], 16)
                            current_script["scripts"][-1]["data"].append(byte_value)

    if current_script["label"]:
        routines[current_script["label"]] = {
            'starting_offset': current_script["offset"],
            'scripts': current_script["scripts"],
            'child_labels': current_script["child_labels"],
        }

    # Process routines to adjust data from macros
    for label, routine in routines.items():
        if len(routine["scripts"]) == 0:
            continue
        data = bytearray()
        lua_adjustments = []
        for script in routine["scripts"]:
            if needs_macro_adjustment:
                script_data, _lua_adjustments = adjust_data_from_macro(routines, script, src_file)
                for adjustment in _lua_adjustments:
                    adjustment["offset"] += len(data)
                    lua_adjustments.append(adjustment)
            else:
                script_data = script["data"]
            data.extend(script_data)
        routines[label]["data"] = bytes(data)
        routines[label]["lua_adjustments"] = lua_adjustments

    # Filter out routines that don't have any scripts
    routines = {k: v for k, v in routines.items() if v["scripts"]}
    return routines

def send_notification(message: str):
    """Send a reload notification to porylive.lua via socket"""
    try:
        # Try to connect to the Lua script on localhost:1370
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)  # 2 second timeout
        sock.connect(('localhost', 1370))
        sock.send(message.encode('utf-8'))
        sock.close()
        log_message(f"Sent notification to porylive.lua: {message}")
    except socket.timeout:
        log_message(f"Failed to send notification to porylive.lua: {message}",
                    "Timeout connecting to porylive.lua - make sure mGBA is running with porylive.lua loaded")
    except ConnectionRefusedError:
        log_message(f"Failed to send notification to porylive.lua: {message}",
                    "Connection refused - see tools/porylive/README.md for setup instructions")
    except Exception as e:
        log_message(f"Failed to send notification to porylive.lua: {e}")

def find_most_recent_map_file() -> Path:
    """Find the most recently modified .map file in the project directory"""
    map_files = list(PROJECT_DIR.glob("**/*.map"))
    if not map_files:
        log_message("Error: No .map files found in project directory")
        sys.exit(1)
    # Sort by modification time, most recent first
    most_recent = max(map_files, key=lambda p: p.stat().st_mtime)
    log_message(f"Using map file: {most_recent}")
    return most_recent

def try_process_poryscript_file(pory_file_path: Path):
    """Attempt to invoke tools/poryscript to process a .pory file"""
    filename = pory_file_path.name
    try:
        log_message(f"Processing pory file: {filename}")
        result = subprocess.run(
            [
                "tools/poryscript/poryscript",
                "-cc", "tools/poryscript/command_config.json",
                "-fc", "tools/poryscript/font_config.json",
                "-i", pory_file_path,
                "-o", pory_file_path.with_suffix(".inc"),
            ],
            check=True,
            capture_output=True,
        )
        if result.stderr:
            error_message = result.stderr.decode('utf-8').strip().split('\n')
            error_message.insert(0, f"Error while processing {pory_file_path}:")
            log_message(*error_message)
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.decode('utf-8').strip().split('\n')
        error_message.insert(0, f"Error while processing {pory_file_path}:")
        log_message(*error_message)
        sys.exit(1)

def main():
    main_start = time.perf_counter()
    log_profiling("Starting main function")
    
    # Skip initial watchman trigger
    if len(sys.argv) > 2:
        return

    # Write arguments to watchman.log
    _args = ["Script invoked with arguments:"]
    for i, arg in enumerate(sys.argv):
        _args.append(f"  argv[{i}]: {arg}")
    log_message(*_args)

    if len(sys.argv) > 1:
        updated_file = sys.argv[1]
    else:
        updated_file = None

    load_porylive_config()

    # If the file ends with .pory, try to process it with poryscript
    if updated_file and updated_file.endswith('.pory'):
        try_process_poryscript_file(PROJECT_DIR / updated_file)
        return

    global MAP_FILE
    MAP_FILE = find_most_recent_map_file()

    # Determine which supported file to process
    selected_file = None
    if updated_file:
        # If the file ends with .inc, use event_scripts.s
        if updated_file.endswith('.inc'):
            selected_file = 'data/event_scripts.s'
        else:
            # Try to match with supported files by comparing the relative path
            for supported_file in supported_files:
                if updated_file.endswith(supported_file):
                    selected_file = supported_file
                    break
    
    # Exit early if no matching file found
    if not selected_file:
        log_message(f"File not supported with porylive: {updated_file}")
        return

    # Exit early if build/modern-porylive directory doesn't exist or is empty
    if not (BUILD_DIR).exists() or not (BUILD_DIR).iterdir():
        log_message(f"{BUILD_DIR} directory does not exist or is empty - run make live first")
        return
    
    send_notification("PROCESSING")
    
    # Set up file paths based on selected file
    # Generate .lst file paths by replacing .s with .live.lst and .o.lst
    base_path = str(selected_file).replace('.s', '')
    src_lst_live = BUILD_DIR / (base_path + '.live.lst')
    src_lst_old = BUILD_DIR / (base_path + '.lst')

    # Path(SRC_LST).unlink(missing_ok=True)
    make_start = time.perf_counter()
    env = os.environ.copy()
    if "modern" in str(BUILD_DIR):
        env["MODERN"] = "1"
    try:
        result = subprocess.run(["make", "live-update"], check=True, capture_output=True, env=env)
        if result.stderr:
            # Convert result.stderr from bytes to string
            error_message = result.stderr.decode('utf-8').strip().split('\n')
            error_message.insert(0, "Error while assembling scripts:")
            log_message(*error_message)
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        log_message(f"make live-update failed with return code {e.returncode}")
        if e.stdout:
            log_message(f"stdout: {e.stdout}")
        if e.stderr:
            log_message(f"stderr: {e.stderr}")
        sys.exit(1)
    make_end = time.perf_counter()
    log_profiling(f"make live-update took {make_end - make_start:.4f}s")

    scripts_start = time.perf_counter()
    updated_scripts, needs_macro_adjustment = get_updated_scripts(src_lst_old, src_lst_live, selected_file)
    scripts_end = time.perf_counter()
    log_profiling(f"get_updated_scripts took {scripts_end - scripts_start:.4f}s")
    
    if len(new_script_labels) > 0:
        log_message(f"Found {len(updated_scripts) - len(new_script_globals)} updated script(s) and {len(new_script_globals)} new script(s)")
    else:
        log_message(f"Found {len(updated_scripts)} updated script(s)")

    parse_start = time.perf_counter()
    new_routines = parse_lst(src_lst_live, updated_scripts, selected_file, needs_macro_adjustment) if len(updated_scripts) > 0 else {}
    parse_end = time.perf_counter()
    log_profiling(f"parse_lst took {parse_end - parse_start:.4f}s")

    # Create output directory if it doesn't exist
    changed_output_path = BUILD_DIR / "bin/" / base_path
    changed_output_path.mkdir(parents=True, exist_ok=True)

    # Delete all files in the output directory
    cleanup_start = time.perf_counter()
    for file in changed_output_path.iterdir():
        file.unlink()
    cleanup_end = time.perf_counter()
    log_profiling(f"File cleanup took {cleanup_end - cleanup_start:.4f}s")

    generated_files: Dict[str, List[Dict[str, str]]] = {}

    json_load_start = time.perf_counter()
    try:
        with open(BUILD_DIR / "porylive_generated_files.json", "r") as f:
            generated_files = json.load(f)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    json_load_end = time.perf_counter()
    log_profiling(f"JSON load took {json_load_end - json_load_start:.4f}s")

    generated_files[selected_file] = []

    file_write_start = time.perf_counter()
    for label, data in new_routines.items():
        if data['data'] is None:
            continue
        # Create filename with label and starting offset
        filename = f"{label}-{data['original_address'] if data['original_address'] else '0'}.bin"
        output_path = changed_output_path / filename

        # Write binary data to file
        output_path.write_bytes(data['data'])
        log_message(f"Wrote {filename}")

        # Add to generated files list
        generated_files[selected_file].append({
            'label': label,
            'address': data['original_address'],
            'filename': str(output_path),
            'lua_adjustments': data['lua_adjustments'],
        })
    file_write_end = time.perf_counter()
    log_profiling(f"Binary file writing took {file_write_end - file_write_start:.4f}s")

    json_write_start = time.perf_counter()
    with open(BUILD_DIR / "porylive_generated_files.json", "w") as f:
        json.dump(generated_files, f)
    json_write_end = time.perf_counter()
    log_profiling(f"JSON write took {json_write_end - json_write_start:.4f}s")
    
    # Write the generated files list to porylive_generated_files.lua
    lua_write_start = time.perf_counter()
    lua_output_path = BUILD_DIR / "porylive_generated_files.lua"
    with open(lua_output_path, 'w') as f:
        f.write("return {\n")
        for file_group in generated_files.keys():
            for file_info in generated_files[file_group]:
                f.write(f"  {{\n")
                f.write(f"    label = \"{file_info['label']}\",\n")
                f.write(f"    address = {file_info['address']},\n")
                f.write(f"    filename = \"{file_info['filename']}\",\n")
                if 'lua_adjustments' in file_info.keys():
                    f.write( "    lua_adjustments = {\n")
                    for adjustment in file_info['lua_adjustments']:
                        f.write(f"      {{\n")
                        f.write(f"        label = \"{adjustment['label']}\",\n")
                        f.write(f"        offset = {adjustment['offset']},\n")
                        f.write(f"        address_offset = {adjustment['address_offset']},\n")
                        f.write(f"      }},\n")
                    f.write(f"    }},\n")
                f.write(f"  }},\n")
        f.write("}\n")
    lua_write_end = time.perf_counter()
    log_profiling(f"Lua file write took {lua_write_end - lua_write_start:.4f}s")
    log_message(f"Wrote porylive_generated_files.lua")

    send_notification("RELOAD")
    
    main_end = time.perf_counter()
    total_main_time = main_end - main_start
    log_profiling(f"main function total time: {total_main_time:.4f}s")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_message(f"Error: {e}")
        sys.exit(1)
