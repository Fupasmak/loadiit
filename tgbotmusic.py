import os
import asyncio
import json
import hashlib
import yt_dlp
import logging
import re
import urllib.request
import urllib.parse
from PIL import Image
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import (
    Message, FSInputFile, InlineQuery, ChosenInlineResult,
    InlineQueryResultArticle, InputTextMessageContent, 
    InputMediaAudio, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error

# Настройка логирования LoadIt X
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LoadIt_X")

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения")

ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "5192928148"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = 'audio_db.json'
STATS_FILE = 'stats_db.json'

# Глобальные кеши для работы сессии
search_cache = {}
temp_map = {}
SC_CLIENT_ID = None

# === РАБОТА С БАЗОЙ ДАННЫХ ===
def load_db(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения DB {file_path}: {e}")
            return {}
    return {}

def save_db(db, file_path):
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения DB {file_path}: {e}")

audio_db = load_db(DB_FILE)
stats_db = load_db(STATS_FILE)

if 'users' not in stats_db: stats_db['users'] = {}

def track_user(user_id):
    uid = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if uid not in stats_db['users'] or stats_db['users'][uid] != today:
        stats_db['users'][uid] = today
        save_db(stats_db, STATS_FILE)

def get_short_id(text):
    return hashlib.md5(str(text).encode()).hexdigest()[:10]

# === ЛОГИКА ЗАГРУЗКИ И ОБРАБОТКИ ===
def embed_metadata(mp3_path, cover_path, title, artist):
    # Без ffmpeg мы не всегда получаем mp3. ID3-метаданные можно безопасно
    # вшивать только в mp3, для m4a/webm/opus просто отправляем thumbnail в Telegram.
    if not str(mp3_path).lower().endswith('.mp3'):
        return
    try:
        audio = MP3(mp3_path, ID3=ID3)
        try: audio.add_tags()
        except error: pass
        
        if cover_path and os.path.exists(cover_path):
            with open(cover_path, 'rb') as img:
                audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img.read()))
        
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save(v2_version=3)
    except Exception as e:
        logger.error(f"Metadata error: {e}")

def get_best_thumbnail_url(info):
    urls = []
    if info.get('thumbnail'):
        urls.append(info.get('thumbnail'))
    for t in info.get('thumbnails') or []:
        if isinstance(t, dict) and t.get('url'):
            urls.append(t.get('url'))

    # Берём самый крупный/последний URL, SoundCloud часто отдаёт mini/large/t500x500.
    url = urls[-1] if urls else None
    if not url:
        return None

    # Улучшаем качество SoundCloud artwork, если URL в типичном формате sndcdn.
    replacements = ['-mini.', '-small.', '-t67x67.', '-large.', '-t300x300.']
    for marker in replacements:
        if marker in url:
            url = url.replace(marker, '-t500x500.')
            break
    return url

def download_cover_jpg(info, filename):
    url = get_best_thumbnail_url(info)
    if not url:
        return None

    cover_path = f"{filename}.jpg"
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Referer': 'https://soundcloud.com/'
        })
        with urllib.request.urlopen(req, timeout=15) as response:
            image_data = response.read()

        tmp_path = f"{filename}_cover_src"
        with open(tmp_path, 'wb') as f:
            f.write(image_data)

        with Image.open(tmp_path) as img:
            img = img.convert('RGB')
            img.thumbnail((320, 320))
            canvas = Image.new('RGB', (320, 320), (0, 0, 0))
            x = (320 - img.width) // 2
            y = (320 - img.height) // 2
            canvas.paste(img, (x, y))

            # Telegram thumbnail должен быть маленьким. Снижаем качество, если файл > 190 KB.
            quality = 90
            while quality >= 55:
                canvas.save(cover_path, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(cover_path) <= 190 * 1024:
                    break
                quality -= 10

        try: os.remove(tmp_path)
        except: pass

        return cover_path if os.path.exists(cover_path) else None
    except Exception as e:
        logger.error(f"Cover download error: {e}")
        return None

async def download_track(query, filename):
    loop = asyncio.get_event_loop()
    is_url = bool(re.search(r'https?://', query))
    search_query = query if is_url else f"scsearch1:{query}"

    # Версия без ffmpeg: скачиваем готовый аудиофайл и отправляем его как есть.
    # Это нужно для хостингов, где нельзя поставить системный ffmpeg.
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
        'outtmpl': f'{filename}.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'writethumbnail': False,
        'no_warnings': True,
        'default_search': 'scsearch',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=True)
            return info['entries'][0] if 'entries' in info else info

    info = await loop.run_in_executor(None, _extract)

    # Берём любой скачанный аудиофайл. Не конвертируем в mp3, потому что ffmpeg на хостинге нет.
    audio_exts = ('.m4a', '.mp3', '.webm', '.opus', '.ogg', '.aac', '.mp4')
    audio_file = next((f for f in os.listdir() if f.startswith(filename) and f.lower().endswith(audio_exts)), None)

    if not audio_file:
        candidates = [f for f in os.listdir() if f.startswith(filename)]
        raise Exception(f"Файл не найден после загрузки. Найдено: {candidates}")

    title = info.get('title', 'Unknown')
    artist = info.get('uploader') or info.get('user', {}).get('username') or 'SoundCloud'

    # Обложку делаем без ffmpeg: скачиваем artwork и пережимаем в JPG через Pillow.
    cover_file = download_cover_jpg(info, filename)

    # Если повезло и источник уже mp3 — вшиваем ID3-метаданные/обложку без ffmpeg.
    # Если это m4a/webm/opus — Telegram всё равно получит thumbnail отдельным параметром.
    embed_metadata(audio_file, cover_file, title, artist)

    return info, audio_file, cover_file, title, artist

async def download_and_send_single(inline_id, query, user_id, chat_id=None, wait_msg=None, is_album_part=False):
    safe_id = get_short_id(str(inline_id) + str(query) + str(datetime.now().timestamp()))
    filename = f"tmp_{safe_id}"
    try:
        info, mp3_path, cover_path, title, artist = await download_track(query, filename)
        
        thumb = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None
        temp_msg = await bot.send_audio(chat_id=ADMIN_ID, audio=FSInputFile(mp3_path), thumbnail=thumb, title=title, performer=artist)
        
        f_id, f_uid = temp_msg.audio.file_id, temp_msg.audio.file_unique_id
        
        if f_uid not in audio_db:
            best_thumb = info.get('thumbnail') or (info.get('thumbnails', [{}])[0].get('url') if info.get('thumbnails') else None)
            audio_db[f_uid] = {'file_id': f_id, 'title': title, 'performer': artist, 'thumb': best_thumb, 'users': []}
        
        if user_id not in audio_db[f_uid]['users']:
            audio_db[f_uid]['users'].append(user_id)
        save_db(audio_db, DB_FILE)

        if inline_id:
            await bot.edit_message_media(media=InputMediaAudio(media=f_id, title=title, performer=artist), inline_message_id=inline_id)
        elif chat_id:
            await bot.send_audio(chat_id=chat_id, audio=f_id)
            if wait_msg and not is_album_part: 
                try: await wait_msg.delete()
                except: pass
                
        try: await bot.delete_message(chat_id=ADMIN_ID, message_id=temp_msg.message_id)
        except: pass
    except Exception as e:
        logger.error(f"Single Download Error: {e}")
        if wait_msg and not is_album_part: 
            try: await wait_msg.edit_text("❌ Ошибка загрузки")
            except: pass
    finally:
        await asyncio.sleep(1)
        for f in os.listdir():
            if f.startswith(filename):
                try: os.remove(f)
                except: pass

async def perform_swap(inline_id, query, user_id, chat_id=None, wait_msg=None):
    is_url = bool(re.search(r'https?://', query))
    is_playlist = False
    if is_url:
        if any(x in query for x in ['/sets/', '/album/', 'playlist?list=', '/playlist/', '/likes']):
            is_playlist = True

    if is_playlist and chat_id:
        if wait_msg: 
            try: await wait_msg.edit_text("🔍 Анализирую содержимое...")
            except: pass
        
        loop = asyncio.get_event_loop()
        def _get_playlist():
            ydl_opts = {
                'quiet': True, 
                'extract_flat': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'http_headers': { 'Referer': 'https://soundcloud.com/' }
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(query, download=False)
        try:
            p_info = await loop.run_in_executor(None, _get_playlist)
            entries = p_info.get('entries', [])
            p_title = p_info.get('title', 'Плейлист/Лайки')
            
            if not entries:
                if wait_msg: await wait_msg.edit_text("❌ Список пуст или недоступен.")
                return
                
            if wait_msg: await wait_msg.edit_text(f"💽 Начинаю загрузку:\n**{p_title}**\nТреков: {len(entries)}\n\nПожалуйста, подожди...")
            
            for i, entry in enumerate(entries):
                track_url = entry.get('url') or entry.get('webpage_url')
                if not track_url: continue
                
                try:
                    if wait_msg: await wait_msg.edit_text(f"⏳")
                except: pass
                
                await download_and_send_single(None, track_url, user_id, chat_id, wait_msg=None, is_album_part=True)
                await asyncio.sleep(1.2)
                
            if wait_msg: await wait_msg.edit_text(f"✅ Загрузка **{p_title}** завершена!")
        except Exception as e:
            logger.error(f"Playlist Error: {e}")
            if wait_msg: await wait_msg.edit_text("❌ Ошибка при обработке содержимого.")
    else:
        if is_playlist and inline_id:
            loop = asyncio.get_event_loop()
            def _get_first():
                with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
                    p = ydl.extract_info(query, download=False)
                    return p.get('entries', [{}])[0].get('url')
            try:
                query = await loop.run_in_executor(None, _get_first)
            except: pass
            
        await download_and_send_single(inline_id, query, user_id, chat_id, wait_msg, is_album_part=False)

# === ДОП. ПОИСК (ФИКС ЧЕРЕЗ SOUNDCLOUD API V2) ===
def get_soundcloud_client_id():
    global SC_CLIENT_ID
    if SC_CLIENT_ID: return SC_CLIENT_ID
    try:
        req = urllib.request.Request("https://soundcloud.com", headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        js_urls = re.findall(r'src="(https://[^"]+\.sndcdn\.com/assets/[^"]+\.js)"', html)
        for url in reversed(js_urls):
            try:
                js_req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                js = urllib.request.urlopen(js_req, timeout=5).read().decode('utf-8')
                match = re.search(r'client_id:"([a-zA-Z0-9]{32})"', js)
                if match:
                    SC_CLIENT_ID = match.group(1)
                    return SC_CLIENT_ID
            except: continue
    except Exception as e:
        logger.error(f"Error fetching SC client_id: {e}")
    
    # Фолбэк, если парсинг не удался
    SC_CLIENT_ID = "a3e059563d7fd3372b49b37f00a00bcf"
    return SC_CLIENT_ID

def _search_soundcloud_sets(query, stype):
    client_id = get_soundcloud_client_id()
    # SoundCloud API использует разные эндпоинты для альбомов и плейлистов
    endpoint = "albums" if stype == "album" else "playlists_without_albums"
    
    api_url = f"https://api-v2.soundcloud.com/search/{endpoint}?q={urllib.parse.quote(query)}&client_id={client_id}&limit=10"
    
    try:
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Origin': 'https://soundcloud.com',
            'Referer': 'https://soundcloud.com/'
        })
        res = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        data = json.loads(res)
        
        final_sets = []
        for item in data.get('collection', []):
            url = item.get('permalink_url')
            if not url: continue
            
            final_sets.append({
                'title': item.get('title', 'Unknown'),
                'webpage_url': url,
                'uploader': item.get('user', {}).get('username', 'SoundCloud'),
                'url': url
            })
            
        return final_sets[:10]
    except Exception as e:
        logger.error(f"API Search Error: {e}")
        return []

# === ПАГИНАЦИЯ И КЛАВИАТУРЫ ===
def get_pagination_kb(results, page, page_size=5, prefix="p"):
    builder = InlineKeyboardBuilder()
    start = page * page_size
    current = results[start:start+page_size]
    
    for res in current:
        if 'file_id' in res:
            name = f"{res.get('performer')} - {res.get('title')}"
            sid = get_short_id(res['file_id'])
            builder.row(types.InlineKeyboardButton(text=f"📜 {name[:35]}", callback_data=f"dl:h_{sid}"))
        else:
            artist = res.get('uploader') or res.get('user', {}).get('username') or 'SoundCloud'
            name = f"{artist} - {res.get('title')}"
            url = res.get('webpage_url') or res.get('url', '')
            is_set = '/sets/' in url or '/album/' in url
            icon = "💽" if is_set else "🎵"
            
            sid = get_short_id(url)
            temp_map[sid] = url
            builder.row(types.InlineKeyboardButton(text=f"{icon} {name[:35]}", callback_data=f"dl:s_{sid}"))
            
    nav = []
    total = (len(results) + page_size - 1) // page_size
    if total <= 0: return builder.as_markup()
    
    if page > 0: nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:{page-1}"))
    nav.append(types.InlineKeyboardButton(text=f"{page+1}/{total}", callback_data="none"))
    if start + page_size < len(results): nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:{page+1}"))
    builder.row(*nav)
    return builder.as_markup()

def get_search_type_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🎵 Треки", callback_data="stype:track"))
    builder.row(types.InlineKeyboardButton(text="💽 Альбомы", callback_data="stype:album"))
    builder.row(types.InlineKeyboardButton(text="📜 Плейлисты", callback_data="stype:playlist"))
    return builder.as_markup()

# === АДМИНКА ===
@dp.message(F.text == "📊 Посмотреть статистику", F.from_user.id == ADMIN_ID)
async def admin_stats(m: Message):
    today = datetime.now().strftime("%Y-%m-%d")
    total_u = len(stats_db.get('users', {}))
    active_today = sum(1 for d in stats_db['users'].values() if d == today)
    sorted_tracks = sorted(audio_db.values(), key=lambda x: len(x.get('users', [])), reverse=True)
    top_10 = sorted_tracks[:10]
    
    text = f"📈 **LoadIt X: Статистика**\n\n👥 Всего: {total_u}\n🔥 Сегодня: {active_today}\n💾 База: {len(audio_db)}\n\n🔝 **Топ-10:**\n"
    for i, t in enumerate(top_10, 1):
        text += f"{i}. {t.get('performer')} - {t.get('title')} ({len(t.get('users'))})\n"
    await m.answer(text)

# === ОБРАБОТЧИКИ ===
@dp.message(Command("start", "history"))
async def cmd_start_history(m: Message):
    track_user(m.from_user.id)
    if m.text == "/history":
        history = [d for d in audio_db.values() if m.from_user.id in d.get('users', [])]
        if not history: return await m.answer("Твоя история пуста.")
        return await m.answer("📜 Твоя история:", reply_markup=get_pagination_kb(history[::-1], 0, prefix="h"))
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Посмотреть статистику")]] if m.from_user.id == ADMIN_ID else [], resize_keyboard=True)
    await m.answer("Привет! Пришли ссылку на трек/плейлист или название для поиска.", reply_markup=kb)

@dp.message(F.text.regexp(r'https?://\S+'))
async def link_handler(m: Message):
    track_user(m.from_user.id)
    url_match = re.search(r'(https?://\S+)', m.text)
    if not url_match: return
    url = url_match.group(1)
    
    if "soundcloud.com/" in url and not any(x in url for x in ["/sets/", "/likes", "playlist", "/track", "/album/"]):
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="❤️ Скачать лайки профиля", callback_data=f"dl_likes:{get_short_id(url)}"))
        temp_map[get_short_id(url)] = url
        return await m.answer(f"Обнаружен профиль SoundCloud. Что вы хотите сделать?", reply_markup=builder.as_markup())

    wait = await m.answer("⏳ Анализирую ссылку...")
    asyncio.create_task(perform_swap(None, url, m.from_user.id, chat_id=m.chat.id, wait_msg=wait))

@dp.message(F.text, ~F.text.startswith("/"))
async def search_handler(m: Message):
    track_user(m.from_user.id)
    search_cache[m.from_user.id] = {"query": m.text}
    await m.answer(f"Что именно искать по запросу «{m.text}»?", reply_markup=get_search_type_kb())

@dp.callback_query(F.data.startswith("stype:"))
async def search_type_callback(call: CallbackQuery):
    stype = call.data.split(":")[1]
    user_data = search_cache.get(call.from_user.id)
    if not user_data: return await call.answer("Запрос устарел. Введите название снова.")
    
    query = user_data["query"]
    await call.message.edit_text(f"🔍 Ищу {stype} по запросу: {query}...")
    
    loop = asyncio.get_event_loop()
    if stype == "track":
        def _search_tracks():
            ydl_opts = {'quiet': True, 'extract_flat': True, 'user_agent': 'Mozilla/5.0'}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try: 
                    info = ydl.extract_info(f"scsearch10:{query}", download=False)
                    return info.get('entries', []) if info else []
                except Exception: return []
        res = await loop.run_in_executor(None, _search_tracks)
    else:
        res = await loop.run_in_executor(None, lambda: _search_soundcloud_sets(query, stype))
    
    if not res: return await call.message.edit_text(f"❌ Ничего не найдено в категории {stype}. Попробуйте другое название.")
    search_cache[call.from_user.id]["results"] = res
    await call.message.edit_text(f"Результаты ({stype}): {query}", reply_markup=get_pagination_kb(res, 0, prefix="p"))

@dp.callback_query(F.data.startswith("dl_likes:"))
async def download_likes_callback(call: CallbackQuery):
    sid = call.data.split(":")[1]
    url = temp_map.get(sid)
    if not url: return await call.answer("Ошибка ссылки.")
    
    likes_url = url.rstrip('/') + "/likes"
    wait = await call.message.answer("❤️ Собираю ваши лайки...")
    asyncio.create_task(perform_swap(None, likes_url, call.from_user.id, chat_id=call.message.chat.id, wait_msg=wait))
    await call.answer()

@dp.callback_query(lambda c: c.data.startswith(("p:", "h:")) or c.data == "none")
async def pagination_handler(call: CallbackQuery):
    if call.data == "none": return await call.answer()
    prefix, pg = call.data.split(":")
    pg = int(pg)
    if prefix == "p":
        results = search_cache.get(call.from_user.id, {}).get('results', [])
        await call.message.edit_reply_markup(reply_markup=get_pagination_kb(results, pg, prefix="p"))
    else:
        history = [d for d in audio_db.values() if call.from_user.id in d.get('users', [])]
        await call.message.edit_reply_markup(reply_markup=get_pagination_kb(history[::-1], pg, prefix="h"))
    await call.answer()

@dp.callback_query(F.data.startswith("dl:"))
async def download_callback(call: CallbackQuery):
    dtype, sid = call.data.split(":")[1].split("_")
    if dtype == "s":
        url = temp_map.get(sid)
        if url:
            wait_msg = await call.message.answer("⏳ Начинаю загрузку...")
            asyncio.create_task(perform_swap(None, url, call.from_user.id, chat_id=call.message.chat.id, wait_msg=wait_msg))
    else:
        track = next((d for d in audio_db.values() if get_short_id(d['file_id']) == sid), None)
        if track: await call.message.answer_audio(track['file_id'])
    await call.answer()

@dp.inline_query()
async def inline_search(q: InlineQuery):
    track_user(q.from_user.id)
    results, txt = [], q.query.strip().lower()
    kb = InlineKeyboardBuilder().button(text="⏳", callback_data="none").as_markup()

    if not txt:
        history = [d for d in audio_db.values() if q.from_user.id in d.get('users', [])]
        for d in history[::-1][:20]:
            sid = get_short_id(d['file_id'])
            results.append(InlineQueryResultArticle(
                id=f"h_{sid}", title=f"📜 {d['performer']} - {d['title']}", 
                thumbnail_url=d.get('thumb'),
                input_message_content=InputTextMessageContent(message_text="⏳"),
                reply_markup=kb
            ))
    else:
        loop = asyncio.get_event_loop()
        def _search_t():
            with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
                try: 
                    info = ydl.extract_info(f"scsearch10:{txt}", download=False)
                    return info.get('entries', []) if info else []
                except Exception: return []
        tracks = await loop.run_in_executor(None, _search_t)
        
        for r in tracks:
            url = r.get('url') or r.get('webpage_url', '')
            sid = get_short_id(url)
            temp_map[sid] = url
            thumb = r.get('thumbnail') or (r.get('thumbnails', [{}])[0].get('url') if r.get('thumbnails') else None)
            artist = r.get('uploader') or r.get('user', {}).get('username') or 'SoundCloud'
            results.append(InlineQueryResultArticle(
                id=f"s_{sid}", title=r.get('title')[:100],
                description=f"🎵 Трек | {artist}",
                thumbnail_url=thumb,
                input_message_content=InputTextMessageContent(message_text=f"⏳"),
                reply_markup=kb
            ))
    await q.answer(results, cache_time=5, is_personal=True)

@dp.chosen_inline_result()
async def chosen_result(c: ChosenInlineResult):
    try:
        parts = c.result_id.split("_")
        if len(parts) < 2: return
        dtype, sid = parts[0], parts[1]
        if dtype == "s":
            query = temp_map.get(sid)
            if query: asyncio.create_task(perform_swap(c.inline_message_id, query, c.from_user.id))
        elif dtype == "h":
            track = next((d for d in audio_db.values() if get_short_id(d['file_id']) == sid), None)
            if track:
                await bot.edit_message_media(
                    media=InputMediaAudio(media=track['file_id'], title=track['title'], performer=track['performer']),
                    inline_message_id=c.inline_message_id
                )
    except: pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())