import asyncio
import logging
import re
import os
import random
from collections import deque
import datetime
import aiohttp
import motor.motor_asyncio
from aiogram import Bot, Dispatcher, types, F, html
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
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
# ----------------------------------------

# Channel ID
TELEGRAM_CHANNEL_ID = -1002139779491
BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
TRAKT_BASE_URL = "https://api.trakt.tv"
WELCOME_IMAGE_URL = "https://i.imgur.com/DJSUzQh.jpeg"

# Storage for scheduled posts and recent posts
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)
user_requests = {}
admin_data = {}
user_message_ids = {}

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
SEARCH_RESULTS_PER_PAGE = 5

# New states for the state machine
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()
    waiting_for_requested_movie_link = State()

class MovieRequestStates(StatesGroup):
    waiting_for_movie_name = State()
    waiting_for_actor_name = State()
    waiting_for_confirmation = State()
    waiting_for_search_query = State()

class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()
    waiting_for_manual_movie_info = State()
    waiting_for_edit_movie_info = State()

class VotingStates(StatesGroup):
    waiting_for_votes = State()

class SupportStates(StatesGroup):
    waiting_for_support_message = State()

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

async def get_movie_results_by_title(title, page=1):
    url = f"{BASE_TMDB_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "es-ES", "page": page}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", []), data.get("total_pages", 1)
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar película en TMDB por título: {e}")
        return [], 1

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

async def get_popular_movies(page=1):
    url = f"{BASE_TMDB_URL}/movie/popular"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "page": page}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", []), data.get("total_pages", 1)
    except aiohttp.ClientError as e:
        logging.error(f"Error al obtener películas populares de TMDB: {e}")
        return [], 1

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

async def get_upcoming_movies(page=1):
    url = f"{BASE_TMDB_URL}/discover/movie"
    current_year = datetime.datetime.now().year
    params = {
        "api_key": TMDB_API_KEY,
        "language": "es-ES",
        "sort_by": "popularity.desc",
        "primary_release_date.gte": f"{current_year}-01-01",
        "vote_count.gte": 50,
        "page": page,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("results", []), data.get("total_pages", 1)
    except aiohttp.ClientError as e:
        logging.error(f"Error al obtener próximos estrenos de TMDB: {e}")
        return [], 1

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
    headers = {"User-Agent": "MyBot/0.1"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                posts = data['data']['children']
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

@dp.message(F.text == "🆘 Soporte")
async def start_support_handler(message: types.Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_for_support_message)
    await message.reply("Escribe tu mensaje para el equipo de soporte. Te responderán lo antes posible.")

@dp.message(SupportStates.waiting_for_support_message)
async def process_support_message(message: types.Message, state: FSMContext):
    user_info = message.from_user
    support_message = f"<b>Nuevo mensaje de soporte:</b>\n\n" \
                      f"<b>De:</b> {user_info.full_name} (@{user_info.username if user_info.username else 'N/A'})\n" \
                      f"<b>ID:</b> <code>{user_info.id}</code>\n" \
                      f"<b>Mensaje:</b>\n" \
                      f"{message.text}"
    
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=support_message, parse_mode=ParseMode.HTML)
        await message.reply("✅ Tu mensaje ha sido enviado. Gracias por contactarnos.")
    except Exception as e:
        await message.reply("❌ Hubo un error al enviar tu mensaje. Por favor, inténtalo de nuevo más tarde.")
        logging.error(f"Error al reenviar mensaje de soporte al administrador: {e}")
    finally:
        await state.clear()

@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await state.clear()
    
    if str(user_id) == ADMIN_ID:
        keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="➕ Agregar película"), types.KeyboardButton(text="📋 Ver catálogo")],
                [types.KeyboardButton(text="⚙️ Configuración auto-publicación"), types.KeyboardButton(text="🗳️ Iniciar votación")]
            ],
            resize_keyboard=True
        )
        sent_message = await message.reply(
            "¡Hola, Administrador! Elige una opción:",
            reply_markup=keyboard,
        )
        user_message_ids[user_id] = [sent_message.message_id]

    else:
        if user_id in user_message_ids:
            for msg_id in user_message_ids[user_id]:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
        user_message_ids[user_id] = []
        
        user_keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="🔍 Buscar película"), types.KeyboardButton(text="✨ Recomiéndame")],
                [types.KeyboardButton(text="🎞️ Estrenos"), types.KeyboardButton(text="📰 Noticias"), types.KeyboardButton(text="🆘 Soporte")]
            ],
            resize_keyboard=True
        )
        
        caption = "¡Hola! Soy un bot que te ayuda a encontrar tus películas favoritas. ¡Usa el menú de abajo para empezar!"
        
        sent_message = await bot.send_photo(
            chat_id=message.chat.id,
            photo=WELCOME_IMAGE_URL,
            caption=caption,
            reply_markup=user_keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        user_message_ids[user_id].append(sent_message.message_id)


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
    poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
    caption = f"Por favor, ahora envía el enlace de la película '{tmdb_data.get('title')}' para publicarla."
    
    if poster_url:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=poster_url,
            caption=caption,
        )
    else:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=caption,
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
    # --- CAMBIO #4: IMPLEMENTACIÓN DE PAGINACIÓN EN "ESTRENOS" ---
    await show_estrenos_page(message.chat.id, page=1)

@dp.callback_query(F.data.startswith("estrenos_page_"))
async def navigate_estrenos_page(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    await bot.answer_callback_query(callback_query.id)
    await show_estrenos_page(callback_query.message.chat.id, page)

async def show_estrenos_page(chat_id, page):
    await bot.send_message(chat_id, "Buscando los últimos estrenos...")
    upcoming_movies, total_pages = await get_upcoming_movies(page)
    if not upcoming_movies:
        await bot.send_message(chat_id, "No se encontraron más estrenos recientes en este momento.")
        return

    await bot.send_message(chat_id, f"**🎞️ ¡Últimos estrenos!** (Página {page}/{total_pages})\n\nAquí tienes las películas más recientes que se han estrenado.\n", parse_mode=ParseMode.MARKDOWN)

    for movie in upcoming_movies:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        button_text = "🎬 Publicar ahora" if movie_in_db else "🎬 Pedir esta película"
        callback_data = f"publish_now_manual_{tmdb_id}" if movie_in_db else f"request_movie_by_id_{tmdb_id}"
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=button_text, callback_data=callback_data)]
        ])
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(chat_id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar estreno: {e}")
    
    if page < total_pages:
        keyboard_next = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"estrenos_page_{page+1}")]
        ])
        await bot.send_message(chat_id, "Ver más estrenos:", reply_markup=keyboard_next)
# --- FIN CAMBIO #4 ---

@dp.message(F.text == "🔍 Buscar película")
async def show_search_options_by_text(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Por Género", callback_data="search_by_genre")],
        [types.InlineKeyboardButton(text="Por Actor", callback_data="search_by_actor")],
        [types.InlineKeyboardButton(text="Buscar Película", callback_data="search_by_name")],
        [types.InlineKeyboardButton(text="✨ Solicitar una película", callback_data="request_movie_from_user")]
    ])
    await message.reply(
        "¿Cómo quieres buscar la película?",
        reply_markup=keyboard
    )

@dp.message(F.text == "✨ Recomiéndame")
async def show_recomendar_by_text(message: types.Message):
    # --- CAMBIO #3: IMPLEMENTACIÓN DE PAGINACIÓN EN "RECOMIÉNDAME" ---
    await show_recomendar_page(message.chat.id, page=1)

@dp.callback_query(F.data.startswith("recomendar_page_"))
async def navigate_recomendar_page(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    await bot.answer_callback_query(callback_query.id)
    await show_recomendar_page(callback_query.message.chat.id, page)

async def show_recomendar_page(chat_id, page):
    await bot.send_message(chat_id, "Obteniendo recomendaciones...")
    popular_movies, total_pages = await get_popular_movies(page)
    if not popular_movies:
        await bot.send_message(chat_id, "No se pudieron obtener más recomendaciones en este momento.")
        return

    await bot.send_message(chat_id, f"**✨ ¡Películas recomendadas!** (Página {page}/{total_pages})\n\nAquí tienes algunas películas populares que podrían gustarte.\n", parse_mode=ParseMode.MARKDOWN)

    for movie in popular_movies:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue

        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)

        button_text = "🎬 Publicar ahora" if movie_in_db else "🎬 Pedir esta película"
        callback_data = f"publish_now_manual_{tmdb_id}" if movie_in_db else f"request_movie_by_id_{tmdb_id}"
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=button_text, callback_data=callback_data)]
        ])
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(chat_id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar recomendación: {e}")
    
    if page < total_pages:
        keyboard_next = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"recomendar_page_{page+1}")]
        ])
        await bot.send_message(chat_id, "Ver más recomendaciones:", reply_markup=keyboard_next)
# --- FIN CAMBIO #3 ---

@dp.message(F.text == "📰 Noticias")
async def send_latest_news_handler(message: types.Message):
    await message.reply("Buscando las últimas noticias de cine...")
    articles = await get_latest_news()
    if not articles:
        await message.reply("Lo siento, no se encontraron noticias de cine en este momento.")
        return

    for article in articles[:3]:
        title = article.get("title", "Sin título")
        description = article.get("description", "Sin descripción")
        url = article.get("url", "#")
        image_url = article.get("urlToImage", None)

        news_text = (
            f"<b>{html.quote(title)}</b>\n\n"
            f"<i>{html.quote(description)}</i>\n\n"
            f"<a href='{html.quote(url)}'>Leer más</a>"
        )
        try:
            if image_url:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=image_url,
                    caption=news_text,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=news_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logging.error(f"Error al enviar la noticia: {e}")
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
    ] + [[types.InlineKeyboardButton(text="⬅️ Regresar", callback_data="back_to_search_menu")]])
    await bot.send_message(callback_query.message.chat.id, "Elige un género:", reply_markup=keyboard)

@dp.callback_query(F.data == "back_to_search_menu")
async def back_to_search_menu(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await show_search_options_by_text(callback_query.message)


@dp.callback_query(F.data.startswith("genre_"))
async def show_movies_by_genre(callback_query: types.CallbackQuery, page=1):
    await bot.answer_callback_query(callback_query.id)
    genre_id_str = callback_query.data.split('_')[1]
    genre_id = int(genre_id_str)
    
    movies, total_pages = await get_movies_by_genre(genre_id, page=page)

    if not movies:
        await bot.send_message(callback_query.message.chat.id, "No se encontraron más películas para este género.")
        return

    await bot.send_message(callback_query.message.chat.id, f"**Aquí tienes algunas películas de {next((k for k, v in GENRES.items() if v == genre_id), 'este género')}:**", parse_mode=ParseMode.MARKDOWN)

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

    keyboard_buttons = []
    if page > 1:
        keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"genre_page_{genre_id}_{page-1}"))
    if page + 1 < total_pages:
        keyboard_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"genre_page_{genre_id}_{page+1}"))
    
    keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Regresar", callback_data="back_to_search_menu"))

    keyboard_pag = types.InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
    await bot.send_message(callback_query.message.chat.id, "Navega en los resultados:", reply_markup=keyboard_pag)

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
    await state.update_data(search_type='actor')
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre del actor que quieres buscar. Puedes volver al menú principal de búsqueda en cualquier momento presionando /cancelar.")
    await state.set_state(MovieRequestStates.waiting_for_search_query)

@dp.callback_query(F.data == "search_by_name")
async def ask_for_movie_by_name(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.update_data(search_type='name', page=1)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre completo de la película y el año. Asegúrate de escribir correctamente el título y el año."
    )
    await state.set_state(MovieRequestStates.waiting_for_search_query)

@dp.callback_query(F.data == "request_movie_from_user")
async def request_movie_from_user(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.update_data(search_type='request')
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre de la película que te gustaría solicitar. Puedes volver al menú principal de búsqueda en cualquier momento presionando /cancelar."
    )
    await state.set_state(MovieRequestStates.waiting_for_search_query)

@dp.message(Command("cancelar"))
async def cancel_search_mode(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Has salido del modo de búsqueda. Puedes usar el menú principal para buscar de nuevo.")

@dp.message(MovieRequestStates.waiting_for_search_query)
async def process_search_query(message: types.Message, state: FSMContext):
    search_query = message.text.strip()
    user_data = await state.get_data()
    search_type = user_data.get('search_type', 'name')
    page = user_data.get('page', 1)
    
    await message.reply(f"Buscando '{search_query}'...")
    
    # --- CAMBIO #1: BÚSQUEDA SIMULTÁNEA Y PAGINACIÓN DE RESULTADOS ---
    async def search_all_sources(query, page_num):
        tmdb_results, total_pages = await get_movie_results_by_title(query, page=page_num)
        return tmdb_results, total_pages
        
    movie_results, total_pages = await search_all_sources(search_query, page)

    if not movie_results:
        await message.reply(f"No se encontraron resultados para '{search_query}'. Por favor, intenta con otra búsqueda o usa /cancelar para salir.")
        return
    
    await message.reply(f"Resultados para '{search_query}':")
    
    for movie in movie_results[:SEARCH_RESULTS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
            
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        button_text = f"🎬 Publicar ahora" if movie_in_db else f"🎬 Pedir esta película"
        callback_data = f"publish_now_manual_{tmdb_id}" if movie_in_db else f"request_movie_by_id_{tmdb_id}"
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=button_text, callback_data=callback_data)]
        ])
        
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=poster_url,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logging.error(f"Error al enviar resultado de búsqueda: {e}")
    
    pagination_buttons = []
    if page < total_pages:
        pagination_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"search_next_page_{page+1}_{search_query}"))
    pagination_buttons.append(types.InlineKeyboardButton(text="Buscar otra película 🔍", callback_data="search_by_name"))

    if pagination_buttons:
        keyboard_pag = types.InlineKeyboardMarkup(inline_keyboard=[pagination_buttons])
        await bot.send_message(message.chat.id, "Navega en los resultados:", reply_markup=keyboard_pag)

    await state.clear()
# --- FIN CAMBIO #1 ---

@dp.callback_query(F.data.startswith("search_next_page_"))
async def navigate_search_results(callback_query: types.CallbackQuery, state: FSMContext):
    parts = callback_query.data.split('_')
    page = int(parts[3])
    search_query = parts[4]

    await bot.answer_callback_query(callback_query.id)
    await state.update_data(search_type='name', search_query=search_query, page=page)
    await process_search_query(callback_query.message, state)


@dp.callback_query(F.data == "back_to_search_menu")
async def back_to_search_menu(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await show_search_options_by_text(callback_query.message)


@dp.callback_query(F.data.startswith("genre_"))
async def show_movies_by_genre(callback_query: types.CallbackQuery, page=1):
    await bot.answer_callback_query(callback_query.id)
    genre_id_str = callback_query.data.split('_')[1]
    genre_id = int(genre_id_str)
    
    movies, total_pages = await get_movies_by_genre(genre_id, page=page)

    if not movies:
        await bot.send_message(callback_query.message.chat.id, "No se encontraron más películas para este género.")
        return

    await bot.send_message(callback_query.message.chat.id, f"**Aquí tienes algunas películas de {next((k for k, v in GENRES.items() if v == genre_id), 'este género')}:**", parse_mode=ParseMode.MARKDOWN)

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

    keyboard_buttons = []
    if page > 1:
        keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"genre_page_{genre_id}_{page-1}"))
    if page + 1 < total_pages:
        keyboard_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"genre_page_{genre_id}_{page+1}"))
    
    keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Regresar", callback_data="back_to_search_menu"))

    keyboard_pag = types.InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
    await bot.send_message(callback_query.message.chat.id, "Navega en los resultados:", reply_markup=keyboard_pag)

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
    await state.update_data(search_type='actor')
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre del actor que quieres buscar. Puedes volver al menú principal de búsqueda en cualquier momento presionando /cancelar.")
    await state.set_state(MovieRequestStates.waiting_for_search_query)

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
    poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
    caption = f"Por favor, ahora envía el enlace de la película '{tmdb_data.get('title')}' para publicarla."
    
    if poster_url:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=poster_url,
            caption=caption,
        )
    else:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=caption,
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

@dp.callback_query(F.data.startswith("confirm_request_"))
async def confirm_movie_request(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    
    if user_id in user_requests:
        for msg_id in user_requests[user_id]["message_ids"]:
            try:
                await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=msg_id)
            except Exception:
                pass
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
        poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
        caption_text = f"El usuario {callback_query.from_user.full_name} (@{callback_query.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n" \
                       f"ℹ️ **Se encontró en TMDB con ID:** `{tmdb_id}`"
        
        try:
            if poster_url:
                await bot.send_photo(
                    ADMIN_ID,
                    photo=poster_url,
                    caption=caption_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                        [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
                    ])
                )
            else:
                await bot.send_message(
                    ADMIN_ID,
                    text=caption_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                        [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
                    ])
                )
            await bot.send_message(callback_query.message.chat.id, "Tu solicitud ha sido enviada al administrador. ¡Pronto estará lista!")
        except Exception as e:
            logging.error(f"Error al enviar la solicitud al administrador con portada: {e}")
            await bot.send_message(callback_query.message.chat.id, "Tu solicitud ha sido enviada al administrador, pero ocurrió un error al procesarla. Disculpa las molestias.")
    
    await state.clear()

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
    poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
    caption_text = f"El usuario {callback_query.from_user.full_name} (@{callback_query.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n" \
                   f"ℹ️ **Se encontró en TMDB con ID:** `{tmdb_id}`"
    
    try:
        if poster_url:
            await bot.send_photo(
                ADMIN_ID,
                photo=poster_url,
                caption=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
                ])
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                text=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{tmdb_id}")]
                ])
            )
        await bot.send_message(callback_query.message.chat.id, f"Tu solicitud ha sido enviada al administrador.")
    except Exception as e:
        logging.error(f"Error al enviar la solicitud al administrador con portada: {e}")
        await bot.send_message(callback_query.message.chat.id, "Tu solicitud ha sido enviada al administrador, pero ocurrió un error al procesarla. Disculpa las molestias.")
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
    await asyncio.sleep(600)
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
                    
                    text = (
                        f"<b>{html.quote(article.get('title', 'Sin título'))}</b>\n\n"
                        f"<i>{html.quote(article.get('description', 'Sinopsis no disponible'))}</i>\n\n"
                        f"<a href='{html.quote(article.get('url'))}'>Leer más</a>"
                    )
                    
                    poster_url = article.get("urlToImage")
                    
                    try:
                        if poster_url:
                            await bot.send_photo(TELEGRAM_CHANNEL_ID, photo=poster_url, caption=text, parse_mode=ParseMode.HTML)
                        else:
                            await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
                        logging.info("Noticia de cine publicada con éxito.")
                    except Exception as e:
                        logging.error(f"Error al publicar una noticia: {e}")
            await asyncio.sleep(4 * 3600)
        except Exception as e:
            logging.error(f"Error en el programador de contenido del canal: {e}")
            await asyncio.sleep(60)

# WEBHOOK SETUP
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
        return web.Response(text="OK")

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/webhook', handle_telegram_webhook)
    app.router.add_get('/', handle_home)
    app.on_startup.append(on_startup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

# MAIN EXECUTION
async def main():
    
    auto_post_task = asyncio.create_task(auto_post_scheduler())
    scheduled_posts_task = asyncio.create_task(check_scheduled_posts())
    channel_content_task = asyncio.create_task(channel_content_scheduler())
    
    webhook_task = asyncio.create_task(start_webhook_server())

    try:
        await asyncio.gather(auto_post_task, scheduled_posts_task, channel_content_task, webhook_task)
    except asyncio.CancelledError:
        logging.info("Las tareas automáticas han sido canceladas.")
    except Exception as e:
        logging.error(f"Error general en la ejecución del bot: {e}")
        
if __name__ == "__main__":
    asyncio.run(main())
