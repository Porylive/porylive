import difflib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Set, Tuple, Dict
from .logger import Logger
from .config import ConfigManager
from .porylive_types import SECTION_PATTERN, GlobalState

class ScriptDiffer:
    """Handles script comparison and diffing between LST files"""

    def __init__(self, logger: Logger, config_manager: ConfigManager):
        self.logger = logger
        self.config_manager = config_manager

        # Global state tracking
        self.new_script_labels: Set[str] = set()
        self.new_script_globals: Set[str] = set()
        self.used_global_labels: Set[str] = set()
        self.child_to_label_map: Dict[str, str] = {}

    def strip_lst_file(self, lst_path: Path) -> list:
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
        self.logger.log_profiling(f"strip_lst_file({lst_path}) took {strip_end - strip_start:.4f}s, {len(stripped_lines)} lines")
        return stripped_lines

    def get_updated_scripts(self, lst_path_old: Path, lst_path_new: Path, src_file: str) -> Tuple[Set[str], bool]:
        """Strip file down to just labels and script calls, then diff the two files"""

        start_time = time.perf_counter()
        self.logger.log_profiling("Starting get_updated_scripts")

        if not lst_path_old.exists():
            self.logger.log_message(f"File not found: {lst_path_old}")
            sys.exit(1)
        if not lst_path_new.exists():
            self.logger.log_message(f"File not found: {lst_path_new}")
            sys.exit(1)

        # Strip both files in parallel
        parallel_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as executor:
            old_future = executor.submit(self.strip_lst_file, lst_path_old)
            new_future = executor.submit(self.strip_lst_file, lst_path_new)

            old_stripped = old_future.result()
            new_stripped = new_future.result()
        parallel_end = time.perf_counter()
        self.logger.log_profiling(f"Parallel file stripping took {parallel_end - parallel_start:.4f}s")

        # Generate diff using unified_diff (more efficient than Differ)
        diff_start = time.perf_counter()
        unified_diff = list(difflib.unified_diff(
            old_stripped,
            new_stripped,
            lineterm='',
            n=0  # No context lines needed
        ))
        diff_end = time.perf_counter()
        self.logger.log_profiling(f"Unified diff generation took {diff_end - diff_start:.4f}s, {len(unified_diff)} diff lines")

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
                if current_label and len(current_script_names) > 0 and (current_label in updated_scripts or current_label in self.new_script_globals):
                    # Edge case where a new label has the same scripts as an old label
                    if current_label in self.new_script_globals and current_label not in updated_scripts:
                        updated_scripts.add(current_label)
                    # Check if any of the script names are in the keys of the macros_to_adjust list under the current src_file
                    if not needs_macro_adjustment:
                        macros_to_adjust = self.config_manager.get_macros_to_adjust(src_file)
                        keys = list(macros_to_adjust.keys())
                        for script_name in current_script_names:
                            if script_name in keys:
                                needs_macro_adjustment = True
                                break
                if current_label in updated_scripts:
                    updated_script_params.update(current_script_params)
                else:
                    for child_label in current_child_labels:
                        self.child_to_label_map[child_label] = current_label

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
                    self.new_script_globals.add(line.lstrip(".global").strip().split()[0])
                elif action == 'added' and ":" in line:
                    self.new_script_labels.add(line.rstrip(":").lstrip())
                elif current_label not in updated_scripts and line.split() and len(line.split()) > 1 and line.split()[1] != ".align":
                    updated_scripts.add(current_label)

        # Check if any of the updated_script_params are in other_labels_in_file
        # If so, those are the only other scripts that need to be processed for address offsets
        for script_param in updated_script_params:
            if script_param in self.child_to_label_map.keys():
                self.used_global_labels.add(self.child_to_label_map[script_param])

        analysis_end = time.perf_counter()
        self.logger.log_profiling(f"Diff analysis took {analysis_end - analysis_start:.4f}s")

        total_time = time.perf_counter() - start_time
        self.logger.log_profiling(f"get_updated_scripts total time: {total_time:.4f}s")
        self.logger.log_profiling(f"Found {len(updated_scripts)} updated scripts, needs_macro_adjustment: {needs_macro_adjustment}")

        return updated_scripts, needs_macro_adjustment

    @property
    def global_state(self) -> GlobalState:
        """Get the current global state"""
        return {
            'new_script_labels': self.new_script_labels,
            'new_script_globals': self.new_script_globals,
            'used_global_labels': self.used_global_labels,
            'child_to_label_map': self.child_to_label_map
        }
