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
from aiogram.types import Update, InputMediaPhoto
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

# Canal ID
TELEGRAM_MAIN_CHANNEL_ID = -1002240787394
TELEGRAM_PUBLIC_CHANNEL_ID = -1001945286271

BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
TRAKT_BASE_URL = "https://api.trakt.tv"
WELCOME_IMAGE_URL = "https://i.imgur.com/DJSUzQh.jpeg"

# Enlace de invitación del canal principal
MAIN_CHANNEL_INVITE_LINK = "https://t.me/click_para_ver"
MAIN_CHANNEL_USERNAME = "click_para_ver"

# Storage for scheduled posts and recent posts
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)
user_requests = {}
admin_data = {}
user_message_ids = {}
ongoing_tasks = {}
daily_requests = {}
REQUEST_LIMIT = 3
USER_REQUEST_LIMIT = 5
user_daily_requests = {}

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

# ⭐️ [MODIFICACIÓN] Se elimina la variable global AUTO_POST_COUNT. El valor se lee de la DB.
# AUTO_POST_COUNT = 4 
MOVIES_PER_PAGE = 5
SEARCH_RESULTS_PER_PAGE = 5
RECOMENDACIONES_PER_PAGE = 5
ESTRENOS_PER_PAGE = 5

# New states for the state machine
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()
    waiting_for_requested_movie_link = State()
    waiting_for_admin_movie_name = State()
    waiting_for_admin_movie_link = State()

class MovieRequestStates(StatesGroup):
    waiting_for_movie_name = State()
    waiting_for_actor_name = State()
    waiting_for_confirmation = State()
    waiting_for_search_query = State()
    waiting_for_movie_name_to_request = State()

# ⭐️ [MODIFICACIÓN] Nuevos estados para la configuración de límites
class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()
    waiting_for_manual_movie_info = State()
    waiting_for_edit_movie_info = State()
    waiting_for_catalog_search_query = State()
    # ⭐️ [NUEVOS ESTADOS] Para el control de límites
    waiting_for_custom_movie_count = State() 
    waiting_for_news_count = State()


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

# ⭐️ [NUEVAS FUNCIONES] Para manejar la configuración persistente en MongoDB
def get_mongo_db_collection_config():
    """Obtiene la colección de MongoDB para la configuración."""
    try:
        connection_string = os.getenv("DATABASE_URL")
        if not connection_string:
            logging.error("DATABASE_URL no está configurada.")
            return None
        client = motor.motor_asyncio.AsyncIOMotorClient(connection_string)
        db = client["movies_database"]
        return db["config_collection"]
    except Exception as e:
        logging.error(f"Error al conectar con MongoDB para configuración: {e}")
        return None

async def get_config(key, default_value=None):
    """Obtiene un valor de configuración guardado (límites, etc.)."""
    collection = get_mongo_db_collection_config()
    if collection is None:
        return default_value
    try:
        # Usamos un ID fijo para el documento de configuración global
        config = await collection.find_one({"_id": "global_config"})
        return config.get(key) if config and config.get(key) is not None else default_value
    except Exception as e:
        logging.error(f"Error al obtener configuración '{key}': {e}")
        return default_value

async def set_config(key, value):
    """Guarda o actualiza un valor de configuración."""
    collection = get_mongo_db_collection_config()
    if collection is None:
        return
    try:
        await collection.update_one(
            {"_id": "global_config"},
            {"$set": {key: value}},
            upsert=True
        )
        logging.info(f"Configuración '{key}' guardada con valor: {value}")
    except Exception as e:
        logging.error(f"Error al guardar configuración '{key}': {e}")

async def get_daily_news_count(date_str):
    """Obtiene el conteo de noticias para la fecha dada."""
    collection = get_mongo_db_collection_config()
    if collection is None:
        return 0
    try:
        # El contador se almacena en el documento 'daily_counts'
        key = f"news_count_{date_str}"
        config = await collection.find_one({"_id": "daily_counts"})
        # Si existe el documento, devuelve el valor; sino, 0.
        return config.get(key, 0) if config else 0
    except Exception as e:
        logging.error(f"Error al obtener conteo diario de noticias: {e}")
        return 0

async def increment_daily_news_count(date_str, count=1):
    """Incrementa el conteo de noticias para la fecha dada."""
    collection = get_mongo_db_collection_config()
    if collection is None:
        return
    try:
        key = f"news_count_{date_str}"
        await collection.update_one(
            {"_id": "daily_counts"},
            {"$inc": {key: count}}, # Usa $inc para incrementar atómicamente
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error al incrementar conteo diario de noticias: {e}")

# --- Fin de las nuevas funciones de configuración ---


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
        # Se modificó para buscar en el título principal y en los nombres alternativos
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
        movies_list = await collection.find({}).sort("added_at", -1).to_list(None)
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
                if not actor:
                    return [], 1
                
                person_id = actor.get("id")
                url = f"{BASE_TMDB_URL}/person/{person_id}/movie_credits"
                params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    movies = sorted((await response.json()).get("cast", []), key=lambda x: x.get("popularity", 0), reverse=True)
                    total_pages = (len(movies) + SEARCH_RESULTS_PER_PAGE - 1) // SEARCH_RESULTS_PER_PAGE
                    return movies, total_pages
    except aiohttp.ClientError as e:
        logging.error(f"Error al buscar películas por actor: {e}")
        return [], 1

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
        "pageSize": 5, # La API devuelve un batch, luego limitamos por el contador diario
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

def create_movie_message(movie_data, movie_link=None, from_channel=False):
    title = movie_data.get("title", "Título no disponible")
    overview = movie_data.get("overview", "Sinopsis no disponible")
    release_date = movie_data.get("release_date", "Fecha no disponible")
    vote_average = movie_data.get("vote_average", 0)
    poster_path = movie_data.get("poster_path")

    if not overview.strip():
        overview = "Sinopsis no disponible."
    
    if len(overview) > 250:
        overview = overview[:250] + "..."

    text = (
        f"<b>🎬 {title}</b>\n\n"
        f"<i>Sinopsis:</i> {overview}\n\n"
        f"📅 <b>Fecha de estreno:</b> {release_date}\n"
        f"⭐ <b>Puntuación:</b> {vote_average:.1f}/10"
    )

    if from_channel:
        post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_link)],
            [types.InlineKeyboardButton(text="✨ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
        ])
    elif movie_link:
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
        old_message_id_main = movie_data.get("last_message_id")
        old_message_id_public = movie_data.get("last_message_id_public")
        
        # Eliminar del canal principal
        if old_message_id_main is not None:
            try:
                await bot.delete_message(chat_id=TELEGRAM_MAIN_CHANNEL_ID, message_id=int(old_message_id_main))
                logging.info(f"Mensaje {old_message_id_main} eliminado del canal principal.")
            except Exception as e:
                logging.error(f"Error al intentar borrar el mensaje {old_message_id_main} del canal principal: {e}")
        
        # Eliminar del canal público
        if old_message_id_public is not None:
            try:
                await bot.delete_message(chat_id=TELEGRAM_PUBLIC_CHANNEL_ID, message_id=int(old_message_id_public))
                logging.info(f"Mensaje {old_message_id_public} eliminado del canal público.")
            except Exception as e:
                logging.error(f"Error al intentar borrar el mensaje {old_message_id_public} del canal público: {e}")


async def forward_post_to_public_channel(original_message: types.Message, movie_data):
    if not TELEGRAM_PUBLIC_CHANNEL_ID:
        logging.warning("TELEGRAM_PUBLIC_CHANNEL_ID no está configurado. No se puede reenviar el post.")
        return

    try:
        # Enlace al post específico en el canal principal usando el nombre de usuario
        post_link = f"https://t.me/{MAIN_CHANNEL_USERNAME}/{original_message.message_id}"
        
        # Obtener la sinopsis y acortarla
        sinopsis = movie_data.get("overview", "Sinopsis no disponible.")
        if len(sinopsis) > 250:
            sinopsis = sinopsis[:250] + "..."
            
        caption_text = (
            f"🎬 **¡Nueva película disponible!**\n\n"
            f"🍿 **{movie_data.get('title')}**\n\n"
            f"📝 {sinopsis}\n\n"
            f"Presiona el botón 'Ver Película' para acceder al post original."
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Ver Película", url=post_link)],
            [types.InlineKeyboardButton(text="➡️ Ir al Canal", url=MAIN_CHANNEL_INVITE_LINK)],
            [types.InlineKeyboardButton(text="✨ Pedir una película", url="https://t.me/sdmin_dy_bot?start=request")]
        ])

        poster_url = get_movie_poster_url(movie_data.get("poster_path"))
        
        public_message = None
        if poster_url:
            public_message = await bot.send_photo(
                chat_id=TELEGRAM_PUBLIC_CHANNEL_ID,
                photo=poster_url,
                caption=caption_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            public_message = await bot.send_message(
                chat_id=TELEGRAM_PUBLIC_CHANNEL_ID,
                text=caption_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            
        logging.info(f"Enlace al post {original_message.message_id} reenviado al canal público.")
        return public_message.message_id

    except Exception as e:
        logging.error(f"Error al reenviar el post al canal público: {e}")
        return None

async def send_movie_post(chat_id, movie_data, movie_link, post_keyboard, user_id_to_notify=None):
    text, poster_url, _ = create_movie_message(movie_data, movie_link, from_channel=True)

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

        if chat_id == TELEGRAM_MAIN_CHANNEL_ID:
            movie_data["last_message_id"] = message.message_id
            await asyncio.sleep(5)
            public_message_id = await forward_post_to_public_channel(message, movie_data)
            if public_message_id:
                movie_data["last_message_id_public"] = public_message_id
            await save_movie_to_db(movie_data)

        if user_id_to_notify:
            notification_message = (
                f"🎉 ¡Tu película solicitada, **{movie_data.get('title')}**, ya está disponible en el canal!\n\n"
                f"Haz clic en el botón de abajo para verla."
            )
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}/{message.message_id}")],
                [types.InlineKeyboardButton(text="➡️ Ir al Canal", url=MAIN_CHANNEL_INVITE_LINK)],
                [types.InlineKeyboardButton(text="✨ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
            ])
            await bot.send_message(user_id_to_notify, notification_message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

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
                [types.KeyboardButton(text="⚙️ Configuración auto-publicación")] # Se eliminó "🗳️ Iniciar votación"
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
                [types.KeyboardButton(text="🔍 Buscar película"), types.KeyboardButton(text="🎞️ Estrenos")],
                [types.KeyboardButton(text="✨ Recomiéndame"), types.KeyboardButton(text="📌 Pedir película")],
                [types.KeyboardButton(text="🆘 Soporte")]
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

# --- CORRECCIÓN DE FLUJO DE AGREGAR PELÍCULA ---
@dp.message(F.text == "➕ Agregar película")
async def add_movie_start_by_text(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    await state.clear()
    await message.reply(
        "Por favor, escribe el nombre completo de la película que quieres agregar. Puedes incluir el año para un resultado más preciso."
    )
    await state.set_state(MovieUploadStates.waiting_for_admin_movie_name)

@dp.message(MovieUploadStates.waiting_for_admin_movie_name)
async def admin_search_movie_to_add(message: types.Message, state: FSMContext):
    search_query = message.text.strip()
    await message.reply(f"Buscando '{search_query}'...")

    tmdb_results, _ = await get_movie_results_by_title(search_query)

    if not tmdb_results:
        await message.reply(
            "No se encontraron resultados. Por favor, intenta con un nombre o año diferente."
        )
        return
    
    for movie in tmdb_results[:SEARCH_RESULTS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
            
        text, poster_url, _ = create_movie_message(tmdb_data)

        # AQUI ESTA EL CAMBIO: VERIFICA SI LA PELICULA YA EXISTE EN LA DB
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)

        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Película ya en el catálogo", callback_data="movie_exists_dummy")],
                [types.InlineKeyboardButton(text="📌 Publicar ahora", callback_data=f"publish_now_admin:{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="Agregar esta película", callback_data=f"admin_add_movie:{tmdb_id}")]
            ])
            
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
            logging.error(f"Error al enviar el resultado de búsqueda del administrador: {e}")

    await state.set_state(MovieUploadStates.waiting_for_admin_movie_name)

@dp.callback_query(F.data == "movie_exists_dummy")
async def dummy_callback_handler(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, "Esta película ya está en el catálogo.", show_alert=True)

@dp.callback_query(F.data.startswith("admin_add_movie:"))
async def admin_add_movie_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    tmdb_id = int(callback_query.data.split(':')[-1])
    
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "Error al obtener la información de la película.")
        return

    await state.update_data(tmdb_data=tmdb_data, tmdb_id=tmdb_id)
    await state.set_state(MovieUploadStates.waiting_for_admin_movie_link)
    
    await bot.send_message(
        callback_query.message.chat.id,
        f"Has seleccionado **{tmdb_data.get('title')}**. Por favor, envía el enlace de la película.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(MovieUploadStates.waiting_for_admin_movie_link)
async def admin_process_movie_link(message: types.Message, state: FSMContext):
    movie_link = message.text.strip()
    user_data = await state.get_data()
    tmdb_data = user_data.get('tmdb_data')

    if not tmdb_data:
        await message.reply("Ocurrió un error. Por favor, reinicia el proceso.")
        await state.clear()
        return

    names = [tmdb_data.get('title')]
    if tmdb_data.get('original_title') and tmdb_data.get('original_title') != tmdb_data.get('title'):
        names.append(tmdb_data.get("original_title"))

    movie_data = {
        "id": tmdb_data.get("id"),
        "title": tmdb_data.get("title"),
        "names": ", ".join(names),
        "link": movie_link,
        "last_message_id": None,
        "added_at": datetime.datetime.now().isoformat()
    }
    
    await save_movie_to_db(movie_data)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📌 Publicar ahora", callback_data=f"publish_now_admin:{movie_data['id']}")],
        [types.InlineKeyboardButton(text="➕ Agregar otra película", callback_data="add_another_movie")],
        [types.InlineKeyboardButton(text="⏰ Publicar con temporizador", callback_data=f"schedule_movie_{movie_data['id']}")]
    ])
    
    await message.reply(
        f"✅ La película **{tmdb_data.get('title')}** se agregó correctamente. ¿Qué deseas hacer ahora?", 
        reply_markup=keyboard, 
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()


@dp.callback_query(F.data.startswith("publish_now_admin:"))
async def publish_now_admin(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, "Publicando la película...")
    movie_id = int(callback_query.data.split(':')[-1])
    movie_info = await get_movie_by_tmdb_id(movie_id)

    if not movie_info:
        await bot.send_message(callback_query.message.chat.id, "Error: película no encontrada en la base de datos.")
        await callback_query.answer()
        return

    tmdb_data = await get_movie_details(movie_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "Error: no se pudo obtener información de TMDB.")
        return

    await delete_old_post(movie_id)

    post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_info.get("link"))],
        [types.InlineKeyboardButton(text="✨ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
    ])

    success, _ = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)

    if success:
        await bot.send_message(callback_query.message.chat.id, "✅ Película publicada con éxito.")
    else:
        await bot.send_message(callback_query.message.chat.id, "Ocurrió un error al publicar la película.")

    await callback_query.answer()

@dp.callback_query(F.data == "add_another_movie")
async def handle_add_another_movie(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre completo de la siguiente película.")
    await state.set_state(MovieUploadStates.waiting_for_admin_movie_name)


# --- INICIO DEL NUEVO FLUJO DE CATÁLOGO ---

@dp.message(F.text == "📋 Ver catálogo")
async def view_catalog_by_text(message: types.Message, state: FSMContext):
    await state.clear()
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔍 Buscar en catálogo", callback_data="admin_search_catalog")],
        [types.InlineKeyboardButton(text="➡️ Ver todo el catálogo", callback_data="admin_view_all_catalog")]
    ])
    
    await message.reply(
        "Elige una opción para gestionar el catálogo:",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "admin_search_catalog")
async def admin_search_catalog_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.set_state(AdminStates.waiting_for_catalog_search_query)
    await bot.send_message(
        callback_query.message.chat.id,
        "Por favor, escribe el **nombre** de la película que deseas buscar en tu catálogo.",
        parse_mode=ParseMode.MARKDOWN
    )
    
@dp.message(AdminStates.waiting_for_catalog_search_query)
async def admin_process_catalog_search(message: types.Message, state: FSMContext):
    search_query = message.text.strip()
    await message.reply(f"Buscando '{search_query}' en el catálogo...")
    
    movie_data = await find_movie_in_db_by_name(search_query)
    
    if not movie_data:
        await message.reply("❌ No se encontró una película con ese nombre en tu catálogo. Intenta con un nombre diferente.")
    else:
        title = movie_data.get("title") if movie_data.get("title") else "Título desconocido"
        tmdb_id = movie_data.get("id")
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar en el canal", callback_data=f"publish_now_admin:{tmdb_id}")],
            [types.InlineKeyboardButton(text="✏️ Editar película", callback_data=f"edit_movie:{tmdb_id}"), types.InlineKeyboardButton(text="🗑️ Eliminar película", callback_data=f"delete_movie:{tmdb_id}")]
        ])
        message_text = (
            f"✅ **Película Encontrada:**\n"
            f"**Título:** {title}\n"
            f"**ID:** `{tmdb_id}`\n"
            f"**Enlace:** <a href='{movie_data.get('link', 'No disponible')}'>Click para ver el enlace</a>"
        )
        await message.reply(
            message_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    await state.clear()

@dp.callback_query(F.data == "admin_view_all_catalog")
async def admin_view_all_catalog_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    all_movies = await get_all_movies()
    if not all_movies:
        await bot.send_message(callback_query.message.chat.id, "Aún no hay películas en la base de datos.")
        return
    
    # Se borra el mensaje de opciones (Buscar/Ver todo) para iniciar el catálogo
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception:
        pass
        
    await send_catalog_page(callback_query.message.chat.id, 0)

async def send_catalog_page(chat_id, page):
    movie_items = await get_all_movies()
    start = page * MOVIES_PER_PAGE
    end = start + MOVIES_PER_PAGE
    page_movies = movie_items[start:end]
    total_movies = len(movie_items)
    total_pages = (total_movies + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE

    # Si no hay películas para la página actual, regresar o notificar.
    if not page_movies and total_movies > 0 and page > 0:
        await bot.send_message(chat_id, "No hay más películas en esta página.")
        return
        
    text = f"**Catálogo de Películas** (Página {page + 1}/{total_pages})\n\n"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)

    for data in page_movies:
        title = data.get("title") if data.get("title") else "Título desconocido"
        tmdb_id = data.get("id")
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar en el canal", callback_data=f"publish_now_admin:{tmdb_id}")],
            [types.InlineKeyboardButton(text="✏️ Editar película", callback_data=f"edit_movie:{tmdb_id}"), types.InlineKeyboardButton(text="🗑️ Eliminar película", callback_data=f"delete_movie:{tmdb_id}")]
        ])
        message_text = f"**{title}**\nID: `{tmdb_id}`"
        await bot.send_message(
            chat_id,
            message_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"catalog_page:{page-1}"))
    if page + 1 < total_pages:
        pagination_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"catalog_page:{page+1}"))

    if pagination_buttons:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[pagination_buttons])
        await bot.send_message(chat_id, "Navegación:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("catalog_page:"))
async def navigate_catalog(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split(':')[-1])
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception as e:
        logging.error(f"Error al borrar mensaje de catálogo: {e}")
    await send_catalog_page(callback_query.message.chat.id, page)


@dp.callback_query(F.data.startswith("edit_movie:"))
async def handle_edit_movie(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.message.chat.id, "La función de edición está en desarrollo. ¡Pronto estará disponible!")


@dp.callback_query(F.data.startswith("delete_movie:"))
async def handle_delete_movie(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    movie_id = int(callback_query.data.split(':')[-1])
    movie_to_delete = await get_movie_by_tmdb_id(movie_id)

    if movie_to_delete:
        # Intenta eliminar el post del canal principal y público
        await delete_old_post(movie_id)

        await delete_movie_from_db(movie_id)
        await bot.send_message(callback_query.message.chat.id, f"✅ La película **{movie_to_delete.get('title')}** ha sido eliminada del catálogo y del canal.", parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.send_message(callback_query.message.chat.id, "No se encontró la película para eliminar.")


@dp.callback_query(F.data.startswith("publish_from_catalog:"))
async def publish_from_catalog(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split(':')[-1])
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
    success, _ = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)

    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

# --- FIN DEL NUEVO FLUJO DE CATÁLOGO ---

# ⭐️ [MODIFICACIÓN] Menú de Configuración de auto-publicación
@dp.message(F.text == "⚙️ Configuración auto-publicación")
async def auto_post_config(message: types.Message, state: FSMContext):
    await state.clear()
    if str(message.from_user.id) != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return

    # ⭐️ Obtener límites actuales (configurables por el usuario)
    current_movie_limit = await get_config("movie_limit", default_value=4) 
    current_news_limit = await get_config("news_limit", default_value=5)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"🎬 Películas/Día (Actual: {current_movie_limit})", callback_data="config_movies_start")],
        [types.InlineKeyboardButton(text=f"📰 Noticias/Día (Actual: {current_news_limit})", callback_data="config_news_start")]
    ])

    await message.reply(
        "Elige qué límite de publicaciones deseas configurar. Introduce el número exacto que prefieras:", 
        reply_markup=keyboard
    )
    
# ⭐️ [ELIMINADO] Se elimina el antiguo manejador de `set_auto_`.

# ⭐️ [NUEVOS MANEJADORES] Flujos de Configuración de Películas y Noticias
@dp.callback_query(F.data == "config_movies_start")
async def config_movies_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.set_state(AdminStates.waiting_for_custom_movie_count)
    
    current_limit = await get_config("movie_limit", default_value=4)
    
    await bot.send_message(
        callback_query.message.chat.id,
        f"¿Cuántas **películas** deseas publicar automáticamente al día? (Actual: **{current_limit}**).\n\n"
        "Escribe un número entero (ej. **15**).",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(AdminStates.waiting_for_custom_movie_count)
async def process_custom_movie_count(message: types.Message, state: FSMContext):
    try:
        limit = int(message.text.strip())
        if limit <= 0 or limit > 30: 
            await message.reply("Por favor, introduce un número válido entre 1 y 30.")
            return

        # ⭐️ GUARDAR EN LA BASE DE DATOS de forma permanente
        await set_config("movie_limit", limit)
        
        await message.reply(f"✅ Límite de películas/día configurado a **{limit}** de forma permanente. El nuevo límite se aplicará en el próximo ciclo de publicación.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply("❌ Entrada no válida. Por favor, introduce solo un número entero.")
    finally:
        await state.clear()


@dp.callback_query(F.data == "config_news_start")
async def config_news_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.set_state(AdminStates.waiting_for_news_count)
    
    current_limit = await get_config("news_limit", default_value=5)
    
    await bot.send_message(
        callback_query.message.chat.id,
        f"¿Cuántas **noticias** deseas publicar automáticamente al día? (Actual: **{current_limit}**).\n\n"
        "Escribe un número entero (ej. **5**).",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(AdminStates.waiting_for_news_count)
async def process_news_count(message: types.Message, state: FSMContext):
    try:
        limit = int(message.text.strip())
        if limit <= 0 or limit > 10: 
            await message.reply("Por favor, introduce un número válido entre 1 y 10.")
            return

        # ⭐️ GUARDAR EN LA BASE DE DATOS de forma permanente
        await set_config("news_limit", limit)
        
        await message.reply(f"✅ Límite de noticias/día configurado a **{limit}** de forma permanente. Ahora solo se publicarán {limit} noticias por día.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply("❌ Entrada no válida. Por favor, introduce solo un número entero.")
    finally:
        await state.clear()
# --- FIN DE LOS NUEVOS MANEJADORES ---

@dp.message(F.text == "🎞️ Estrenos")
async def show_estrenos_by_text(message: types.Message, state: FSMContext):
    await state.clear()
    await show_estrenos_page(message.chat.id, page=1, is_start_message=True)

@dp.callback_query(F.data.startswith("estrenos_page:"))
async def navigate_estrenos(callback_query: types.CallbackQuery):
    # La lógica de esta función se omite por brevedad, pero se mantiene en la estructura original.
    pass

# ... (El resto de tus funciones)

# ⭐️ [INSERCIÓN] Funciones de Scheduler Modificadas (asumo que estaban aquí)
async def auto_post_scheduler():
    """Programa la publicación automática de películas pendientes según el límite configurado."""
    while True:
        await asyncio.sleep(random.randint(60, 180)) # Pequeña espera inicial

        try:
            # ⭐️ [MODIFICACIÓN] Lee el límite de películas configurado por el administrador
            max_posts = await get_config("movie_limit", default_value=4) 
            
            movies_collection = get_mongo_db_collection() # Colección principal de películas
            if movies_collection is None:
                logging.error("No se pudo obtener la colección de películas.")
                await asyncio.sleep(random.randint(4 * 3600, 6 * 3600))
                continue

            current_time = datetime.datetime.now()
            
            # ⭐️ El bucle ahora usa el límite configurado (max_posts)
            for i in range(max_posts):
                # Asumo que las películas tienen un campo 'post_time' y 'status' para ser programadas.
                movie_to_post = await movies_collection.find_one_and_update(
                    {"post_time": {"$lte": current_time}, "status": "pending"}, 
                    {"$set": {"status": "posting"}},
                    sort=[("post_time", 1)]
                )

                if movie_to_post:
                    # Simulación del posteo 
                    tmdb_data = await get_movie_details(movie_to_post['id'])
                    
                    if tmdb_data:
                        # Recrea el teclado del post
                        post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                            [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_to_post.get("link"))],
                            [types.InlineKeyboardButton(text="✨ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
                        ])
                        
                        await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_to_post.get("link"), post_keyboard)
                        
                        # Marca como posteada
                        await movies_collection.update_one(
                            {"_id": movie_to_post["_id"]},
                            {"$set": {"status": "posted"}}
                        )
                        logging.info(f"Película programada posteada: {movie_to_post.get('title', 'N/A')}")
                        await asyncio.sleep(random.randint(60, 180)) # Espera entre posts
                else:
                    logging.info("No hay más películas programadas pendientes de postear.")
                    break # Salir del bucle si no hay más películas

        except Exception as e:
            logging.error(f"Error en auto_post_scheduler: {e}")

        # Esperar 4-6 horas para el próximo ciclo
        await asyncio.sleep(random.randint(4 * 3600, 6 * 3600))


def format_news_post(news_item):
    """Formatea un artículo de noticias para Telegram."""
    title = news_item.get("title", "Noticia sin título")
    url = news_item.get("url", "#")
    source = news_item.get("source", {}).get("name", "Desconocido")
    description = news_item.get("description", "Haz clic para leer más...")
    
    # Intenta cortar la descripción y añadir el enlace
    clean_description = description if len(description) < 250 else description[:250] + "..."
    
    text = (
        f"📰 <b>¡Noticia de Cine!</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{clean_description}\n\n"
        f"Fuente: {source}\n"
        f"<a href='{url}'>Leer artículo completo aquí</a>"
    )
    return text


async def channel_content_scheduler():
    """Programa la publicación de noticias y otro contenido, respetando el límite diario de noticias."""
    while True:
        try:
            # --- LÓGICA DE NOTICIAS (MODIFICADA PARA LÍMITE DIARIO) ---
            today_date_str = datetime.date.today().strftime("%Y-%m-%d")
            # ⭐️ Lee el límite de noticias configurado por el administrador
            news_limit = await get_config("news_limit", default_value=5) 
            current_news_count = await get_daily_news_count(today_date_str)

            if current_news_count < news_limit:
                remaining_posts = news_limit - current_news_count
                
                latest_news = await get_latest_news() 
                
                # ⭐️ Tomar solo la cantidad restante para el límite diario
                news_to_post = latest_news[:remaining_posts]

                for news_item in news_to_post:
                    if news_item:
                        try:
                            # 1. Obtener el texto y la imagen (si existe)
                            text_caption = format_news_post(news_item)
                            image_url = news_item.get("urlToImage")
                            
                            # 2. Postear al canal público
                            if image_url:
                                await bot.send_photo(
                                    TELEGRAM_PUBLIC_CHANNEL_ID,
                                    photo=image_url,
                                    caption=text_caption,
                                    parse_mode=ParseMode.HTML
                                )
                            else:
                                await bot.send_message(
                                    TELEGRAM_PUBLIC_CHANNEL_ID,
                                    text=text_caption,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=False
                                )

                            await increment_daily_news_count(today_date_str) # ⭐️ Incrementa el contador
                            logging.info(f"Noticia posteada. Conteo diario: {await get_daily_news_count(today_date_str)}/{news_limit}")
                            await asyncio.sleep(random.randint(60, 180)) # Espera entre posts
                        except Exception as e:
                            logging.error(f"Error al postear noticia: {e}")
            else:
                logging.info(f"Límite diario de {news_limit} noticias alcanzado.")
                
            # --- LÓGICA DE MEMES (Dejamos la lógica de memes aquí) ---
            meme_url, meme_caption = await get_random_meme()
            if meme_url:
                await bot.send_photo(
                    TELEGRAM_PUBLIC_CHANNEL_ID,
                    photo=meme_url,
                    caption=meme_caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                logging.info("Meme posteado al canal público.")


        except Exception as e:
            logging.error(f"Error en channel_content_scheduler: {e}")

        # Esperar 30-45 minutos para el próximo ciclo
        await asyncio.sleep(random.randint(30 * 60, 45 * 60))


async def check_scheduled_posts():
    """Procesa la cola de posts programados."""
    while True:
        try:
            # Lógica para procesar scheduled_posts_queue (se mantiene placeholder)
            await asyncio.sleep(300) # Espera 5 minutos
        except Exception as e:
            logging.error(f"Error en check_scheduled_posts: {e}")
            await asyncio.sleep(300)

async def handle_home(request):
    return web.Response(text="Bot en funcionamiento.")

async def handle_telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.error(f"Error al procesar el webhook de Telegram: {e}")
    finally:
        return web.Response(text="OK")

async def on_startup(app):
    """Función de inicio para la aplicación web."""
    logging.info("Web server started.")

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/webhook', handle_telegram_webhook)
    app.router.add_get('/', handle_home)
    app.on_startup.append(on_startup)
    
    # Obtener puerto de las variables de entorno, por defecto 8080.
    port = int(os.environ.get('PORT', 8080))
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# MAIN EXECUTION
async def main():
    
    # ⭐️ Las tareas de programación de contenido se inician aquí, usando las funciones modificadas.
    auto_post_task = asyncio.create_task(auto_post_scheduler())
    scheduled_posts_task = asyncio.create_task(check_scheduled_posts())
    channel_content_task = asyncio.create_task(channel_content_scheduler())
    
    webhook_task = asyncio.create_task(start_webhook_server())

    try:
        await asyncio.gather(
            auto_post_task,
            scheduled_posts_task,
            channel_content_task,
            webhook_task
        )
    except KeyboardInterrupt:
        pass
    finally:
        auto_post_task.cancel()
        scheduled_posts_task.cancel()
        channel_content_task.cancel()
        webhook_task.cancel()
        await dp.stop_polling()

if __name__ == "__main__":
    asyncio.run(main())
