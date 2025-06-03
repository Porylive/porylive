# Porylive

**⚠️ Beta Software**: Porylive is currently in beta. While functional, you may encounter bugs or limitations.
- If the game crashes while running a script, it's likely a macro has not yet been added to `porylive_macro_data.json`. See the [Contributing](#contributing) section for more information.

Porylive is a live script editing system for pokeemerald-expansion that allows you to modify battle animation scripts and event scripts in real-time without rebuilding the entire ROM. When you save changes to supported script files, Porylive automatically processes and injects the updated scripts directly into the game's memory while running in mGBA.

## Dependencies

In addition to the base pokeemerald-expansion project dependencies, you'll need:

- **[Watchman](https://facebook.github.io/watchman/docs/install.html)**: File watching service
  - **macOS**: `brew install watchman`
  - **Ubuntu/Debian**: `sudo apt-get install watchman`
  - **Windows**: Download from [Watchman releases](https://github.com/facebook/watchman/releases/latest)
- **[mGBA](https://mgba.io/)**: Emulator
  - The latest version (0.10.5) does not work with Porylive. Use a nightly build or wait for the next 0.11.x stable release.
- **Porylive git remote**:
  - While this repository's files will reside in `tools/poryscript`, code changes need to be pulled from a Porylive feature branch based on your base repository:
    - **pokeemerald-expansion**: https://github.com/Porylive/pokeemerald-expansion-porylive
  - To merge the changes into your repository:
    - Run `git remote add porylive <porylive-repo-url>`, with the url being the one matching your base repository
    - Pull changes with `git pull porylive main` to pull the changes into your currently checked out branch

## Supported Files

### Fully Supported
- `data/battle_anim_scripts.s` - Battle animation scripts

### Partially Supported
- `data/scripts/*.inc` - Individual event scripts included in `data/event_scripts.s`

## Quick Start

### 1. Build and Start Porylive

```bash
make live -j$(nproc)
```

This command will:
- Build the ROM with Porylive enabled
- Set up file watchers for supported script files
- Display the Porylive logo and status messages
- Begin monitoring for file changes

### 2. Set Up mGBA

1. **Open mGBA** and load the compiled `pokeemerald.gba` ROM
2. **Open Scripting Window**:
   - Go to `Tools` → `Scripting...`
3. **Load Porylive Script**:
   - Click `File` → `Load script...`
   - Navigate to `tools/porylive/porylive.lua`
   - Select and open the file

You should see Porylive initialization messages in the mGBA console.

### 3. Make Live Changes

1. **Edit a supported file** (e.g., `data/battle_anim_scripts.s` or any `.inc` file in `data/scripts/`)
2. **Save the file**
3. **Watch the magic happen**:
   - Porylive detects the change
   - Processes the updated scripts
   - Injects changes into mGBA memory
   - Scripts are immediately available in-game

_Tip: Avoiding saving changes to a script while a script is currently running in-game. Doing so could risk hsving your game crash_

### 4. Stop Monitoring

Press `Ctrl+C` in the terminal where `make live` is running to stop file monitoring and clean up.

## How It Works

1. **File Watching**: Watchman monitors supported script files for changes
2. **Diff Analysis**: When a file changes, Porylive compares the old and new assembly listings to identify modified scripts
3. **Script Processing**: Updated scripts are compiled and processed, with addresses resolved using the `pokeemerald.map` file
4. **Memory Injection**: The Lua script receives the processed data and writes it directly to mGBA's memory
5. **Address Patching**: When a script attempts to invoke a modified script, the script's pointer value is replaced with the new script's address.

## Troubleshooting

### "Connection refused" Error
- Make sure mGBA is running with the Porylive Lua script loaded
- Verify the script is active in mGBA's Scripting window
  - You can test this by typing `reload()` in the input field and pressing enter

### "File not supported" Message
- Ensure you're editing files in the supported list
- Check that the file path matches exactly (case-sensitive)

### Scripts Not Updating
- Verify the ROM was built with `make live`
- Check that Watchman is running: `watchman version`
- Look for error messages in the terminal output

### Build Errors
- Ensure all base pokeemerald dependencies are installed
- Try `make clean` followed by `make live` again

### Game Crashing
- If the game crashes while running a script:
  - Try recompiling the ROM without Porylive enabled using `make` to see if the issue persists.
  - If the game still crashes, it's likely a macro with an address dependency has not yet been added to `porylive_macro_data.json`. See the [Contributing](#contributing) section for more information.
  - If the game does not crash, it's likely a problem with your script, not Porylive.

## Advanced Usage

### Cleaning Up
```bash
# Removes only built script files, recommended to run before `make live`
make tidylive

# Cleans the entire build/modern-porylive directory
make clean-live
```

### Macro Configuration

Porylive uses `porylive_macro_data.json` to understand how to handle script macros that reference addresses. If you've created custom macros or encounter address resolution issues, you may need to add entries to this file.

## Contributing

### Adding Macro Support

If you're game is crashing while running a script, a macro has likely not yet been added to `porylive_macro_data.json`.

#### Before submitting an issue:
- **Build a regular rom** using `make`
- If the game still crashes, it's mostly likely a problem with your script, not Porylive.
- If the game does not crash, it's likely a macro with an address dependency has not yet been added to `porylive_macro_data.json`.
  - Feel free to submit an issue with the script that crashes only with Porylive enabled.

#### Steps to add a macro:
1. **Identify the macro** and which command accepts an address (should always be a `.4byte` command)
  - A macro that does not require an address does not need to be added.
1. **Add an entry** to `tools/porylive/porylive_macro_data.json` under the correct file name
1. **Specify the parameter handling**:
   - `"type": "dynamic"` - Auto-detect based on content _(you can usually try this one first)_
     - If the macro invokes another macro, you can also manually figure out the offset by counting the number of bytes.
   - `"type": "address"` - Always uses a direct address from pokeemerald.map
   - `"type": "macro"` - The macro invokes another macro which uses an address. Use this only if a param is not passed to the nested macro.
1. **Specify the params** for the macro.
   - `"index"` - The argument index in the macro
     - For example, `ptr2` is index 1 for `.macro choosetwoturnanim ptr1:req, ptr2:req`
   - `"offset"` - The offset of the param in the macro
     - For example, `ptr2` is offset 5 for `choosetwoturnanim` because a `.byte` and `.4byte` preceed it.

Example macro and entry:
```asm
.macro your_macro_name ptr1:req, value:req, ptr2:req
  .byte 0x00
  .4byte \ptr1
  .2byte \value
  .4byte \ptr2
```
```json
"your_macro_name": [
  { "index": 0, "offset": 1, "type": "dynamic" },
  { "index": 1, "offset": 7, "type": "address" }
]
```

## Limitations

- **Beta software**: May have bugs or unexpected behavior
- **Limited support**: Only certain script files and macros are supported
- **Memory constraints**: Using porylive for too long without rebuilding the ROM may exceed buffer limits
  - Limit of **1kb of script data** and **200 individual scripts**

## Support

If you encounter issues:

1. Check the `.porylive/porylive_on_change.log` file for error messages
2. Verify your setup matches the requirements
3. Try the [troubleshooting steps](#troubleshooting) above
4. Report bugs with detailed reproduction steps, providing the script that causes the issue

You can reach out to me on Discord at `@te_on` if you have any questions or feedback.
