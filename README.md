# 📥 Instagram Saver Bot

Instagram post, reel va story larni yuklovchi Telegram bot.

## ✨ Imkoniyatlar

- 🎬 Instagram Reels yuklash
- 🖼 Instagram Post (rasm/video) yuklash
- 📖 Instagram Stories yuklash

## 🚀 Railway da ishga tushirish

### 1. Environment Variables sozlash

Railway dashboard → Variables bo'limiga qo'shing:

| Kalit | Qiymat |
|-------|--------|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) dan olingan token |
| `API_ID` | [my.telegram.org](https://my.telegram.org) dan |
| `API_HASH` | [my.telegram.org](https://my.telegram.org) dan |

### 2. Deploy

1. GitHub ga push qiling
2. Railway da "New Project" → "Deploy from GitHub repo"
3. Repo ni tanlang — avtomatik deploy bo'ladi ✅

## 📁 Fayl tuzilmasi

```
├── bot.py            # Asosiy bot kodi
├── requirements.txt  # Python kutubxonalari
├── Procfile          # Railway process
├── nixpacks.toml     # Railway Nixpacks config
├── runtime.txt       # Python versiyasi
└── .gitignore
```

## ⚠️ Cheklovlar

- Private akkaunt postlari yuklanmaydi
- 50MB dan katta fayllar yuklanmaydi
- O'chirilgan postlar yuklanmaydi
