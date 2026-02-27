"""
=============================================================
  MMORPG Healer Accessibility Assistant — Proof of Concept
=============================================================
  Внешний инструмент доступности для класса "Лекарь".
  Анализирует полоску здоровья на экране и автоматически
  нажимает клавиши лечения через DirectInput.

  Горячие клавиши:
    F8              — Калибровка (указать зону HP-бара)
    F9              — Пауза / Возобновление
    F10             — Включить / выключить окно отладки
    Ctrl+Shift+Q    — Выход

  Автор: accessibility-tools community
  Лицензия: MIT
=============================================================
"""

import json
import os
import sys
import time
import threading

import cv2
import numpy as np
import mss
import pydirectinput
import keyboard

# ──────────────────────────────────────────────
#  Константы и пути
# ──────────────────────────────────────────────

# Файл настроек лежит рядом с .exe / скриптом
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

DEFAULT_SETTINGS = {
    # Область интереса (Region Of Interest) — координаты HP-бара.
    # Заполняется автоматически через режим калибровки (F8).
    "roi": {
        "top": 0,
        "left": 0,
        "width": 200,
        "height": 20,
    },
    # Клавиша обычного лечения (привязана к слоту в игре).
    "key_heal": "2",
    # Клавиша экстренного лечения / «паника».
    "key_panic": "3",
    # Порог: выше этого значения (%) — ничего не делаем.
    "threshold_high": 85,
    # Порог: ниже этого значения (%) — экстренное лечение.
    "threshold_low": 40,
    # Кулдаун между нажатиями обычного лечения (секунды).
    "cooldown_heal": 1.5,
    # Кулдаун между нажатиями экстренного лечения (секунды).
    "cooldown_panic": 1.5,
    # ──────────────────────────────────────────
    #  Диапазон HSV для зелёного цвета (здоровье).
    #
    #  КАК ИЗМЕНИТЬ, ЕСЛИ ЦВЕТ ПОЛОСКИ ДРУГОЙ:
    #
    #  OpenCV использует диапазон H: 0-179, S: 0-255, V: 0-255.
    #
    #  Зелёный:  H  35-85,  S  50-255, V  50-255   (по умолчанию)
    #  Красный:  H   0-10  и 170-179,  S 70-255, V  50-255
    #            (красный «оборачивается» через 0, нужны 2 маски)
    #  Жёлтый:  H  20-35,  S  80-255, V  80-255
    #  Синий:   H  90-130, S  50-255, V  50-255
    #  Оранж.:  H  10-20,  S  80-255, V  80-255
    #
    #  Чтобы подобрать точные значения для вашей игры:
    #   1. Включите режим отладки (F10) — появится окно с маской.
    #   2. Если маска пустая (чёрная), расширьте диапазон H.
    #   3. Если маска ловит лишнее, сузьте диапазон.
    # ──────────────────────────────────────────
    "hsv_lower": [35, 50, 50],
    "hsv_upper": [85, 255, 255],
    # Флаг: калибровка пройдена?
    "calibrated": False,
}

# ──────────────────────────────────────────────
#  Глобальное состояние программы
# ──────────────────────────────────────────────

state = {
    "paused": True,           # Начинаем на паузе, пока не откалибровано
    "debug_window": False,    # Показывать cv2.imshow
    "running": True,          # False → выход из главного цикла
    "calibration_step": 0,    # 0 — нет, 1 — ждём первую точку, 2 — ждём вторую
    "calib_point1": None,     # (x, y) левый верхний угол
}

# Таймеры кулдаунов (время последнего нажатия)
last_press = {
    "heal": 0.0,
    "panic": 0.0,
}

# ──────────────────────────────────────────────
#  Работа с настройками
# ──────────────────────────────────────────────


def load_settings() -> dict:
    """Загружает settings.json. Если файла нет — создаёт с дефолтами."""
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Дополняем недостающие ключи дефолтами
            for key, value in DEFAULT_SETTINGS.items():
                if key not in data:
                    data[key] = value
            return data
        except (json.JSONDecodeError, IOError):
            print("[!] settings.json повреждён, пересоздаю...")
    save_settings(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    """Сохраняет настройки в JSON с красивым форматированием."""
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)


# ──────────────────────────────────────────────
#  Калибровка (F8)
# ──────────────────────────────────────────────

def get_mouse_position() -> tuple:
    """Возвращает текущие (x, y) координаты курсора через mss/win32."""
    import ctypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def on_calibrate(settings: dict) -> None:
    """
    Обработчик F8.
    Шаг 1: запоминает верхний-левый угол HP-бара.
    Шаг 2: запоминает нижний-правый угол, вычисляет ROI, сохраняет.
    """
    if state["calibration_step"] == 0:
        # Начинаем калибровку
        state["calibration_step"] = 1
        state["paused"] = True  # Ставим на паузу на время калибровки
        print("\n" + "=" * 50)
        print("  РЕЖИМ КАЛИБРОВКИ")
        print("=" * 50)
        print("  Наведите курсор на ЛЕВЫЙ ВЕРХНИЙ угол")
        print("  полоски здоровья и нажмите F8.")
        print("=" * 50)

    elif state["calibration_step"] == 1:
        pos = get_mouse_position()
        state["calib_point1"] = pos
        state["calibration_step"] = 2
        print(f"  [OK] Точка 1 (лев. верх): x={pos[0]}, y={pos[1]}")
        print()
        print("  Теперь наведите курсор на ПРАВЫЙ НИЖНИЙ угол")
        print("  полоски здоровья и нажмите F8.")
        print("=" * 50)

    elif state["calibration_step"] == 2:
        pos = get_mouse_position()
        p1 = state["calib_point1"]

        left = min(p1[0], pos[0])
        top = min(p1[1], pos[1])
        width = abs(pos[0] - p1[0])
        height = abs(pos[1] - p1[1])

        if width < 5 or height < 2:
            print("  [!] Область слишком маленькая. Попробуйте снова.")
            state["calibration_step"] = 0
            return

        settings["roi"] = {
            "top": top,
            "left": left,
            "width": width,
            "height": height,
        }
        settings["calibrated"] = True
        save_settings(settings)

        state["calibration_step"] = 0
        state["paused"] = False  # Автоматически снимаем паузу

        print(f"  [OK] Точка 2 (прав. низ): x={pos[0]}, y={pos[1]}")
        print()
        print(f"  ROI сохранён: top={top}, left={left}, "
              f"width={width}, height={height}")
        print("=" * 50)
        print("  Калибровка завершена! Помощник АКТИВЕН.")
        print("=" * 50 + "\n")


# ──────────────────────────────────────────────
#  Обработчики горячих клавиш
# ──────────────────────────────────────────────

def on_pause() -> None:
    """F9 — переключение паузы."""
    state["paused"] = not state["paused"]
    status = "ПАУЗА" if state["paused"] else "АКТИВЕН"
    print(f"  >> Помощник: {status}")


def on_debug() -> None:
    """F10 — переключение окна отладки."""
    state["debug_window"] = not state["debug_window"]
    if not state["debug_window"]:
        cv2.destroyAllWindows()
    status = "ВКЛ" if state["debug_window"] else "ВЫКЛ"
    print(f"  >> Окно отладки: {status}")


def on_quit() -> None:
    """Ctrl+Shift+Q — завершение."""
    print("\n  >> Завершение работы...")
    state["running"] = False
    state["paused"] = True
    cv2.destroyAllWindows()


# ──────────────────────────────────────────────
#  CV-пайплайн: анализ здоровья
# ──────────────────────────────────────────────

def calc_health_percent(frame: np.ndarray, settings: dict) -> float:
    """
    Принимает BGR-кадр зоны ROI.
    Возвращает процент заполненности «цветом здоровья» (0.0 — 100.0).

    Алгоритм:
      1. Конвертируем BGR → HSV.
      2. Создаём маску по заданному диапазону HSV.
      3. Считаем долю ненулевых пикселей маски — это и есть % здоровья.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    lower = np.array(settings["hsv_lower"], dtype=np.uint8)
    upper = np.array(settings["hsv_upper"], dtype=np.uint8)

    mask = cv2.inRange(hsv, lower, upper)

    # Показать маску, если включена отладка
    if state["debug_window"]:
        # Рисуем маску поверх оригинала для наглядности
        debug_img = cv2.bitwise_and(frame, frame, mask=mask)
        # Увеличим для удобства просмотра
        scale = max(1, 300 // max(frame.shape[1], 1))
        if scale > 1:
            debug_img = cv2.resize(
                debug_img, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_NEAREST,
            )
            mask_show = cv2.resize(
                mask, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_NEAREST,
            )
        else:
            mask_show = mask

        cv2.imshow("Debug: HP mask", mask_show)
        cv2.imshow("Debug: HP overlay", debug_img)
        cv2.waitKey(1)

    total_pixels = mask.shape[0] * mask.shape[1]
    if total_pixels == 0:
        return 0.0

    green_pixels = cv2.countNonZero(mask)
    return (green_pixels / total_pixels) * 100.0


# ──────────────────────────────────────────────
#  Логика действий
# ──────────────────────────────────────────────

def perform_actions(hp_percent: float, settings: dict) -> None:
    """
    Решает, какую клавишу нажать, исходя из текущего % здоровья.

    Пороги берутся из settings:
      - hp > threshold_high  → ничего (здоровье в порядке)
      - threshold_low ≤ hp ≤ threshold_high → обычное лечение
      - hp < threshold_low   → экстренное лечение (паника)

    Кулдаун предотвращает спам клавиш.
    """
    now = time.time()

    if hp_percent > settings["threshold_high"]:
        # Здоровье достаточно — ничего не делаем.
        return

    if hp_percent < settings["threshold_low"]:
        # Экстренное лечение
        if now - last_press["panic"] >= settings["cooldown_panic"]:
            pydirectinput.press(settings["key_panic"])
            last_press["panic"] = now
            print(f"  [!] HP {hp_percent:5.1f}% — ЭКСТРЕННОЕ лечение "
                  f"(клавиша «{settings['key_panic']}»)")
    else:
        # Обычное лечение
        if now - last_press["heal"] >= settings["cooldown_heal"]:
            pydirectinput.press(settings["key_heal"])
            last_press["heal"] = now
            print(f"  [+] HP {hp_percent:5.1f}% — обычное лечение "
                  f"(клавиша «{settings['key_heal']}»)")


# ──────────────────────────────────────────────
#  Баннер приветствия
# ──────────────────────────────────────────────

def print_banner(settings: dict) -> None:
    print()
    print("=" * 56)
    print("     MMORPG HEALER ACCESSIBILITY ASSISTANT  v0.1")
    print("=" * 56)
    print()
    print("  Горячие клавиши:")
    print("  ------------------------------------------------")
    print("  F8              Калибровка зоны HP-бара")
    print("  F9              Пауза / Возобновление")
    print("  F10             Окно отладки (вкл/выкл)")
    print("  Ctrl+Shift+Q    Выход из программы")
    print("  ------------------------------------------------")
    print()
    print(f"  Клавиша лечения:     «{settings['key_heal']}»")
    print(f"  Клавиша паники:      «{settings['key_panic']}»")
    print(f"  Порог «всё ОК»:      {settings['threshold_high']}%")
    print(f"  Порог «паника»:      < {settings['threshold_low']}%")
    print(f"  Кулдаун лечения:     {settings['cooldown_heal']} сек")
    print(f"  Кулдаун паники:      {settings['cooldown_panic']} сек")
    print()

    if settings["calibrated"]:
        roi = settings["roi"]
        print(f"  ROI (сохранённый): top={roi['top']}, left={roi['left']}, "
              f"{roi['width']}x{roi['height']}")
        print("  Нажмите F9 для старта или F8 для повторной калибровки.")
    else:
        print("  [!] Калибровка не пройдена.")
        print("  Нажмите F8 для калибровки зоны здоровья.")

    print()
    print("=" * 56)
    print()


# ──────────────────────────────────────────────
#  Главная функция
# ──────────────────────────────────────────────

def main() -> None:
    # pydirectinput: отключаем встроенную паузу между действиями
    pydirectinput.PAUSE = 0.05

    # Загрузка / создание настроек
    settings = load_settings()

    # Красивый баннер
    print_banner(settings)

    # ── Регистрация горячих клавиш ──
    # Используем suppress=False, чтобы клавиши проходили в игру.
    keyboard.add_hotkey("F8", lambda: on_calibrate(settings), suppress=False)
    keyboard.add_hotkey("F9", on_pause, suppress=False)
    keyboard.add_hotkey("F10", on_debug, suppress=False)
    keyboard.add_hotkey("ctrl+shift+q", on_quit, suppress=False)

    # Если калибровка уже пройдена, снимаем паузу
    if settings["calibrated"]:
        state["paused"] = False
        print("  >> Помощник: АКТИВЕН (используется сохранённая калибровка)")
    else:
        print("  >> Помощник: ПАУЗА (ожидание калибровки)")

    print()

    # ── Главный цикл ──
    with mss.mss() as sct:
        while state["running"]:
            # Ограничение ~10 тиков/сек
            time.sleep(0.1)

            # Пропускаем, если на паузе или идёт калибровка
            if state["paused"] or state["calibration_step"] != 0:
                # Обработка событий cv2, чтобы окно не зависало
                if state["debug_window"]:
                    cv2.waitKey(1)
                continue

            # Область захвата из настроек
            roi = settings["roi"]
            monitor = {
                "top": roi["top"],
                "left": roi["left"],
                "width": roi["width"],
                "height": roi["height"],
            }

            # Захват экрана (mss возвращает BGRA)
            try:
                screenshot = sct.grab(monitor)
            except Exception as e:
                print(f"  [!] Ошибка захвата экрана: {e}")
                continue

            # Конвертация в numpy BGR (убираем альфа-канал)
            frame = np.array(screenshot, dtype=np.uint8)[:, :, :3]

            # Анализ здоровья
            hp = calc_health_percent(frame, settings)

            # Действие
            perform_actions(hp, settings)

    # Очистка
    cv2.destroyAllWindows()
    keyboard.unhook_all()
    print("\n  Программа завершена. До встречи!\n")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    main()
