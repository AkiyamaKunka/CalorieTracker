# 📸 AI Calorie Tracker: The "Zero-Tap" Diet Assistant

Welcome to the **AI Calorie Tracker**—a completely frictionless, entirely automated dietary tracking system that makes counting calories as easy as snapping a photo.

No more manually searching databases. No more guessing portion sizes. No more clunky apps.

---

## ✨ Key Features

- **📱 True "Zero-Tap" iPhone/Android Sync**
  Forget manual uploads. With our custom iOS Shortcut and Android background scripts, you simply take a picture of your food with your native Camera app. The system silently grabs the photo in the background, compresses it, and beams it to the server automatically over Wi-Fi or Cellular Data (Port 80).
  
- **🧠 Advanced Visual AI (Powered by Google Gemini 2.5 Flash)**
  Our backend AI instantly analyzes the image, identifies the food items, estimates the portion sizes, and calculates the exact Macros (Protein, Carbs, Fats) and total Calories with stunning accuracy.
  
- **💬 Telegram Bot Interface**
  Receive your calorie breakdowns directly in Telegram within seconds. Need to correct an entry? Just reply to the bot in natural language: *"Change the lunch to 400 calories"* or *"I didn't eat the rice,"* and the AI will recalculate and update the database instantly.
  
- **🌏 Cultural Dietary Profiling**
  Unlike generic trackers that mislabel cultural foods, this tracker uses a customizable `dietary_profile.txt`. Tell it your cuisine preferences once (e.g., *"I'm Chinese, large meat rolls are Roulong"*), and the AI permanently biases its visual recognition to respect your specific cultural diet.

- **⚡ Blazing Fast Asynchronous Backend**
  Built on a highly optimized Flask and Python-Telegram-Bot architecture, the server handles Apple's heavy HEIC photos natively and processes AI requests completely asynchronously. The result? A system that never times out.

- **📈 Daily Push Notifications**
  Every night at 11:30 PM, the system generates a comprehensive daily report of your totals and pushes it to your designated chat (e.g., WeChat via PushPlus) to keep your coach or accountability partner updated automatically.

---

## 🚀 Quick Start / Deployment Guide

You can easily deploy this system to your own server (Google Cloud, AWS, DigitalOcean, or a home Linux server).

### 1. Prerequisites
- **Python 3.10+** installed on your server.
- A **Gemini API Key** from [Google AI Studio](https://aistudio.google.com/app/apikey) (Free).
- A **Telegram Bot Token** from [@BotFather](https://t.me/BotFather).
- A **PushPlus Token** (Optional, for WeChat daily reports).

### 2. Clone and Install
SSH into your server and run the following commands:
```bash
# Clone the repository
git clone https://github.com/AkiyamaKunka/CalorieTracker.git
cd CalorieTracker

# Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install the required dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the root directory:
```bash
nano .env
```
Paste in your credentials:
```env
GEMINI_API_KEY=your_gemini_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
PUSHPLUS_TOKEN=your_pushplus_token  # Optional
PORT=5000
```

### 4. Running the Bot Locally (Testing)
To test the bot, simply run:
```bash
python3 telegram_bot.py
```
You should see `Starting Flask server on port 5000...` and `Starting Telegram Bot polling...`. You can now send a photo to your Telegram bot to test the AI!

### 5. Production Deployment (Running 24/7)
To keep the bot running forever in the background, we use `systemd`.

Create a service file:
```bash
sudo nano /etc/systemd/system/caloriebot.service
```
Paste the following (replace `/home/ubuntu` with your actual username path):
```ini
[Unit]
Description=Calorie Tracker Telegram Bot & Flask API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/CalorieTracker
Environment="PATH=/home/ubuntu/CalorieTracker/venv/bin"
ExecStart=/home/ubuntu/CalorieTracker/venv/bin/python3 telegram_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable caloriebot.service
sudo systemctl start caloriebot.service
```

### 6. Bypassing Cellular Network Blocks
Mobile carriers (5G/LTE) often block outbound connections to port 5000. To fix this, use `iptables` to secretly route universal HTTP port 80 to port 5000 so your phone can sync photos over cellular data without issues:
```bash
sudo iptables -t nat -I PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 5000
```

---

## 📱 Mobile Phone Setup

### iOS (iPhone)
1. Open the **Shortcuts** app and create an automation for "When Camera is closed".
2. Add a **Find Photos** action to grab the most recent photo.
3. Add a **Get Contents of URL** action:
   - **URL:** `http://YOUR_SERVER_IP/upload` (Don't include the port if you set up port 80).
   - **Method:** `POST`
   - **File:** Pass the photo object in a Form variable named `photo`.

### Android
For Android, we provide a Termux script in the `android/` folder.
1. Copy `android/upload_photo.py` and `android/android_watcher.sh` to your phone via Termux.
2. Edit `upload_photo.py` to point to your `SERVER_URL`.
3. Run `bash android_watcher.sh` in the background to automatically sync photos as they are taken!

---

*Enjoy tracking your calories completely friction-free!*
