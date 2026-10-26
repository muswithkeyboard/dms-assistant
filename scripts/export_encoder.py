"""Однократный экспорт энкодера rubert-tiny2 (cointegrated/rubert-tiny2, MIT) в ONNX + квантование int8.

Сервису torch не нужен: в рантайме — только onnxruntime и tokenizers (ADR 0009, решение пользователя 24.10.2026).
Экспорт делается во временном окружении:

    uv run --with torch --with transformers --with onnx python scripts/export_encoder.py

Результат — data/models/rubert-tiny2-onnx/ (вне git): model.onnx (fp32), model.int8.onnx (динамическое
квантование весов), tokenizer.json, meta.json. Пулинг модели — вектор [CLS] (1_Pooling/config.json), размерность 312.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dms_core.config import data_dir

MODEL = "cointegrated/rubert-tiny2"


def main() -> int:
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModel, AutoTokenizer

    out = data_dir() / "models" / "rubert-tiny2-onnx"
    out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModel.from_pretrained(MODEL).eval()
    sample = tok(["пример вопроса менеджера"], return_tensors="pt")
    fp32 = out / "model.onnx"
    class Encoder(torch.nn.Module):
        """Явные входы: в новых transformers позиционные аргументы forward не совпадают с порядком экспорта."""

        def __init__(self, m: torch.nn.Module) -> None:
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.m(input_ids=input_ids, attention_mask=attention_mask,
                          token_type_ids=token_type_ids).last_hidden_state

    with torch.no_grad():
        torch.onnx.export(
            Encoder(model), (sample["input_ids"], sample["attention_mask"], sample["token_type_ids"]), str(fp32),
            input_names=["input_ids", "attention_mask", "token_type_ids"], output_names=["last_hidden_state"],
            dynamic_axes={n: {0: "batch", 1: "seq"} for n in ("input_ids", "attention_mask", "token_type_ids")}
            | {"last_hidden_state": {0: "batch", 1: "seq"}},
            opset_version=17, dynamo=False,   # классический экспортёр: стабилен для BERT, без onnxscript
        )
    quantize_dynamic(str(fp32), str(out / "model.int8.onnx"), weight_type=QuantType.QInt8)
    tok.backend_tokenizer.save(str(out / "tokenizer.json"))
    # Эталонные векторы для проверки экспорта в тестах (без torch): косинус ONNX vs исходной модели
    texts = ["Входит ли МРТ в программу?", "Добавить клинику в программу"]
    with torch.no_grad():
        enc = tok(texts, return_tensors="pt", padding=True)
        ref = torch.nn.functional.normalize(model(**enc).last_hidden_state[:, 0], dim=-1).tolist()
    (out / "meta.json").write_text(json.dumps({"model": MODEL, "pooling": "cls", "dim": 312, "max_len": 128,
                                               "ref_texts": texts, "ref_vectors": ref}, ensure_ascii=False),
                                   encoding="utf-8")
    for f in sorted(out.iterdir()):
        print(f"{f.name}: {f.stat().st_size / 2**20:.1f} МБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
