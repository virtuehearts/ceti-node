"""Loop detection for generated prose — the 3B model's failure mode on
long generations is to repeat a sentence/paragraph until max_tokens,
instead of stopping. Word counts then lie (3,859 words, only 64 unique
sentences in one test part).

dedup_truncate() cuts prose at the first loop, before it is saved:
- same paragraph twice in a row -> cut before the 2nd copy
- same paragraph 3x total -> cut
- same sentence 3x in a row -> cut before the 2nd copy
- same sentence 6x total -> cut before it loops further

This runs on every generated block/part, after the MEMORY footer is
stripped, before word-count / save / index. A truncated part is then
expanded once from its clean tail (not from the looped tail) if it is
still under 70% of target — expansion is also dedup-checked.
"""
import re


def dedup_truncate(text: str, max_consecutive: int = 2) -> str:
    paras = re.split(r"(\n\s*\n)", text)
    out_paras = []
    seen_paras = {}
    for i in range(0, len(paras), 2):
        para = paras[i]
        sep = paras[i + 1] if i + 1 < len(paras) else ""
        key = para.strip()
        if key and len(key) > 40:
            seen_paras[key] = seen_paras.get(key, 0) + 1
            if seen_paras[key] >= 2 and out_paras and out_paras[-1].strip() == key:
                break
            if seen_paras[key] >= 3:
                break
        out_paras.append(para)
        if sep and i + 2 < len(paras):
            out_paras.append(sep)
    text2 = "".join(out_paras)
    parts = re.split(r"(?<=[.!?])\s+", text2)
    out = []
    run = 0
    last = None
    counts = {}
    for s in parts:
        key = s.strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        if key == last:
            run += 1
            if run >= max_consecutive + 1:
                break
        else:
            run = 1
            last = key
        if counts[key] >= 6:
            break
        out.append(s)
    return " ".join(out).strip()
