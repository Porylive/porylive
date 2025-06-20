import re
import sys
from typing import Dict, List, Tuple, Any
from .logger import Logger
from .config import ConfigManager
from .map_file import MapFileManager
from .conditional_processor import ConditionalProcessor
from .porylive_types import ScriptParams, RoutineData, LuaAdjustment

class MacroProcessor:
    """Handles macro adjustments for script data"""

    def __init__(self, logger: Logger, config_manager: ConfigManager, map_file_manager: MapFileManager):
        self.logger = logger
        self.config_manager = config_manager
        self.map_file_manager = map_file_manager
        self.conditional_processor = ConditionalProcessor(logger)

    def adjust_data_from_macro(self, routines: Dict[str, RoutineData], script: ScriptParams,
                               src_file: str, new_script_labels: set, new_script_globals: set) -> Tuple[bytearray, List[LuaAdjustment]]:
        """Adjust script data based on macro definitions"""

        def adjust_by_address(script: ScriptParams, info: Dict[str, Any], lua_adjustments: List[LuaAdjustment]):
            """Adjust script data by address lookup"""
            if "name" in info.keys():
                _name = info["name"]
            else:
                _name = script["params"][info["index"]]
            # Do not process if _name is a hex number (e.g. 0x8000000)
            if _name.startswith("0x"):
                return script["data"], []

            address = self.map_file_manager.get_map_file_address(_name)
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
                self.logger.log_message(f"[address] Unknown symbol for {script['name']}: {_name}")
                sys.exit(1)

        def adjust_by_offset(script: ScriptParams, info: Dict[str, Any], lua_adjustments: List[LuaAdjustment]):
            """Adjust script data by offset lookup"""
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
                            address = self.map_file_manager.get_map_file_address(label)
                            if address is not None:
                                script["data"][info["offset"]:info["offset"]+4] = (address + child_label["offset"]).to_bytes(4, "little")
                                found = True
                                break
                    if found:
                        break
            if not found:
                # Exit with error
                self.logger.log_message(f"[offset] Unknown symbol for {script['name']}: {script['params'][info['index']]}")
                sys.exit(1)

        macros_to_adjust = self.config_manager.get_macros_to_adjust(src_file)
        macro_info = macros_to_adjust.get(script["name"])
        lua_adjustments: List[LuaAdjustment] = []

        if not macro_info:
            return script["data"], []

        # Determine which adjustments to apply
        if isinstance(macro_info, dict) and ("$condition" in macro_info or "$if" in macro_info):
            # Handle conditional macros
            adjustments = self.conditional_processor.process_conditional_macro(script, macro_info)
        elif isinstance(macro_info, list):
            # Handle non-conditional macros (when macro_info is a list)
            adjustments = macro_info
        else:
            # Exit with error
            self.logger.log_message(f"[adjust_data_from_macro] Unknown macro_info type for {script['name']}: {macro_info}")
            sys.exit(1)

        # Process all adjustments
        for info in adjustments:
            if info["type"] == "macro":
                params = [0] * info["param_len"] if "param_len" in info else info["params"]
                script["data"], _lua_adjustments = self.adjust_data_from_macro(routines, {
                    "name": info["name"],
                    "params": params,
                    "data": script["data"],
                }, src_file, new_script_labels, new_script_globals)
                lua_adjustments.extend(_lua_adjustments)
            elif "index" in info.keys() and info["index"] >= len(script["params"]):
                continue
            elif info["type"] == "address":
                # Replace 32 bits starting at info["offset"] with the address from the map file
                adjust_by_address(script, info, lua_adjustments)
            elif info["type"] == "offset":
                # Find a routine with a child label that matches script["params"][info["index"]]
                adjust_by_offset(script, info, lua_adjustments)
            elif info["type"] == "dynamic":
                # Check if the 4 bytes to overwrite are all 0
                if script["data"][info["offset"]:info["offset"]+4] == b"\x00\x00\x00\x00":
                    # Replace with the address of the script
                    adjust_by_address(script, info, lua_adjustments)
                else:
                    adjust_by_offset(script, info, lua_adjustments)

        return script["data"], lua_adjustments
