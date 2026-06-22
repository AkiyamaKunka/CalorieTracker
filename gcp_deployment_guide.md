# GCP Deployment Guide

Your codebase has been successfully rewritten and optimized for the cloud. All legacy sync scripts have been deleted, and the app is now a pure Telegram-native bot!

Follow these exact steps to deploy it to Google Cloud for free, 24/7.

## Step 1: Create the Free Tier Server
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Go to **Compute Engine > VM instances** and click **Create Instance**.
3. Configure it exactly like this to ensure it is 100% free:
   - **Name:** `calorie-tracker-server`
   - **Region:** `us-central1`, `us-east1`, or `us-west1` *(Must be one of these!)*
   - **Machine Configuration:** E2 Series -> `e2-micro`
   - **Boot Disk:** Ubuntu 22.04 LTS (Standard persistent disk, 30GB)
4. Click **Create**.

## Step 2: Upload Your Code
Once the VM is running, click the **SSH** button next to it in the Google Cloud Console. This will open a terminal in your browser.

Run this command to create a folder for your bot:
```bash
mkdir CalorieTracker && cd CalorieTracker
```

Now, from your Mac's terminal, securely copy your code to the new server:
```bash
scp -r /Users/robertwong/CalorieTracker/* username@YOUR_GCP_EXTERNAL_IP:~/CalorieTracker/
```

## Step 3: Install Dependencies
Go back to the Google Cloud SSH terminal and run:
```bash
sudo apt update && sudo apt install -y python3-pip
cd ~/CalorieTracker
pip3 install -r requirements.txt
```

## Step 4: Run the Bot 24/7
To keep the bot running perfectly even after you close the browser window, use `nohup`:
```bash
export TELEGRAM_BOT_TOKEN="your-telegram-token"
export GEMINI_API_KEY="your-gemini-key"
export PUSHPLUS_TOKEN="your-pushplus-token"
nohup python3 telegram_bot.py > bot.log 2>&1 &
```

## Step 5: Set up the 11:30 PM Cron Job
Type `crontab -e` in the Google Cloud terminal and paste this at the very bottom:
```bash
30 23 * * * cd ~/CalorieTracker && export TELEGRAM_BOT_TOKEN="your-telegram-token" && export PUSHPLUS_TOKEN="your-pushplus-token" && python3 daily_report.py >> logs/daily_report.log 2>&1
```

> [!TIP]
> The server's timezone might be in UTC. You can run `sudo timedatectl set-timezone Your/Timezone` (e.g. `America/Los_Angeles` or `Asia/Shanghai`) before setting the cron job to ensure it runs at your local 11:30 PM!

That's it! Your bot will now run forever with absolutely zero downtime, allowing you to seamlessly track calories from any device in the world without your Mac needing to be awake!
