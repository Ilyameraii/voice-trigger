# -*- coding: utf-8 -*-
"""
Голосовой триггер — один exe: фон + иконка в трее + настройки.

Обычный запуск: программа сразу слушает микрофон без окна, в трее появляется
иконка. Правый клик по иконке:
    Настройки   — откроется консольное меню (микрофон, добавить фразу, фразы)
    Открыть лог — voice_trigger.log
    Выход       — закрыть программу

Изменения из настроек применяются сразу, перезапускать ничего не нужно.
Настройки открываются отдельным процессом этого же exe (ключ --settings),
поэтому закрытие окна настроек крестиком не останавливает прослушивание.

Установка (один раз):
    pip install vosk sounddevice pystray pillow
"""

import os
import sys
import time

SETTINGS_MODE = "--settings" in sys.argv
MONITOR_MODE = "--monitor" in sys.argv
CONSOLE_MODE = SETTINGS_MODE or MONITOR_MODE  # окна с консолью (не фон)

if getattr(sys, "frozen", False):
    _base = os.path.dirname(sys.executable)
else:
    _base = os.path.dirname(os.path.abspath(__file__))

LOG_PATH = os.path.join(_base, "voice_trigger.log")


def attach_console():
    """В процессе настроек (exe без консоли) подключает консоль к вводу/выводу."""
    import ctypes
    import io
    import msvcrt
    from ctypes import wintypes

    k = ctypes.windll.kernel32
    if not k.GetConsoleWindow():
        k.AllocConsole()

    fd_out = os.open("CONOUT$", os.O_RDWR)
    fd_in = os.open("CONIN$", os.O_RDWR)
    os.dup2(fd_out, 1)
    os.dup2(fd_out, 2)
    os.dup2(fd_in, 0)

    # Чтобы vt.enable_ansi() нашёл консоль через GetStdHandle
    k.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    k.SetStdHandle(-10 & 0xFFFFFFFF, msvcrt.get_osfhandle(0))
    k.SetStdHandle(-11 & 0xFFFFFFFF, msvcrt.get_osfhandle(1))
    k.SetStdHandle(-12 & 0xFFFFFFFF, msvcrt.get_osfhandle(2))

    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except Exception:
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
        sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False),
                                      encoding="utf-8", line_buffering=True)
        sys.stderr = sys.stdout
        sys.stdin = io.TextIOWrapper(io.FileIO(0, "r", closefd=False), encoding="utf-8")


if CONSOLE_MODE:
    attach_console()
    _log = None
else:
    # Лог вместо консоли. Делаем это ДО импорта voice_trigger: в сборке
    # с --noconsole у sys.stdout нет значения, и любой print/write упал бы.
    _log = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log

import json
import queue
import subprocess
import threading
import webbrowser

import voice_trigger as vt

if not CONSOLE_MODE:
    import pystray
    from PIL import Image, ImageDraw

stop_event = threading.Event()
console_procs = {}  # ключ — флаг запуска ("--settings" / "--monitor")


def log(message):
    if _log:
        _log.write(time.strftime("[%H:%M:%S] ") + message + "\n")
        _log.flush()


_mutex = None


def already_running():
    """Не даём запустить вторую копию (иначе ссылки открывались бы дважды)."""
    global _mutex
    if os.name != "nt":
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    _mutex = kernel32.CreateMutexW(None, False, "Local\\VoiceTriggerBackground")
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# ----------------------------- слушатель ------------------------------------

def config_stamp():
    try:
        return os.path.getmtime(vt.CONFIG_PATH)
    except OSError:
        return 0


def run(cfg, model, stamp):
    """Слушает, пока не выйдут из программы или не изменится config.json."""
    device = vt.resolve_device(cfg)
    recognizer = vt.make_recognizer(model, cfg["phrases"], log)
    q = queue.Queue()
    last_fire = {}

    def fire_if_matched(text):
        now = time.time()
        fired = False
        for p in vt.find_matches(text, cfg["phrases"]):
            key = p["url"] + "|" + " ".join(p["words"])
            if now - last_fire.get(key, 0) < vt.COOLDOWN_SECONDS:
                continue
            last_fire[key] = now
            log(f"Сработало: [{' '.join(p['words'])}] -> {p['url']}")
            try:
                webbrowser.open(p["url"])
            except Exception as e:
                log(f"Не удалось открыть браузер: {e}")
            fired = True
        return fired

    with vt.open_stream(device, q):
        log(f"Слушаю... (фраз: {len(cfg['phrases'])}, микрофон: {cfg.get('device') or 'по умолчанию'})")
        while not stop_event.is_set():
            if config_stamp() != stamp:
                log("Настройки изменились, перезапускаю прослушивание.")
                stop_event.wait(0.5)  # дать файлу дозаписаться
                return
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                continue

            if recognizer.AcceptWaveform(data):
                text = vt.clean_text(json.loads(recognizer.Result()).get("text", ""))
                if text:
                    log(f"Услышал: {text}")
                    fire_if_matched(text)
            else:
                partial = vt.clean_text(json.loads(recognizer.PartialResult()).get("partial", ""))
                if partial and fire_if_matched(partial):
                    recognizer.Reset()


def worker(icon):
    """Фоновый поток: грузит модель, слушает, подхватывает новые настройки."""
    model = None
    first = True
    while not stop_event.is_set():
        stamp = config_stamp()
        cfg = vt.load_config()

        if not cfg["phrases"]:
            log("Нет сохранённых фраз. Откройте «Настройки» в меню иконки.")
            if first:
                open_settings()
            first = False
            while not stop_event.wait(0.5):
                if config_stamp() != stamp:
                    stop_event.wait(0.5)
                    break
            continue
        first = False

        if model is None:
            model = vt.ensure_model()
            if model is None:
                log("Модель распознавания недоступна, выхожу.")
                icon.stop()
                return

        # Если микрофон отвалился (отключили, спящий режим) — пробуем снова.
        try:
            run(cfg, model, stamp)
        except Exception as e:
            log(f"Ошибка аудио: {e}. Повтор через 5 секунд.")
            stop_event.wait(5)


# ----------------------------- трей -----------------------------------------

def make_icon_image():
    """Синий кружок с белым микрофоном."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(30, 120, 255, 255))
    d.rounded_rectangle((24, 12, 40, 36), radius=8, fill="white")     # капсула
    d.arc((18, 22, 46, 46), start=0, end=180, fill="white", width=3)  # дужка
    d.line((32, 46, 32, 52), fill="white", width=3)                    # ножка
    d.line((24, 52, 40, 52), fill="white", width=3)                    # подставка
    return img


def launch_console(flag):
    """Запускает этот же exe с ключом flag в новом окне консоли."""
    proc = console_procs.get(flag)
    if proc is not None and proc.poll() is None:
        return  # такое окно уже открыто
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, flag]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), flag]
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    try:
        console_procs[flag] = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=env,
            cwd=_base,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"Не удалось открыть окно {flag}: {e}")


def open_settings(icon=None, item=None):
    launch_console("--settings")


def open_monitor(icon=None, item=None):
    launch_console("--monitor")


def open_log(icon, item):
    try:
        os.startfile(LOG_PATH)
    except Exception as e:
        log(f"Не удалось открыть лог: {e}")


def quit_app(icon, item):
    log("Выход по запросу пользователя.")
    stop_event.set()
    for proc in console_procs.values():
        if proc.poll() is None:
            proc.terminate()
    icon.stop()


def main():
    if already_running():
        return

    icon = pystray.Icon(
        "voice_trigger",
        make_icon_image(),
        "Голосовой триггер — слушает",
        menu=pystray.Menu(
            pystray.MenuItem("Настройки", open_settings, default=True),
            pystray.MenuItem("Прослушивание в консоли", open_monitor),
            pystray.MenuItem("Открыть лог", open_log),
            pystray.MenuItem("Выход", quit_app),
        ),
    )

    t = threading.Thread(target=worker, args=(icon,), daemon=True)
    t.start()

    icon.run()  # блокирует, пока не нажмут «Выход»
    stop_event.set()
    t.join(timeout=3)
    os._exit(0)


# ----------------------------- окно настроек --------------------------------

def settings_main():
    import traceback
    try:
        vt.enable_ansi()
        vt.set_console_title("Голосовой триггер — настройки")
        cfg = vt.load_config()
        index = 0
        while True:
            items = [
                ("Выбрать микрофон", f"Сейчас: {cfg.get('device') or 'по умолчанию'}"),
                ("Добавить фразу", "Слова фразы и URL, который откроется"),
                ("Фразы", f"Сохранено: {len(cfg['phrases'])} — просмотр и удаление"),
                ("Закрыть настройки", "Изменения уже применены, прослушивание идёт в фоне"),
            ]
            choice = vt.select_menu(
                "Настройки голосового триггера", items, start=index,
                hint="↑↓ выбор · Enter подтвердить · Esc закрыть",
            )
            if choice is None or choice == 3:
                break
            index = choice
            if choice == 0:
                vt.choose_device(cfg)
            elif choice == 1:
                vt.add_phrase(cfg)
            elif choice == 2:
                vt.show_phrases(cfg)
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        print("\nНажмите любую клавишу, чтобы закрыть окно...")
        try:
            import msvcrt
            msvcrt.getwch()
        except Exception:
            time.sleep(10)


# ----------------------------- окно прослушивания ---------------------------

def monitor_run(cfg, model, stamp):
    """Показывает полоску громкости и распознанный текст. Ссылки НЕ открывает."""
    device = vt.resolve_device(cfg)
    recognizer = vt.make_recognizer(model, cfg["phrases"], vt.say)
    q = queue.Queue()
    last_seen = {}
    level = 0.0

    def show_matches(text):
        now = time.time()
        shown = False
        for p in vt.find_matches(text, cfg["phrases"]):
            key = p["url"] + "|" + " ".join(p["words"])
            if now - last_seen.get(key, 0) < vt.COOLDOWN_SECONDS:
                continue
            last_seen[key] = now
            vt.say(f"*** Совпало: [{' '.join(p['words'])}] -> {p['url']}")
            shown = True
        return shown

    with vt.open_stream(device, q):
        while True:
            if config_stamp() != stamp:
                vt.say("Настройки изменились, перезапускаю...")
                time.sleep(0.5)
                return
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                continue
            level = max(vt.block_level(data), level * 0.85)
            partial = ""

            if recognizer.AcceptWaveform(data):
                text = vt.clean_text(json.loads(recognizer.Result()).get("text", ""))
                if text:
                    vt.say(f"Услышал: {text}")
                    show_matches(text)
            else:
                partial = vt.clean_text(json.loads(recognizer.PartialResult()).get("partial", ""))
                if partial and show_matches(partial):
                    recognizer.Reset()
                    partial = ""

            vt.draw_bar(level, partial)


def monitor_main():
    import traceback
    try:
        vt.enable_ansi()
        vt.set_console_title("Голосовой триггер — прослушивание")
        model = vt.ensure_model()
        if model is None:
            raise RuntimeError("Модель распознавания недоступна.")
        print("\nПрослушивание (только просмотр: ссылки открывает фоновая программа).")
        print("Закрыть окно: Ctrl+C или крестик — фон продолжит работать.\n")
        while True:
            stamp = config_stamp()
            cfg = vt.load_config()
            monitor_run(cfg, model, stamp)
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        print("\nНажмите любую клавишу, чтобы закрыть окно...")
        try:
            import msvcrt
            msvcrt.getwch()
        except Exception:
            time.sleep(10)


if __name__ == "__main__":
    if SETTINGS_MODE:
        settings_main()
    elif MONITOR_MODE:
        monitor_main()
    else:
        main()
