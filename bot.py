import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
import requests
import psycopg2
from psycopg2 import sql
import os

# Configuración del logging
logging.basicConfig(level=logging.INFO)

# Configuración del bot y la base de datos (asegúrate de que estas variables de entorno estén configuradas)
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) # Convertir a int
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Estados para la máquina de estados finitos (FSM)
class MovieSearchStates(StatesGroup):
    waiting_for_movie_title = State()

class MovieUploadStates(StatesGroup):
    waiting_for_movie_title = State()
    waiting_for_names = State()
    waiting_for_link = State()
    waiting_for_requested_movie_link = State() # Nuevo estado para enlaces solicitados

# --- Funciones de Base de Datos ---

def get_db_connection():
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST
    )
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            names TEXT,
            link TEXT,
            last_message_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_movies_names ON movies (names);
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_movie_to_db(movie_data):
    conn = get_db_connection()
    cur = conn.cursor()
    movie_id = movie_data.get("id")
    title = movie_data.get("title")
    names = movie_data.get("names")
    link = movie_data.get("link")
    last_message_id = movie_data.get("last_message_id")

    cur.execute("SELECT id FROM movies WHERE id = %s", (movie_id,))
    existing_movie = cur.fetchone()

    if existing_movie:
        cur.execute(
            """
            UPDATE movies
            SET title=%s, names=%s, link=%s, last_message_id=%s
            WHERE id=%s
            """,
            (title, names, link, last_message_id, movie_id)
        )
        logging.info(f"Película actualizada en DB: {title} (ID: {movie_id})")
    else:
        cur.execute(
            """
            INSERT INTO movies (id, title, names, link, last_message_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (movie_id, title, names, link, last_message_id)
        )
        logging.info(f"Nueva película guardada en DB: {title} (ID: {movie_id})")
    
    conn.commit()
    cur.close()
    conn.close()

def get_movie_from_db(query):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Intenta buscar por ID de TMDB (si la consulta es un número)
    try:
        movie_id = int(query)
        cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
        movie = cur.fetchone()
        if movie:
            return {
                "id": movie[0],
                "title": movie[1],
                "names": movie[2],
                "link": movie[3],
                "last_message_id": movie[4]
            }
    except ValueError:
        pass # No es un ID, continúa con la búsqueda por nombre

    # Búsqueda más flexible por nombres
    search_query = f"%{query.lower()}%"
    cur.execute(
        """
        SELECT * FROM movies
        WHERE LOWER(title) LIKE %s OR LOWER(names) LIKE %s
        ORDER BY CASE
            WHEN LOWER(title) = %s THEN 0
            WHEN LOWER(names) LIKE %s THEN 1
            ELSE 2
        END, title
        LIMIT 1
        """,
        (search_query, search_query, query.lower(), search_query)
    )
    movie = cur.fetchone()
    cur.close()
    conn.close()

    if movie:
        return {
            "id": movie[0],
            "title": movie[1],
            "names": movie[2],
            "link": movie[3],
            "last_message_id": movie[4]
        }
    return None

def get_all_movies_from_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title, link FROM movies ORDER BY title")
    movies = cur.fetchall()
    cur.close()
    conn.close()
    return movies

def delete_movie_from_db(movie_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM movies WHERE id = %s", (movie_id,))
    conn.commit()
    cur.close()
    conn.close()
    logging.info(f"Película eliminada de DB: ID {movie_id}")

# --- Funciones de TMDB ---

def search_tmdb_movie(query):
    url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={query}&language=es-ES"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    return data.get("results")

def get_movie_details(movie_id):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={TMDB_API_KEY}&language=es-ES"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def get_movie_poster_url(poster_path):
    if poster_path:
        return f"https://image.tmdb.org/t/p/w500{poster_path}"
    return None

# --- Handlers de Usuario ---

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    await message.reply("¡Hola! Soy tu bot de películas. Puedes pedirme una película por su título o ID de TMDB. Para buscar una película, simplemente envíame el título.")

@dp.message(Command("buscar"))
async def start_search(message: types.Message, state: FSMContext):
    await message.reply("Por favor, ingresa el título o ID de la película que deseas buscar.")
    await state.set_state(MovieSearchStates.waiting_for_movie_title)

@dp.message(MovieSearchStates.waiting_for_movie_title)
async def process_movie_request(message: types.Message, state: FSMContext):
    query = message.text.strip()
    await state.clear()

    movie_info_db = get_movie_from_db(query)

    if movie_info_db:
        # La película ya está en la base de datos
        link = movie_info_db.get("link")
        if link:
            await message.reply(f"¡He encontrado la película! Aquí tienes el enlace: {link}")
        else:
            await message.reply(f"¡He encontrado la película '{movie_info_db['title']}', pero no tengo el enlace. Por favor, solicítalo al administrador.")
        return

    # Si no está en la base de datos, busca en TMDB
    results = search_tmdb_movie(query)

    if not results:
        await message.reply("Lo siento, no pude encontrar esa película. ¿Podrías intentar con otro título o ID?")
        return

    # Construir teclado con opciones de TMDB
    keyboard_buttons = []
    for movie in results[:5]: # Mostrar hasta 5 resultados
        keyboard_buttons.append(
            [InlineKeyboardButton(text=f"{movie.get('title')} ({movie.get('release_date', 'N/A')[:4]})", callback_data=f"select_tmdb_{movie.get('id')}_{message.from_user.id}")]
        )
    
    cancel_button = [InlineKeyboardButton(text="Cancelar búsqueda", callback_data="cancel_search")]
    keyboard_buttons.append(cancel_button)

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.reply("He encontrado estas películas. ¿Cuál estabas buscando?", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith("select_tmdb_"))
async def handle_tmdb_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer() # Evita que el cliente siga cargando

    parts = callback_query.data.split("_")
    tmdb_id = parts[2]
    original_requester_id = int(parts[3])

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await callback_query.message.reply("No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        return

    movie_title = movie_data.get("title")
    poster_url = get_movie_poster_url(movie_data.get("poster_path"))
    
    # Notificar al administrador para que agregue el enlace
    admin_message_text = (
        f"Un usuario ha solicitado la película: *{movie_title}*\n"
        f"ID de TMDB: `{tmdb_id}`\n\n"
        f"Por favor, envía el enlace de descarga/streaming para esta película."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Publicar ahora", callback_data=f"publish_now_from_tmdb_{tmdb_id}_{original_requester_id}")]
    ])
    
    if poster_url:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=poster_url,
            caption=admin_message_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    await callback_query.message.reply(
        f"He encontrado la película '{movie_title}'. He notificado al administrador para que agregue el enlace."
    )
    
    # Guardar estado para cuando el administrador envíe el enlace
    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)
    await state.update_data(
        tmdb_id=tmdb_id,
        movie_title=movie_title,
        original_request_id=callback_query.message.chat.id # ID del chat del usuario original
    )


@dp.callback_query(lambda c: c.data == "cancel_search")
async def cancel_search_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    await callback_query.message.edit_text("Búsqueda cancelada.")


# --- Handlers de Administrador ---

@dp.message(Command("admin_help"), F.from_user.id == ADMIN_ID)
async def admin_help(message: types.Message):
    help_text = (
        "Comandos de administrador:\n"
        "/add_movie - Añadir una nueva película a la base de datos\n"
        "/list_movies - Listar todas las películas en la base de datos\n"
        "/delete_movie <id> - Eliminar una película por su ID de TMDB\n"
        "/broadcast <mensaje> - Enviar un mensaje a todos los usuarios\n"
        "/admin_help - Mostrar este mensaje de ayuda"
    )
    await message.reply(help_text)

@dp.message(Command("add_movie"), F.from_user.id == ADMIN_ID)
async def add_movie_start(message: types.Message, state: FSMContext):
    await message.reply("Por favor, ingresa el título de la película a añadir (también puedes incluir el año).")
    await state.set_state(MovieUploadStates.waiting_for_movie_title)

@dp.message(MovieUploadStates.waiting_for_movie_title, F.from_user.id == ADMIN_ID)
async def process_movie_title(message: types.Message, state: FSMContext):
    query = message.text.strip()
    results = search_tmdb_movie(query)

    if not results:
        await message.reply("Lo siento, no pude encontrar esa película en TMDB. Intenta con otro título o cancela.")
        return

    keyboard_buttons = []
    for movie in results[:5]:
        keyboard_buttons.append(
            [InlineKeyboardButton(text=f"{movie.get('title')} ({movie.get('release_date', 'N/A')[:4]})", callback_data=f"select_tmdb_admin_{movie.get('id')}")]
        )
    cancel_button = [InlineKeyboardButton(text="Cancelar", callback_data="cancel_upload")]
    keyboard_buttons.append(cancel_button)

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.reply("He encontrado estas películas. ¿Cuál es?", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith("select_tmdb_admin_"), F.from_user.id == ADMIN_ID)
async def select_tmdb_movie_admin(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    tmdb_id = callback_query.data.split("_")[3]

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await callback_query.message.reply("No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        await state.clear()
        return

    await state.update_data(tmdb_id=tmdb_id, movie_title=movie_data.get("title"))
    await callback_query.message.edit_text(
        f"Has seleccionado: *{movie_data.get('title')}* ({movie_data.get('release_date', 'N/A')[:4]}).\n"
        "Ahora, por favor, ingresa los nombres alternativos de la película, separados por comas (e.g., 'Nombre alternativo 1, Otro nombre')."
        "Si no hay nombres alternativos, simplemente escribe 'ninguno'."
    )
    await state.set_state(MovieUploadStates.waiting_for_names)

@dp.message(MovieUploadStates.waiting_for_names, F.from_user.id == ADMIN_ID)
async def process_names(message: types.Message, state: FSMContext):
    names_str = message.text.strip()
    if names_str.lower() == "ninguno":
        names = []
    else:
        names = [name.strip() for name in names_str.split(',')]

    await state.update_data(names=names)
    await message.reply("Ahora, por favor, ingresa el enlace de la película (descarga o streaming).")
    await state.set_state(MovieUploadStates.waiting_for_link)

@dp.message(MovieUploadStates.waiting_for_link, F.from_user.id == ADMIN_ID)
async def add_movie_info(message: types.Message, state: FSMContext):
    movie_link = message.text.strip()
    user_data = await state.get_data()
    tmdb_id = user_data.get("tmdb_id")
    
    tmdb_data = get_movie_details(tmdb_id)
    if not tmdb_data:
        await message.reply("Error al obtener detalles de TMDB. Por favor, inténtalo de nuevo o cancela.")
        await state.clear()
        return

    # Aquí se asegura que 'names' sea una cadena, uniendo los elementos de la lista
    # Si names en user_data es una lista, la convierte a string.
    # Si names en user_data es 'None' o vacío, se asegura de que sea una cadena vacía o None según la necesidad.
    names_list = user_data.get("names", [])
    names_for_db = ", ".join(names_list) if names_list else tmdb_data.get("title") # Usar el título principal si no hay nombres alternativos

    movie_data = {
        "id": tmdb_data.get("id"),
        "title": tmdb_data.get("title"),
        "names": names_for_db, # <--- Corregido: 'names' es ahora una cadena
        "link": movie_link,
        "last_message_id": None
    }
    
    save_movie_to_db(movie_data)
    await message.reply(f"Película '{movie_data.get('title')}' añadida correctamente con el enlace.")
    await state.clear()


@dp.callback_query(lambda c: c.data and c.data.startswith("publish_now_from_tmdb_"), F.from_user.id == ADMIN_ID)
async def publish_now_from_tmdb(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    parts = callback_query.data.split("_")
    tmdb_id = parts[3]
    original_requester_id = int(parts[4])

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await callback_query.message.reply("No se pudo obtener la información de la película. Por favor, inténtalo de nuevo.")
        return

    # Guardar el ID de la solicitud original para poder responder al usuario
    await state.update_data(
        tmdb_id=tmdb_id,
        movie_title=movie_data.get("title"),
        original_request_id=original_requester_id # Guardar el ID del chat del usuario original
    )

    await callback_query.message.edit_text(
        f"Has seleccionado *{movie_data.get('title')}* para publicar. Por favor, envía el enlace de descarga/streaming."
    )
    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)

@dp.message(MovieUploadStates.waiting_for_requested_movie_link, F.from_user.id == ADMIN_ID)
async def process_requested_movie_link(message: types.Message, state: FSMContext):
    movie_link = message.text.strip()
    user_data = await state.get_data()
    tmdb_id = user_data.get("tmdb_id")
    movie_title = user_data.get("movie_title")
    original_request_id = user_data.get("original_request_id")

    tmdb_data = get_movie_details(tmdb_id)
    if not tmdb_data:
        await message.reply("No se pudo obtener la información de la película desde TMDB. Reenvía el enlace o cancela el proceso.")
        return

    main_title = tmdb_data.get("title")
    names_from_tmdb = [main_title]
    if tmdb_data.get("original_title") and tmdb_data.get("original_title") != main_title:
        names_from_tmdb.append(tmdb_data.get("original_title"))
    
    names_for_db = ", ".join(names_from_tmdb) # Convertir la lista a cadena

    new_movie = {
        "id": tmdb_id,
        "title": main_title,
        "names": names_for_db,
        "link": movie_link,
        "last_message_id": None
    }
    
    save_movie_to_db(new_movie)
    await message.reply(f"Película '{main_title}' guardada y publicada. Enlace: {movie_link}")

    # Notificar al usuario que hizo la solicitud original
    try:
        await bot.send_message(original_request_id, f"¡Tu película solicitada, '{main_title}', ya está disponible! Aquí tienes el enlace: {movie_link}")
    except Exception as e:
        logging.error(f"No se pudo enviar mensaje al usuario {original_request_id}: {e}")
    
    await state.clear()


@dp.callback_query(lambda c: c.data == "cancel_upload", F.from_user.id == ADMIN_ID)
async def cancel_upload_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    await callback_query.message.edit_text("Carga de película cancelada.")

@dp.message(Command("list_movies"), F.from_user.id == ADMIN_ID)
async def list_movies(message: types.Message):
    movies = get_all_movies_from_db()
    if not movies:
        await message.reply("No hay películas en la base de datos.")
        return

    response = "Películas en la base de datos:\n"
    for movie in movies:
        response += f"ID: {movie[0]}, Título: {movie[1]}\n"
        if movie[2]: # Si hay un enlace
            response += f"  Enlace: {movie[2]}\n"
        else:
            response += "  (Sin enlace)\n"
    
    await message.reply(response)

@dp.message(Command("delete_movie"), F.from_user.id == ADMIN_ID)
async def delete_movie(message: types.Message):
    try:
        movie_id = int(message.text.split(" ", 1)[1])
        delete_movie_from_db(movie_id)
        await message.reply(f"Película con ID {movie_id} eliminada correctamente.")
    except (IndexError, ValueError):
        await message.reply("Uso: /delete_movie <ID de TMDB>")
    except Exception as e:
        await message.reply(f"Error al eliminar la película: {e}")

@dp.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def broadcast_message(message: types.Message):
    try:
        text_to_broadcast = message.text.split(" ", 1)[1]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT original_request_id FROM movie_requests WHERE original_request_id IS NOT NULL")
        user_ids = cur.fetchall()
        cur.close()
        conn.close()

        sent_count = 0
        failed_count = 0
        
        for user_id_tuple in user_ids:
            user_id = user_id_tuple[0]
            try:
                await bot.send_message(user_id, text_to_broadcast)
                sent_count += 1
                await asyncio.sleep(0.1) # Pequeña pausa para evitar límites de tasa
            except Exception as e:
                logging.warning(f"No se pudo enviar el mensaje a {user_id}: {e}")
                failed_count += 1
        
        await message.reply(f"Mensaje enviado a {sent_count} usuarios. Falló en {failed_count} usuarios.")

    except IndexError:
        await message.reply("Uso: /broadcast <mensaje>")
    except Exception as e:
        await message.reply(f"Error al enviar el broadcast: {e}")


# Inicializa la base de datos
init_db()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
