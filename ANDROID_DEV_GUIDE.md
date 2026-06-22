# Android Development Guide & Legacy Architecture

This document is for the next agent who takes over developing the Android side of the CalorieTracker project.

## 1. The Original Android Workflow (Legacy)
Originally, Robert (the user) wanted the Android phone to automatically detect when a photo was taken and automatically log it if it was food, completely invisibly. 

We built a solution running entirely inside **Termux** on his Android phone:
- **`inotifywait`:** We used the `inotify-tools` package in Termux to monitor the Android camera directory (`/storage/emulated/0/DCIM/Camera/`).
- **Python Script (`calorie_watcher.py`):** Whenever `inotifywait` detected a new file (`CLOSE_WRITE`), it triggered a Python script.
- **Offline Caching (ntfy.sh):** The Android phone frequently disconnected from the VPN (due to the Great Firewall), preventing it from reaching the Telegram API. To solve this, we used a local caching mechanism combined with `ntfy.sh`. If the phone was offline, the script queued the photo. Once internet/VPN was restored, it processed the backlog.
- **Gemini Vision:** The Termux script originally used `gemini-2.5-flash` directly on the phone to analyze the image, and if it detected food, it forwarded the analysis to the Mac or Telegram.

## 2. Why We Scrapped It
During the "Cloud Migration", we deleted the local Mac's `android/calorie_watcher.py` because the architecture shifted to a **Native Telegram Bot**. The new workflow relies on the user simply opening the Telegram app and sending the photo directly to the bot. 
- **Reasoning:** It removed the need for complex offline queuing, VPN tunneling issues within Termux, and background battery drain.

## 3. Rebuilding the Android Side (Future Work)
The user has indicated they may want to continue developing the Android side (possibly restoring the automatic camera scanning but pointing it at the new Cloud server).

### If you rebuild the Termux Auto-Scanner:
1. **Directory to watch:** `/storage/emulated/0/DCIM/Camera/`
2. **Tooling:** You must use `inotifywait` inside Termux, wrapped in a bash loop that calls the Python script.
3. **Important Bug Fix:** Previously, the script announced "🔍 Analyzing your food photo..." for *every* photo taken (documents, cats, etc.). We fixed this by making the analysis completely silent, only sending a Telegram message if `is_food == true`. Make sure to retain this silent validation!
4. **Integration with New Cloud Backend:** Instead of using `ntfy.sh` to talk to the Mac, the new Android script should just upload the photo directly to the newly hosted Telegram Bot (or a dedicated REST API endpoint on the GCP server) to ensure all data safely reaches the central SQLite `meals.db`.
5. **Auto-Start on Boot (Termux:Boot):** 
   To ensure the script survives phone reboots, we utilized the **Termux:Boot** add-on app. 
   - You must create a shell script inside `~/.termux/boot/` (e.g., `~/.termux/boot/start_watcher.sh`).
   - Termux:Boot automatically executes all scripts in this directory when the Android OS finishes booting.
6. **Running Silently & Preventing App Kills:**
   Android's aggressive battery management will kill Termux if it runs in the background for too long. To solve this, the boot script must execute two crucial commands:
   - `termux-wake-lock`: This acquires an Android wakelock, physically preventing the CPU from sleeping and ensuring Termux stays alive in the background indefinitely.
   - `nohup`: You must launch the Python watcher using `nohup python calorie_watcher.py > /dev/null 2>&1 &` to detach the process from the terminal session so it runs completely silently in the background without UI output.

### Core Tech Stack for Android:
- Termux
- `termux-api` (for device interactions if needed)
- `inotify-tools`
- Python 3

*When working on the Android script, always remember that the device operates behind a strict firewall, meaning direct connections to Telegram or Google APIs might intermittently fail unless the VPN is active. Offline queueing or robust retry logic is essential.*
