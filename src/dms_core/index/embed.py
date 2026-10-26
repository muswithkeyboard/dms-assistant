"""Энкодер вопросов rubert-tiny2 в ONNX (ADR 0009): векторы для маршрутизации kNN, без torch.

Модель — data/models/rubert-tiny2-onnx/ (вне git; создаётся scripts/export_encoder.py) или DMS_ENCODER_DIR.
Файл модели — model.int8.onnx (квантованные веса, ~28 МБ), если нет — model.onnx. Пулинг — вектор [CLS],
нормализованный (как в исходной модели: 1_Pooling/config.json). Один поток (DMS_ENCODER_THREADS) — сервер 2–4 ГБ.
Нет файлов модели или onnxruntime — available() = False: маршрутизация работает на правилах, сервис не падает.
"""
from __future__ import annotations

import json
import os
import threading
from functools import cache
from pathlib import Path

from ..config import data_dir

MAX_LEN = 128          # вопрос менеджера короче; длиннее — обрезается (на маршрутизацию не влияет)


def encoder_dir() -> Path:
    return Path(os.environ.get("DMS_ENCODER_DIR", "") or data_dir() / "models" / "rubert-tiny2-onnx")


class Encoder:
    def __init__(self, root: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model = root / "model.int8.onnx"
        if not model.is_file():
            model = root / "model.onnx"
        opts = ort.SessionOptions()
        threads = int(os.environ.get("DMS_ENCODER_THREADS", "1"))
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._np = np
        self._sess = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self._tok = Tokenizer.from_file(str(root / "tokenizer.json"))
        self._tok.enable_truncation(MAX_LEN)
        self._tok.enable_padding()
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self._lock = threading.Lock()          # сессия потокобезопасна, но держим один расчёт за раз (память)
        self.model_file = model.name

    def encode(self, texts: list[str]):
        """Нормализованные векторы [CLS] (numpy, float32, форма n × 312)."""
        np = self._np
        enc = self._tok.encode_batch(list(texts))
        feeds = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                 "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
                 "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64)}
        with self._lock:
            out = self._sess.run(None, {k: v for k, v in feeds.items() if k in self._inputs})[0]
        cls = out[:, 0, :]
        return cls / np.maximum(np.linalg.norm(cls, axis=1, keepdims=True), 1e-12)


@cache
def get_encoder() -> Encoder | None:
    root = encoder_dir()
    if not (root / "tokenizer.json").is_file():
        return None
    try:
        return Encoder(root)
    except (ImportError, OSError, RuntimeError):     # нет onnxruntime / битый файл — работаем на правилах
        return None


def available() -> bool:
    return get_encoder() is not None


def meta() -> dict:
    p = encoder_dir() / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
