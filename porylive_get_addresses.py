#!/usr/bin/env python3
import sys
import re
import os
import subprocess

def extract_and_write_lua(map_file_path, output_lua_path):
    """
    Parses a GBA .map file to find specific symbol addresses and writes them
    into a Lua file as a table.
    """
    symbols_to_find = {
        "gPoryLiveScriptInitialized": None,
        "gPoryLiveOverrides": None,
        "gPoryLiveScriptBuffer": None,
    }
    symbols_found_count = 0
    found_addresses = {}

    # Regex to match .sym file format: <hex_address> <g|l> <size> <symbol_name>
    # Example: 02031d0c g 00000004 gPoryLiveScriptInitialized
    sym_regex = re.compile(r"^([0-9a-fA-F]+)\s+[gl]\s+[0-9a-fA-F]+\s+(.+)$")


    try:
        with open(map_file_path, 'r') as f:
            for line in f:
                line = line.strip()
                match = sym_regex.match(line)

                if match:
                    address_hex = match.group(1)
                    symbol_name = match.group(2)

                    # Check if this symbol is one we're looking for
                    if symbol_name in symbols_to_find and symbol_name not in found_addresses:
                        # Add 0x prefix to match expected format
                        found_addresses[symbol_name] = f"0x{address_hex}"
                        symbols_found_count += 1
                        # Optimization: stop if all symbols are found
                        if symbols_found_count == len(symbols_to_find):
                            break

    except FileNotFoundError:
        print(f"Error: Map file not found at {map_file_path}", file=sys.stderr)
        return False # Indicate failure
    except Exception as e:
        print(f"Error processing map file '{map_file_path}': {e}", file=sys.stderr)
        return False # Indicate failure

    # Ensure the output directory exists
    output_dir = os.path.dirname(output_lua_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Generate Lua file content
    lua_content = f"-- Auto-generated by {os.path.basename(__file__)} from {os.path.basename(map_file_path)}\n"
    lua_content += "-- DO NOT EDIT THIS FILE MANUALLY!\n"
    lua_content += "return {\n"
    all_found = True
    for symbol in sorted(symbols_to_find.keys()): # Sort for consistent output
        address = found_addresses.get(symbol)
        if address:
            lua_content += f'  {symbol} = {address},\n'
        else:
            print(f"Warning: Symbol '{symbol}' not found in {map_file_path}.", file=sys.stderr)
            lua_content += f'  -- {symbol} = nil, -- Symbol not found in map file\n'
            all_found = False
    lua_content += "}\n"

    # Write to output Lua file
    try:
        with open(output_lua_path, 'w') as f:
            f.write(lua_content)
        # print(f"Successfully wrote addresses to {output_lua_path}") # Reduce verbosity
    except Exception as e:
        print(f"Error writing Lua address file '{output_lua_path}': {e}", file=sys.stderr)
        return False # Indicate failure

    return all_found # Return True if all symbols were found, False otherwise


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python {os.path.basename(__file__)} <map_file_path> <output_lua_path>", file=sys.stderr)
        sys.exit(1)

    map_file = sys.argv[1]
    output_lua = sys.argv[2]

    print(f"Generating {map_file.replace('.map', '.sym')}...")

    # Run `PORYLIVE=1 make syms`, then replace the map file with the .sym file
    # for backwards compatibility
    env = os.environ.copy()
    env["PORYLIVE"] = "1"
    # Remove jobserver environment variables to avoid jobserver issues
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)
    try:
        result = subprocess.run(
            ["make", "-j1", "syms"],  # Use -j1 to avoid parallel job issues
            cwd=os.getcwd(),
            env=env,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to run `make syms`", file=sys.stderr)
        if e.stderr:
            print(e.stderr.decode("utf-8"), file=sys.stderr)
        sys.exit(1)

    map_file = map_file.replace(".map", ".sym")
    if not os.path.exists(map_file):
        print(f"Error: {map_file} does not exist", file=sys.stderr)
        sys.exit(1)

    # Create a porylive_config.lua file the build directory
    project_dir = os.path.dirname(map_file)
    config_file = os.path.join(project_dir, "build", "porylive_config.lua")
    build_dir = os.path.dirname(output_lua)

    with open(config_file, "w") as f:
        f.write(f"-- Auto-generated by {os.path.basename(__file__)}\n")
        f.write(f"-- DO NOT EDIT THIS FILE MANUALLY!\n")
        f.write( "return {\n")
        f.write(f"  current_build_dir = '{build_dir}',\n")
        f.write( "}\n")

    if not extract_and_write_lua(map_file, output_lua):
        print(f"Error: Not all required symbols were found in {map_file}. Check {output_lua}.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0) # Ensure exit code 0 on success or warning
