import asyncio
import logging
import json
import re
import os
import requests
from collections import deque
import datetime
import time
import random
from flask import Flask
from threading import Thread

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update
from aiohttp import web

# --- VARIABLES DE ENTORNO ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID")
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET")
ADMIN_ID = os.getenv("ADMIN_ID")
# ------------------------

# Channel ID
TELEGRAM_CHANNEL_ID =  -1001945286271
BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
MOVIES_DB_FILE = "movies.json"

# Trakt.tv constants
TRAKT_BASE_URL = "https://api.trakt.tv"

# Storage for scheduled posts and recent posts
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)

# Temporary storage for user requests and admin data
user_requests = {}
admin_data = {}
memes = [
    # URLs corregidas para que apunten directamente a las imágenes
    {"photo_url": "https://i.imgflip.com/64s72q.jpg", "caption": "Cuando te dicen que hay una película nueva... y es la que no querías."},
    {"photo_url": "https://i.imgflip.com/71j22e.jpg", "caption": "Yo esperando la película que pedí en el canal..."},
    {"photo_url": "https://i.imgflip.com/83p14j.jpg", "caption": "Mi reacción cuando el bot me dice que la película ya está en el catálogo."},
    {"photo_url": "https://i.imgflip.com/4q3e3i.jpg", "caption": "Cuando me entero que la película que quiero ya está disponible en alta calidad."},
    {"photo_url": "https://i.imgflip.com/776k1w.jpg", "caption": "Yo después de ver 3 películas seguidas en un día."}
]

# Géneros de TMDB
GENRES = {
    "Acción": 28, "Aventura": 12, "Animación": 16, "Comedia": 35, "Crimen": 80,
    "Documental": 99, "Drama": 18, "Familia": 10751, "Fantasía": 14, "Historia": 36,
    "Terror": 27, "Música": 10402, "Misterio": 9648, "Romance": 10749, "Ciencia ficción": 878,
    "Película de TV": 10770, "Suspense": 53, "Guerra": 10752, "Western": 37
}

# Logging configuration
logging.basicConfig(level=logging.INFO)

# Bot, dispatcher, and database initialization
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
movies_db = {}
AUTO_POST_COUNT = 4
MOVIES_PER_PAGE = 5

# New states for the state machine
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()
    waiting_for_requested_movie_link = State()

class MovieRequestStates(StatesGroup):
    waiting_for_movie_name = State()
    waiting_for_actor_name = State()

class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()
    waiting_for_manual_movie_info = State()

class VotingStates(StatesGroup):
    waiting_for_votes = State()

# --- Auxiliary functions for the movie database
def load_movies_db():
    global movies_db
    try:
        with open(MOVIES_DB_FILE, "r", encoding="utf-8") as f:
            movies_db = json.load(f)
            logging.info(f"Se cargaron {len(movies_db)} películas de la base de datos.")
    except (FileNotFoundError, json.JSONDecodeError):
        logging.warning("No se encontró el archivo de la base de datos o está vacío. Se creará uno nuevo.")
        movies_db = {}

def save_movies_db():
    with open(MOVIES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(movies_db, f, ensure_ascii=False, indent=4)
        logging.info("Base de datos de películas guardada con éxito.")

def find_movie_in_db(title_to_find):
    for main_title, movie_data in movies_db.items():
        if "names" in movie_data and title_to_find.lower() in [name.lower() for name in movie_data["names"]]:
            return main_title, movie_data
        elif main_title.lower() == title_to_find.lower():
            return main_title, movie_data
    return None, None

def get_movie_by_tmdb_id(tmdb_id):
    for movie_data in movies_db.values():
        if movie_data.get("id") == tmdb_id:
            return movie_data
    return None

# --- Auxiliary functions for the TMDB API
def get_movie_id_by_title(title, year=None):
    url = f"{BASE_TMDB_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "es-ES"}
    if year:
        params["year"] = year
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            return results[0].get("id")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar película en TMDB por título: {e}")
        return []

def get_movie_details(movie_id):
    url = f"{BASE_TMDB_URL}/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conectar con la API de TMDB: {e}")
        return None

def get_popular_movies():
    url = f"{BASE_TMDB_URL}/movie/popular"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "page": 1}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json().get("results", [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al obtener películas populares de TMDB: {e}")
        return []

def get_movies_by_genre(genre_id):
    url = f"{BASE_TMDB_URL}/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "with_genres": genre_id, "sort_by": "popularity.desc"}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json().get("results", [])[:5]
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar películas por género: {e}")
        return []

def get_movies_by_actor(actor_name):
    url = f"{BASE_TMDB_URL}/search/person"
    params = {"api_key": TMDB_API_KEY, "query": actor_name, "language": "es-ES"}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        actor = response.json().get("results")[0] if response.json().get("results") else None
        if not actor: return []
        
        person_id = actor.get("id")
        url = f"{BASE_TMDB_URL}/person/{person_id}/movie_credits"
        params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        movies = sorted(response.json().get("cast", []), key=lambda x: x.get("popularity", 0), reverse=True)
        return movies[:5]
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar películas por actor: {e}")
        return []

def trakt_api_search_movie(title):
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID
    }
    url = f"{TRAKT_BASE_URL}/search/movie"
    params = {"query": title}
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        results = response.json()
        if results:
            for result in results:
                tmdb_id = result.get("movie", {}).get("ids", {}).get("tmdb")
                if tmdb_id:
                    return tmdb_id
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar película en Trakt.tv: {e}")
        return None

# --- Movie message creation
def create_movie_message(movie_data, movie_link=None):
    title = movie_data.get("title", "Título no disponible")
    overview = movie_data.get("overview", "Sinopsis no disponible")
    release_date = movie_data.get("release_date", "Fecha no disponible")
    vote_average = movie_data.get("vote_average", 0)
    poster_path = movie_data.get("poster_path")

    if not overview.strip():
        overview = "Sinopsis no disponible."

    text = (
        f"<b>🎬 {title}</b>\n\n"
        f"<i>Sinopsis:</i> {overview}\n\n"
        f"📅 <b>Fecha de estreno:</b> {release_date}\n"
        f"⭐ <b>Puntuación:</b> {vote_average:.1f}/10"
    )

    if movie_link:
        post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_link)],
            [types.InlineKeyboardButton(text="📽️ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
        ])
    else:
        post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 ¿Quieres pedir una película? Pídela aquí 👇", url="https://t.me/sdmin_dy_bot?start=request")]
        ])

    poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

    return text, poster_url, post_keyboard

# --- Functions for managing messages on the channel
async def delete_old_post(movie_id_tmdb):
    found_key = None
    for key, data in movies_db.items():
        if data.get("id") == movie_id_tmdb:
            found_key = key
            break

    if found_key:
        old_message_id = movies_db[found_key].get("last_message_id")
        if old_message_id:
            try:
                await bot.delete_message(chat_id=TELEGRAM_CHANNEL_ID, message_id=old_message_id)
                logging.info(f"Mensaje anterior con ID {old_message_id} de '{found_key}' eliminado.")
                movies_db[found_key]["last_message_id"] = None
                save_movies_db()
            except Exception as e:
                logging.error(f"Error al intentar borrar el mensaje {old_message_id}: {e}")

async def send_movie_post(chat_id, movie_data, movie_link, post_keyboard):
    text, poster_url, _ = create_movie_message(movie_data, movie_link)

    try:
        if poster_url:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=poster_url,
                caption=text,
                reply_markup=post_keyboard
            )
        else:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=post_keyboard
            )

        if chat_id == TELEGRAM_CHANNEL_ID:
            movie_key = next((k for k, v in movies_db.items() if movie_data.get("id") == v.get("id")), None)
            if movie_key:
                movies_db[movie_key]["last_message_id"] = message.message_id
                save_movies_db()

        return True, message.message_id
    except Exception as e:
        logging.error(f"Error al enviar la publicación: {e}")
        return False, None

# --- Command and button handlers
@dp.message(Command("start"))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    if str(user_id) == ADMIN_ID:
        keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="➕ Agregar película"), types.KeyboardButton(text="📋 Ver catálogo")],
                [types.KeyboardButton(text="⚙️ Configuración auto-publicación"), types.KeyboardButton(text="🗳️ Iniciar votación")]
            ],
            resize_keyboard=True
        )
        await message.reply(
            "¡Hola, Administrador! Elige una opción:",
            reply_markup=keyboard,
        )
    else:
        user_keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="🔍 Buscar película"), types.KeyboardButton(text="✨ Recomiéndame")],
                [types.KeyboardButton(text="🎞️ Estrenos")]
            ],
            resize_keyboard=True
        )
        await message.reply(
            "¡Hola! Soy un bot que te ayuda a encontrar tus películas favoritas. ¡Usa el menú de abajo para empezar!",
            reply_markup=user_keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

@dp.message(F.text.contains("ordershunter.ru"))
async def delete_spam_message(message: types.Message):
    try:
        await message.delete()
    except Exception as e:
        logging.error(f"No se pudo eliminar el mensaje de spam: {e}")

@dp.message(F.text == "➕ Agregar película")
async def add_movie_start_by_text(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    await message.reply(
        "Por favor, envía el título principal y todos los nombres de la película, seguidos por el enlace, en este formato:\n"
        "Título Principal (Año) | Nombre_1, Nombre_2, Nombre_3 | Enlace_de_la_película"
    )
    await state.set_state(MovieUploadStates.waiting_for_movie_info)

@dp.message(F.text == "📋 Ver catálogo")
async def view_catalog_by_text(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    if not movies_db:
        await message.reply("Aún no hay películas en la base de datos.")
        return
    await send_catalog_page(message.chat.id, 0)

async def send_catalog_page(chat_id, page):
    movie_items = list(movies_db.items())
    start = page * MOVIES_PER_PAGE
    end = start + MOVIES_PER_PAGE
    page_movies = movie_items[start:end]
    total_pages = (len(movie_items) + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE
    text = f"**Catálogo de Películas** (Página {page + 1}/{total_pages})\n\n"
    keyboard_buttons = []
    for _, data in page_movies:
        title = data.get("names")[0] if "names" in data and data.get("names") else "Título desconocido"
        movie_id = data.get("id")
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Publicar '{title}'", callback_data=f"publish_from_catalog_{movie_id}")])
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"catalog_page_{page-1}"))
    if page + 1 < total_pages:
        pagination_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"catalog_page_{page+1}"))
    if pagination_buttons:
        keyboard_buttons.append(pagination_buttons)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("catalog_page_"))
async def navigate_catalog(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    await send_catalog_page(callback_query.message.chat.id, page)

@dp.callback_query(F.data.startswith("publish_from_catalog_"))
async def publish_from_catalog(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[-1])
    movie_info = get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return
    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return
    await delete_old_post(movie_id)
    text, poster_url, post_keyboard = create_movie_message(movie_data, movie_info.get("link"))
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"), post_keyboard)
    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

@dp.message(F.text == "⚙️ Configuración auto-publicación")
async def auto_post_config(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="2 películas al día", callback_data="set_auto_2")],
        [types.InlineKeyboardButton(text="4 películas al día", callback_data="set_auto_4")],
        [types.InlineKeyboardButton(text="6 películas al día", callback_data="set_auto_6")],
        [types.InlineKeyboardButton(text="8 películas al día", callback_data="set_auto_8")]
    ])
    await message.reply("Elige cuántas películas quieres que se publiquen automáticamente cada día:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("set_auto_"))
async def set_auto_post_count(callback_query: types.CallbackQuery):
    global AUTO_POST_COUNT
    AUTO_POST_COUNT = int(callback_query.data.split("_")[2])
    await bot.answer_callback_query(callback_query.id, f"Publicación automática configurada para {AUTO_POST_COUNT} películas al día.")
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=f"✅ Publicación automática configurada para {AUTO_POST_COUNT} películas al día."
    )

@dp.message(MovieUploadStates.waiting_for_movie_info)
async def add_movie_info(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para usar esta función.")
        await state.clear()
        return
    parts = message.text.split("|")
    if len(parts) < 3:
        await message.reply("Formato incorrecto. Por favor, usa el formato: Título Principal (Año) | Nombres | Enlace\n\nInténtalo de nuevo:")
        return
    main_title_with_year = parts[0].strip()
    names_str = parts[1].strip()
    movie_link = parts[2].strip()
    match = re.search(r'\((19|20)\d{2}\)', main_title_with_year)
    if not match:
        await message.reply("Formato de año incorrecto. Debe ser (YYYY). Por favor, corrige y envía el formato completo de nuevo.")
        return
    year = match.group(0).replace('(', '').replace(')', '')
    main_title = main_title_with_year.replace(match.group(0), '').strip()
    names = [name.strip() for name in names_str.split(',')]
    await message.reply(f"Buscando '{main_title}' del año {year} en TMDB...")
    movie_id = get_movie_id_by_title(main_title, year)
    if not movie_id:
        await message.reply(
            f"No se pudo encontrar la película '{main_title}' del año {year} en TMDB. "
            "Por favor, asegúrate de escribir el título y el año correctamente. Inténtalo de nuevo."
        )
        return
    movies_db[main_title.lower()] = {
        "names": names,
        "id": movie_id,
        "link": movie_link,
        "last_message_id": None
    }
    save_movies_db()
    await state.clear()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Agregar otra película", callback_data="add_movie_again")],
        [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{movie_id}")],
        [types.InlineKeyboardButton(text="⏰ Programar publicación", callback_data=f"schedule_movie_{movie_id}")]
    ])
    await message.reply("✅ Tu película fue agregada correctamente. ¿Qué quieres hacer ahora?", reply_markup=keyboard)

@dp.callback_query(F.data == "add_movie_again")
async def add_movie_again_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, envía la información de la siguiente película en el formato: Título Principal (Año) | Nombres | Enlace"
    )
    await state.set_state(MovieUploadStates.waiting_for_movie_info)

@dp.callback_query(F.data.startswith("publish_now_manual_"))
async def publish_now_manual_callback(callback_query: types.CallbackQuery):
    try:
        movie_id = int(callback_query.data.split("_")[3])
    except (ValueError, IndexError):
        await bot.answer_callback_query(callback_query.id, "Error: no se pudo obtener el ID de la película. Inténtalo de nuevo.", show_alert=True)
        return
    
    movie_info = get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return
    
    movie_data = get_movie_details(movie_id)
    if not movie_data:
        movie_data = {
            "title": movie_info.get("names")[0],
            "overview": "Sinopsis no disponible",
            "vote_average": 0,
            "release_date": "N/A",
            "poster_path": None,
            "id": movie_id
        }
    
    await delete_old_post(movie_id)
    text, poster_url, post_keyboard = create_movie_message(movie_data, movie_info.get("link"))
    
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"), post_keyboard)
    
    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)
    
@dp.callback_query(F.data.startswith("publish_now_from_trakt_"))
async def publish_now_from_trakt_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return
    await bot.answer_callback_query(callback_query.id, "Preparando para agregar la película...", show_alert=True)
    parts = callback_query.data.split('_')
    tmdb_id = int(parts[-1])
    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información completa de la película desde TMDB. Por favor, reinicie el proceso manualmente.")
        return
    await state.update_data(
        tmdb_id=tmdb_id,
        movie_title=movie_data.get("title"),
        original_request_id=callback_query.message.message_id
    )
    await bot.send_message(
        ADMIN_ID,
        f"Por favor, ahora envía el enlace de la película '{movie_data.get('title')}' para publicarla."
    )
    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)

@dp.message(MovieUploadStates.waiting_for_requested_movie_link)
async def process_requested_movie_link(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para usar esta función.")
        await state.clear()
        return
    movie_link = message.text.strip()
    user_data = await state.get_data()
    tmdb_id = user_data.get("tmdb_id")
    movie_title = user_data.get("movie_title")
    original_request_id = user_data.get("original_request_id")
    if not tmdb_id or not movie_title:
        await message.reply("Ocurrió un error. Por favor, reenvía el enlace. Si el problema persiste, inicia el proceso de nuevo.")
        return
    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await message.reply("No se pudo obtener la información de la película desde TMDB. Reenvía el enlace o cancela el proceso.")
        return
    main_title = movie_data.get("title")
    names = [main_title]
    if movie_data.get("original_title") != main_title:
        names.append(movie_data.get("original_title"))
    movies_db[main_title.lower()] = {
        "names": names,
        "id": tmdb_id,
        "link": movie_link,
        "last_message_id": None
    }
    save_movies_db()
    await delete_old_post(tmdb_id)
    text, poster_url, post_keyboard = create_movie_message(movie_data, movie_link)
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_link, post_keyboard)
    await state.clear()
    if success:
        await message.reply("✅ Película agregada a la base de datos y publicada con éxito.")
    else:
        await message.reply("✅ Película agregada a la base de datos, pero ocurrió un error al publicarla en el canal.")
    if original_request_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=original_request_id)
        except Exception as e:
            logging.error(f"No se pudo eliminar el mensaje original de la solicitud: {e}")

@dp.callback_query(F.data.startswith("schedule_movie_"))
async def schedule_callback(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[2])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="En 30 minutos", callback_data=f"schedule_30m_{movie_id}")],
        [types.InlineKeyboardButton(text="En 1 hora", callback_data=f"schedule_1h_{movie_id}")]
    ])
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Elige cuándo quieres programar la publicación:",
        reply_markup=keyboard
    )
    await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)

@dp.callback_query(F.data.startswith("schedule_30m_") | F.data.startswith("schedule_1h_"))
async def final_schedule_callback(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    delay_type = parts[1]
    movie_id = int(parts[2])
    delay_minutes = 0
    if delay_type == "30m":
        delay_minutes = 30
    elif delay_type == "1h":
        delay_minutes = 60
    movie_info = get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return
    await scheduled_posts.put((movie_info, delay_minutes))
    await bot.answer_callback_query(callback_query.id, f"✅ Publicación programada para dentro de {delay_minutes} minutos.", show_alert=True)
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=f"✅ Película programada para publicación."
    )

@dp.message(F.text == "🎞️ Estrenos")
async def show_estrenos_by_text(message: types.Message):
    if not movies_db:
        await message.reply("Aún no hay películas en el catálogo. ¡Pronto habrá!")
        return
    sorted_movies = sorted(movies_db.values(), key=lambda x: (x.get('last_message_id') is None, x.get('last_message_id', 0)), reverse=True)
    recent_movies = sorted_movies[:10]
    text = "**🎞️ ¡Estrenos!**\n\nAquí tienes las últimas películas publicadas en el canal. Si quieres ver una, solo escribe su nombre completo.\n\n"
    if not recent_movies or all(m.get('last_message_id') is None for m in recent_movies):
      text = "**🎞️ ¡Estrenos!**\n\nNo hay estrenos recientes publicados en el canal, pero aquí tienes una lista de películas de nuestra base de datos que podrían interesarte.\n\n"
      recent_movies = random.sample(list(movies_db.values()), min(len(movies_db), 10))
    for movie in recent_movies:
        title = movie.get("names")[0] if "names" in movie and movie.get("names") else "Título desconocido"
        text += f"- {title}\n"
    await message.reply(text, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🔍 Buscar película")
async def show_search_options_by_text(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Por Género", callback_data="search_by_genre")],
        [types.InlineKeyboardButton(text="Por Actor", callback_data="search_by_actor")],
        [types.InlineKeyboardButton(text="Por Nombre", callback_data="search_by_name")]
    ])
    await message.reply(
        "¿Cómo quieres buscar la película?",
        reply_markup=keyboard
    )

@dp.message(F.text == "✨ Recomiéndame")
async def show_recomendar_by_text(message: types.Message):
    await message.reply("Obteniendo recomendaciones...")
    popular_movies = get_popular_movies()
    text = "**✨ ¡Películas recomendadas!**\n\nAquí tienes algunas películas populares que podrían gustarte. \n\n"
    if not popular_movies:
        await message.reply("No se pudieron obtener recomendaciones en este momento.")
        return
    for movie in popular_movies[:5]:
        text += f"- {movie.get('title')}\n"
    await message.reply(text, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "search_by_genre")
async def search_by_genre_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=genre, callback_data=f"genre_{id}") for genre, id in list(GENRES.items())[i:i+3]] for i in range(0, len(GENRES), 3)
    ])
    await bot.send_message(callback_query.message.chat.id, "Elige un género:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("genre_"))
async def show_movies_by_genre(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    genre_id = int(callback_query.data.split("_")[1])
    movies = get_movies_by_genre(genre_id)
    if not movies:
        await bot.send_message(callback_query.message.chat.id, "No se encontraron películas para este género.")
        return

    await bot.send_message(callback_query.message.chat.id, "**Aquí tienes algunas películas populares para este género:**", parse_mode=ParseMode.MARKDOWN)

    for movie in movies:
        tmdb_id = movie.get("id")
        movie_data = get_movie_details(tmdb_id)
        if not movie_data:
            continue
        
        movie_in_db = get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{tmdb_id}")]
            ])

        text, poster_url, _ = create_movie_message(movie_data)
        
        try:
            if poster_url:
                await bot.send_photo(
                    chat_id=callback_query.message.chat.id,
                    photo=poster_url,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=callback_query.message.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logging.error(f"Error al enviar la publicación en el catálogo: {e}")

@dp.callback_query(F.data == "search_by_actor")
async def search_by_actor_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre del actor que quieres buscar.")
    await state.set_state(MovieRequestStates.waiting_for_actor_name)

@dp.message(MovieRequestStates.waiting_for_actor_name)
async def show_movies_by_actor(message: types.Message, state: FSMContext):
    actor_name = message.text.strip()
    await state.clear()
    movies = get_movies_by_actor(actor_name)
    if not movies:
        await message.reply(f"No se encontraron películas para el actor '{actor_name}'.")
        return
    text = f"**Aquí tienes algunas de las películas más populares de {actor_name}:**\n\n"
    keyboard_buttons = []
    for movie in movies:
        text += f"- {movie.get('title')} ({movie.get('release_date', 'N/A')[:4]})\n"
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Pedir '{movie.get('title')}'", callback_data=f"request_movie_by_id_{movie.get('id')}")])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.reply(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "search_by_name")
async def ask_for_movie_by_name(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre correcto de tu película."
    )
    await state.set_state(MovieRequestStates.waiting_for_movie_name)

@dp.message(MovieRequestStates.waiting_for_movie_name)
async def process_movie_request(message: types.Message, state: FSMContext):
    movie_title = message.text.strip()
    await state.clear()
    main_title, movie_info = find_movie_in_db(movie_title)
    if not movie_info:
        trakt_id = trakt_api_search_movie(movie_title)
        if trakt_id:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{trakt_id}")]
            ])
            user_requests[movie_title.lower()] = {"id": message.from_user.id, "title": movie_title}
            await bot.send_message(
                ADMIN_ID,
                f"El usuario {message.from_user.full_name} (@{message.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n"
                f"ℹ️ **Se encontró en Trakt.tv con ID de TMDB:** `{trakt_id}`",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
            await message.reply(
                "La película que solicitaste no está en la base de datos, pero el administrador ha sido notificado para que pueda revisarla. ¡Pronto estará lista!"
            )
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="➕ Agregar manualmente", callback_data=f"add_manually_{movie_title}_{message.from_user.id}")]
            ])
            user_requests[movie_title.lower()] = {"id": message.from_user.id, "title": movie_title}
            await bot.send_message(
                ADMIN_ID,
                f"El usuario {message.from_user.full_name} (@{message.from_user.username}) ha solicitado la película: <b>{movie_title}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
            await message.reply(
                "Lo siento, esa película aún no está disponible y no se encontró en las bases de datos. El administrador ha sido notificado de tu solicitud. ¡Pronto estará lista!"
            )
        return
    movie_id = movie_info.get("id")
    movie_link = movie_info.get("link")
    if not movie_id or not movie_link:
        await message.reply("Ocurrió un error. El administrador debe volver a subirla. Intenta contactarlo.")
        return
    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await message.reply(
            "Lo siento, hubo un problema al obtener la información de la película. Por favor, intenta de nuevo más tarde."
        )
        return
    await delete_old_post(movie_data.get("id"))
    text, poster_url, post_keyboard = create_movie_message(movie_data, movie_link)
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_link, post_keyboard)
    if success:
        await message.reply(
            f"✅ Tu película fue publicada en el canal principal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("Ocurrió un error al intentar publicar la película. Por favor, contacta al administrador.")

@dp.callback_query(F.data.startswith("add_requested_"))
async def add_requested_movie_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return
    parts = callback_query.data.split('_', 2)
    requested_title = parts[2]
    user_id = user_requests.pop(requested_title.lower(), None)
    if not user_id:
        await bot.send_message(callback_query.from_user.id, "Error: la solicitud original no fue encontrada. Intenta de nuevo.")
        return
    movie_id = get_movie_id_by_title(requested_title)
    if not movie_id:
        await bot.send_message(callback_query.from_user.id, "No se pudo encontrar la película en TMDB. No se puede continuar.")
        return
    await state.update_data(tmdb_id=movie_id, movie_title=requested_title, user_request_id=user_id)
    await bot.send_message(
        callback_query.from_user.id,
        f"Por favor, ahora envía el enlace de la película '{requested_title}'."
    )
    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)

@dp.callback_query(F.data.startswith("add_manually_"))
async def add_manually_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return
    
    parts = callback_query.data.split('_', 2)
    requested_title = parts[2]
    user_request_data = user_requests.pop(requested_title.lower(), None)
    
    if not user_request_data:
        await bot.send_message(callback_query.from_user.id, "Error: la solicitud original no fue encontrada. Intenta de nuevo.")
        return

    await state.update_data(user_request_id=user_request_data.get("id"))

    await bot.send_message(
        callback_query.from_user.id,
        "Por favor, envía los datos de la película en este formato, ya que no se encontró en las bases de datos:\n\n"
        "Título Principal (Año) | Sinopsis | Puntuación | Enlace | URL del Póster\n\n"
        "Ejemplo:\n"
        "El Padrino (1972) | Crónica de la ascensión de un capo mafioso. | 9.0 | https://t.me/enlace | https://ejemplo.com/poster.jpg"
    )
    await state.set_state(AdminStates.waiting_for_manual_movie_info)

@dp.message(AdminStates.waiting_for_manual_movie_info)
async def process_manual_movie_info(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para usar esta función.")
        await state.clear()
        return

    parts = message.text.split("|")
    if len(parts) < 5:
        await message.reply("Formato incorrecto. Por favor, asegúrate de incluir: Título (Año) | Sinopsis | Puntuación | Enlace | URL del Póster")
        return

    main_title_with_year = parts[0].strip()
    overview = parts[1].strip()
    vote_average = float(parts[2].strip()) if parts[2].strip().replace('.', '', 1).isdigit() else None
    movie_link = parts[3].strip()
    poster_url = parts[4].strip()

    if not all([main_title_with_year, overview, vote_average is not None, movie_link, poster_url]):
        await message.reply("Datos incompletos o en formato incorrecto. Por favor, inténtalo de nuevo.")
        return

    movie_id = random.randint(1000000, 9999999)
    movie_data = {
        "title": main_title_with_year,
        "overview": overview,
        "vote_average": vote_average,
        "release_date": "N/A",
        "poster_path": poster_url,
        "id": movie_id
    }
    
    movies_db[main_title_with_year.lower()] = {
        "names": [main_title_with_year],
        "id": movie_id,
        "link": movie_link,
        "last_message_id": None
    }
    save_movies_db()
    
    user_data = await state.get_data()
    user_request_id = user_data.get("user_request_id")
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{movie_id}")],
        [types.InlineKeyboardButton(text="🔔 Publicar y notificar al usuario", callback_data=f"publish_and_notify_{movie_id}_{user_request_id}")]
    ])
    
    await message.reply("✅ Película agregada manualmente. ¿Qué quieres hacer ahora?", reply_markup=keyboard)
    await state.clear()

@dp.callback_query(F.data.startswith("publish_and_notify_"))
async def publish_and_notify_callback(callback_query: types.CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    parts = callback_query.data.split("_")
    movie_id = int(parts[3])
    user_request_id = int(parts[4])
    
    movie_info = get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada.", show_alert=True)
        return
    
    movie_data_from_db = {
        "title": movie_info.get("names")[0],
        "overview": "N/A",  # Esto podría necesitar una mejora para guardar la sinopsis
        "vote_average": "N/A",
        "release_date": "N/A",
        "poster_path": movie_info.get("poster_path", None) # No se guarda en la DB, solo la URL
    }
    
    text, _, post_keyboard = create_movie_message(movie_data_from_db, movie_info.get("link"))
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data_from_db, movie_info.get("link"), post_keyboard)

    if success:
        try:
            await bot.send_message(user_request_id, f"✅ ¡Buenas noticias! La película que solicitaste, **{movie_data_from_db['title']}**, ya fue publicada en el canal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>", parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"No se pudo notificar al usuario {user_request_id}: {e}")
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada y usuario notificado.", show_alert=True)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

@dp.callback_query(F.data.startswith("request_movie_by_id_"))
async def request_movie_by_id(callback_query: types.CallbackQuery):
    tmdb_id = int(callback_query.data.split("_")[-1])
    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede solicitar.", show_alert=True)
        return
    movie_title = movie_data.get("title")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
    ])
    user_requests[movie_title.lower()] = {"id": callback_query.from_user.id, "title": movie_title}
    await bot.send_message(
        ADMIN_ID,
        f"El usuario {callback_query.from_user.full_name} (@{callback_query.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n"
        f"ℹ️ **Se encontró en TMDB con ID:** `{tmdb_id}`",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    await bot.answer_callback_query(callback_query.id, f"Tu solicitud ha sido enviada al administrador.", show_alert=True)

@dp.message(F.text == "🗳️ Iniciar votación")
async def start_voting_command(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    unposted_movies = [m for m in movies_db.values() if m.get("last_message_id") is None]
    if len(unposted_movies) < 3:
        await message.reply("No hay suficientes películas nuevas para iniciar una votación. Agrega al menos 3 películas.")
        return
    selected_movies = random.sample(unposted_movies, min(len(unposted_movies), 3))
    
    voting_data = {
        "movie_ids": [m.get("id") for m in selected_movies],
        "votes": {m.get("id"): 0 for m in selected_movies},
        "voters": set()
    }
    await state.set_state(VotingStates.waiting_for_votes)
    await state.update_data(voting_data)
    
    text = "**🗳️ ¡Vota por la próxima película!**\n\n"
    keyboard_buttons = []
    
    for movie_info in selected_movies:
        movie_data = get_movie_details(movie_info.get("id"))
        if movie_data and movie_data.get("poster_path"):
            await bot.send_photo(message.chat.id, photo=f"{POSTER_BASE_URL}{movie_data.get('poster_path')}", caption=f"**{movie_data.get('title')}**")
        else:
            await bot.send_message(message.chat.id, text=f"**{movie_info.get('names')[0]}**")
        # CORRECCIÓN: Usar callback_data en lugar de url para los botones de votación
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Votar por '{movie_info.get('names')[0]}'", callback_data=f"vote_{movie_info.get('id')}")])
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await bot.send_message(message.chat.id, text=text + "¡Elige tu favorita para que sea la próxima en publicarse!", reply_markup=keyboard)

    await asyncio.sleep(600)
    
    final_data = await state.get_data()
    if not final_data or not final_data.get("votes"):
        await message.reply("La votación ha terminado sin votos. ¡Intenta de nuevo más tarde!")
        return

    winning_movie_id = max(final_data["votes"], key=final_data["votes"].get)
    winning_movie_info = get_movie_by_tmdb_id(winning_movie_id)
    
    if winning_movie_info and final_data["votes"][winning_movie_id] > 0:
        await message.reply(f"🏆 ¡La película ganadora es **{winning_movie_info.get('names')[0]}** con {final_data['votes'][winning_movie_id]} votos! Publicando ahora...")
        movie_data = get_movie_details(winning_movie_id)
        if movie_data:
            await delete_old_post(winning_movie_id)
            text, poster_url, post_keyboard = create_movie_message(movie_data, winning_movie_info.get("link"))
            await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, winning_movie_info.get("link"), post_keyboard)
    else:
        await message.reply("La votación ha terminado sin votos. ¡Intenta de nuevo más tarde!")

    await state.clear()

@dp.callback_query(F.data.startswith("vote_"), VotingStates.waiting_for_votes)
async def process_vote(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    user_data = await state.get_data()
    voters = user_data["voters"]
    if user_id in voters:
        await bot.answer_callback_query(callback_query.id, "Ya has votado. ¡Gracias!")
        return
    movie_id = int(callback_query.data.split("_")[1])
    user_data["votes"][movie_id] += 1
    user_data["voters"].add(user_id)
    await state.update_data(user_data)
    await bot.answer_callback_query(callback_query.id, "¡Voto registrado!")


# --- Automated tasks
async def auto_post_scheduler():
    while True:
        try:
            total_posts_per_day = AUTO_POST_COUNT
            interval_seconds = 24 * 60 * 60 / total_posts_per_day
            unposted_movies = [v for v in movies_db.values() if v.get("last_message_id") is None]
            if unposted_movies:
                movie_info = random.choice(unposted_movies)
                movie_id = movie_info.get("id")
                movie_data = get_movie_details(movie_id)
                if movie_data:
                    logging.info("Hora de una nueva publicación automática.")
                    await delete_old_post(movie_id)
                    text, poster_url, post_keyboard = create_movie_message(movie_data, movie_info.get("link"))
                    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"), post_keyboard)
                    if success:
                        logging.info(f"Publicación automática de '{movie_data.get('title')}' enviada con éxito.")
                    else:
                        logging.error("Error al enviar la publicación automática.")
                else:
                    logging.error("Error: No se pudo obtener la información de la película para la publicación automática.")
            await asyncio.sleep(interval_seconds)
        except Exception as e:
            logging.error(f"Error en el programador de publicaciones automáticas: {e}")
            await asyncio.sleep(60)

async def check_scheduled_posts():
    while True:
        try:
            while not scheduled_posts.empty():
                movie_info, delay = scheduled_posts.get_nowait()
                logging.info(f"Programando publicación para '{movie_info.get('names')[0]}' en {delay} minutos.")
                async def publish_later(movie_info, delay):
                    await asyncio.sleep(delay * 60)
                    try:
                        movie_data = get_movie_details(movie_info.get("id"))
                        if movie_data:
                            await delete_old_post(movie_info.get("id"))
                            text, poster_url, post_keyboard = create_movie_message(movie_data, movie_info.get("link"))
                            success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"), post_keyboard)
                            if success:
                                logging.info(f"Publicación programada de '{movie_data.get('title')}' enviada con éxito.")
                            else:
                                logging.error("Error al enviar la publicación programada.")
                    except Exception as e:
                        logging.error(f"Error en la tarea de publicación programada: {e}")
                asyncio.create_task(publish_later(movie_info, delay))
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Error en la tarea de revisión de publicaciones programadas: {e}")
            await asyncio.sleep(60)

async def channel_content_scheduler():
    while True:
        try:
            content_type = random.choice(["meme", "news"])
            if content_type == "meme":
                meme = random.choice(memes)
                try:
                    await bot.send_photo(TELEGRAM_CHANNEL_ID, photo=meme["photo_url"], caption=meme["caption"])
                    logging.info("Meme publicado con éxito.")
                except Exception as e:
                    logging.error(f"Error al publicar un meme: {e}")
            elif content_type == "news":
                popular_movies = get_popular_movies()
                if popular_movies:
                    movie = random.choice(popular_movies)
                    text = f"**Novedad del cine:** '{movie.get('title')}' - {movie.get('overview', 'Sinopsis no disponible')}"
                    
                    poster_path = movie.get("poster_path")
                    poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path else None
                    
                    try:
                        if poster_url:
                            await bot.send_photo(TELEGRAM_CHANNEL_ID, photo=poster_url, caption=text, parse_mode=ParseMode.MARKDOWN)
                        else:
                            await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
                        logging.info("Noticia de cine publicada con éxito.")
                    except Exception as e:
                        logging.error(f"Error al publicar una noticia: {e}")
            await asyncio.sleep(4 * 3600)
        except Exception as e:
            logging.error(f"Error en el programador de contenido del canal: {e}")
            await asyncio.sleep(60)

# --- WEBHOOK SETUP ---
async def on_startup(app):
    WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') + '/webhook'
    await bot.set_webhook(WEBHOOK_URL)
    logging.info("Webhook establecido con éxito.")

async def handle_telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return web.Response()
    except Exception as e:
        logging.error(f"Error al procesar el webhook de Telegram: {e}")
        return web.Response(text="Error", status=500)

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/webhook', handle_telegram_webhook)
    app.on_startup.append(on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

# --- MAIN EXECUTION ---
async def main():
    load_movies_db()
    
    # Inicia las tareas automáticas
    auto_post_task = asyncio.create_task(auto_post_scheduler())
    scheduled_posts_task = asyncio.create_task(check_scheduled_posts())
    channel_content_task = asyncio.create_task(channel_content_scheduler())
    
    # Inicia el servidor de webhook
    webhook_task = asyncio.create_task(start_webhook_server())

    try:
        await asyncio.gather(auto_post_task, scheduled_posts_task, channel_content_task, webhook_task)
    except asyncio.CancelledError:
        logging.info("Las tareas automáticas han sido canceladas.")
    except Exception as e:
        logging.error(f"Error general en la ejecución del bot: {e}")
        
if __name__ == "__main__":
    asyncio.run(main())

