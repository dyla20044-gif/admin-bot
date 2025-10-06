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

AUTO_POST_COUNT = 4
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

class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()
    waiting_for_manual_movie_info = State()
    waiting_for_edit_movie_info = State()
    waiting_for_catalog_search_query = State() # NUEVO ESTADO PARA BUSCAR EN CATÁLOGO


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
            [types.InlineKeyboardButton(text="✏️ Editar película", callback_data=f"edit_movie:{tmdb_id}"),
             types.InlineKeyboardButton(text="🗑️ Eliminar película", callback_data=f"delete_movie:{tmdb_id}")]
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
            [types.InlineKeyboardButton(text="✏️ Editar película", callback_data=f"edit_movie:{tmdb_id}"),
            types.InlineKeyboardButton(text="🗑️ Eliminar película", callback_data=f"delete_movie:{tmdb_id}")]
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
        if movie_to_delete.get('last_message_id'):
            try:
                await bot.delete_message(
                    chat_id=TELEGRAM_MAIN_CHANNEL_ID,
                    message_id=movie_to_delete['last_message_id']
                )
            except Exception as e:
                logging.warning(f"No se pudo eliminar el post del canal: {e}")
        
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


@dp.message(F.text == "⚙️ Configuración auto-publicación")
async def auto_post_config(message: types.Message, state: FSMContext):
    await state.clear()
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

@dp.message(F.text == "🎞️ Estrenos")
async def show_estrenos_by_text(message: types.Message, state: FSMContext):
    await state.clear()
    await show_estrenos_page(message.chat.id, page=1, is_start_message=True)

@dp.callback_query(F.data.startswith("estrenos_page:"))
async def navigate_estrenos_page(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split(":")[-1])
    await bot.answer_callback_query(callback_query.id)
    await show_estrenos_page(callback_query.message.chat.id, page)

async def show_estrenos_page(chat_id, page, is_start_message=False):
    if is_start_message:
        await bot.send_message(chat_id, "Buscando los últimos estrenos... 🎬")

    upcoming_movies, total_pages = await get_upcoming_movies(page)
    
    if not upcoming_movies:
        await bot.send_message(chat_id, "No se encontraron más estrenos recientes en este momento. Vuelve a intentarlo más tarde.")
        return

    for movie in upcoming_movies[:ESTRENOS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))],
                [types.InlineKeyboardButton(text="📢 Publicar en el canal", callback_data=f"publish_now_manual:{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id:{tmdb_id}")]
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
            [types.InlineKeyboardButton(text="Ver más estrenos ➡️", callback_data=f"estrenos_page:{page+1}")]
        ])
        await bot.send_message(chat_id, "Mira lo que sigue:", reply_markup=keyboard_next)

@dp.message(F.text == "🔍 Buscar película")
async def show_search_options_by_text(message: types.Message, state: FSMContext):
    await state.clear()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Por Género", callback_data="search_by_genre")],
        [types.InlineKeyboardButton(text="Por Actor", callback_data="search_by_actor")],
        [types.InlineKeyboardButton(text="Buscar Película", callback_data="search_by_name")],
    ])
    await message.reply(
        "¿Cómo quieres buscar la película? 🔎",
        reply_markup=keyboard
    )
    
@dp.callback_query(F.data == "search_by_actor")
async def search_by_actor_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    await state.set_state(MovieRequestStates.waiting_for_actor_name)
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre del actor. 🎭")

@dp.message(MovieRequestStates.waiting_for_actor_name)
async def search_by_actor_process(message: types.Message, state: FSMContext):
    actor_name = message.text.strip()
    await message.reply(f"Buscando películas de '{actor_name}'...")
    movies, total_pages = await get_movies_by_actor(actor_name)
    
    if not movies:
        await message.reply("No se encontraron películas para este actor. Intenta con un nombre diferente.")
        await state.clear()
        return

    for movie in movies[:SEARCH_RESULTS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id:{tmdb_id}")]
            ])
            
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(chat_id=message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar la publicación de actor: {e}")
    
    await state.clear()


@dp.callback_query(F.data == "search_by_name")
async def search_by_name_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    await state.set_state(MovieRequestStates.waiting_for_search_query)
    await bot.send_message(callback_query.message.chat.id, "Por favor, escribe el nombre de la película. 🎬")

@dp.message(MovieRequestStates.waiting_for_search_query)
async def search_by_name_process(message: types.Message, state: FSMContext):
    query = message.text.strip()
    await message.reply(f"Buscando '{query}'...")
    results, total_pages = await get_movie_results_by_title(query)
    
    if not results:
        await message.reply("No se encontraron películas con ese nombre. Intenta con otro.")
        await state.clear()
        return
        
    for movie in results[:SEARCH_RESULTS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))],
                [types.InlineKeyboardButton(text="📢 Publicar en el canal", callback_data=f"publish_now_manual:{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id:{tmdb_id}")]
            ])
            
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        try:
            if poster_url:
                await bot.send_photo(chat_id=message.chat.id, photo=poster_url, caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=message.chat.id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Error al enviar la publicación de búsqueda: {e}")
            
    await state.clear()


@dp.message(F.text == "✨ Recomiéndame")
async def show_recomendar_by_text(message: types.Message, state: FSMContext):
    await state.clear()
    await show_recomendar_page(message.chat.id, page=1, is_start_message=True)

@dp.callback_query(F.data.startswith("recomendar_page:"))
async def navigate_recomendar_page(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split(":")[-1])
    await bot.answer_callback_query(callback_query.id)
    await show_recomendar_page(callback_query.message.chat.id, page)

async def show_recomendar_page(chat_id, page, is_start_message=False):
    if is_start_message:
        await bot.send_message(chat_id, "Obteniendo recomendaciones... ✨")

    popular_movies, total_pages = await get_popular_movies(page)
    
    if not popular_movies:
        await bot.send_message(chat_id, "No se pudieron obtener más recomendaciones en este momento. Vuelve a intentarlo más tarde.")
        return

    for movie in popular_movies[:RECOMENDACIONES_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue

        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)

        if movie_in_db:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))],
                [types.InlineKeyboardButton(text="📢 Publicar en el canal", callback_data=f"publish_now_manual:{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id:{tmdb_id}")]
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
            [types.InlineKeyboardButton(text="Ver más recomendaciones ➡️", callback_data=f"recomendar_page:{page+1}")]
        ])
        await bot.send_message(chat_id, "Mira lo que sigue:", reply_markup=keyboard_next)

@dp.message(F.text == "📰 Noticias")
async def send_latest_news_handler(message: types.Message, state: FSMContext):
    await state.clear()
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
async def search_by_genre_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=genre, callback_data=f"genre:{id}") for genre, id in list(GENRES.items())[i:i+3]] for i in range(0, len(GENRES), 3)
    ] + [[types.InlineKeyboardButton(text="⬅️ Regresar", callback_data="back_to_search_menu")]])
    await bot.send_message(callback_query.message.chat.id, "Elige un género:", reply_markup=keyboard)

@dp.callback_query(F.data == "back_to_search_menu")
async def back_to_search_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    await show_search_options_by_text(callback_query.message)


@dp.callback_query(F.data.startswith("genre:"))
async def show_movies_by_genre(callback_query: types.CallbackQuery, page=1):
    await bot.answer_callback_query(callback_query.id)
    genre_id_str = callback_query.data.split(':')[1]
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
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))],
                [types.InlineKeyboardButton(text="📢 Publicar en el canal", callback_data=f"publish_now_manual:{tmdb_id}")]
            ])
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Pedir esta película", callback_data=f"request_movie_by_id:{tmdb_id}")]
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
        keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"genre_page:{genre_id}:{page-1}"))
    if page + 1 < total_pages:
        keyboard_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"genre_page:{genre_id}:{page+1}"))
    
    keyboard_buttons.append(types.InlineKeyboardButton(text="⬅️ Regresar", callback_data="back_to_search_menu"))

    keyboard_pag = types.InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
    await bot.send_message(callback_query.message.chat.id, "Navega en los resultados:", reply_markup=keyboard_pag)

@dp.callback_query(F.data.startswith("genre_page:"))
async def navigate_genre_page(callback_query: types.CallbackQuery):
    parts = callback_query.data.split(':')
    genre_id = int(parts[1])
    page = int(parts[2])
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception as e:
        logging.error(f"Error al borrar mensaje de catálogo: {e}")
    await show_movies_by_genre(callback_query, page=page)


@dp.message(F.text == "📌 Pedir película")
async def start_request_flow(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    today = datetime.date.today().isoformat()
    if user_id not in user_daily_requests or user_daily_requests[user_id]["date"] != today:
        user_daily_requests[user_id] = {"count": 0, "date": today}
    
    if user_daily_requests[user_id]["count"] >= USER_REQUEST_LIMIT:
        await message.reply("🚫 Has alcanzado el límite de solicitudes diarias. Inténtalo de nuevo mañana.")
        await state.clear()
        return
        
    await state.set_state(MovieRequestStates.waiting_for_movie_name_to_request)
    await message.reply(
        "Por favor, escribe el nombre de la película que te gustaría solicitar. Buscaremos las mejores opciones para ti."
    )

@dp.callback_query(F.data == "request_movie_from_main_menu")
async def start_request_flow_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await state.clear()
    await state.set_state(MovieRequestStates.waiting_for_movie_name_to_request)
    await bot.send_message(
        callback_query.message.chat.id,
        "Por favor, escribe el nombre de la película que te gustaría solicitar. Buscaremos las mejores opciones para ti."
    )
    try:
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    except Exception as e:
        logging.warning(f"No se pudo borrar el mensaje original al solicitar otra película: {e}")


@dp.message(MovieRequestStates.waiting_for_movie_name_to_request)
async def process_movie_name_for_request(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    movie_title = message.text.strip()
    await message.reply(f"Buscando **{movie_title}** en la base de datos... 🔍")
    
    tmdb_results, _ = await get_movie_results_by_title(movie_title, page=1)
    
    if not tmdb_results:
        await message.reply(
            f"Lo siento, no se encontraron resultados para **{movie_title}**. Intenta con un nombre diferente o más preciso."
        )
        return
        
    await message.reply("Hemos encontrado algunas opciones. ¿Cuál de estas es la que buscas?")
    
    for movie in tmdb_results[:SEARCH_RESULTS_PER_PAGE]:
        tmdb_id = movie.get("id")
        tmdb_data = await get_movie_details(tmdb_id)
        if not tmdb_data:
            continue
            
        text, poster_url, _ = create_movie_message(tmdb_data)
        
        movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
        
        today = datetime.date.today().isoformat()
        if tmdb_id not in daily_requests:
            daily_requests[tmdb_id] = {"count": 0, "date": today}
        if daily_requests[tmdb_id]["date"] != today:
            daily_requests[tmdb_id]["count"] = 0
            daily_requests[tmdb_id]["date"] = today
        
        if movie_in_db and daily_requests[tmdb_id]["count"] >= REQUEST_LIMIT:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))]
            ])
            text += "\n\n🚫 Esta película ha superado el límite de solicitudes diarias. Haz clic en 'Ver ahora' para acceder al enlace."
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Solicitar esta", callback_data=f"request_movie:{tmdb_id}:{message.from_user.id}")]
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
            logging.error(f"Error al enviar la opción de película para solicitud: {e}")

    await state.clear()
    
@dp.callback_query(F.data.startswith("request_movie_by_id:"))
async def handle_movie_request_by_id(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    tmdb_id = int(callback_query.data.split(':')[1])
    requester_id = callback_query.from_user.id
    
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        return

    movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
    
    today = datetime.date.today().isoformat()
    if tmdb_id not in daily_requests:
        daily_requests[tmdb_id] = {"count": 0, "date": today}
    if daily_requests[tmdb_id]["date"] != today:
        daily_requests[tmdb_id]["count"] = 0
        daily_requests[tmdb_id]["date"] = today
        
    if movie_in_db and daily_requests[tmdb_id]["count"] >= REQUEST_LIMIT:
        await bot.send_message(callback_query.message.chat.id, f"🚫 Esta película ha superado el límite de solicitudes diarias. Aquí tienes el enlace para verla:")
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))]
        ])
        await bot.send_message(callback_query.message.chat.id, f"**{movie_in_db.get('title')}**", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    elif movie_in_db:
        await bot.send_message(callback_query.message.chat.id, f"La película **{movie_in_db.get('title')}** ya existe en el catálogo. Publicándola en el canal...")
        daily_requests[tmdb_id]["count"] += 1
        
        await delete_old_post(tmdb_id)
        
        text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_in_db.get("link"))
        success, message_id = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_in_db.get("link"), post_keyboard)
        
        if success:
            notification_message = (
                f"Tu película fue publicada en el canal principal. Haz clic aquí para verla"
            )
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📢 Ver en el canal", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}/{message_id}")]
            ])
            await bot.send_message(callback_query.from_user.id, notification_message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    else:
        poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
        caption_text = (
            f"✨ **Nueva solicitud de película**\n\n"
            f"El usuario **{callback_query.from_user.full_name}** (@{callback_query.from_user.username})\n"
            f"ha solicitado: **{tmdb_data.get('title')}**\n"
            f"ID de la película: `{tmdb_id}`\n\n"
        )
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt:{tmdb_id}:{requester_id}")]
        ])
        
        if poster_url:
            await bot.send_photo(
                ADMIN_ID,
                photo=poster_url,
                caption=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                text=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
            
        await bot.send_message(callback_query.message.chat.id, f"✅ Tu solicitud para **{tmdb_data.get('title')}** ha sido enviada al administrador. ¡Te avisaremos cuando esté lista!")


@dp.callback_query(F.data.startswith("request_movie:"))
async def handle_movie_request_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    parts = callback_query.data.split(':')
    tmdb_id = int(parts[1])
    requester_id = int(parts[2])
    
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        return

    movie_in_db = await get_movie_by_tmdb_id(tmdb_id)
    
    today = datetime.date.today().isoformat()
    if tmdb_id not in daily_requests:
        daily_requests[tmdb_id] = {"count": 0, "date": today}
    if daily_requests[tmdb_id]["date"] != today:
        daily_requests[tmdb_id]["count"] = 0
        daily_requests[tmdb_id]["date"] = today
        
    if movie_in_db and daily_requests[tmdb_id]["count"] >= REQUEST_LIMIT:
        await bot.send_message(callback_query.message.chat.id, f"🚫 Esta película ha superado el límite de solicitudes diarias. Aquí tienes el enlace para verla:")
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🎬 Ver ahora", url=movie_in_db.get("link"))]
        ])
        await bot.send_message(callback_query.message.chat.id, f"**{movie_in_db.get('title')}**", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    elif movie_in_db:
        await bot.send_message(callback_query.message.chat.id, f"La película **{movie_in_db.get('title')}** ya existe en el catálogo. Publicándola en el canal...")
        daily_requests[tmdb_id]["count"] += 1
        
        await delete_old_post(tmdb_id)
        
        text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_in_db.get("link"))
        success, message_id = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_in_db.get("link"), post_keyboard)
        
        if success:
            notification_message = (
                f"Tu película fue publicada en el canal principal. Haz clic aquí para verla"
            )
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📢 Ver en el canal", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}/{message_id}")]
            ])
            await bot.send_message(callback_query.from_user.id, notification_message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    else:
        poster_url = get_movie_poster_url(tmdb_data.get("poster_path"))
        caption_text = (
            f"✨ **Nueva solicitud de película**\n\n"
            f"El usuario **{callback_query.from_user.full_name}** (@{callback_query.from_user.username})\n"
            f"ha solicitado: **{tmdb_data.get('title')}**\n"
            f"ID de la película: `{tmdb_id}`\n\n"
        )
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt:{tmdb_id}:{requester_id}")]
        ])
        
        if poster_url:
            await bot.send_photo(
                ADMIN_ID,
                photo=poster_url,
                caption=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                text=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
            
        await bot.send_message(callback_query.message.chat.id, f"✅ Tu solicitud para **{tmdb_data.get('title')}** ha sido enviada al administrador. ¡Te avisaremos cuando esté lista!")


@dp.callback_query(F.data.startswith("publish_now_from_trakt:"))
async def publish_now_from_trakt_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return
    await bot.answer_callback_query(callback_query.id, "Preparando para agregar la película...", show_alert=True)
    parts = callback_query.data.split(':')
    tmdb_id = int(parts[1])
    requester_id = int(parts[2])
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "No se pudo obtener la información completa de la película desde TMDB. Por favor, reinicie el proceso manualmente.")
        return
    await state.update_data(
        tmdb_id=tmdb_id,
        movie_title=tmdb_data.get("title"),
        original_request_id=callback_query.message.message_id,
        requester_id=requester_id
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
    requester_id = user_data.get('requester_id')
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
    success, message_id = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_link, post_keyboard)
    await state.clear()
    if success:
        await message.reply("✅ Película agregada a la base de datos y publicada con éxito.")
        if requester_id:
            notification_message = (
                f"🎉 ¡Tu película solicitada, **{tmdb_data.get('title')}**, ya está disponible en el canal!\n\n"
                f"Haz clic en el botón de abajo para verla."
            )
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🎬 Ver ahora", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}/{message_id}")],
                [types.InlineKeyboardButton(text="➡️ Ir al Canal", url=MAIN_CHANNEL_INVITE_LINK)],
                [types.InlineKeyboardButton(text="✨ Pedir otra película", url="https://t.me/sdmin_dy_bot?start=request")]
            ])
            await bot.send_message(requester_id, notification_message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply("✅ Película agregada a la base de datos, pero ocurrió un error al publicarla en el canal.")
    if original_request_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=original_request_id)
        except Exception as e:
            logging.error(f"No se pudo eliminar el mensaje original de la solicitud: {e}")

@dp.callback_query(F.data.startswith("publish_now_manual:"))
async def publish_now_manual(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    tmdb_id = int(callback_query.data.split(':')[1])
    movie_info = await get_movie_by_tmdb_id(tmdb_id)
    if not movie_info:
        await bot.send_message(callback_query.message.chat.id, "Error: película no encontrada en la base de datos.")
        return
    
    tmdb_data = await get_movie_details(tmdb_id)
    if not tmdb_data:
        await bot.send_message(callback_query.message.chat.id, "Error al obtener la información de la película. No se puede publicar.")
        return
    
    await delete_old_post(tmdb_id)
    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
    success, message_id = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
    
    if success:
        notification_message = "✅ Tu película fue publicada en el canal principal. Haz clic aquí para verla."
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📢 Ver en el canal", url=f"https://t.me/{MAIN_CHANNEL_USERNAME}/{message_id}")]
        ])
        await bot.send_message(callback_query.from_user.id, notification_message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.send_message(callback_query.message.chat.id, "Ocurrió un error al publicar la película.")


@dp.callback_query(F.data.startswith("schedule_movie_"))
async def schedule_callback(callback_query: types.CallbackQuery, state: FSMContext):
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
async def final_schedule_callback(callback_query: types.CallbackQuery, state: FSMContext):
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


# --- Automated tasks
async def auto_post_scheduler():
    while True:
        try:
            total_posts_per_day = AUTO_POST_COUNT
            # Evita la división por cero si AUTO_POST_COUNT es 0, aunque por defecto es 4.
            interval_seconds = 24 * 60 * 60 / total_posts_per_day if total_posts_per_day > 0 else 3600 
            unposted_movies = [v for v in await get_all_movies() if str(v.get("last_message_id")) == 'None' or v.get("last_message_id") == '']
            if unposted_movies:
                movie_info = random.choice(unposted_movies)
                movie_id = movie_info.get("id")
                tmdb_data = await get_movie_details(movie_id)
                if tmdb_data:
                    logging.info("Hora de una nueva publicación automática.")
                    
                    await delete_old_post(movie_id)

                    text, poster_url, post_keyboard = create_movie_message(tmdb_data, movie_info.get("link"))
                    success, _ = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
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
                            success, _ = await send_movie_post(TELEGRAM_MAIN_CHANNEL_ID, tmdb_data, movie_info.get("link"), post_keyboard)
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
                        await bot.send_photo(TELEGRAM_PUBLIC_CHANNEL_ID, photo=meme_url, caption=meme_caption)
                        logging.info("Meme publicado con éxito en el canal público.")
                    except Exception as e:
                        logging.error(f"Error al publicar un meme en el canal público: {e}")
                else:
                    logging.warning("No se encontraron memes. Intentando publicar una noticia en su lugar.")
                    content_type = "news"

            if content_type == "news":
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
                            await bot.send_photo(TELEGRAM_PUBLIC_CHANNEL_ID, photo=poster_url, caption=text, parse_mode=ParseMode.HTML)
                        else:
                            await bot.send_message(TELEGRAM_PUBLIC_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
                        logging.info("Noticia de cine publicada con éxito en el canal público.")
                    except Exception as e:
                        logging.error(f"Error al publicar una noticia en el canal público: {e}")
                else:
                    logging.warning("No se encontraron noticias para publicar.")

            await asyncio.sleep(4 * 3600)  # Espera 4 horas antes de la siguiente publicación de contenido
        except Exception as e:
            logging.error(f"Error en el programador de contenido del canal: {e}")
            await asyncio.sleep(60)

# WEBHOOK SETUP
async def handle_home(request):
    return web.Response(text="Tu bot está activo y funcionando. ¡El webhook está configurado!")

async def on_startup(app):
    # Usar variable de entorno si está configurada, sino, asumir el entorno local/dev
    RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL') 
    if RENDER_EXTERNAL_URL:
        WEBHOOK_URL = RENDER_EXTERNAL_URL + '/webhook'
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook establecido con éxito: {WEBHOOK_URL}")
    else:
        logging.warning("RENDER_EXTERNAL_URL no está configurada. El bot podría estar corriendo en modo polling o debe configurarse manualmente.")

async def handle_telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.error(f"Error al procesar el webhook de Telegram: {e}")
    finally:
        return web.Response(text="OK")

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
