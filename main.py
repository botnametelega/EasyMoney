import logging
import time
import feedparser
import os
import html
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from pathlib import Path
from urllib.parse import urlparse

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()  # вывод в консоль
    ]
)

# Настройки из .env
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

CHANNEL_ID_STR = os.environ.get('CHANNEL_ID')
if not CHANNEL_ID_STR:
    raise ValueError("CHANNEL_ID не задан в .env")
CHANNEL_ID = int(CHANNEL_ID_STR)

RSS_FEED_URL = os.environ.get('RSS_FEED_URL')
if not RSS_FEED_URL:
    raise ValueError("RSS_FEED_URL не задан в .env")

CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 300))  # по умолчанию 5 минут
MAX_RETRIES = 3  # попытки повторных запросов
POST_COOLDOWN = int(os.environ.get('POST_COOLDOWN', 60))  # пауза после отправки поста

# Путь к файлу с ID последней обработанной записи
LAST_ENTRY_FILE = Path('last_entry_id.txt')

# Инициализация бота
bot = Bot(token=TOKEN)

def is_valid_url(url):
    """Проверка валидности URL."""
    if not url:
        return False
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False

def init_bot():
    """Проверка авторизации бота и создание необходимых файлов."""
    try:
        bot.get_me()
        logging.info("Бот авторизован успешно.")
    except TelegramError as e:
        logging.critical(f"Ошибка авторизации бота: {e}")
        exit(1)
    except Exception as e:
        logging.critical(f"Критическая ошибка при инициализации бота: {e}")
        exit(1)

    # Создаём файл для хранения ID последней записи
    if not LAST_ENTRY_FILE.exists():
        try:
            LAST_ENTRY_FILE.write_text('', encoding='utf-8')
        except UnicodeEncodeError as e:
            logging.error(f"Ошибка записи в last_entry_id.txt: {e}")
            exit(1)

def get_last_entry_id():
    """Чтение ID последней обработанной записи."""
    try:
        content = LAST_ENTRY_FILE.read_text(encoding='utf-8').strip()
        return content if content else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logging.error(f"Ошибка чтения last_entry_id.txt: {e}")
        return None

def save_last_entry_id(entry_id):
    """Сохранение ID последней обработанной записи."""
    try:
        LAST_ENTRY_FILE.write_text(entry_id, encoding='utf-8')
    except UnicodeEncodeError as e:
        logging.error(f"Ошибка кодировки при записи в last_entry_id.txt: {e}")
    except Exception as e:
        logging.error(f"Ошибка записи last_entry_id.txt: {e}")

def truncate_text(text, max_len=3500):
    """Обрезка текста до последнего пробела в пределах max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(' ', 1)[0]
    return truncated + '...' if truncated else text[:max_len] + '...'

def extract_image_url(entry):
    """Извлечение URL изображения из записи RSS."""
    # Поиск в media_content
    if 'media_content' in entry and entry.media_content:
        for media in entry.media_content:
            if 'url' in media:
                url = media['url']
                if is_valid_url(url):
                    return url

    # Поиск в links
    if 'links' in entry:
        for link_data in entry.links:
            link_type = link_data.get('type', '')
            href = link_data.get('href')
            if href and is_valid_url(href):
                if link_type.startswith('image'):
                    return href
                # Дополнительная эвристика: ищем ссылки с ключевыми словами в URL
                if any(keyword in href.lower() for keyword in ['image', 'img', 'photo', 'picture']):
                    return href
    return None

def process_and_send_entries(entries):
    """Обработка и отправка записей, избегая дубликатов."""
    last_id = get_last_entry_id()
    new_entries_sent = 0

    for entry in entries:
        entry_id = entry.get('id', entry.link)

        # Пропускаем уже обработанные записи
        if last_id and entry_id == last_id:
            break

        title = html.escape(entry.title)
        link = entry.link
        summary = (entry.summary or '') if hasattr(entry, 'summary') else ''
        summary = html.escape(summary)

        # Формируем сообщение
        message = f"<b>{title}</b>\n\n{truncate_text(summary, 3500)}\n\nЧитать далее: {link}"

        # Ищем изображение
        image_url = extract_image_url(entry)

        # Отправка поста с повторными попытками
        sent = False
        for send_attempt in range(MAX_RETRIES):
            try:
                if image_url:
                    bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=image_url,
                        caption=truncate_text(message, 1024),
                        parse_mode='HTML'
                    )
                    logging.info(f"Пост с фото опубликован: {title}")
                else:
                    bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=message,
                        parse_mode='HTML',
                        disable_web_page_preview=False
                    )
                    logging.info(f"Текст опубликован: {title}")

                # Операции после успешной отправки
                save_last_entry_id(entry_id)
                logging.info(f"Обработана запись: {title} (ID: {entry_id})")
                sent = True
                new_entries_sent += 1
                break  # Успешная отправка — выход из цикла попыток

            except TelegramError as e:
                logging.warning(f"Telegram ошибка при отправке {send_attempt + 1}: {e}")
                if send_attempt == MAX_RETRIES - 1:
                    logging.error("Все попытки отправки сообщения провалены.")
                time.sleep(2)
            except Exception as e:
                logging.warning(f"Общая ошибка при отправке {send_attempt + 1}: {e}")
                if send_attempt == MAX_RETRIES - 1:
                    logging.error("Все попытки отправки сообщения провалены.")
                time.sleep(2)

        if sent:
            # Пауза между отправкой постов
            time.sleep(POST_COOLDOWN)

    return new_entries_sent
def main():
    """Основная функция бота с бесконечным циклом."""
    # Инициализация бота
    init_bot()

    while True:
        try:
            # Загрузка RSS‑ленты
            feed = feedparser.parse(RSS_FEED_URL)

            if feed.bozo:
                logging.error("Ошибка парсинга RSS‑ленты")
            else:
                # Обработка и отправка записей
                new_count = process_and_send_entries(feed.entries)
                if new_count > 0:
                    logging.info(f"Опубликовано {new_count} новых записей")
                else:
                    logging.info("Новых записей нет")

            # Пауза перед следующей проверкой
            logging.info(f"Ожидание {CHECK_INTERVAL} секунд перед следующей проверкой...")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.error(f"Критическая ошибка в основном цикле: {e}")
            logging.info(f"Перезапуск через 60 секунд...")
            time.sleep(60)  # пауза перед повторной попыткой

if __name__ == "__main__":
    main()
