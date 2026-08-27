"""Распознавание речи через faster-whisper.

Модель тяжёлая (medium ~1.5 ГБ в int8), поэтому грузится лениво и один раз:
первый голосовой после старта бота обрабатывается заметно дольше, дальше быстро.
OGG/Opus из Telegram декодируется через PyAV, системный ffmpeg не нужен.
"""
import io
import logging

import config

log = logging.getLogger(__name__)

_model = None


class SttUnavailable(Exception):
    """Голосовой ввод выключен или модель не поднялась."""


def enabled() -> bool:
    return bool(config.VOICE_ENABLED)


def _load():
    """Единственная точка загрузки модели. Держим её в памяти процесса."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # импорт здесь: тянет торч-подобные зависимости

        log.info("Загружаю модель Whisper %s (%s)…",
                 config.WHISPER_MODEL, config.WHISPER_COMPUTE)
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu",
                              compute_type=config.WHISPER_COMPUTE)
        log.info("Модель Whisper загружена")
    return _model


def transcribe(audio_bytes: bytes) -> str:
    """Голосовое сообщение → текст. Пустая строка, если расслышать нечего."""
    if not config.VOICE_ENABLED:
        raise SttUnavailable("голосовой ввод выключен")
    if not audio_bytes:
        return ""
    try:
        model = _load()
    except SttUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise SttUnavailable(f"модель не загрузилась: {e}") from e

    segments, _info = model.transcribe(
        io.BytesIO(audio_bytes),
        language=config.WHISPER_LANGUAGE,
        vad_filter=True,          # отсекаем тишину по краям
    )
    return " ".join((s.text or "").strip() for s in segments).strip()
