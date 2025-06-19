import sys
from pathlib import Path
from typing import Dict, List, Optional
from .logger import Logger

class MapFileManager:
    """Handles map file operations and address lookups"""

    def __init__(self, logger: Logger, project_dir: Path):
        self.logger = logger
        self.project_dir = project_dir
        self._map_file_lines: Optional[List[str]] = None
        self._address_cache: Dict[str, int] = {}
        self._current_map_file: Optional[Path] = None

    def find_most_recent_map_file(self) -> Path:
        """Find the most recently modified .map file in the project directory"""
        map_files = list(self.project_dir.glob("**/*.map"))
        if not map_files:
            self.logger.log_message("Error: No .map files found in project directory")
            sys.exit(1)
        # Sort by modification time, most recent first
        most_recent = max(map_files, key=lambda p: p.stat().st_mtime)
        self.logger.log_message(f"Using map file: {most_recent}")
        return most_recent

    def load_map_file(self, map_file_path: Optional[Path] = None):
        """Load the map file into memory"""
        if map_file_path is None:
            map_file_path = self.find_most_recent_map_file()

        self._current_map_file = map_file_path
        with open(map_file_path, "r") as f:
            self._map_file_lines = f.readlines()

        # Clear the cache when loading a new map file
        self._address_cache.clear()

    def get_map_file_address(self, variable_name: str) -> Optional[int]:
        """Get the address of a variable from the map file"""
        if self._map_file_lines is None:
            self.load_map_file()

        # Check cache first
        if variable_name in self._address_cache:
            return self._address_cache[variable_name]

        # Search in map file
        for line in self._map_file_lines:
            # Find the line that contains the variable name as a whole word
            if variable_name in line.split():
                # Extract the address from the line
                address = line.split()[0].split("x")[1]
                self._address_cache[variable_name] = int(address, 16)
                return self._address_cache[variable_name]

        return None

    def clear_cache(self):
        """Clear the address cache"""
        self._address_cache.clear()

    @property
    def current_map_file(self) -> Optional[Path]:
        """Get the currently loaded map file path"""
        return self._current_map_file
