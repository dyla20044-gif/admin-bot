import asyncio
import logging
import re
import os
import random
from collections import deque
import datetime

import aiohttp
import motor.motor_asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web
from aiogram.types import Update
from bs4 import BeautifulSoup
import lxml

# --- VARIABLES DE ENTORNO ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID")
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET")
ADMIN_ID = os.getenv("ADMIN_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
NEWS_API_KEY = os.getenv("NEWS_API_KEY") # Nueva variable
# ----------------------------------------

# Channel ID
TELEGRAM_CHANNEL_ID = -1001945286271
BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
TRAKT_BASE_URL = "https://api.trakt.tv"

# Storage for scheduled posts and recent posts
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)

# Temporary storage for user requests and admin data
user_requests = {}
admin_data = {}

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

AUTO_POST_COUNT = 4
MOVIES_PER_PAGE = 5

# New states for the state machine
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()
    waiting_for_requested_movie_link = State()

class MovieRequestStates(StatesGroup):
    waiting_for_movie_name = State()
    waiting_for_actor_name = State()
    waiting_for_confirmation = State()

class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()
    waiting_for_manual_movie_info = State()
    waiting_for_edit_movie_info = State()

class VotingStates(StatesGroup):
    waiting_for_votes = State()

# --- Funciones de Base de Datos (Motor - Asíncrono) ---

def get_mongo_db_collection():
    try:
        connection_string = os.getenv("DATABASE_URL")
        if not connection_string:
            logging.error("DATABASE_URL no está configurada. No se puede conectar a la base de datos.")
            return None

        client = motor.motor_asyncio.AsyncIOMotorClient(connection_string)
        db = client["movies_database"]
        collection = db["movies_collection"]
        return collection
    except Exception as e:
        logging.error(f"Error al conectar con MongoDB: {e}")
        return None

async def save_movie_to_db(movie_data):
    collection = get_mongo_db_collection()
    if collection is None:
        return

    try:
        movie_id = movie_data.get("id")
        
        # En MongoDB, actualizamos si existe o insertamos si no
        await collection.update_one(
            {"id": movie_id},
            {"$set": movie_data},
            upsert=True
        )
        logging.info(f"Película '{movie_data.get('title')}' guardada/actualizada en MongoDB.")
    except Exception as e:
        logging.error(f"Error al guardar la película en MongoDB: {e}")

async def get_movie_by_tmdb_id(tmdb_id):
    collection = get_mongo_db_collection()
    if collection is None:
        return None

    try:
        movie_document = await collection.find_one({"id": tmdb_id})
        return movie_document
    except Exception as e:
        logging.error(f"Error al obtener la película de MongoDB: {e}")
        return None

async def find_movie_in_db_by_name(title_to_find):
    collection = get_mongo_db_collection()
    if collection is None:
        return None

    try:
        movie_document = await collection.find_one({
            "$or": [
                {"title": {"$regex": title_to_find, "$options": "i"}},
                {"names": {"$regex": title_to_find, "$options": "i"}}
            ]
        })
        return movie_document
    except Exception as e:
        logging.error(f"Error al buscar película por nombre en MongoDB: {e}")
        return None

async def get_all_movies():
    collection = get_mongo_db_collection()
    if collection is None:
        return []
    
    try:
        movies_list = await collection.find({}).to_list(None)
        return movies_list
    except Exception as e:
        logging.error(f"Error al obtener todas las películas de MongoDB: {e}")
        return []

async def delete_movie_from_db(movie_id):
    collection = get_mongo_db_collection()
    if collection is None:
        return

    try:
        await collection.delete_one({"id": movie_id})
        logging.info(f"Película con ID {movie_id} eliminada de MongoDB.")
    except Exception as e:
        logging.error(f"Error al eliminar la película de MongoDB: {e}")


# --- Funciones de TMDB y Trakt (aiohttp - Asíncrono) ---

async def get_movie_results_by_title(title):
    url = f"{BASE_TMDB_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "es-ES"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", [])
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar película en TMDB por título: {e}")
        return []

async def get_movie_details(movie_id):
    url = f"{BASE_TMDB_URL}/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as e:
        logging.error(f"Error al conectar con la API de TMDB: {e}")
        return None

async def get_popular_movies():
    url = f"{BASE_TMDB_URL}/movie/popular"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "page": 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", [])
    except aiohttp.ClientError as e:
        logging.error(f"Error al obtener películas populares de TMDB: {e}")
        return []

async def get_movies_by_genre(genre_id, page=1):
    url = f"{BASE_TMDB_URL}/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "with_genres": genre_id, "sort_by": "popularity.desc", "page": page}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", []), data.get("total_pages", 1)
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar películas por género: {e}")
        return [], 1

async def get_upcoming_movies():
    url = f"{BASE_TMDB_URL}/discover/movie"
    current_year = datetime.datetime.now().year
    params = {
        "api_key": TMDB_API_KEY,
        "language": "es-ES",
        "sort_by": "popularity.desc",
        "primary_release_date.gte": f"{current_year}-01-01",
        "vote_count.gte": 50,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", [])
    except aiohttp.ClientError as e:
        logging.error(f"Error al obtener próximos estrenos de TMDB: {e}")
        return []

async def get_movies_by_actor(actor_name):
    url = f"{BASE_TMDB_URL}/search/person"
    params = {"api_key": TMDB_API_KEY, "query": actor_name, "language": "es-ES"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                actor = (await response.json()).get("results")[0] if (await response.json()).get("results") else None
                if not actor: return []
                
                person_id = actor.get("id")
                url = f"{BASE_TMDB_URL}/person/{person_id}/movie_credits"
                params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    movies = sorted((await response.json()).get("cast", []), key=lambda x: x.get("popularity", 0), reverse=True)
                    return movies[:5]
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar películas por actor: {e}")
        return []

async def trakt_api_search_movie(title):
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID
    }
    url = f"{TRAKT_BASE_URL}/search/movie"
    params = {"query": title}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                response.raise_for_status()
                results = await response.json()
                if results:
                    for result in results:
                        tmdb_id = result.get("movie", {}).get("ids", {}).get("tmdb")
                        if tmdb_id:
                            return tmdb_id
                return None
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar película en Trakt.tv: {e}")
        return None

# --- NUEVAS FUNCIONES PARA NOTICIAS Y MEMES ---
async def get_latest_news():
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": "cine",
        "sortBy": "publishedAt",
        "language": "es",
        "apiKey": NEWS_API_KEY,
        "pageSize": 5,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("articles", [])
    except aiohttp.ClientError as e:
        logging.error(f"Error al obtener noticias de NewsAPI: {e}")
        return []

async def get_random_meme():
    url = "https://www.reddit.com/r/memesenespanol/.json?limit=50"
    headers = {"User-Agent": "MyBot/0.1"} # Necesario para Reddit
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                posts = data['data']['children']
                # Filtra solo los posts con imágenes
                image_posts = [p for p in posts if p['data'].get('url_overridden_by_dest') and p['data']['url_overridden_by_dest'].endswith(('.jpg', '.png'))]
                if image_posts:
                    random_post = random.choice(image_posts)
                    meme_url = random_post['data']['url_overridden_by_dest']
                    meme_caption = random_post['data']['title']
                    return meme_url, meme_caption
    except aiohttp.ClientError as e:
        logging.error(f"Error al hacer scraping de memes: {e}")
    except KeyError:
        logging.error("Error al procesar la respuesta de Reddit.")
    return None, "¡Aquí tienes un meme divertido!"


def get_movie_poster_url(poster_path):
    if poster_path:
        return f"{POSTER_BASE_URL}{poster_path}"
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

    poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path and not poster_path.startswith("http") else poster_path

    return text, poster_url, post_keyboard

# --- Functions for managing messages on the channel
async def delete_old_post(movie_id_tmdb):
    movie_data = await get_movie_by_tmdb_id(movie_id_tmdb)
    if movie_data:
        old_message_id = movie_data.get("last_message_id")
        if old_message_id is not None:
            try:
                await bot.delete_message(chat_id=TELEGRAM_CHANNEL_ID, message_id=int(old_message_id))
                logging.info(f"Mensaje anterior con ID {old_message_id} de '{movie_data.get('title')}' eliminado.")
            except Exception as e:
                logging.error(f"Error al intentar borrar el mensaje {old_message_id}: {e}")

async def send_movie_post(chat_id, movie_data, movie_link, post_keyboard):
    text, poster_url, _ = create_movie_message(movie_data, movie_link)

    try:
        if poster_url and (poster_url.startswith('http://') or poster_url.startswith('https://')):
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
            movie_data["last_message_id"] = message.message_id
            await save_movie_to_db(movie_data)

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
                [types.KeyboardButton(text="🎞️ Estrenos"), types.KeyboardButton(text="📰 Noticias")] # Nuevo botón de Noticias
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
    all_movies = await get_all_movies()
    if not all_movies:
        await message.reply("Aún no hay películas en la base de datos.")
        return
    await send_catalog_page(message.chat.id, 0)

async def send_catalog_page(chat_id, page):
    movie_items = await get_all_movies()
    start = page * MOVIES_PER_PAGE
    end = start + MOVIES_PER_PAGE
    page_movies = movie_items[start:end]
    total_pages = (len(movie_items) + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE
    text = f"**Catálogo de Películas** (Página {page + 1}/{total_pages})\n\n"
    keyboard_buttons = []
    for data in page_movies:
        title = data.get("title") if data.get("title") else "Título desconocido"
        tmdb_id = data.get("id")
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Publicar '{title}'", callback_data=f"publish_from_catalog_{tmdb_id}")])
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"catalog_page_{page-1}"))
    if page + 1 < total_pages:
        pagination_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"catalog_page_{page+1}"))
    if pagination_buttons:
        keyboard_buttons.append(pagination_buttons)
    keyboard_buttons.append([types.InlineKeyboardButton(text="✍️ Editar película", callback_data="edit_movie_start")])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "edit_movie_start")
async def edit_movie_start_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.message.chat.id, "Por favor, envía el título o ID de la película que quieres editar.")
    await state.set_state(AdminStates.waiting_for_edit_movie_info)

@dp.message(AdminStates.waiting_for_edit_movie_info)
async def find_movie_to_edit(message: types.Message, state: FSMContext):
    search_query = message.text.strip()
    movie_to_edit = None
    try:
        search_id = int(search_query)
        movie_to_edit = await get_movie_by_tmdb_id(search_id)
    except ValueError:
        movie_to_edit = await find_movie_in_db_by_name(search_query)

    if not movie_to_edit:
        await message.reply("No se encontró ninguna película con ese título o ID. Inténtalo de nuevo.")
        return
    
    await state.update_data(movie_to_edit=movie_to_edit)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✏️ Editar Título/Nombres", callback_data="edit_movie_names")],
        [types.InlineKeyboardButton(text="🔗 Editar Enlace", callback_data="edit_movie_link")],
        [types.InlineKeyboardButton(text="❌ Cancelar", callback_data="cancel_edit_movie")]
    ])
    await message.reply(f"Seleccionaste la película: **{movie_to_edit.get('names', '').split(',')[0]}**. ¿Qué quieres editar?", reply_markup=keyboard)
    await state.set_state(AdminStates.waiting_for_edit_movie_info)

@dp.callback_query(F.data.startswith("edit_movie_"))
async def edit_movie_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    movie_to_edit = user_data.get("movie_to_edit")
    if not movie_to_edit:
        await bot.answer_callback_query(callback_query.id, "Error: Película no seleccionada. Intenta de nuevo.", show_alert=True)
        return
    
    if callback_query.data == "edit_movie_names":
        await bot.send_message(callback_query.message.chat.id, "Por favor, envía el nuevo título principal y los nombres de la película separados por comas. Ejemplo: `Volver al futuro, Back to the Future`")
        await state.update_data(edit_type="names")
    elif callback_query.data == "edit_movie_link":
        await bot.send_message(callback_query.message.chat.id, "Por favor, envía el nuevo enlace de la película.")
        await state.update_data(edit_type="link")
    elif callback_query.data == "cancel_edit_movie":
        await state.clear()
        await bot.send_message(callback_query.message.chat.id, "Edición cancelada.")
    
    await bot.answer_callback_query(callback_query.id)

@dp.message(AdminStates.waiting_for_edit_movie_info)
async def process_edit_movie(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    movie_to_edit = user_data.get("movie_to_edit")
    edit_type = user_data.get("edit_type")
    
    if not movie_to_edit or not edit_type:
        await message.reply("Ocurrió un error. Por favor, intenta de nuevo desde el inicio.")
        await state.clear()
        return
    
    new_value = message.text.strip()
    if edit_type == "names":
        new_names = [name.strip() for name in new_value.split(',')]
        movie_to_edit["names"] = ", ".join(new_names)
        movie_to_edit["title"] = new_names[0]
    elif edit_type == "link":
        movie_to_edit["link"] = new_value
        
    await save_movie_to_db(movie_to_edit)
    await message.reply("✅ Película actualizada correctamente.")
    await state.clear()


@dp.callback_query(F.data.startswith("catalog_page_"))
async def navigate_catalog(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception as e:
        logging.error(f"Error al borrar mensaje de catálogo: {e}")
    await send_catalog_page(callback_query.message.chat.id, page)

@dp.callback_query(F.data.startswith("publish_from_catalog_"))
async def publish_from_catalog(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[-1])
    movie_info = await get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return
    tmdb_data = await get_movie_details(movie_id)
    if not tmdb_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return
    await delete_old_post(movie_id)
    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
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
    
    movie_results = await get_movie_results_by_title(main_title)
    found_movie_id = None
    if not movie_results:
        # --- NUEVA LÓGICA DE BÚSQUEDA EN TRAKT COMO PLAN B ---
        trakt_id = await trakt_api_search_movie(main_title)
        if trakt_id:
            found_movie_id = trakt_id
    else:
        for movie in movie_results:
            if movie.get("release_date") and movie.get("release_date").startswith(year):
                found_movie_id = movie.get("id")
                break
    
    if not found_movie_id:
        await message.reply(
            f"No se pudo encontrar la película '{main_title}' del año {year} en TMDB o Trakt. "
            "Por favor, asegúrate de escribir el título y el año correctamente. Inténtalo de nuevo."
        )
        return
    
    tmdb_data = await get_movie_details(found_movie_id)
    if not tmdb_data:
        await message.reply("Ocurrió un error al obtener los detalles de la película desde TMDB.")
        return

    names_for_db = ", ".join(names)
    
    movie_data = {
        "id": tmdb_data.get("id"),
        "title": tmdb_data.get("title"),
        "names": names_for_db,
        "link": movie_link,
        "last_message_id": None
    }
    
    await save_movie_to_db(movie_data)
    await state.clear()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Agregar otra película", callback_data="add_movie_again")],
        [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{found_movie_id}")],
        [types.InlineKeyboardButton(text="⏰ Programar publicación", callback_data=f"schedule_movie_{found_movie_id}")]
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
    
    movie_info = await get_movie_by_tmdb_id(movie_id)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return
    
    tmdb_data = await get_movie_details(movie_id)
    if not tmdb_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return
    
    await delete_old_post(movie_id)
    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
    
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
    
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
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información completa de la película desde TMDB. Por favor, reinicie el proceso manualmente.")
        return
    await state.update_data(
        tmdb_id=tmdb_id,
        movie_title=tmdb_data.get("title"),
        original_request_id=callback_query.message.message_id
    )
    await bot.send_message(
        ADMIN_ID,
        f"Por favor, ahora envía el enlace de la película '{tmdb_data.get('title')}' para publicarla."
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
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await message.reply("No se pudo obtener la información de la película desde TMDB. Reenvía el enlace o cancela el proceso.")
        return
    main_title = tmdb_data.get("title")
    names = [main_title]
    if tmdb_data.get("original_title") and tmdb_data.get("original_title") != main_title:
        names.append(tmdb_data.get("original_title"))
    
    new_movie = {
        "title": main_title,
        "names": ", ".join(names),
        "id": tmdb_id,
        "link": movie_link,
        "last_message_id": None 
    }
    await save_movie_to_db(new_movie)
    await delete_old_post(tmdb_id)
    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_link)
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_link, post_keyboard)
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
    movie_info = await get_movie_by_tmdb_id(movie_id)
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
    await message.reply("Buscando los últimos estrenos...")
    upcoming_movies = await get_upcoming_movies()
    if not upcoming_movies:
        await message.reply("No se encontraron estrenos recientes en este momento.")
        return
    
    await message.reply(f"**🎞️ ¡Últimos estrenos!**\n\nAquí tienes las películas más recientes que se han estrenado.\n", parse_mode=ParseMode.MARKDOWN)
    
    for movie in upcoming_movies[:5]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{tmdb_id}")]
            ])

        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar estreno: {e}")

@dp.message(F.text == "🔍 Buscar película")
async def show_search_options_by_text(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Por Género", callback_data="search_by_genre")],
        [types.InlineKeyboardButton(text="Por Actor", callback_data="search_by_actor")],
        [types.InlineKeyboardButton(text="Por Nombre", callback_data="search_by_name")],
        [types.InlineKeyboardButton(text="✨ Solicitar una película", callback_data="request_movie_from_user")]
    ])
    await message.reply(
        "¿Cómo quieres buscar la película?",
        reply_markup=keyboard
    )

@dp.message(F.text == "✨ Recomiéndame")
async def show_recomendar_by_text(message: types.Message):
    await message.reply("Obteniendo recomendaciones...")
    popular_movies = await get_popular_movies()
    if not popular_movies:
        await message.reply("No se pudieron obtener recomendaciones en este momento.")
        return
    
    await message.reply(f"**✨ ¡Películas recomendadas!**\n\nAquí tienes algunas películas populares que podrían gustarte.\n", parse_mode=ParseMode.MARKDOWN)

    for movie in popular_movies[:5]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{tmdb_id}")]
        ])
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar recomendación: {e}")

@dp.message(F.text == "📰 Noticias") # Nuevo handler para el botón de noticias
async def send_latest_news_handler(message: types.Message):
    await message.reply("Buscando las últimas noticias de cine...")
    articles = await get_latest_news()
    if not articles:
        await message.reply("Lo siento, no se encontraron noticias de cine en este momento.")
        return

    for article in articles[:3]: # Publicar 3 noticias
        title = article.get("title", "Sin título")
        description = article.get("description", "Sin descripción")
        url = article.get("url", "#")
        image_url = article.get("urlToImage", None)

        news_text = (
            f"<b>{title}</b>\n\n"
            f"{description}\n\n"
            f"<a href='{url}'>Leer más</a>"
        )
        if image_url:
            try:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=image_url,
                    caption=news_text,
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=news_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text=news_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

@dp.callback_query(F.data == "search_by_genre")
async def search_by_genre_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=genre, callback_data=f"genre_{id}") for genre, id in list(GENRES.items())[i:i+3]] for i in range(0, len(GENRES), 3)
    ])
    await bot.send_message(callback_query.message.chat.id, "Elige un género:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("genre_"))
async def show_movies_by_genre(callback_query: types.CallbackQuery, page=1):
    await bot.answer_callback_query(callback_query.id)
    genre_id_str = callback_query.data.split('_')[1]
    genre_id = int(genre_id_str)
    
    movies, total_pages = await get_movies_by_genre(genre_id, page=page)

    if not movies:
        await bot.send_message(callback_query.message.chat.id, "No se encontraron más películas para este género.")
        return

    keyboard_buttons = []
    if page > 1:
        keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"genre_page_{genre_id}_{page-1}"))
    if page + 1 < total_pages:
        keyboard_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"genre_page_{genre_id}_{page+1}"))
        
    keyboard_pag = types.InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])

    await bot.send_message(callback_query.message.chat.id, f"**Aquí tienes algunas películas de {next((k for k, v in GENRES.items() if v == genre_id), 'este género')}:**", reply_markup=keyboard_pag, parse_mode=ParseMode.MARKDOWN)

    for movie in movies[:5]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{tmdb_id}")]
            ])

        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(chat_id=callback_query.message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=callback_query.message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar la publicación en el catálogo: {e}")

@dp.callback_query(F.data.startswith("genre_page_"))
async def navigate_genre_page(callback_query: types.CallbackQuery):
    parts = callback_query.data.split('_')
    genre_id = int(parts[2])
    page = int(parts[3])
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception as e:
        logging.error(f"Error al borrar mensaje de catálogo: {e}")
    await show_movies_by_genre(callback_query, page=page)

@dp.callback_query(F.data == "search_by_actor")
async def search_by_actor_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre del actor que quieres buscar.")
    await state.set_state(MovieRequestStates.waiting_for_actor_name)

@dp.message(MovieRequestStates.waiting_for_actor_name)
async def show_movies_by_actor(message: types.Message, state: FSMContext):
    actor_name = message.text.strip()
    await state.clear()
    
    await message.reply("Buscando las películas más populares de ese actor...")
    
    movies = await get_movies_by_actor(actor_name)
    if not movies:
        await message.reply(f"No se encontraron películas para el actor '{actor_name}'. Por favor, revisa la ortografía y vuelve a intentarlo.")
        return
    
    for movie in movies:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{tmdb_id}")]
            ])
        
        try:
            if poster_url:
                await bot.send_photo(chat_id=message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar la publicación en el catálogo del actor: {e}")

@dp.callback_query(F.data == "search_by_name")
async def ask_for_movie_by_name(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre de la película. Si hay muchas coincidencias, puedes agregar el año para una búsqueda más precisa. Ejemplo: `Volver al futuro (1985)`."
    )
    await state.set_state(MovieRequestStates.waiting_for_movie_name)

@dp.callback_query(F.data == "request_movie_from_user")
async def request_movie_from_user(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre de la película que te gustaría solicitar. Puedes agregar el año para una búsqueda más precisa. Ejemplo: `Volver al futuro (1985)`."
    )
    await state.set_state(MovieRequestStates.waiting_for_movie_name)

@dp.message(MovieRequestStates.waiting_for_movie_name)
async def process_movie_request(message: types.Message, state: FSMContext):
    movie_title = message.text.strip()
    
    movie_info_db = await find_movie_in_db_by_name(movie_title)

    if movie_info_db:
        movie_id = movie_info_db.get("id")
        movie_link = movie_info_db.get("link")
        tmdb_data = await get_movie_details(movie_id)
        if not tmdb_data:
            await message.reply("Lo siento, hubo un problema al obtener la información de la película. Por favor, intenta de nuevo más tarde.")
            return

        await delete_old_post(movie_id)
        text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_link)
        success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_link, post_keyboard)
        
        if success:
            await message.reply(
                f"✅ ¡La película ya estaba en el catálogo! Fue publicada en el canal principal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
                parse_mode=ParseMode.HTML
            )
        else:
            await message.reply("Ocurrió un error al intentar publicar la película. Por favor, contacta al administrador.")
        
        await state.clear()
        return

    await message.reply("Buscando en la base de datos de películas...")
    
    year_match = re.search(r'\((19|20)\d{2}\)', movie_title)
    if year_match:
        year = year_match.group(0).replace('(', '').replace(')', '')
        title_only = movie_title.replace(year_match.group(0), '').strip()
        movie_results = await get_movie_results_by_title(title_only)
    else:
        movie_results = await get_movie_results_by_title(movie_title)
        
    found_movie_id = None
    if not movie_results:
        # --- NUEVA LÓGICA DE BÚSQUEDA EN TRAKT COMO PLAN B ---
        trakt_id = await trakt_api_search_movie(movie_title)
        if trakt_id:
            found_movie_id = trakt_id
    else:
        for movie in movie_results:
            if movie.get("release_date") and movie.get("release_date").startswith(year if year_match else ''):
                found_movie_id = movie.get("id")
                break

    if not found_movie_id:
        await message.reply(f"No se encontraron películas con el título '{movie_title}'. Por favor, intenta de nuevo con otro nombre o revisa la ortografía.")
        await state.clear()
        return

    tmdb_data = await get_movie_details(found_movie_id)
    if not tmdb_data:
        await message.reply("Ocurrió un error al obtener los detalles de la película desde TMDB.")
        return

    user_requests[message.from_user.id] = {
        "results": [tmdb_data], # Solo mostramos el resultado encontrado
        "query": movie_title,
        "message_ids": []
    }
    
    await message.reply("Encontré una coincidencia. ¿Es esta la película que buscabas?")
    
    text, poster_url, _ = create_movie_message(tmdb_data)
    
    movie_in_db = await get_movie_by_tmdb_id(found_movie_id)
    
    if movie_in_db:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_manual_{found_movie_id}")]
        ])
    else:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id_{found_movie_id}")]
        ])
    
    try:
        if poster_url:
            sent_message = await bot.send_photo(chat_id=message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        else:
            sent_message = await bot.send_message(chat_id=message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        
        user_requests[message.from_user.id]["message_ids"].append(sent_message.message_id)

    except Exception as e:
        logging.error(f"Error al enviar la opción de película: {e}")
            
    await state.set_state(MovieRequestStates.waiting_for_confirmation)

@dp.callback_query(F.data.startswith("confirm_request_"))
async def confirm_movie_request(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    
    if user_id in user_requests:
        for msg_id in user_requests[user_id]["message_ids"]:
            try:
                await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=msg_id)
            except Exception:
                pass # Ignorar errores si el mensaje ya fue borrado
        del user_requests[user_id]
        
    await bot.answer_callback_query(callback_query.id)
    
    tmdb_id = int(callback_query.data.split("_")[-1])
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        await state.clear()
        return
        
    movie_title = tmdb_data.get("title")
    
    movie_info_db = await get_movie_by_tmdb_id(tmdb_id)
    
    if movie_info_db:
        movie_link = movie_info_db.get("link")
        await delete_old_post(tmdb_id)
        text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_link)
        success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_link, post_keyboard)
        
        if success:
            await bot.send_message(
                callback_query.message.chat.id,
                f"✅ ¡La película ya estaba en el catálogo! Fue publicada en el canal principal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(callback_query.message.chat.id, "Ocurrió un error al intentar publicar la película. Por favor, contacta al administrador.")
    else:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
        ])
        
        await bot.send_message(
            ADMIN_ID,
            f"El usuario {callback_query.from_user.full_name} (@{callback_query.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n"
            f"ℹ️ **Se encontró en TMDB con ID:** `{tmdb_id}`",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        
        await bot.send_message(callback_query.message.chat.id, "Tu solicitud ha sido enviada al administrador. ¡Pronto estará lista!")
    
    await state.clear()

@dp.message(MovieRequestStates.waiting_for_confirmation)
async def handle_non_callback_message(message: types.Message):
    await message.reply("Por favor, elige una de las opciones del catálogo o reinicia la búsqueda.")
    
@dp.callback_query(F.data.startswith("request_movie_by_id_"))
async def request_movie_by_id(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    tmdb_id = int(callback_query.data.split("_")[-1])
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        await state.clear()
        return

    movie_info_db = await get_movie_by_tmdb_id(tmdb_id)
    
    if movie_info_db:
        movie_link = movie_info_db.get("link")
        await delete_old_post(tmdb_id)
        text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_link)
        success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_link, post_keyboard)
        
        if success:
            await bot.send_message(
                callback_query.message.chat.id,
                f"✅ ¡La película ya estaba en el catálogo! Fue publicada en el canal principal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(callback_query.message.chat.id, "Ocurrió un error al intentar publicar la película. Por favor, contacta al administrador.")
        await state.clear()
        return
        
    movie_title = tmdb_data.get("title")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
    ])
    
    await bot.send_message(
        ADMIN_ID,
        f"El usuario {callback_query.from_user.full_name} (@{callback_query.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n"
        f"ℹ️ **Se encontró en TMDB con ID:** `{tmdb_id}`",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    
    await bot.send_message(callback_query.message.chat.id, f"Tu solicitud ha sido enviada al administrador.")
    await state.clear()


@dp.message(F.text == "🗳️ Iniciar votación")
async def start_voting_command(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    unposted_movies = [v for v in await get_all_movies() if str(v.get("last_message_id")) == 'None' or v.get("last_message_id") == '']
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
        tmdb_data = await get_movie_details(movie_info.get("id"))
        if tmdb_data and tmdb_data.get("poster_path"):
            await bot.send_photo(message.chat.id, photo=f"{POSTER_BASE_URL}{tmdb_data.get('poster_path')}", caption=f"**{tmdb_data.get('title')}**")
        else:
            await bot.send_message(message.chat.id, text=f"**{movie_info.get('names', '').split(',')[0]}**")
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Votar por '{movie_info.get('names', '').split(',')[0]}'", callback_data=f"vote_{movie_info.get('id')}")])
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await bot.send_message(message.chat.id, text=text + "¡Elige tu favorita para que sea la próxima en publicarse!", reply_markup=keyboard)

    # El temporizador de votación se debe ejecutar en segundo plano y notificar al terminar.
    asyncio.create_task(end_voting_task(message.chat.id, state))

@dp.callback_query(F.data.startswith("vote_"), VotingStates.waiting_for_votes)
async def process_vote(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    user_data = await state.get_data()
    voters = user_data.get("voters", set())
    if user_id in voters:
        await bot.answer_callback_query(callback_query.id, "Ya has votado. ¡Gracias!")
        return
    movie_id = int(callback_query.data.split("_")[1])
    votes = user_data.get("votes", {})
    if movie_id in votes:
        votes[movie_id] += 1
    else:
        votes[movie_id] = 1
    voters.add(user_id)
    user_data["votes"] = votes
    user_data["voters"] = voters
    await state.update_data(user_data)
    await bot.answer_callback_query(callback_query.id, "¡Voto registrado!")

async def end_voting_task(chat_id, state):
    await asyncio.sleep(600)  # 10 minutos para votar
    final_data = await state.get_data()
    if not final_data or not final_data.get("votes"):
        await bot.send_message(chat_id, "La votación ha terminado sin votos. ¡Intenta de nuevo más tarde!")
        return

    winning_movie_id = max(final_data["votes"], key=final_data["votes"].get)
    winning_movie_info = await get_movie_by_tmdb_id(winning_movie_id)
    
    if winning_movie_info and final_data["votes"][winning_movie_id] > 0:
        await bot.send_message(chat_id, f"🏆 ¡La película ganadora es **{winning_movie_info.get('names', '').split(',')[0]}** con {final_data['votes'][winning_movie_id]} votos! Publicando ahora...")
        tmdb_data = await get_movie_details(winning_movie_id)
        if tmdb_data:
            await delete_old_post(winning_movie_id)
            text, poster_url, post_keyboard = create_movie_message(tmdb_data, winning_movie_info.get("link"))
            await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, winning_movie_info.get("link"), post_keyboard)
    else:
        await bot.send_message(chat_id, "La votación ha terminado sin votos. ¡Intenta de nuevo más tarde!")

    await state.clear()

# --- Automated tasks
async def auto_post_scheduler():
    while True:
        try:
            total_posts_per_day = AUTO_POST_COUNT
            interval_seconds = 24 * 60 * 60 / total_posts_per_day
            unposted_movies = [v for v in await get_all_movies() if str(v.get("last_message_id")) == 'None' or v.get("last_message_id") == '']
            if unposted_movies:
                movie_info = random.choice(unposted_movies)
                movie_id = movie_info.get("id")
                tmdb_data = await get_movie_details(movie_id)
                if tmdb_data:
                    logging.info("Hora de una nueva publicación automática.")
                    await delete_old_post(movie_id)
                    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
                    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
                    if success:
                        logging.info(f"Publicación automática de '{tmdb_data.get('title')}' enviada con éxito.")
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
                logging.info(f"Programando publicación para '{movie_info.get('names', '').split(',')[0]}' en {delay} minutos.")
                async def publish_later(movie_info, delay):
                    await asyncio.sleep(delay * 60)
                    try:
                        tmdb_data = await get_movie_details(movie_info.get("id"))
                        if tmdb_data:
                            await delete_old_post(movie_info.get("id"))
                            text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
                            success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
                            if success:
                                logging.info(f"Publicación programada de '{tmdb_data.get('title')}' enviada con éxito.")
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
                meme_url, meme_caption = await get_random_meme()
                if meme_url:
                    try:
                        await bot.send_photo(TELEGRAM_CHANNEL_ID, photo=meme_url, caption=meme_caption)
                        logging.info("Meme publicado con éxito.")
                    except Exception as e:
                        logging.error(f"Error al publicar un meme: {e}")
            elif content_type == "news":
                articles = await get_latest_news()
                if articles:
                    article = random.choice(articles)
                    text = f"**Novedad del cine:** '{article.get('title', 'Sin título')}' - {article.get('description', 'Sinopsis no disponible')}\n\n<a href='{article.get('url')}'>Leer más</a>"
                    
                    poster_url = article.get("urlToImage")
                    
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
async def handle_home(request):
    return web.Response(text="Tu bot está activo y funcionando. ¡El webhook está configurado!")

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
    app.router.add_get('/', handle_home)
    app.on_startup.append(on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

# --- MAIN EXECUTION ---
async def main():
    
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


