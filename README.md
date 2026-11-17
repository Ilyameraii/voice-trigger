# Voice trigger

[English](#english) · Русский

Небольшое приложение для Windows: слушает микрофон в фоне и, когда в одной
фразе звучат все заданные слова, открывает нужную ссылку в браузере по
умолчанию. Распознавание речи работает **офлайн** на [Vosk](https://alphacephei.com/vosk/).

## Возможности

- Работа в фоне, иконка в системном трее
- Несколько фраз, у каждой своя ссылка
- Срабатывание по набору слов, а не по точной фразе
- Выбор микрофона, настройки применяются без перезапуска
- Распознавание локально, звук никуда не отправляется

## Установка (готовый exe)

1. Скачайте архив `voice-trigger-vX.Y.Z-win64.zip` со страницы
   [Releases](../../releases).
2. Распакуйте в любую папку, лучше без русских букв в пути (например `C:\voice`).
3. Запустите `voice_trigger_bg.exe`. При первом запуске скачается языковая
   модель (около 45 МБ), нужен интернет.
4. Правый клик по иконке в трее → **Настройки** → **Добавить фразу**.

Рядом с exe создаются `config.json` (ваши фразы), `models/` и `voice_trigger.log`.

> Windows SmartScreen или антивирус могут предупредить о неизвестном
> приложении: exe не подписан. Контрольная сумма SHA-256 лежит в релизе,
> сборка выполняется публично в GitHub Actions.

## Меню в трее

| Пункт | Что делает |
|---|---|
| Настройки | Консольное меню: микрофон, добавить фразу, список фраз |
| Прослушивание в консоли | Показывает громкость и распознанный текст |
| Открыть лог | Открывает `voice_trigger.log` |
| Выход | Закрывает программу |

## Запуск из исходников

Нужны Windows 10/11 и Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python voice_trigger_bg.py      # фон + трей
python voice_trigger.py         # только консольная версия
```

## Сборка exe

```bash
pip install -r requirements.txt -r requirements-dev.txt
pyinstaller --clean voice_trigger_bg.spec
```

Результат: `dist/voice_trigger_bg.exe`.

## Выпуск новой версии

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions соберёт exe и создаст релиз с архивом.

## Конфигурация

Пример в `config.example.json`. Поле `words` — слова, которые должны прозвучать
в одной фразе, `url` — что открыть.

## Ограничения

- Только Windows (используются `msvcrt` и `ctypes.windll`)
- Используется компактная модель `vosk-model-small-ru-0.22`: быстро, но
  точность ниже, чем у больших моделей

## Лицензии

Код проекта: MIT (см. `LICENSE`). Vosk и модель распространяются под своими
лицензиями (Apache 2.0), проверьте актуальные условия на сайте Vosk.

---

## English

Windows tray app that listens to your microphone in the background and opens a
URL in your default browser when all words of a configured phrase are heard in
one utterance. Speech recognition is fully offline, powered by Vosk.

**Install:** download the zip from [Releases](../../releases), unpack it to a
path without non-ASCII characters, run `voice_trigger_bg.exe`. The Russian
speech model (~45 MB) is downloaded on first launch. Right-click the tray icon
→ Settings → add a phrase and a URL.

**Run from source:** `pip install -r requirements.txt`, then
`python voice_trigger_bg.py`. **Build:** `pyinstaller --clean voice_trigger_bg.spec`.

Windows only. Licensed under MIT.
