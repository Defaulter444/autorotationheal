"""
================================================================
  WoW Discipline Priest (Voidweaver) — Accessibility Assistant
================================================================
  Внешний инструмент доступности для Disc Priest в M+ (5 чел).
  Сканирует HP-бары всех членов группы через Computer Vision,
  применяет ротацию Atonement + урон через DirectInput.

  Ротация (по приоритету):
    1. Desperate Prayer     — если свои HP < 20% (off-GCD)
    2. Fade                 — DR когда группе плохо (off-GCD)
    3. Flash Heal           — экстренный хил на умирающего
    4. Power Word: Radiance — если 3+ раненых (Atonement на всех)
    5. PW: Shield / Plea    — точечный Atonement
    6. Mind Blast > Penance > SW: Death > Smite — урон (лечит
       через Atonement)

  Горячие клавиши:
    F8              — Калибровка (3 точки для 5 HP-баров)
    F9              — Пауза / Возобновление
    F10             — Окно отладки (вкл/выкл)
    Ctrl+Shift+Q    — Безопасный выход

  Лицензия: MIT
================================================================
"""

import json
import os
import sys
import time
import ctypes
import copy

import cv2
import numpy as np
import mss
import pydirectinput
import keyboard

# ──────────────────────────────────────────────────────────────
#  Пути
# ──────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# ──────────────────────────────────────────────────────────────
#  Настройки по умолчанию
# ──────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    # --- Группа (5 человек) ---
    # select_key : клавиша выбора цели в игре (F1-F5).
    # roi        : координаты HP-бара (заполняются калибровкой).
    "party_size": 5,
    "members": [
        {"name": "Участник 1", "select_key": "f1",
         "roi": {"top": 0, "left": 0, "width": 0, "height": 0}},
        {"name": "Участник 2", "select_key": "f2",
         "roi": {"top": 0, "left": 0, "width": 0, "height": 0}},
        {"name": "Участник 3", "select_key": "f3",
         "roi": {"top": 0, "left": 0, "width": 0, "height": 0}},
        {"name": "Участник 4", "select_key": "f4",
         "roi": {"top": 0, "left": 0, "width": 0, "height": 0}},
        {"name": "Участник 5", "select_key": "f5",
         "roi": {"top": 0, "left": 0, "width": 0, "height": 0}},
    ],
    # Индекс «себя» в списке (0-based). Нужен для Desperate Prayer.
    "self_index": 0,

    # --- Способности ---
    # key      : клавиша в игре
    # cooldown : перезарядка (сек). 0 = нет КД.
    # cast_time: время каста (сек). 0 = мгновенное.
    # off_gcd  : True = не тратит GCD (можно жать во время GCD).
    #
    # ВАЖНО: подставьте СВОИ клавиши, если биндинги отличаются.
    # Кулдауны зависят от хасты и талантов — подправьте под персонажа.
    "abilities": {
        # ── Лечение / Atonement ──
        "radiance":   {"key": "1", "cooldown": 15.0, "cast_time": 2.0,
                       "off_gcd": False},
        "penance":    {"key": "2", "cooldown": 9.0,  "cast_time": 2.0,
                       "off_gcd": False},
        "shield":     {"key": "3", "cooldown": 6.0,  "cast_time": 0.0,
                       "off_gcd": False},
        "flash_heal": {"key": "4", "cooldown": 0.0,  "cast_time": 1.5,
                       "off_gcd": False},
        "plea":       {"key": "5", "cooldown": 0.0,  "cast_time": 0.0,
                       "off_gcd": False},
        # ── Урон (лечит через Atonement) ──
        "mind_blast": {"key": "q", "cooldown": 24.0, "cast_time": 1.5,
                       "off_gcd": False},
        "smite":      {"key": "e", "cooldown": 0.0,  "cast_time": 1.5,
                       "off_gcd": False},
        "sw_death":   {"key": "r", "cooldown": 20.0, "cast_time": 0.0,
                       "off_gcd": False},
        # ── Защита (off-GCD) ──
        "desperate_prayer": {"key": "shift+q", "cooldown": 90.0,
                             "cast_time": 0.0, "off_gcd": True},
        "fade":             {"key": "shift+e", "cooldown": 30.0,
                             "cast_time": 0.0, "off_gcd": True},
    },

    # Клавиша возврата на враждебную цель после лечения союзника.
    # Стандартно Tab, или макрос /targetlastenemy.
    "target_enemy_key": "tab",

    # GCD в WoW (базовое 1.5 сек, хаста уменьшает).
    "gcd_duration": 1.5,

    # Задержка между переключением цели и кастом (сек).
    # 50 мс хватает для регистрации.
    "target_switch_delay": 0.05,

    # --- Пороги HP (%) ---
    "thresholds": {
        "healthy":   85,   # Выше — всё в порядке
        "moderate":  60,   # Ниже — нужно подлечить
        "critical":  40,   # Ниже — срочно
        "emergency": 20,   # Ниже — экстренно, прямой хил
        "dead":       2,   # Ниже — вероятно мёртв, не тратим хил
    },

    # Сколько раненых нужно для каста Radiance (AoE Atonement).
    "group_heal_count": 3,

    # Длительность Atonement от Radiance (60% от базовых 15 сек).
    # После каста Radiance приоритет переключается на урон.
    "atonement_duration": 9.0,

    # ──────────────────────────────────────────────────────
    #  HSV-диапазон для определения «цвета здоровья».
    #
    #  OpenCV: H 0-179, S 0-255, V 0-255.
    #
    #  Зелёный (WoW default):  H  35-85,  S 50-255, V 50-255
    #  Класс-цвета (примеры):
    #    Жрец (белый):     H 0-179, S  0-30,  V 180-255
    #    Друид (оранж.):   H  10-20, S 80-255, V  80-255
    #    Маг (голубой):    H  90-110, S 80-255, V 100-255
    #
    #  Как подобрать:
    #    1. Включите отладку (F10) — увидите маску каждого бара.
    #    2. Если маска пуста — расширьте H.
    #    3. Если ловит лишнее — сузьте S/V снизу.
    # ──────────────────────────────────────────────────────
    "hsv_lower": [35, 50, 50],
    "hsv_upper": [85, 255, 255],

    "calibrated": False,

    # Интервал сканирования (сек). 0.1 = ~10 Гц.
    "scan_interval": 0.1,
}

# ──────────────────────────────────────────────────────────────
#  Глобальное состояние
# ──────────────────────────────────────────────────────────────
state = {
    "paused": True,
    "debug_window": False,
    "running": True,

    # Калибровка: 0=нет, 1=ждём точку 1, 2=ждём точку 2, 3=ждём точку 3
    "calibration_step": 0,
    "calib_points": [],

    # Кулдауны: ability_name -> timestamp когда КД закончится
    "cooldowns": {},
    # GCD: timestamp когда GCD закончится
    "gcd_expires_at": 0.0,
    # Время последнего каста Radiance (для Atonement-фазы)
    "last_radiance_time": 0.0,
    # Время, когда нужно переключиться обратно на врага
    "retarget_at": 0.0,
}


# ──────────────────────────────────────────────────────────────
#  Настройки: загрузка / сохранение
# ──────────────────────────────────────────────────────────────

def load_settings() -> dict:
    """Загружает settings.json. Если нет — создаёт с дефолтами."""
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Дополняем отсутствующие ключи верхнего уровня
            for key, value in DEFAULT_SETTINGS.items():
                if key not in data:
                    data[key] = copy.deepcopy(value)
            # Дополняем отсутствующие способности
            default_abs = DEFAULT_SETTINGS["abilities"]
            for ab_name, ab_data in default_abs.items():
                if ab_name not in data.get("abilities", {}):
                    data.setdefault("abilities", {})[ab_name] = \
                        copy.deepcopy(ab_data)
            return data
        except (json.JSONDecodeError, IOError):
            print("[!] settings.json повреждён — пересоздаю...")
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    save_settings(settings)
    return settings


def save_settings(settings: dict) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────
#  Утилиты
# ──────────────────────────────────────────────────────────────

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_mouse_position() -> tuple:
    """Текущие (x, y) координаты курсора (Win32 API)."""
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def press_combo(key_str: str) -> None:
    """
    Нажимает клавишу или комбинацию через pydirectinput.
    Примеры: "q", "f1", "shift+q", "ctrl+shift+1".
    """
    parts = key_str.lower().split("+")
    if len(parts) == 1:
        pydirectinput.press(parts[0])
    else:
        modifiers = parts[:-1]
        key = parts[-1]
        for mod in modifiers:
            pydirectinput.keyDown(mod)
        time.sleep(0.02)
        pydirectinput.press(key)
        time.sleep(0.02)
        for mod in reversed(modifiers):
            pydirectinput.keyUp(mod)


# ──────────────────────────────────────────────────────────────
#  Калибровка (3 точки → 5 HP-баров)
# ──────────────────────────────────────────────────────────────
#
#  Шаг 1 (F8): курсор на ЛЕВЫЙ ВЕРХНИЙ угол HP-бара Участника 1
#  Шаг 2 (F8): курсор на ПРАВЫЙ НИЖНИЙ угол HP-бара Участника 1
#  Шаг 3 (F8): курсор на ЛЕВЫЙ ВЕРХНИЙ угол HP-бара Участника 5
#
#  Программа вычисляет шаг (dx, dy) между участниками и
#  интерполирует ROI для участников 2, 3, 4.
# ──────────────────────────────────────────────────────────────

def on_calibrate(settings: dict) -> None:
    step = state["calibration_step"]

    if step == 0:
        state["calibration_step"] = 1
        state["calib_points"] = []
        state["paused"] = True
        n = settings["party_size"]
        print()
        print("=" * 56)
        print("  КАЛИБРОВКА GRID-ФРЕЙМА (%d участников)" % n)
        print("=" * 56)
        print("  Шаг 1/3: Наведите курсор на ЛЕВЫЙ ВЕРХНИЙ угол")
        print("           HP-бара ПЕРВОГО участника → F8")
        print("=" * 56)
        return

    pos = get_mouse_position()

    if step == 1:
        state["calib_points"].append(pos)
        state["calibration_step"] = 2
        print(f"  [OK] Точка 1: x={pos[0]}, y={pos[1]}")
        print()
        print("  Шаг 2/3: Наведите курсор на ПРАВЫЙ НИЖНИЙ угол")
        print("           HP-бара ПЕРВОГО участника → F8")
        print("=" * 56)

    elif step == 2:
        state["calib_points"].append(pos)
        state["calibration_step"] = 3
        print(f"  [OK] Точка 2: x={pos[0]}, y={pos[1]}")
        print()
        n = settings["party_size"]
        print("  Шаг 3/3: Наведите курсор на ЛЕВЫЙ ВЕРХНИЙ угол")
        print("           HP-бара ПОСЛЕДНЕГО (%d-го) участника → F8" % n)
        print("=" * 56)

    elif step == 3:
        state["calib_points"].append(pos)
        p1, p2, p3 = state["calib_points"]

        bar_w = abs(p2[0] - p1[0])
        bar_h = abs(p2[1] - p1[1])

        if bar_w < 5 or bar_h < 2:
            print("  [!] HP-бар слишком маленький. Начните заново (F8).")
            state["calibration_step"] = 0
            return

        n = settings["party_size"]
        dx = (p3[0] - p1[0]) / max(n - 1, 1)
        dy = (p3[1] - p1[1]) / max(n - 1, 1)

        left0 = min(p1[0], p2[0])
        top0 = min(p1[1], p2[1])

        for i in range(n):
            settings["members"][i]["roi"] = {
                "top":    int(top0 + i * dy),
                "left":   int(left0 + i * dx),
                "width":  bar_w,
                "height": bar_h,
            }

        settings["calibrated"] = True
        save_settings(settings)

        state["calibration_step"] = 0
        state["calib_points"] = []
        state["paused"] = False

        print(f"  [OK] Точка 3: x={pos[0]}, y={pos[1]}")
        print()
        print(f"  Размер бара: {bar_w}x{bar_h} px")
        print(f"  Шаг между участниками: dx={dx:.1f}, dy={dy:.1f}")
        print()
        for i, m in enumerate(settings["members"]):
            r = m["roi"]
            print(f"    #{i+1} {m['name']:14s}  "
                  f"top={r['top']}, left={r['left']}")
        print()
        print("=" * 56)
        print("  Калибровка завершена! Помощник АКТИВЕН.")
        print("=" * 56)
        print()


# ──────────────────────────────────────────────────────────────
#  Горячие клавиши
# ──────────────────────────────────────────────────────────────

def on_pause() -> None:
    state["paused"] = not state["paused"]
    status = "ПАУЗА" if state["paused"] else "АКТИВЕН"
    print(f"  >> Помощник: {status}")


def on_debug() -> None:
    state["debug_window"] = not state["debug_window"]
    if not state["debug_window"]:
        cv2.destroyAllWindows()
    status = "ВКЛ" if state["debug_window"] else "ВЫКЛ"
    print(f"  >> Окно отладки: {status}")


def on_quit() -> None:
    print("\n  >> Завершение работы...")
    state["running"] = False
    state["paused"] = True
    cv2.destroyAllWindows()


# ──────────────────────────────────────────────────────────────
#  CV: захват и анализ HP
# ──────────────────────────────────────────────────────────────

def capture_member_hp(roi: dict, sct, settings: dict) -> tuple:
    """
    Захватывает зону ROI одного участника и считает % здоровья.
    Возвращает (BGR-кадр, процент HP).
    """
    monitor = {
        "top": roi["top"], "left": roi["left"],
        "width": roi["width"], "height": roi["height"],
    }
    screenshot = sct.grab(monitor)
    frame = np.array(screenshot, dtype=np.uint8)[:, :, :3]

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.array(settings["hsv_lower"], dtype=np.uint8)
    upper = np.array(settings["hsv_upper"], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)

    total = mask.shape[0] * mask.shape[1]
    hp_pct = (cv2.countNonZero(mask) / total * 100.0) if total > 0 else 0.0

    return frame, mask, hp_pct


def scan_all_hp(settings: dict, sct) -> tuple:
    """
    Сканирует HP-бары всех участников группы.
    Возвращает (список HP%, список кадров, список масок).
    """
    hp_values = []
    frames = []
    masks = []
    for member in settings["members"]:
        try:
            frame, mask, hp = capture_member_hp(member["roi"], sct, settings)
        except Exception:
            frame = np.zeros((10, 10, 3), dtype=np.uint8)
            mask = np.zeros((10, 10), dtype=np.uint8)
            hp = 0.0
        hp_values.append(hp)
        frames.append(frame)
        masks.append(mask)
    return hp_values, frames, masks


def show_debug_window(hp_values: list, frames: list, masks: list) -> None:
    """Отображает окно отладки: все 5 HP-баров с масками."""
    strips = []
    for i, (frame, mask, hp) in enumerate(zip(frames, masks, hp_values)):
        # Раскрашиваем маску поверх оригинала
        overlay = cv2.bitwise_and(frame, frame, mask=mask)
        # Масштабируем для читаемости
        target_w = 250
        scale = max(1, target_w // max(frame.shape[1], 1))
        if scale > 1:
            overlay = cv2.resize(overlay, None, fx=scale, fy=scale,
                                 interpolation=cv2.INTER_NEAREST)
        # Подпись
        label = f"#{i+1}: {hp:.0f}%"
        cv2.putText(overlay, label, (4, 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        strips.append(overlay)

    # Приводим к одинаковой ширине для vstack
    max_w = max(s.shape[1] for s in strips)
    padded = []
    for s in strips:
        if s.shape[1] < max_w:
            pad = np.zeros((s.shape[0], max_w - s.shape[1], 3), np.uint8)
            s = np.hstack([s, pad])
        padded.append(s)

    composite = np.vstack(padded)
    cv2.imshow("Debug: Party HP", composite)
    cv2.waitKey(1)


# ──────────────────────────────────────────────────────────────
#  Система кулдаунов и GCD
# ──────────────────────────────────────────────────────────────

def is_available(ability_name: str, settings: dict) -> bool:
    """Проверяет, доступна ли способность (КД истёк и GCD свободен)."""
    now = time.time()
    ability = settings["abilities"].get(ability_name)
    if ability is None:
        return False
    # Проверка КД
    if state["cooldowns"].get(ability_name, 0.0) > now:
        return False
    # Проверка GCD (off-GCD способности игнорируют GCD)
    if not ability.get("off_gcd", False):
        if state["gcd_expires_at"] > now:
            return False
    return True


def gcd_ready() -> bool:
    """GCD свободен?"""
    return time.time() >= state["gcd_expires_at"]


def record_ability_use(ability_name: str, settings: dict) -> None:
    """Записывает использование способности: ставит КД и GCD."""
    now = time.time()
    ability = settings["abilities"][ability_name]

    # Кулдаун
    cd = ability.get("cooldown", 0.0)
    if cd > 0:
        state["cooldowns"][ability_name] = now + cd

    # GCD / lockout
    if not ability.get("off_gcd", False):
        cast_time = ability.get("cast_time", 0.0)
        lockout = max(settings["gcd_duration"], cast_time)
        state["gcd_expires_at"] = now + lockout


def in_atonement_phase(settings: dict) -> bool:
    """Активна ли фаза Atonement (после Radiance)?"""
    elapsed = time.time() - state["last_radiance_time"]
    return elapsed < settings.get("atonement_duration", 9.0)


# ──────────────────────────────────────────────────────────────
#  Ротация: выбор действия
# ──────────────────────────────────────────────────────────────
#
#  Возвращает кортеж:
#    ("idle",)                         — ничего не делаем
#    ("off_gcd", ability_name)         — off-GCD способность
#    ("heal", member_idx, ability)     — хил на участника
#    ("damage", ability_name)          — урон (враг в таргете)
# ──────────────────────────────────────────────────────────────

def choose_damage_action(settings: dict) -> tuple:
    """
    Приоритет урона (из гайда / Combat Assistant):
      1. Mind Blast
      2. Penance (наступательный)
      3. Shadow Word: Death
      4. Smite (филлер)
    """
    if not gcd_ready():
        return ("idle",)
    if is_available("mind_blast", settings):
        return ("damage", "mind_blast")
    if is_available("penance", settings):
        return ("damage", "penance")
    if is_available("sw_death", settings):
        return ("damage", "sw_death")
    if is_available("smite", settings):
        return ("damage", "smite")
    return ("idle",)


def choose_action(hp_values: list, settings: dict) -> tuple:
    """
    Главный «мозг» ротации Disc Priest (Voidweaver, M+).

    Приоритет:
      OFF-GCD:
        1. Desperate Prayer — свои HP < emergency
        2. Fade — DR когда 2+ раненых

      GCD (экстренно):
        3. Flash Heal — кто-то < emergency (прямой хил)

      GCD (Atonement-фаза после Radiance):
        → урон (Mind Blast > Penance > SW:Death > Smite)

      GCD (нормальный режим):
        4. Radiance — 3+ раненых (AoE Atonement на всех 5)
        5. Shield / Plea — точечный Atonement
        6. Урон — все здоровы, качаем через Atonement
    """
    t = settings["thresholds"]
    self_idx = settings["self_index"]
    self_hp = hp_values[self_idx] if self_idx < len(hp_values) else 100.0

    # ── 1. Desperate Prayer (off-GCD, только на себя) ──
    if self_hp < t["emergency"] and self_hp > t["dead"]:
        if is_available("desperate_prayer", settings):
            return ("off_gcd", "desperate_prayer")

    # ── 2. Fade — DR когда группе плохо (off-GCD) ──
    hurt_count = sum(1 for hp in hp_values
                     if hp < t["moderate"] and hp > t["dead"])
    if hurt_count >= 2 and is_available("fade", settings):
        return ("off_gcd", "fade")

    # Дальше только GCD-способности
    if not gcd_ready():
        return ("idle",)

    # ── 3. Экстренный хил: кто-то при смерти ──
    for i, hp in enumerate(hp_values):
        if hp < t["emergency"] and hp > t["dead"]:
            if is_available("flash_heal", settings):
                return ("heal", i, "flash_heal")
            if is_available("shield", settings):
                return ("heal", i, "shield")
            if is_available("plea", settings):
                return ("heal", i, "plea")

    # ── Atonement-фаза: после Radiance приоритет на урон ──
    if in_atonement_phase(settings):
        return choose_damage_action(settings)

    # ── 4. Radiance: массовый Atonement ──
    members_hurt = [(i, hp) for i, hp in enumerate(hp_values)
                    if hp < t["moderate"] and hp > t["dead"]]
    if len(members_hurt) >= settings.get("group_heal_count", 3):
        if is_available("radiance", settings):
            # Кастуем на самого раненого
            target_i = min(members_hurt, key=lambda x: x[1])[0]
            return ("heal", target_i, "radiance")

    # ── 5. Точечный хил: 1-2 раненых ──
    members_needing = [(i, hp) for i, hp in enumerate(hp_values)
                       if hp < t["healthy"] and hp > t["dead"]]
    if members_needing:
        target_i, target_hp = min(members_needing, key=lambda x: x[1])

        if target_hp < t["critical"]:
            # Срочно: Shield → Flash Heal → Plea
            if is_available("shield", settings):
                return ("heal", target_i, "shield")
            if is_available("flash_heal", settings):
                return ("heal", target_i, "flash_heal")
            if is_available("plea", settings):
                return ("heal", target_i, "plea")
        else:
            # Умеренно: Shield → Plea (экономим ману)
            if is_available("shield", settings):
                return ("heal", target_i, "shield")
            if is_available("plea", settings):
                return ("heal", target_i, "plea")

    # ── 6. Все здоровы → урон через Atonement ──
    return choose_damage_action(settings)


# ──────────────────────────────────────────────────────────────
#  Исполнение действий
# ──────────────────────────────────────────────────────────────

def execute_action(action: tuple, settings: dict) -> None:
    """Выполняет действие: нажимает клавиши через pydirectinput."""
    delay = settings.get("target_switch_delay", 0.05)

    if action[0] == "off_gcd":
        ability_name = action[1]
        ability = settings["abilities"][ability_name]
        press_combo(ability["key"])
        record_ability_use(ability_name, settings)

    elif action[0] == "heal":
        member_idx = action[1]
        ability_name = action[2]
        ability = settings["abilities"][ability_name]
        member = settings["members"][member_idx]

        # 1. Выбираем союзника
        press_combo(member["select_key"])
        time.sleep(delay)

        # 2. Кастуем способность
        press_combo(ability["key"])
        record_ability_use(ability_name, settings)

        # 3. Возвращаемся на врага
        cast_time = ability.get("cast_time", 0.0)
        if cast_time <= 0:
            # Мгновенное: сразу обратно на врага
            time.sleep(delay)
            press_combo(settings["target_enemy_key"])
        else:
            # С кастом: ставим таймер, вернёмся после каста
            state["retarget_at"] = time.time() + cast_time

        # Radiance: запускаем Atonement-фазу
        if ability_name == "radiance":
            state["last_radiance_time"] = time.time()

    elif action[0] == "damage":
        ability_name = action[1]
        ability = settings["abilities"][ability_name]
        press_combo(ability["key"])
        record_ability_use(ability_name, settings)


# ──────────────────────────────────────────────────────────────
#  Логирование действий
# ──────────────────────────────────────────────────────────────

# Названия способностей для консоли (кириллица)
ABILITY_NAMES_RU = {
    "radiance":         "Radiance (AoE Atonement)",
    "penance":          "Penance",
    "shield":           "PW: Shield",
    "flash_heal":       "Flash Heal",
    "plea":             "Plea",
    "mind_blast":       "Mind Blast",
    "smite":            "Smite",
    "sw_death":         "SW: Death",
    "desperate_prayer": "Desperate Prayer",
    "fade":             "Fade",
}


def log_action(action: tuple, hp_values: list) -> None:
    ts = time.strftime("%H:%M:%S")
    hp_str = "|".join(f"{h:3.0f}" for h in hp_values)

    if action[0] == "off_gcd":
        name = ABILITY_NAMES_RU.get(action[1], action[1])
        print(f"  [{ts}] HP:[{hp_str}] >> {name} (off-GCD)")

    elif action[0] == "heal":
        idx = action[1]
        name = ABILITY_NAMES_RU.get(action[2], action[2])
        print(f"  [{ts}] HP:[{hp_str}] >> {name} -> "
              f"#{idx+1} ({hp_values[idx]:.0f}%)")

    elif action[0] == "damage":
        name = ABILITY_NAMES_RU.get(action[1], action[1])
        print(f"  [{ts}] HP:[{hp_str}] >> {name} (урон/Atonement)")


# ──────────────────────────────────────────────────────────────
#  Баннер
# ──────────────────────────────────────────────────────────────

def print_banner(settings: dict) -> None:
    ab = settings["abilities"]
    t = settings["thresholds"]
    print()
    print("=" * 60)
    print("  DISC PRIEST (VOIDWEAVER) ACCESSIBILITY ASSISTANT  v0.2")
    print("=" * 60)
    print()
    print("  Горячие клавиши программы:")
    print("  ----------------------------------------------------------")
    print("  F8              Калибровка HP-баров (3 точки → 5 баров)")
    print("  F9              Пауза / Возобновление")
    print("  F10             Окно отладки (вкл/выкл)")
    print("  Ctrl+Shift+Q    Выход")
    print("  ----------------------------------------------------------")
    print()
    print("  Биндинги способностей (из settings.json):")
    print("  ----------------------------------------------------------")
    print(f"    Radiance       «{ab['radiance']['key']}»    "
          f"CD {ab['radiance']['cooldown']}s")
    print(f"    Penance        «{ab['penance']['key']}»    "
          f"CD {ab['penance']['cooldown']}s")
    print(f"    PW: Shield     «{ab['shield']['key']}»    "
          f"CD {ab['shield']['cooldown']}s")
    print(f"    Flash Heal     «{ab['flash_heal']['key']}»    "
          f"(без КД)")
    print(f"    Plea           «{ab['plea']['key']}»    "
          f"(без КД, инстант)")
    print(f"    Mind Blast     «{ab['mind_blast']['key']}»    "
          f"CD {ab['mind_blast']['cooldown']}s")
    print(f"    Smite          «{ab['smite']['key']}»    "
          f"(филлер)")
    print(f"    SW: Death      «{ab['sw_death']['key']}»    "
          f"CD {ab['sw_death']['cooldown']}s")
    print(f"    Desp. Prayer   «{ab['desperate_prayer']['key']}»  "
          f"CD {ab['desperate_prayer']['cooldown']}s  (off-GCD)")
    print(f"    Fade           «{ab['fade']['key']}»  "
          f"CD {ab['fade']['cooldown']}s  (off-GCD)")
    print("  ----------------------------------------------------------")
    print()
    print(f"  Таргет врага: «{settings['target_enemy_key']}»   "
          f"GCD: {settings['gcd_duration']}s   "
          f"Atonement: {settings.get('atonement_duration', 9)}s")
    print()
    print("  Пороги HP:")
    print(f"    Здоров > {t['healthy']}%  |  "
          f"Ранен < {t['moderate']}%  |  "
          f"Критично < {t['critical']}%  |  "
          f"Экстренно < {t['emergency']}%")
    print()

    if settings["calibrated"]:
        print("  Калибровка: СОХРАНЕНА")
        for i, m in enumerate(settings["members"]):
            r = m["roi"]
            print(f"    #{i+1} [{m['select_key'].upper():>3s}] "
                  f"top={r['top']}, left={r['left']}, "
                  f"{r['width']}x{r['height']}")
        print()
        print("  Нажмите F9 для старта или F8 для повторной калибровки.")
    else:
        print("  [!] Калибровка НЕ пройдена.")
        print("  Нажмите F8, чтобы указать расположение HP-баров.")

    print()
    print("  ВАЖНО: запускайте от имени Администратора, если клавиши")
    print("  не проходят в игру. Убедитесь, что F8-F10 не заняты в WoW.")
    print()
    print("=" * 60)
    print()


# ──────────────────────────────────────────────────────────────
#  Главный цикл
# ──────────────────────────────────────────────────────────────

def main() -> None:
    pydirectinput.PAUSE = 0.02

    settings = load_settings()
    print_banner(settings)

    # Регистрация горячих клавиш (suppress=False — проходят в игру)
    keyboard.add_hotkey("F8", lambda: on_calibrate(settings), suppress=False)
    keyboard.add_hotkey("F9", on_pause, suppress=False)
    keyboard.add_hotkey("F10", on_debug, suppress=False)
    keyboard.add_hotkey("ctrl+shift+q", on_quit, suppress=False)

    if settings["calibrated"]:
        # Начинаем на паузе даже с сохранённой калибровкой —
        # пусть игрок нажмёт F9 когда будет готов.
        print("  >> Помощник: ПАУЗА (нажмите F9 для старта)")
    else:
        print("  >> Помощник: ПАУЗА (ожидание калибровки F8)")
    print()

    with mss.mss() as sct:
        while state["running"]:
            time.sleep(settings.get("scan_interval", 0.1))

            # Пропуск на паузе / во время калибровки
            if state["paused"] or state["calibration_step"] != 0:
                if state["debug_window"]:
                    cv2.waitKey(1)
                continue

            # Отложенный возврат на вражескую цель (после каста)
            if state["retarget_at"] > 0:
                if time.time() >= state["retarget_at"]:
                    press_combo(settings["target_enemy_key"])
                    state["retarget_at"] = 0.0

            # Сканируем HP всей группы
            try:
                hp_values, frames, masks = scan_all_hp(settings, sct)
            except Exception as e:
                print(f"  [!] Ошибка захвата: {e}")
                continue

            # Отладочное окно
            if state["debug_window"]:
                show_debug_window(hp_values, frames, masks)

            # Выбираем и исполняем действие
            action = choose_action(hp_values, settings)
            if action[0] != "idle":
                execute_action(action, settings)
                log_action(action, hp_values)

    # Очистка
    cv2.destroyAllWindows()
    keyboard.unhook_all()
    print("\n  Программа завершена. Удачи в подземельях!\n")


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Прервано (Ctrl+C). Выход.")
        cv2.destroyAllWindows()
