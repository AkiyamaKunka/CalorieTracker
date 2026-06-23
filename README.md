# 📸 AI Calorie Tracker: The "Zero-Tap" Diet Assistant

Welcome to the **AI Calorie Tracker**—a completely frictionless, entirely automated dietary tracking system that makes counting calories as easy as snapping a photo.

No more manually searching databases. No more guessing portion sizes. No more clunky apps.

### ✨ Key Features

- **📱 True "Zero-Tap" iPhone Sync**
  Forget manual uploads. With our custom iOS Shortcut background automation, you simply take a picture of your food with your iPhone's native Camera app and lock your phone. The system silently grabs the photo in the background, compresses it, and beams it to the server automatically.
  
- **🧠 Advanced Visual AI (Powered by Google Gemini)**
  Our backend AI instantly analyzes the image, identifies the food items, estimates the portion sizes, and calculates the exact Macros (Protein, Carbs, Fats) and total Calories with stunning accuracy.
  
- **💬 Telegram Bot Interface**
  Receive your calorie breakdowns directly in Telegram within seconds. Need to correct an entry? Just reply to the bot in natural language: *"Change the lunch to 400 calories"* or *"I didn't eat the rice,"* and the AI will recalculate and update the database instantly.
  
- **🌏 Cultural Dietary Profiling**
  Unlike generic trackers that mislabel cultural foods, this tracker uses a customizable `dietary_profile.txt`. Tell it your cuisine preferences once (e.g., *"I'm Chinese, large meat rolls are Roulong"*), and the AI permanently biases its visual recognition to respect your specific cultural diet.

- **⚡ Blazing Fast Asynchronous Backend**
  Built on a highly optimized Flask/Google Cloud backend, the server handles Apple's heavy HEIC photos natively and processes AI requests completely asynchronously. The result? A system that is lightning fast and never times out.

- **📈 Daily Push Notifications**
  Every night, the system generates a comprehensive daily report of your totals and pushes it to your designated chat (e.g., WeChat via PushPlus) to keep your coach or accountability partner updated automatically.

---
*Ready to stop logging and start living? Just point, shoot, and eat. The AI Calorie Tracker handles the rest.*
