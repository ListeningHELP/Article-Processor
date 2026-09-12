# -*- coding: utf-8 -*-
"""
AI 工作助手 - 本地服务
模块：写作助手 / 漫画·小说翻译
技术：Python 标准库（零第三方依赖），DeepSeek API 流式接入
运行：python server.py  →  浏览器打开 http://127.0.0.1:8234
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import urllib.error
import urllib.request
from email.parser import BytesParser
from email.policy import default as email_default_policy
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, parse_qs

# pythonw 无控制台模式下 stdout/stderr 为 None，会导致请求日志崩溃；重定向到空设备
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOOL_TMP_DIR = os.path.join(BASE_DIR, "tmp_tool")
PORT = 8234

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
SUPPORTED_MODELS = ["deepseek-chat", "deepseek-reasoner"]
DEFAULT_CONFIG = {
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.7,
}

# ---------------- 配置 ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# ---------------- 系统提示词 ----------------

SYSTEM_PROMPTS = {
    "writing": (
        "你是一名资深中文写作助手，擅长撰写正式报告、项目方案、商业计划书、工作总结、"
        "会议纪要、演讲稿等各类职场与商务文档。\n"
        "写作要求：\n"
        "1. 结构清晰，逻辑严谨，层次分明（善用标题、小标题、要点罗列）。\n"
        "2. 语言专业、准确、精炼，符合中文正式文体习惯，避免口水话。\n"
        "3. 涉及数据、事实时标注为待补充，不要编造。\n"
        "4. 除非用户要求，默认输出简体中文。\n"
        "5. 直接给出成品内容，不要解释你做了什么。"
    ),
}

# 翻译专用系统提示
TRANSLATE_STYLES = {
    "academic": "学术风格：用语正式严谨，术语准确规范，句式清晰、逻辑严密，避免口语化与抒情化表达，适合论文、研究报告、教材等学术类文本。",
    "novel": "通俗小说风格：语言流畅自然、贴近现代通俗小说的阅读习惯，叙述生动但不做作，对话符合人物身份，避免翻译腔与生硬书面语。",
}

LANG_NAMES = {
    "auto": "自动识别",
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
}


def build_translate_system(source, target, style, glossary):
    src = LANG_NAMES.get(source, source or "原语言")
    tgt = LANG_NAMES.get(target, target or "目标语言")
    style_rule = TRANSLATE_STYLES.get(style, TRANSLATE_STYLES["novel"])
    parts = [
        "你是一名专业翻译，擅长学术文本与小说文本翻译。",
        f"翻译方向：{src} → {tgt}。",
        "翻译要求：",
        "1. 忠实原文内容与信息，不增删剧情、不改变人物关系。",
        "2. 人名、地名、专有名词全篇保持一致。",
        f"3. 风格：{style_rule}",
        "4. 只输出译文本身，不要输出任何解释、注释或额外说明。",
        "5. 译文分段结构与原文保持一致。",
    ]
    if glossary and glossary.strip():
        parts.append("以下术语表为硬性规定，翻译时务必严格套用，不要自行更改：")
        parts.append(glossary.strip())
    return "\n".join(parts)


# ---------------- DeepSeek 调用 ----------------

def call_deepseek_stream(cfg, messages, temperature=None, max_tokens=None):
    """调用 DeepSeek API（流式）。返回一个可逐行 readline 的响应对象。"""
    body = {"model": cfg["model"], "messages": messages, "stream": True}
    if cfg["model"] == "deepseek-chat":
        body["temperature"] = float(temperature if temperature is not None else cfg.get("temperature", 0.7))
        body["max_tokens"] = int(max_tokens or 4096)
    else:
        # deepseek-reasoner 不支持 temperature / max_tokens 等参数
        body["max_tokens"] = int(max_tokens or 8192)
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=300)


def extract_stream_delta(line):
    """解析 DeepSeek 流式返回的 data 行，返回增量文本；结束返回 None。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


def stream_translate_chunk(cfg, api_messages, retries=1):
    """翻译单块：失败自动重试（限流/5xx 退避），返回 (ok, text, err, fatal)。

    fatal=True 表示 API Key 无效 / 余额不足等不可恢复错误，调用方应立即停止；
    其余失败在重试耗尽后返回 ok=False，由调用方决定跳过并继续后续分块。
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            resp = call_deepseek_stream(cfg, api_messages, temperature=0.4, max_tokens=6000)
            parts = []
            for line in resp:
                delta = extract_stream_delta(line.decode("utf-8", errors="replace"))
                if delta:
                    parts.append(delta)
            return True, "".join(parts).strip(), "", False
        except urllib.error.HTTPError as e:
            last_err = "HTTP %d" % e.code
            if e.code in (401, 403, 402):
                return False, "", last_err, True
            if attempt < retries:
                time.sleep(2 + attempt * 3)
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(2 + attempt * 3)
    return False, "", last_err, False


# ---------------- 翻译分块引擎 ----------------

def split_long_para(para, max_chunk):
    """把超长段落按句子切块。"""
    pieces = re.split(r"(?<=[。！？!?；;])", para)
    chunks, cur = [], ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(p), max_chunk):
                chunks.append(p[i:i + max_chunk])
        elif len(cur) + len(p) <= max_chunk:
            cur += p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def split_text(text, max_chunk=3200):
    """按空行段落切分长文本，保证每块不超过 max_chunk 字符。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    paras = [p for p in paras if p]
    chunks = []
    cur = ""
    for p in paras:
        if len(p) > max_chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(split_long_para(p, max_chunk))
        elif len(cur) + len(p) + 2 <= max_chunk:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def build_context(prev_translations, max_len=700):
    """拼接上文参考译文（最近几块），用于保证前后文一致。"""
    if not prev_translations:
        return ""
    joined = "\n\n".join(prev_translations[-3:])
    if len(joined) > max_len:
        joined = joined[-max_len:]
    return f"【上文参考译文】\n{joined}\n\n"


def looks_chinese(text, sample_len=2000):
    """粗略检测原文是否以中文为主。"""
    sample = text[:sample_len]
    if not sample:
        return False
    han = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return han / max(len(sample), 1) > 0.3


# ---------------- 文档解析引擎（TXT / EPUB / MOBI，标准库实现） ----------------

MAX_DOC_BYTES = 20 * 1024 * 1024          # 上传文件上限 20MB
MAX_TEXT_CHARS = 2_000_000                # 解析出的文本上限（约 200 万字）
DOC_EXTENSIONS = (".txt", ".epub", ".mobi")


class _TextExtractor(HTMLParser):
    """提取 HTML/XHTML 纯文本，保留段落结构。"""

    _BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "section"}
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self.skip += 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self.skip > 0:
            self.skip -= 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip == 0:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        lines = [ln.strip() for ln in re.split(r"\n+", raw)]
        return "\n\n".join(ln for ln in lines if ln)


def _html_to_text(html):
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.text()


def _decode_bytes_text(raw, is_binary_ok=False):
    """按常见编码依次尝试解码字节为文本。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text_txt(raw):
    """TXT：直接读取文本。"""
    return _decode_bytes_text(raw)


def extract_text_epub(raw):
    """EPUB：zip 解包，按 spine 顺序提取各章节 HTML 文本。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("EPUB 文件损坏或不是有效的压缩包。")
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
    except KeyError:
        raise ValueError("EPUB 缺少 META-INF/container.xml，文件可能不完整。")
    m = re.search(r'full-path\s*=\s*"([^"]+)"', container)
    if not m:
        raise ValueError("EPUB 的 container.xml 中未找到 OPF 路径。")
    opf_path = m.group(1)
    try:
        opf = zf.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        raise ValueError("EPUB 中未找到 OPF 清单文件：%s" % opf_path)
    spine_ids = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    items = {}
    for tag in re.finditer(r"<item\b[^>]*>", opf):
        t = tag.group(0)
        i = re.search(r'id="([^"]+)"', t)
        h = re.search(r'href="([^"]+)"', t)
        if i and h:
            items[i.group(1)] = h.group(1)
    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    texts = []
    for sid in spine_ids:
        href = items.get(sid)
        if not href:
            continue
        full = (base + "/" + href) if base else href
        try:
            raw = zf.read(full)
        except KeyError:
            full = href.replace("../", "").lstrip("/")
            try:
                raw = zf.read(full)
            except KeyError:
                continue
        html = _decode_bytes_text(raw)
        texts.append(_html_to_text(html))
    if not texts:
        raise ValueError("EPUB 中没有可解析的正文章节。")
    return "\n\n".join(texts)


def _palmdoc_decompress(data):
    """PalmDOC LZ77 变体解压，返回原始字节。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x00:
            i += 1
        elif b == 0x01:
            i += 2
            if i + 1 > n:
                break
            c = data[i - 1]
            out.extend(data[i:i + c])
            i += c
        elif b == 0x02:
            i += 2
            if i > n:
                break
            c = data[i - 1]
            out.extend(b"\x00" * c)
        elif b == 0x03:
            i += 1
            out.extend(data[i:i + 8])
            i += 8
        elif 0x04 <= b <= 0x08:
            i += 1
            out.extend(data[i:i + b])
            i += b
        elif b == 0x09:
            i += 2
            if i > n:
                break
            c = data[i - 1]
            out.extend(b"\x00" * c)
        elif 0x0A <= b <= 0x7F:
            i += 1
            out.append(b)
        elif 0x80 <= b <= 0xBF:
            i += 1
            if i >= n:
                break
            nb = data[i]
            i += 1
            offset = ((b & 0x3F) << 5) | (nb >> 3)
            length = (nb & 0x07) + 3
            if offset == 0 or offset > len(out):
                break
            for _ in range(length):
                out.append(out[-offset])
        else:  # 0xC0 - 0xFF
            i += 1
            if i + 1 >= n:
                break
            offset = ((b & 0x3F) << 8) | data[i]
            length = data[i + 1] + 3
            i += 2
            if offset == 0 or offset > len(out):
                break
            for _ in range(length):
                out.append(out[-offset])
    return bytes(out)


def extract_text_mobi(raw):
    """MOBI：PalmDB + PalmDOC 解压，提取正文文本（仅支持未压缩 / PalmDOC LZ77 压缩）。"""
    if len(raw) < 78:
        raise ValueError("MOBI 文件过小，不是有效的 PalmDB 数据库。")
    nrec = int.from_bytes(raw[76:78], "big")
    if nrec < 2:
        raise ValueError("MOBI 记录数异常。")
    rec_offsets = []
    for i in range(nrec):
        off = 78 + i * 8
        if off + 4 > len(raw):
            break
        rec_offsets.append(int.from_bytes(raw[off:off + 4], "big"))
    if len(rec_offsets) < 2:
        raise ValueError("MOBI 记录索引损坏。")
    rec0 = raw[rec_offsets[0]:rec_offsets[1] if len(rec_offsets) > 1 else len(raw)]
    if len(rec0) < 16:
        raise ValueError("MOBI 首条记录过小。")
    compression = int.from_bytes(rec0[0:2], "big")
    text_len = int.from_bytes(rec0[4:8], "big")
    if compression not in (0, 1, 2):
        # 2 = huff/cdic 压缩，需专用库；提示转换
        raise ValueError("该 MOBI 使用 huffdic 压缩（不支持），请先用 Calibre 等工具转成 EPUB 再导入。")
    start = 16
    if rec0[16:24] == b"BOOKMOBI":
        mobi_len = int.from_bytes(rec0[32:36], "big")
        start = 16 + mobi_len
    body = bytearray()
    for idx, off in enumerate(rec_offsets):
        end = rec_offsets[idx + 1] if idx + 1 < len(rec_offsets) else len(raw)
        rec = raw[off:end]
        if idx == 0:
            rec = rec[start:]
        if not rec:
            continue
        if compression == 1:
            body += _palmdoc_decompress(rec)
        else:
            body += rec
    text = bytes(body)[:text_len] if text_len else bytes(body)
    text_str = _decode_bytes_text(text)
    text_str = text_str.replace("\x00", "\n").replace("\r", "\n")
    cleaned = _html_to_text(text_str)
    if not cleaned.strip():
        raise ValueError("MOBI 正文解析为空（可能为纯图片版，请转 EPUB 或提供 TXT）。")
    return cleaned


def extract_document_text(filename, raw):
    """按扩展名分派解析。返回 (文本, 扩展名)。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".txt":
        return extract_text_txt(raw), ext
    if ext == ".epub":
        return extract_text_epub(raw), ext
    if ext == ".mobi":
        return extract_text_mobi(raw), ext
    raise ValueError("不支持的文件格式：%s（支持 txt / epub / mobi）" % (ext or "未知"))


# ---------------- 章节编辑引擎（EasyPub 风格） ----------------

# 预设章节拆分规则（与 EasyPub 默认正则一致）
CHAPTER_PRESET_PATTERNS = [
    {"name": "中文章节（第X章/回/卷…）", "pattern": r"^\s*[第卷][0-9一二三四五六七八九十百千万零〇两]*[章回部节集卷].*"},
    {"name": "英文章节（Chapter N）", "pattern": r"^\s*[Cc]hapter\s*[0-9]+.*"},
    {"name": "卷 / Part / Vol", "pattern": r"^\s*(Part|Vol(ume)?|卷)\s*[0-9一二三四五六七八九十]+.*", "ignore_case": True},
    {"name": "序 / 前言 / 后记 / 附录", "pattern": r"^\s*(简介|序言|序[0-9一二三]?|序曲|引子|楔子|前言|自序|后记|尾声|附录[一二三四五]?|番外[一二三四五]?).*"},
]
DEFAULT_SPLIT_LENGTH = 130  # 按长度均分时的目标章节长度（千字），与 EasyPub 默认一致


def _norm_title(raw, fallback):
    t = re.sub(r"\s+", " ", (raw or "")).strip()
    return t[:80] or fallback


def split_text_by_regex(text, pattern, ignore_case=False):
    """按正则逐行匹配拆分章节：匹配行作为新章节标题。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    chapters = []
    cur_title = None
    cur_lines = []
    rx = re.compile(pattern, re.I if ignore_case else 0)
    for line in lines:
        if rx.match(line):
            if cur_title is not None:
                chapters.append({"title": cur_title, "text": "\n".join(cur_lines).strip()})
            cur_title = _norm_title(line, "第 %d 章" % (len(chapters) + 1))
            cur_lines = [line]
        else:
            if cur_title is None:
                cur_title = "正文"
            cur_lines.append(line)
    if cur_title is not None:
        chapters.append({"title": cur_title, "text": "\n".join(cur_lines).strip()})
    if not chapters:
        chapters = [{"title": "正文", "text": text.strip()}]
    return [c for c in chapters if c["text"].strip()] or [{"title": "正文", "text": ""}]


def _split_long_para(p, chars_per):
    """把超过目标长度的段落按句末标点切分为多段。"""
    pieces = []
    buf = ""
    for ch in p:
        buf += ch
        if ch in "。！？；…" and len(buf) >= chars_per:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    # 把过短的残片合并到前一片，避免出现碎片章
    merged = []
    for piece in pieces:
        if merged and len(piece) < chars_per * 0.5:
            merged[-1] += piece
        else:
            merged.append(piece)
    # 兜底：明显超长的片才按字数硬切
    final = []
    for piece in merged:
        if len(piece) <= chars_per * 1.2:
            final.append(piece)
        else:
            for i in range(0, len(piece), chars_per):
                final.append(piece[i:i + chars_per])
    return final


def split_text_by_length(text, length_k=DEFAULT_SPLIT_LENGTH):
    """按长度均分章节（length_k 为每章目标千字数），尽量按段落切分。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [{"title": "正文", "text": ""}]
    chars_per = max(1000, int(length_k or DEFAULT_SPLIT_LENGTH) * 1000)
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, cur, cur_len = [], [], 0
    for p in paras:
        if len(p) > chars_per:
            if cur:
                chunks.append("\n\n".join(cur))
                cur, cur_len = [], 0
            chunks.extend(_split_long_para(p, chars_per))
            continue
        if cur and cur_len + len(p) > chars_per:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        chunks.append("\n\n".join(cur))
    if not chunks:
        chunks = [text]
    return [{"title": "第 %d 章" % (i + 1), "text": c} for i, c in enumerate(chunks)]


def _epub_title_from_html(html):
    """从章节 HTML 提取标题（第一个 h1-h3，其次 <title>）。"""
    for m in re.finditer(r"<h[123][^>]*>(.*?)</h[123]>", html, re.I | re.S):
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if t:
            return t
    return ""


def extract_chapters_epub(raw):
    """EPUB：按 spine 顺序返回章节 [{title, text}]，标题尽量取自目录/标题标签。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("EPUB 文件损坏或不是有效的压缩包。")
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
    except KeyError:
        raise ValueError("EPUB 缺少 META-INF/container.xml。")
    m = re.search(r'full-path\s*=\s*"([^"]+)"', container)
    if not m:
        raise ValueError("EPUB 的 container.xml 未找到 OPF 路径。")
    opf_path = m.group(1)
    try:
        opf = zf.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        raise ValueError("EPUB 中未找到 OPF 文件。")
    spine_ids = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    items = {}
    for tag in re.finditer(r"<item\b[^>]*>", opf):
        t = tag.group(0)
        i = re.search(r'id="([^"]+)"', t)
        h = re.search(r'href="([^"]+)"', t)
        if i and h:
            items[i.group(1)] = h.group(1)
    # 目录标题映射：从 nav.xhtml / toc.ncx 提取 href -> 标题
    title_map = {}
    for name in zf.namelist():
        low = name.lower()
        if low.endswith("nav.xhtml") or "nav" in low.split("/")[-1] or low.endswith("toc.ncx"):
            try:
                content = zf.read(name).decode("utf-8", errors="replace")
                for am in re.finditer(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", content, re.I | re.S):
                    href = am.group(1).split("#")[0]
                    t = re.sub(r"<[^>]+>", "", am.group(2))
                    t = re.sub(r"\s+", " ", t).strip()
                    if href and t:
                        title_map.setdefault(href, t)
                for cm in re.finditer(
                        r"<navPoint[^>]*>.*?<content[^>]*src=[\"']([^\"']+)[\"'].*?</text>(.*?)</navPoint>",
                        content, re.I | re.S):
                    href = cm.group(1).split("#")[0]
                    t = re.sub(r"<[^>]+>", "", cm.group(2)).strip()
                    if href and t:
                        title_map.setdefault(href, t)
            except Exception:
                continue
    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    chapters = []
    for sid in spine_ids:
        href = items.get(sid)
        if not href:
            continue
        full = (base + "/" + href) if base else href
        try:
            raw_html = zf.read(full)
        except KeyError:
            full2 = href.replace("../", "").lstrip("/")
            try:
                raw_html = zf.read(full2)
            except KeyError:
                continue
        html = _decode_bytes_text(raw_html)
        text = _html_to_text(html).strip()
        title = (title_map.get(href) or title_map.get(href.split("/")[-1])
                 or _epub_title_from_html(html))
        chapters.append({
            "title": _norm_title(title, "第 %d 章" % (len(chapters) + 1)),
            "text": text,
        })
    if not chapters:
        raise ValueError("EPUB 中没有可解析的正文章节。")
    return chapters


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _chapter_to_xhtml(title, text):
    paras = []
    for p in re.split(r"\n{2,}", (text or "").replace("\r\n", "\n").replace("\r", "\n")):
        p = p.strip()
        if not p:
            continue
        paras.append("    <p>%s</p>" % _xml_escape(p).replace("\n", "<br/>"))
    if not paras:
        paras.append("    <p></p>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        '<meta charset="utf-8"/>\n<title>%s</title>\n'
        '<style>body{font-family:serif;line-height:1.6;margin:5%% 6%%;}\n'
        'h1{font-size:1.4em;text-align:center;}\np{text-indent:2em;margin:.6em 0;}</style>\n'
        '</head>\n<body>\n<h1>%s</h1>\n%s\n</body>\n</html>\n'
    ) % (_xml_escape(title), _xml_escape(title), "\n".join(paras))


def build_epub(title, author, chapters):
    """生成标准 EPUB3 字节流（含目录 TOC）。chapters: [{title, text}]"""
    import datetime as _dt
    import uuid as _uuid
    book_id = "urn:uuid:" + str(_uuid.uuid4())
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = len(chapters)
    fnames = ["chap%04d.xhtml" % (i + 1) for i in range(n)]
    manifest_items = "".join(
        '    <item id="c%d" href="%s" media-type="application/xhtml+xml"/>\n' % (i + 1, fname)
        for i, fname in enumerate(fnames))
    spine_refs = "".join('    <itemref idref="c%d"/>\n' % (i + 1) for i in range(n))
    toc_items = "".join(
        '<li><a href="%s">%s</a></li>\n' % (fname, _xml_escape(chapters[i]["title"] or ("第 %d 章" % (i + 1))))
        for i, fname in enumerate(fnames))
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="uid">%s</dc:identifier>\n'
        '    <dc:title>%s</dc:title>\n'
        '    <dc:creator>%s</dc:creator>\n'
        '    <dc:language>zh</dc:language>\n'
        '    <meta property="dcterms:modified">%s</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '%s'
        '  </manifest>\n'
        '  <spine>\n%s  </spine>\n'
        '</package>\n'
    ) % (_xml_escape(book_id), _xml_escape(title or "未命名"), _xml_escape(author or "佚名"),
         now, manifest_items, spine_refs)
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><meta charset="utf-8"/><title>目录</title></head>\n'
        '<body>\n<nav epub:type="toc"><h1>目录</h1>\n<ol>\n%s</ol></nav>\n</body>\n</html>\n'
    ) % toc_items
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        for i, fname in enumerate(fnames):
            zf.writestr("OEBPS/" + fname,
                        _chapter_to_xhtml(chapters[i]["title"] or ("第 %d 章" % (i + 1)), chapters[i]["text"]))
    return buf.getvalue()


def build_txt(chapters):
    """拼接章节为纯文本。"""
    parts = []
    for i, c in enumerate(chapters, 1):
        parts.append(c.get("title") or ("第 %d 章" % i))
        parts.append((c.get("text") or "").strip())
        parts.append("")
    return ("\n".join(parts)).encode("utf-8")


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AIWorkAssistant/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    # ---------- 工具方法 ----------
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, obj):
        """按 chunked 编码发送一条 SSE 事件。"""
        data = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        payload = data.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(payload)).encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        """发送 chunked 结束标记。"""
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _sse_error(self, message):
        """发送错误事件并结束流。"""
        self._sse({"type": "error", "message": message})
        self._sse_end()

    def _send_file_bytes(self, data, filename, content_type):
        """以附件形式返回二进制文件（支持中文文件名）。"""
        ascii_name = filename.encode("ascii", "ignore").decode() or "book"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(filename)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_multipart(self, ctype):
        """解析 multipart/form-data 上传。返回 (fields dict, files [(name, bytes)])。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0:
            return {}, []
        raw = self.rfile.read(length)
        if len(raw) > MAX_DOC_BYTES:
            return {}, []
        # email 模块解析需要消息头（含 Content-Type 的 boundary），补上头再解析
        head = ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % ctype).encode("utf-8")
        msg = BytesParser(policy=email_default_policy).parsebytes(head + raw)
        fields, files = {}, []
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            fn = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if fn:
                files.append((fn, payload))
            elif name:
                try:
                    fields[name] = part.get_content()
                except Exception:
                    fields[name] = ""
        return fields, files

    def _get_cfg(self):
        cfg = load_config()
        if not cfg.get("api_key"):
            self._sse_error("尚未配置 API Key，请点击右上角「设置」填入 DeepSeek API Key 后重试。")
            return None
        return cfg

    def _stream_out_chunk(self, text):
        """把一整块译文分小段以 SSE delta 输出，模拟流式展示。"""
        for j in range(0, len(text), 80):
            self._sse({"type": "delta", "text": text[j:j + 80]})

    def _run_chunked_translate(self, cfg, chunks, system):
        """逐块翻译（自动重试；单块失败跳过不中断）。

        返回 (full_text, failed_count)；full_text 为 None 表示遇到不可恢复错误
        （API Key 无效 / 余额不足），错误信息已通过 SSE 发出。
        """
        prev_translations, full_text = [], []
        failed = 0
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            self._sse({"type": "progress", "done": i, "total": total})
            context = build_context(prev_translations)
            api_messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": context + f"【请翻译以下内容】\n{chunk}"},
            ]
            ok, text, err, fatal = stream_translate_chunk(cfg, api_messages)
            if fatal:
                self._sse_error("翻译失败（不可恢复）：%s" % err)
                return None, failed
            if not ok:
                failed += 1
                self._sse({"type": "warn", "text": "第 %d/%d 块翻译失败已跳过：%s" % (i, total, err)})
                continue
            if text:
                self._stream_out_chunk(text)
                prev_translations.append(text)
                full_text.append(text)
        return full_text, failed

    # ---------- 文本工具（合并 TXT / 网页转 TXT / 批量转 PDF） ----------
    def _tool_ensure_tmp(self):
        try:
            os.makedirs(TOOL_TMP_DIR, exist_ok=True)
        except Exception:
            pass

    def _tool_save_result(self, name, data):
        """把转换结果保存到临时目录，返回可下载的相对文件名。"""
        self._tool_ensure_tmp()
        base = re.sub(r"[\\/:*?\"<>|]", "_", os.path.basename(name))
        path = os.path.join(TOOL_TMP_DIR, "%d_%s" % (int(time.time() * 1000), base))
        with open(path, "wb") as f:
            f.write(data)
        return os.path.basename(path)

    def _handle_tool_merge(self, ctype):
        """合并多个 TXT 为一个文件（按上传顺序）。"""
        if "multipart/form-data" not in ctype:
            self._send_json({"ok": False, "error": "需要 multipart/form-data。"}, 400)
            return
        fields, files = self._read_multipart(ctype)
        if not files:
            self._send_json({"ok": False, "error": "没有收到文件。"}, 400)
            return
        parts = []
        for name, raw in files:
            text = _decode_bytes_text(raw)
            if text.strip():
                parts.append(text.rstrip())
        merged = "\n\n".join(parts)
        if not merged:
            self._send_json({"ok": False, "error": "文件中没有可合并的文本内容。"}, 400)
            return
        out_name = "合并_%s" % os.path.basename(files[0][0])
        if not out_name.lower().endswith(".txt"):
            out_name += ".txt"
        self._send_file_bytes(merged.encode("utf-8"), out_name, "text/plain; charset=utf-8")

    def _handle_tool_html2txt(self, ctype):
        """批量把 HTM/HTML 转 TXT，SSE 逐文件报告进度。"""
        self._send_sse_headers()
        if "multipart/form-data" not in ctype:
            self._sse_error("需要 multipart/form-data 上传文件。")
            return
        fields, files = self._read_multipart(ctype)
        if not files:
            self._sse_error("没有收到文件，请选择 HTM / HTML 文件或文件夹。")
            return
        try:
            total = len(files)
            for i, (name, raw) in enumerate(files, 1):
                self._sse({"type": "progress", "done": i, "total": total, "name": name})
                try:
                    text = _html_to_text(_decode_bytes_text(raw, is_binary_ok=True))
                except Exception as e:
                    self._sse({"type": "file_error", "name": name, "message": str(e)})
                    continue
                text = text.strip()
                if not text:
                    self._sse({"type": "file_error", "name": name, "message": "未提取到文本"})
                    continue
                out_name = re.sub(r"\.(htm|html|shtml)$", ".txt", name, flags=re.I) or (name + ".txt")
                key = self._tool_save_result(out_name, text.encode("utf-8"))
                self._sse({"type": "file_done", "name": out_name, "src": name, "key": key,
                           "size": len(text.encode("utf-8"))})
            self._sse({"type": "done"})
            self._sse_end()
        except Exception as e:
            self._sse_error("网页转文本失败：" + str(e))

    def _handle_tool_txt2pdf(self, ctype):
        """批量把 TXT 转 PDF（调用本机 Word），SSE 逐文件报告进度。"""
        self._send_sse_headers()
        if "multipart/form-data" not in ctype:
            self._sse_error("需要 multipart/form-data 上传文件。")
            return
        fields, files = self._read_multipart(ctype)
        if not files:
            self._sse_error("没有收到文件，请选择 TXT 文件或文件夹。")
            return
        try:
            total = len(files)
            for i, (name, raw) in enumerate(files, 1):
                self._sse({"type": "progress", "done": i, "total": total, "name": name})
                try:
                    pdf_bytes, err = self._txt_to_pdf_bytes(name, raw)
                except Exception as e:
                    self._sse({"type": "file_error", "name": name, "message": str(e)})
                    continue
                if pdf_bytes is None:
                    self._sse({"type": "file_error", "name": name, "message": err or "转换失败"})
                    continue
                out_name = re.sub(r"\.txt$", ".pdf", name, flags=re.I) or (name + ".pdf")
                key = self._tool_save_result(out_name, pdf_bytes)
                self._sse({"type": "file_done", "name": out_name, "src": name, "key": key, "size": len(pdf_bytes)})
            self._sse({"type": "done"})
            self._sse_end()
        except Exception as e:
            self._sse_error("批量转 PDF 失败：" + str(e))

    def _txt_to_pdf_bytes(self, name, raw):
        """通过 PowerShell + Word COM 把 txt 转成 PDF，返回 (pdf_bytes, err)。"""
        self._tool_ensure_tmp()
        tmp = os.path.join(TOOL_TMP_DIR, "_w_%d.txt" % int(time.time() * 1000))
        pdf = os.path.join(TOOL_TMP_DIR, "_w_%d.pdf" % int(time.time() * 1000))
        try:
            text = _decode_bytes_text(raw)
            with open(tmp, "w", encoding="utf-8-sig", errors="replace") as f:
                f.write(text)
            ps = os.path.join(BASE_DIR, "txt2pdf.ps1")
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", ps, tmp, pdf],
                capture_output=True, timeout=90, creationflags=0x08000000)
            if not os.path.exists(pdf) or os.path.getsize(pdf) == 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip() or "Word 转换失败"
                return None, err
            with open(pdf, "rb") as f:
                data = f.read()
            return data, None
        except subprocess.TimeoutExpired:
            return None, "转换超时（文件过大或 Word 未响应）"
        except Exception as e:
            return None, str(e)
        finally:
            for p in (tmp, pdf):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def _handle_tool_download(self, query):
        """下载临时目录中的转换结果。"""
        key = os.path.basename((query.get("f") or [""])[0])
        if not key or "/" in key or "\\" in key:
            self._send_json({"error": "Bad Request"}, 400)
            return
        path = os.path.join(TOOL_TMP_DIR, key)
        if not os.path.exists(path):
            self._send_json({"error": "文件不存在或已过期"}, 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        ascii_name = key.encode("ascii", "ignore").decode() or "download"
        display = re.sub(r"^\d+_", "", key)
        self._send_file_bytes(data, display, "application/octet-stream")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        elif path == "/api/status":
            cfg = load_config()
            self._send_json({
                "server_ver": 2,
                "configured": bool(cfg.get("api_key")),
                "api_key_masked": mask_key(cfg.get("api_key", "")),
                "model": cfg.get("model", "deepseek-chat"),
                "temperature": cfg.get("temperature", 0.7),
                "models": SUPPORTED_MODELS,
            })
        elif path == "/api/balance":
            self._handle_balance()
        elif path == "/api/tool/download":
            self._handle_tool_download(parse_qs(urlparse(self.path).query))
        else:
            self._send_json({"error": "Not Found"}, 404)

    def _handle_balance(self):
        """查询 DeepSeek 账户余额（不向客户端暴露 API Key）。"""
        cfg = load_config()
        key = str(cfg.get("api_key", "")).strip()
        if not key:
            self._send_json({"ok": False, "error": "not_configured"})
            return
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            infos = data.get("balance_infos") or []
            if infos:
                info = infos[0]
                self._send_json({
                    "ok": True,
                    "is_available": bool(data.get("is_available")),
                    "total_balance": info.get("total_balance"),
                    "currency": info.get("currency", "CNY"),
                    "granted_balance": info.get("granted_balance"),
                })
            else:
                self._send_json({"ok": True, "is_available": False, "total_balance": None,
                                 "currency": "CNY", "granted_balance": None})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._send_json({"ok": False, "error": "auth",
                                 "detail": "API Key 无效或已失效（HTTP %s）" % e.code})
            else:
                self._send_json({"ok": False, "error": "http", "detail": "HTTP " + str(e.code)})
        except Exception as e:
            self._send_json({"ok": False, "error": "network", "detail": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        ctype = self.headers.get("Content-Type") or ""
        if path == "/api/doc_translate" and "multipart/form-data" in ctype.lower():
            self._handle_doc_translate(ctype)
            return
        if path == "/api/chapter/open" and "multipart/form-data" in ctype.lower():
            self._handle_chapter_open(ctype)
            return
        if path == "/api/tool/merge" and "multipart/form-data" in ctype.lower():
            self._handle_tool_merge(ctype)
            return
        if path == "/api/tool/html2txt" and "multipart/form-data" in ctype.lower():
            self._handle_tool_html2txt(ctype)
            return
        if path == "/api/tool/txt2pdf" and "multipart/form-data" in ctype.lower():
            self._handle_tool_txt2pdf(ctype)
            return
        body = self._read_body()
        if path == "/api/config":
            cfg = load_config()
            if "api_key" in body:
                cfg["api_key"] = str(body["api_key"]).strip()
            if "model" in body and body["model"] in SUPPORTED_MODELS:
                cfg["model"] = body["model"]
            if "temperature" in body:
                try:
                    cfg["temperature"] = max(0.0, min(2.0, float(body["temperature"])))
                except Exception:
                    pass
            save_config(cfg)
            self._send_json({"ok": True, "configured": bool(cfg["api_key"]),
                             "api_key_masked": mask_key(cfg["api_key"])})
        elif path == "/api/chat":
            self._handle_chat(body)
        elif path == "/api/translate":
            self._handle_translate(body)
        elif path == "/api/chapter/resplit":
            self._handle_chapter_resplit(body)
        elif path == "/api/chapter/export":
            self._handle_chapter_export(body)
        else:
            self._send_json({"error": "Not Found"}, 404)

    # ---------- 对话接口（写作/代码） ----------
    def _handle_chat(self, body):
        self._send_sse_headers()
        cfg = self._get_cfg()
        if cfg is None:
            return
        module = body.get("module", "writing")
        messages = body.get("messages") or []
        if not messages:
            self._sse_error("消息内容为空。")
            return
        system = SYSTEM_PROMPTS.get(module, SYSTEM_PROMPTS["writing"])
        api_messages = [{"role": "system", "content": system}]
        for m in messages[-40:]:
            role = m.get("role")
            if role in ("user", "assistant"):
                api_messages.append({"role": role, "content": str(m.get("content", ""))})
        try:
            resp = call_deepseek_stream(cfg, api_messages)
        except urllib.error.HTTPError as e:
            self._sse_error(self._http_error_text(e))
            return
        except Exception as e:
            self._sse_error("请求失败：" + str(e))
            return
        try:
            for line in resp:
                delta = extract_stream_delta(line.decode("utf-8", errors="replace"))
                if delta:
                    self._sse({"type": "delta", "text": delta})
            self._sse({"type": "done"})
            self._sse_end()
        except Exception as e:
            self._sse_error("读取响应中断：" + str(e))

    # ---------- 翻译接口（长文本分块 + 流式） ----------
    def _handle_translate(self, body):
        self._send_sse_headers()
        cfg = self._get_cfg()
        if cfg is None:
            return
        text = (body.get("text") or "").strip()
        if not text:
            self._sse_error("请先粘贴需要翻译的文本。")
            return
        source = body.get("source") or "auto"
        target = body.get("target") or "zh"
        # 智能方向修正：自动识别源语言 + 目标是中文 + 原文以中文为主 → 自动改为译成英文，
        # 避免出现"中文→中文"的无意义翻译方向。
        if source == "auto" and target == "zh" and looks_chinese(text):
            target = "en"
        style = body.get("style") or "novel"
        glossary = body.get("glossary") or ""
        chunks = split_text(text)
        total = len(chunks)
        system = build_translate_system(source, target, style, glossary)
        full_text, failed = self._run_chunked_translate(cfg, chunks, system)
        if full_text is None:
            return
        self._sse({"type": "done", "full_text": "\n\n".join(full_text), "failed": failed})
        self._sse_end()

    # ---------- 文档翻译接口（导入 TXT / EPUB / MOBI） ----------
    def _handle_doc_translate(self, ctype):
        self._send_sse_headers()
        cfg = self._get_cfg()
        if cfg is None:
            return
        if "multipart/form-data" not in ctype:
            self._sse_error("请求格式错误：需要 multipart/form-data 上传文件。")
            return
        fields, files = self._read_multipart(ctype)
        if not files:
            self._sse_error("没有收到文件，请先选择 TXT / EPUB / MOBI 文档。")
            return
        try:
            self._doc_translate_inner(files, fields, cfg)
        except Exception as e:
            self._sse_error("服务器处理文档时发生错误：%s" % e)

    def _doc_translate_inner(self, files, fields, cfg):
        filename, raw = files[0]
        if len(raw) > MAX_DOC_BYTES:
            self._sse_error("文件超过 20MB 上限，请拆分后重试。")
            return
        try:
            text, ext = extract_document_text(filename, raw)
        except ValueError as e:
            self._sse_error("文档解析失败：%s" % e)
            return
        except Exception as e:
            self._sse_error("文档解析失败：%s" % e)
            return
        text = text.strip()
        if not text:
            self._sse_error("文档中没有提取到可翻译的文本内容。")
            return
        if len(text) > MAX_TEXT_CHARS:
            self._sse_error("文档文本约 %d 字，超过 200 万字上限，请拆分文件后重试。" % len(text))
            return
        source = (fields.get("source") or "auto").strip() or "auto"
        target = (fields.get("target") or "zh").strip() or "zh"
        if source == "auto" and target == "zh" and looks_chinese(text):
            target = "en"
        style = (fields.get("style") or "novel").strip() or "novel"
        glossary = (fields.get("glossary") or "").strip()
        chunks = split_text(text)
        total = len(chunks)
        system = build_translate_system(source, target, style, glossary)
        self._sse({"type": "doc_meta", "filename": filename, "ext": ext,
                   "chars": len(text), "chunks": total})
        full_text, failed = self._run_chunked_translate(cfg, chunks, system)
        if full_text is None:
            return
        self._sse({"type": "done", "full_text": "\n\n".join(full_text),
                   "filename": filename, "failed": failed})
        self._sse_end()

    # ---------- 章节编辑接口（导入 / 拆分 / 导出） ----------
    def _handle_chapter_open(self, ctype):
        """上传 TXT / EPUB / MOBI → 解析出章节列表。"""
        if "multipart/form-data" not in ctype:
            self._send_json({"ok": False, "error": "需要 multipart/form-data 上传文件。"}, 400)
            return
        fields, files = self._read_multipart(ctype)
        if not files:
            self._send_json({"ok": False, "error": "没有收到文件。"}, 400)
            return
        filename, raw = files[0]
        if len(raw) > MAX_DOC_BYTES:
            self._send_json({"ok": False, "error": "文件超过 20MB 上限。"}, 400)
            return
        ext = os.path.splitext(filename)[1].lower()
        try:
            if ext == ".epub":
                chapters = extract_chapters_epub(raw)
            else:
                text, _ = extract_document_text(filename, raw)
                # 默认组合规则：中文章节 + 序/尾声/附录等附加标题（与 EasyPub 一致）
                default_pattern = CHAPTER_PRESET_PATTERNS[0]["pattern"][:-1] + "|" + CHAPTER_PRESET_PATTERNS[3]["pattern"][2:]
                chapters = split_text_by_regex(text, default_pattern)
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        except Exception as e:
            self._send_json({"ok": False, "error": "解析失败：%s" % e}, 400)
            return
        total_chars = sum(len(c.get("text") or "") for c in chapters)
        self._send_json({"ok": True, "filename": filename, "ext": ext,
                         "total_chars": total_chars, "chapters": chapters})

    def _handle_chapter_resplit(self, body):
        """按规则重新拆分：mode=regex（pattern / 预设）/ length（length 千字）。"""
        text = (body.get("text") or "").strip()
        if not text:
            self._send_json({"ok": False, "error": "文本为空，请先导入文件。"}, 400)
            return
        mode = body.get("mode") or "regex"
        try:
            if mode == "length":
                chapters = split_text_by_length(text, int(body.get("length") or DEFAULT_SPLIT_LENGTH))
            else:
                pattern = body.get("pattern") or CHAPTER_PRESET_PATTERNS[0]["pattern"]
                chapters = split_text_by_regex(text, pattern, bool(body.get("ignore_case")))
        except Exception as e:
            self._send_json({"ok": False, "error": "拆分失败：%s" % e}, 400)
            return
        self._send_json({"ok": True, "chapters": chapters})

    def _handle_chapter_export(self, body):
        """导出 EPUB / TXT / MOBI。chapters: [{title, text}]"""
        fmt = (body.get("format") or "epub").lower()
        title = str(body.get("title") or "未命名").strip() or "未命名"
        author = str(body.get("author") or "佚名").strip() or "佚名"
        chapters = body.get("chapters") or []
        chapters = [{"title": str(c.get("title") or ""), "text": str(c.get("text") or "")} for c in chapters]
        chapters = [c for c in chapters if c["text"].strip() or c["title"]]
        if not chapters:
            self._send_json({"ok": False, "error": "没有可导出的章节。"}, 400)
            return
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip() or "book"
        if fmt == "txt":
            self._send_file_bytes(build_txt(chapters), safe + ".txt", "text/plain; charset=utf-8")
            return
        if fmt == "mobi":
            mobi = self._kindlegen_convert(build_epub(title, author, chapters), safe)
            if mobi is None:
                return
            self._send_file_bytes(mobi, safe + ".mobi", "application/x-mobipocket-ebook")
            return
        self._send_file_bytes(build_epub(title, author, chapters), safe + ".epub", "application/epub+zip")

    def _kindlegen_convert(self, epub_bytes, name):
        """用 EasyPub 自带的 kindlegen 把 EPUB 转成 MOBI。"""
        kindlegen = r"D:\EasyPub_1.5\easypub\bin\kindlegen_v2.9.exe"
        if not os.path.exists(kindlegen):
            self._send_json({"ok": False,
                             "error": "未找到 kindlegen（D:\\EasyPub_1.5\\easypub\\bin\\kindlegen_v2.9.exe），"
                                      "MOBI 导出不可用，可改用 EPUB / TXT 导出。"}, 400)
            return None
        import subprocess
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="agent_epub_")
        epub_path = os.path.join(tmpdir, name + ".epub")
        try:
            with open(epub_path, "wb") as f:
                f.write(epub_bytes)
            proc = subprocess.run([kindlegen, epub_path], capture_output=True, timeout=180, cwd=tmpdir)
            mobi_path = os.path.join(tmpdir, name + ".mobi")
            if not os.path.exists(mobi_path):
                for fn in os.listdir(tmpdir):
                    if fn.lower().endswith(".mobi"):
                        mobi_path = os.path.join(tmpdir, fn)
                        break
                else:
                    self._send_json({"ok": False,
                                     "error": "kindlegen 未能生成 MOBI（返回码 %s）。可改用 EPUB 导出。" % proc.returncode},
                                    500)
                    return None
            with open(mobi_path, "rb") as f:
                return f.read()
        except Exception as e:
            self._send_json({"ok": False, "error": "MOBI 转换失败：%s" % e}, 500)
            return None

    @staticmethod
    def _http_error_text(e):
        try:
            raw = e.read().decode("utf-8", errors="replace")
            obj = json.loads(raw)
            msg = (obj.get("error") or {}).get("message") or raw
        except Exception:
            msg = "HTTP %s" % e.code
        if e.code == 401:
            return "API Key 无效或已过期（HTTP 401），请检查设置中的 Key。"
        return "API 返回错误（HTTP %s）：%s" % (e.code, msg[:300])

    def _serve_file(self, path, content_type):
        if not os.path.isfile(path):
            self._send_json({"error": "Not Found"}, 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    print("=" * 52)
    print("  AI 工作助手已启动")
    print("  请用浏览器打开： http://127.0.0.1:%d" % PORT)
    print("  按 Ctrl+C 停止服务")
    print("=" * 52)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
