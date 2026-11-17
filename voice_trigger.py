# -*- coding: utf-8 -*-
"""
Голосовой триггер для Windows.

Слушает микрофон, показывает синюю полоску громкости и, когда в одной фразе
звучат все слова из заданного набора, открывает URL в браузере по умолчанию.

Установка (один раз):
    pip install vosk sounddevice

Запуск:
    python voice_trigger.py

Важно: кладите папку со скриптом в путь без русских букв (например C:\\voice),
иначе библиотека Vosk может не загрузить модель.
"""

import json
import math
import os
import queue
import re
import shutil
import sys
import time
import urllib.request
import webbrowser
import zipfile
from array import array

try:
    import sounddevice as sd
    from vosk import KaldiRecognizer, Model, SetLogLevel
except ImportError:
    print("Не хватает библиотек. Установите их командой:")
    print("    pip install vosk sounddevice")
    sys.exit(1)

# ----------------------------- настройки ------------------------------------

APP_NAME = "Voice trigger"

# В собранном .exe файлы (config.json, models) лежат рядом с самим .exe,
# а не во временной папке, куда PyInstaller распаковывает программу.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MODELS_DIR = os.path.join(BASE_DIR, "models")

# Маленькая быстрая модель (~45 МБ). Для большей точности можно взять
# "vosk-model-ru-0.42" (~1.8 ГБ) — она скачается сама при первом запуске.
MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"

SAMPLE_RATE = 16000
BLOCK_SIZE = 1600          # 0.1 секунды звука за раз
BAR_WIDTH = 40             # ширина полоски громкости
DB_MIN, DB_MAX = -50.0, -10.0   # диапазон громкости, который заполняет полоску
COOLDOWN_SECONDS = 5.0     # защита от повторного запуска одной и той же фразы

BLUE = "\033[104m"
GRAY = "\033[100m"
RESET = "\033[0m"
CLEAR_LINE = "\r\033[2K"

_model = None


# ----------------------------- консоль --------------------------------------

def enable_ansi():
    """Включает цветной вывод (ANSI) в консоли Windows 10/11."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        os.system("")


def set_console_title(title):
    """Название в заголовке окна консоли."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def say(message):
    """Печатает строку поверх полоски громкости, не ломая её."""
    sys.stdout.write(CLEAR_LINE + message + "\n")
    sys.stdout.flush()


# ----------------------------- меню со стрелками ----------------------------

def read_key():
    """Ждёт нажатия клавиши: 'up', 'down', 'home', 'end', 'enter', 'esc' или ''."""
    import msvcrt  # только Windows
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # стрелки и другие служебные клавиши
        return {"H": "up", "P": "down", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
    if ch == "\r":
        return "enter"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ""


def _fit(text, width):
    """Обрезает строку, чтобы она не переносилась и не ломала перерисовку."""
    return text if len(text) < width else text[: max(1, width - 2)] + "…"


def select_menu(title, items, start=0, hint="↑↓ выбор · Enter подтвердить · Esc назад"):
    """Меню со стрелками. items — список (название, описание).

    Возвращает индекс выбранного пункта или None, если нажали Esc.
    """
    if not items:
        return None
    has_desc = any(desc for _, desc in items)
    per_item = 3 if has_desc else 1  # название + описание + пустая строка
    index = min(max(start, 0), len(items) - 1)
    top = 0
    printed = 0
    result = None
    out = sys.stdout
    out.write("\033[?25l")  # прячем курсор
    try:
        while True:
            try:
                cols, rows = os.get_terminal_size()
            except OSError:
                cols, rows = 80, 25
            visible = max(1, min(len(items), (rows - 5) // per_item))
            if index < top:
                top = index
            if index >= top + visible:
                top = index - visible + 1

            lines = [f"\033[1m{_fit(title, cols)}\033[0m", ""]
            for i in range(top, top + visible):
                label, desc = items[i]
                if i == index:
                    lines.append(f"\033[1m▸ {_fit(label, cols - 2)}\033[0m")
                else:
                    lines.append(f"  {_fit(label, cols - 2)}")
                if has_desc:
                    lines.append(f"    \033[90m{_fit(desc, cols - 4)}\033[0m" if desc else "")
                    lines.append("")
            footer = hint
            if len(items) > visible:
                footer += f"  ({index + 1}/{len(items)})"
            lines.append(f"\033[90m{_fit(footer, cols)}\033[0m")

            if printed:
                out.write(f"\033[{printed}A\r")
            out.write("\033[J" + "\n".join(lines) + "\n")
            out.flush()
            printed = len(lines)

            key = read_key()
            if key == "up":
                index = (index - 1) % len(items)
            elif key == "down":
                index = (index + 1) % len(items)
            elif key == "home":
                index = 0
            elif key == "end":
                index = len(items) - 1
            elif key == "enter":
                result = index
                break
            elif key == "esc":
                result = None
                break
    finally:
        if printed:
            out.write(f"\033[{printed}A\r\033[J")  # стираем меню
        out.write("\033[?25h")  # возвращаем курсор
        out.flush()
    return result


# ----------------------------- конфиг ---------------------------------------

def load_config():
    cfg = {"device": None, "phrases": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"Не удалось прочитать config.json ({e}), начинаю с пустого.")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ----------------------------- слова ----------------------------------------

def extract_words(text):
    """Достаёт из текста уникальные слова: нижний регистр, ё -> е."""
    return sorted(set(re.findall(r"\w+", text.lower().replace("ё", "е"))))


def find_matches(text, phrases):
    """Возвращает фразы, все слова которых прозвучали в text (порядок не важен)."""
    heard = set(extract_words(text))
    return [p for p in phrases if p["words"] and set(p["words"]) <= heard]


# ----------------------------- микрофон -------------------------------------

def list_input_devices():
    """Список микрофонов [(индекс, имя)]. Берём только MME, чтобы не было дублей."""
    hostapis = sd.query_hostapis()
    found = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            found.append((i, d["name"], hostapis[d["hostapi"]]["name"]))
    mme = [x for x in found if "MME" in x[2]]
    return [(i, name) for i, name, _ in (mme or found)]


def resolve_device(cfg):
    """Индекс сохранённого микрофона или None (устройство по умолчанию)."""
    wanted = cfg.get("device")
    if not wanted:
        return None
    for i, name in list_input_devices():
        if name == wanted:
            return i
    print(f"Микрофон «{wanted}» не найден, использую устройство по умолчанию.")
    return None


def open_stream(device, q):
    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    return sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        device=device,
        dtype="int16",
        channels=1,
        callback=callback,
    )


def block_level(data):
    """Громкость блока звука от 0.0 до 1.0."""
    samples = array("h")
    samples.frombytes(data)
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    db = 20 * math.log10(max(rms, 1.0) / 32768.0)
    return min(1.0, max(0.0, (db - DB_MIN) / (DB_MAX - DB_MIN)))


def draw_bar(level, extra=""):
    filled = int(level * BAR_WIDTH + 0.5)
    bar = f"{BLUE}{' ' * filled}{GRAY}{' ' * (BAR_WIDTH - filled)}{RESET}"
    line = f"Громкость [{bar}] {int(level * 100):3d}%"
    if extra:
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        room = cols - (10 + 1 + BAR_WIDTH + 1 + 5) - 3
        if room > 5:
            line += "  " + extra[-room:]
    sys.stdout.write(CLEAR_LINE + line)
    sys.stdout.flush()


def level_test(device, seconds=5):
    """Короткая проверка выбранного микрофона: только полоска громкости."""
    print(f"Проверка микрофона ({seconds} сек) — скажите что-нибудь...")
    q = queue.Queue()
    level = 0.0
    end = time.time() + seconds
    try:
        with open_stream(device, q):
            while time.time() < end:
                try:
                    data = q.get(timeout=1)
                except queue.Empty:
                    continue
                level = max(block_level(data), level * 0.85)
                draw_bar(level)
    except Exception as e:
        say(f"Не удалось открыть микрофон: {e}")
        return
    sys.stdout.write(CLEAR_LINE)
    print("Проверка завершена.")


def choose_device(cfg):
    devices = list_input_devices()
    if not devices:
        print("Микрофоны не найдены.")
        return
    current = cfg.get("device")
    names = [None] + [name for _, name in devices]
    items = []
    for name in names:
        label = "Устройство по умолчанию" if name is None else name
        if name == current:
            label += "  (выбрано)"
        items.append((label, ""))
    start = names.index(current) if current in names else 0
    choice = select_menu("Выберите микрофон", items, start=start)
    if choice is None:
        return
    cfg["device"] = names[choice]
    save_config(cfg)
    print(f"Микрофон: {names[choice] or 'по умолчанию'}")
    try:
        level_test(resolve_device(cfg))
    except KeyboardInterrupt:
        sys.stdout.write(CLEAR_LINE)


# ----------------------------- модель ---------------------------------------

def _model_is_complete(path):
    """Папка модели считается целой, только если на месте ключевые файлы.
    Недокачанная/недораспакованная модель (закрыли программу во время
    первой загрузки) приводит к ошибке «Failed to create a model»."""
    if not os.path.isdir(path):
        return False
    required = [
        os.path.join("am", "final.mdl"),
        os.path.join("conf", "mfcc.conf"),
        os.path.join("conf", "model.conf"),
    ]
    return all(os.path.isfile(os.path.join(path, r)) for r in required) and \
        os.path.isdir(os.path.join(path, "graph"))


def _download_model(path):
    """Скачивает и распаковывает модель во временную папку, затем одним
    переименованием кладёт на место — так не останется «полумодели»."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    zip_path = path + ".zip.part"
    tmp_dir = path + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"Скачиваю модель распознавания {MODEL_NAME} (один раз)...")

    def progress(blocks, block_size, total):
        if total > 0:
            done = min(100, blocks * block_size * 100 // total)
            sys.stdout.write(f"\r  {done}%")
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(MODEL_URL, zip_path, progress)
        print("\nРаспаковываю...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp_dir)
        os.remove(zip_path)
        inner = os.path.join(tmp_dir, MODEL_NAME)   # внутри архива — папка с именем модели
        src = inner if os.path.isdir(inner) else tmp_dir
        shutil.rmtree(path, ignore_errors=True)
        os.replace(src, path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return True
    except Exception as e:
        print(f"\nНе удалось скачать модель: {e}")
        print(f"Скачайте вручную {MODEL_URL}")
        print(f"и распакуйте в папку {MODELS_DIR}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return False


def _ascii_path(path):
    """Vosk на Windows не открывает пути с кириллицей. Если в пути есть
    не-ASCII символы, берём короткое 8.3-имя (оно всегда ASCII)."""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)):
                short = buf.value
                try:
                    short.encode("ascii")
                    return short
                except UnicodeEncodeError:
                    pass
        except Exception:
            pass
    return None


def _load_from_safe_path(path):
    """Возвращает Model, при необходимости копируя модель в ASCII-путь."""
    safe = _ascii_path(path)
    if safe is None:
        # Короткие имена отключены — кладём копию в C:\ProgramData (или temp).
        root = os.environ.get("PROGRAMDATA") or os.environ.get("SystemDrive", "C:") + "\\"
        safe = os.path.join(root, "voice_trigger_model", MODEL_NAME)
        if not _model_is_complete(safe):
            shutil.rmtree(safe, ignore_errors=True)
            os.makedirs(os.path.dirname(safe), exist_ok=True)
            shutil.copytree(path, safe)
    return Model(safe)


def ensure_model():
    global _model
    if _model is not None:
        return _model
    path = os.path.join(MODELS_DIR, MODEL_NAME)

    if not _model_is_complete(path):
        if os.path.isdir(path):
            print("Папка модели повреждена или неполная — скачиваю заново.")
        if not _download_model(path):
            return None

    SetLogLevel(-1)
    print("Загружаю модель...")
    try:
        _model = _load_from_safe_path(path)
    except Exception as e:
        # Последний шанс: модель битая — удаляем, качаем заново, пробуем ещё раз.
        print(f"Не удалось загрузить модель ({e}). Перекачиваю...")
        shutil.rmtree(path, ignore_errors=True)
        if not _download_model(path):
            return None
        _model = _load_from_safe_path(path)
    return _model


# ----------------------------- прослушивание --------------------------------

def listen(cfg):
    if not cfg["phrases"]:
        print("Сначала добавьте хотя бы одну фразу (пункт 3).")
        return
    model = ensure_model()
    if model is None:
        return

    device = resolve_device(cfg)
    recognizer = KaldiRecognizer(model, SAMPLE_RATE)
    q = queue.Queue()
    last_fire = {}
    level = 0.0

    def fire_if_matched(text):
        now = time.time()
        fired = False
        for p in find_matches(text, cfg["phrases"]):
            key = p["url"] + "|" + " ".join(p["words"])
            if now - last_fire.get(key, 0) < COOLDOWN_SECONDS:
                continue
            last_fire[key] = now
            say(f"*** Сработало: [{' '.join(p['words'])}] -> {p['url']}")
            try:
                webbrowser.open(p["url"])
            except Exception as e:
                say(f"Не удалось открыть браузер: {e}")
            fired = True
        return fired

    print("\nСлушаю... Ctrl+C — вернуться в меню.\n")
    try:
        with open_stream(device, q):
            while True:
                try:
                    data = q.get(timeout=1)
                except queue.Empty:
                    continue
                level = max(block_level(data), level * 0.85)
                partial = ""

                if recognizer.AcceptWaveform(data):
                    text = json.loads(recognizer.Result()).get("text", "")
                    if text:
                        say(f"Услышал: {text}")
                        fire_if_matched(text)
                else:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "")
                    # Реагируем, не дожидаясь конца фразы
                    if partial and fire_if_matched(partial):
                        recognizer.Reset()
                        partial = ""

                draw_bar(level, partial)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        say(f"Ошибка аудио: {e}")
    sys.stdout.write(CLEAR_LINE)
    print("Прослушивание остановлено.")


# ----------------------------- фразы ----------------------------------------

def normalize_url(url):
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.I):
        url = "https://" + url
    return url


def add_phrase(cfg):
    text = input("\nФраза (слова через пробел, Enter — отмена): ").strip()
    if not text:
        return
    words = extract_words(text)
    if not words:
        print("В тексте нет слов.")
        return
    print("Запомнил слова:", ", ".join(words))
    url = input("URL, который открыть: ").strip()
    if not url:
        print("Отменено.")
        return
    cfg["phrases"].append({"text": text, "words": words, "url": normalize_url(url)})
    save_config(cfg)
    print("Фраза добавлена.")


def show_phrases(cfg):
    index = 0
    while cfg["phrases"]:
        items = [(" + ".join(p["words"]), p["url"]) for p in cfg["phrases"]]
        choice = select_menu("Сохранённые фразы", items, start=index)
        if choice is None:
            return
        index = choice
        phrase = cfg["phrases"][choice]
        action = select_menu(
            f"{' + '.join(phrase['words'])}  →  {phrase['url']}",
            [("Удалить фразу", "Убрать её из списка"), ("Назад", "")],
        )
        if action == 0:
            cfg["phrases"].pop(choice)
            save_config(cfg)
            print(f"Удалено: {' '.join(phrase['words'])}")
            index = max(0, choice - 1)
    print("Фраз пока нет.")


# ----------------------------- меню -----------------------------------------

def main():
    enable_ansi()
    set_console_title(APP_NAME)
    cfg = load_config()
    index = 0
    while True:
        items = [
            ("Начать прослушивание", "Слушать микрофон и открывать ссылки по фразам"),
            ("Выбрать микрофон", f"Сейчас: {cfg.get('device') or 'по умолчанию'}"),
            ("Добавить фразу", "Слова фразы и URL, который откроется"),
            ("Фразы", f"Сохранено: {len(cfg['phrases'])} — просмотр и удаление"),
            ("Выход", "Закрыть программу"),
        ]
        try:
            choice = select_menu(
                "Голосовой триггер", items, start=index,
                hint="↑↓ выбор · Enter подтвердить · Esc выход",
            )
            if choice is None or choice == 4:
                break
            index = choice
            if choice == 0:
                listen(cfg)
            elif choice == 1:
                choose_device(cfg)
            elif choice == 2:
                add_phrase(cfg)
            elif choice == 3:
                show_phrases(cfg)
        except (KeyboardInterrupt, EOFError):
            print()
            break


if __name__ == "__main__":
    main()
