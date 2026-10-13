import os
import io
import wave
import time
import threading
import urllib.request
from PySide6.QtCore import QObject, Signal
from .config import APP_DIR
from .ai import translate_to

# ============ 语音朗读（Melo 离线 / Azure Speech / Piper 离线英文） ============
TTS_VOICES = [
    ("Melo（中英混读·女·离线，默认）", "sherpa:melo-zh_en"),
    ("Lessac（英文·离线，无需联网）", "piper:en_US-lessac-medium"),
    # Azure Speech（官方接口，需在设置里填 Key 和区域；F0 免费层每月 50 万字符，中日韩每字按 2 计）
    ("Nanami（日文·女·Azure）", "azure:ja-JP-NanamiNeural"),
    ("Keita（日文·男·Azure）", "azure:ja-JP-KeitaNeural"),
    ("SunHi（韩文·女·Azure）", "azure:ko-KR-SunHiNeural"),
    ("InJoon（韩文·男·Azure）", "azure:ko-KR-InJoonNeural"),
    ("Denise（法文·女·Azure）", "azure:fr-FR-DeniseNeural"),
    ("Henri（法文·男·Azure）", "azure:fr-FR-HenriNeural"),
    ("Katja（德文·女·Azure）", "azure:de-DE-KatjaNeural"),
    ("Conrad（德文·男·Azure）", "azure:de-DE-ConradNeural"),
    ("Elvira（西班牙文·女·Azure）", "azure:es-ES-ElviraNeural"),
    ("Alvaro（西班牙文·男·Azure）", "azure:es-ES-AlvaroNeural"),
    ("晓晓（中文·女·温柔·Azure）", "azure:zh-CN-XiaoxiaoNeural"),
    ("晓伊（中文·女·活泼·Azure）", "azure:zh-CN-XiaoyiNeural"),
    ("云希（中文·男·少年·Azure）", "azure:zh-CN-YunxiNeural"),
    ("云扬（中文·男·新闻·Azure）", "azure:zh-CN-YunyangNeural"),
    ("云健（中文·男·Azure）", "azure:zh-CN-YunjianNeural"),
    ("Jenny（英文·美式·女·Azure）", "azure:en-US-JennyNeural"),
    ("Aria（英文·美式·女·Azure）", "azure:en-US-AriaNeural"),
    ("Guy（英文·美式·男·Azure）", "azure:en-US-GuyNeural"),
    ("Sonia（英文·英式·女·Azure）", "azure:en-GB-SoniaNeural"),
    ("Ryan（英文·英式·男·Azure）", "azure:en-GB-RyanNeural"),
]
TTS_RATES = ["-50%", "-25%", "-10%", "+0%", "+10%", "+25%", "+50%", "+100%"]
TTS_DEFAULT_VOICE = "sherpa:melo-zh_en"

# Sherpa-ONNX 离线语音（vits-melo-tts-zh_en，MIT，中英混读）。
# 模型文件从 hf-mirror.com 逐个下载到 ~/.pyreader/sherpa_models/（huggingface.co 在本机会被重置连接）。
SHERPA_MELO_ID = "melo-zh_en"
SHERPA_MELO_DIR = "vits-melo-tts-zh_en"
SHERPA_MELO_BASE_URL = "https://hf-mirror.com/csukuangfj/vits-melo-tts-zh_en/resolve/main"
SHERPA_MELO_FILES = [
    "model.onnx", "lexicon.txt", "tokens.txt",
    "date.fst", "number.fst", "phone.fst", "new_heteronym.fst",
    "dict/jieba.dict.utf8", "dict/hmm_model.utf8", "dict/idf.utf8",
    "dict/stop_words.utf8", "dict/user.dict.utf8",
    "dict/pos_dict/char_state_tab.utf8", "dict/pos_dict/prob_emit.utf8",
    "dict/pos_dict/prob_start.utf8", "dict/pos_dict/prob_trans.utf8",
]
_sherpa_tts = None
_sherpa_lock = threading.Lock()   # 同时保护模型加载和合成调用

def sherpa_model_ready() -> bool:
    return os.path.isfile(os.path.join(APP_DIR, "sherpa_models", SHERPA_MELO_DIR, ".complete"))

def _ensure_sherpa_model() -> str:
    """确保 Melo 模型文件齐全（约 190MB，首次需联网），返回模型目录。镜像单文件偶发失败，逐个重试。"""
    d = os.path.join(APP_DIR, "sherpa_models", SHERPA_MELO_DIR)
    ok_flag = os.path.join(d, ".complete")
    if os.path.isfile(ok_flag):
        return d
    _log_tts("[离线朗读] 首次使用，正在下载 Melo 模型（约 190MB，需联网一次）")
    for rel in SHERPA_MELO_FILES:
        path = os.path.join(d, *rel.split("/"))
        if os.path.isfile(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        last_err = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(f"{SHERPA_MELO_BASE_URL}/{rel}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                os.replace(tmp, path)
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                time.sleep(1.0 * (attempt + 1))
        if last_err is not None:
            raise RuntimeError(f"离线语音模型下载失败（{rel}）：{last_err}") from last_err
    with open(ok_flag, "w") as f:
        f.write("ok")
    return d

def _load_sherpa_tts():
    """调用方须已持有 _sherpa_lock。"""
    global _sherpa_tts
    if _sherpa_tts is not None:
        return _sherpa_tts
    try:
        import sherpa_onnx
    except Exception as e:
        raise RuntimeError("未安装 sherpa-onnx，请执行：pip install sherpa-onnx") from e
    d = _ensure_sherpa_model().replace("\\", "/")   # 路径用正斜杠，避免被当成转义字符
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=f"{d}/model.onnx", lexicon=f"{d}/lexicon.txt",
                tokens=f"{d}/tokens.txt", dict_dir=f"{d}/dict"),
            provider="cpu", num_threads=4, debug=False),
        rule_fsts=",".join(f"{d}/{f}" for f in ("date.fst", "phone.fst", "number.fst", "new_heteronym.fst")),
        max_num_sentences=1)
    _sherpa_tts = sherpa_onnx.OfflineTts(cfg)
    return _sherpa_tts

def _rate_to_speed(rate: str) -> float:
    """把 "+10%"/"-25%" 语速转成 Sherpa 的 speed（越大越快）。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    return max(0.5, min(3.0, 1.0 + pct / 100.0))

def synthesize_tts_sherpa(text: str, rate: str) -> bytes:
    """离线合成（Sherpa-ONNX Melo，中英混读），返回 wav 字节。"""
    import numpy as np
    with _sherpa_lock:
        tts = _load_sherpa_tts()
        g = tts.generate(text, sid=0, speed=_rate_to_speed(rate))
    if not len(g.samples):
        raise RuntimeError("Melo 未返回音频")
    pcm = (np.clip(np.asarray(g.samples, dtype=np.float32), -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(g.sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()

# Piper 离线语音：voice id -> Hugging Face piper-voices 仓库里的路径前缀
PIPER_VOICES = {
    "en_US-lessac-medium": "en/en_US/lessac/medium",
}
PIPER_MODEL_BASE_URL = "https://hf-mirror.com/rhasspy/piper-voices/resolve/main"
_piper_voice_cache = {}
_piper_voice_cache_lock = threading.Lock()

def _piper_model_dir():
    d = os.path.join(APP_DIR, "piper_models")
    os.makedirs(d, exist_ok=True)
    return d

def _ensure_piper_model(voice_id: str) -> str:
    """确保 voice_id 对应的 Piper 模型文件（.onnx + .onnx.json）已下载到本地，返回 onnx 路径。"""
    if voice_id not in PIPER_VOICES:
        raise RuntimeError(f"未知的离线语音：{voice_id}")
    d = _piper_model_dir()
    onnx_path = os.path.join(d, f"{voice_id}.onnx")
    cfg_path = onnx_path + ".json"
    prefix = PIPER_VOICES[voice_id]
    if not (os.path.isfile(onnx_path) and os.path.isfile(cfg_path)):
        _log_tts(f"[离线朗读] 首次使用，正在下载模型：{voice_id}（约 60MB，需联网一次）")
        for path, url in ((onnx_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx"),
                          (cfg_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx.json")):
            tmp = path + ".part"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                    out.write(resp.read())
                os.replace(tmp, path)
            except Exception as e:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise RuntimeError(f"离线语音模型下载失败：{e}") from e
    return onnx_path

def _piper_rate_to_length_scale(rate: str) -> float:
    """把 "+10%"/"-25%" 语速转成 Piper 的 length_scale（越大越慢）。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    pct = max(-90, min(200, pct))
    return 1.0 / (1.0 + pct / 100.0)

def _load_piper_voice(voice_id: str):
    with _piper_voice_cache_lock:
        voice = _piper_voice_cache.get(voice_id)
        if voice is not None:
            return voice
        try:
            from piper import PiperVoice
        except Exception as e:
            raise RuntimeError("未安装 piper-tts，请执行：pip install piper-tts") from e
        onnx_path = _ensure_piper_model(voice_id)
        voice = PiperVoice.load(onnx_path)
        _piper_voice_cache[voice_id] = voice
        return voice

def synthesize_tts_piper(text: str, voice_id: str, rate: str) -> bytes:
    """离线合成（Piper），返回 wav 字节。"""
    from piper import SynthesisConfig
    voice = _load_piper_voice(voice_id)
    syn_config = SynthesisConfig(length_scale=_piper_rate_to_length_scale(rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue()

def split_sentences(text: str, base: int = 0):
    """把文本切成句子：返回 [(起始偏移, 结束偏移, 句子文本)]，偏移为全局字符偏移。

    断句规则（按优先级）：
      1. 中文标点 。！？；  / 换行  → 立即断
      2. 英文句末：'. '/'! '/'? ' 后接大写或数字 → 断在空格后
      3. 超长兜底（120字符）→ 向后找最近空格断，避免从单词中间截断
    """
    res = []
    seg_start = 0
    i = 0
    n = len(text)
    MAX_LEN = 120   # 兜底截断阈值（字符数）

    while i < n:
        ch = text[i]
        buf_len = i - seg_start + 1

        # 规则1：中文标点 / 换行 → 立即断
        if ch in "。！？；" or ch == "\n":
            end = i + 1
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            i = end
            continue

        # 规则2：英文句末标点（. ! ?）后接空格+大写/数字 → 断在标点后，空格归下一句
        if ch in ".!?" and i + 2 < n and text[i + 1] == " " and (text[i + 2].isupper() or text[i + 2].isdigit()):
            end = i + 2          # 断在空格，下一句从大写字母开始（空格被包含进当前句，strip 后不影响 TTS）
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            i = end
            continue

        # 规则3：超长兜底 → 向后找最近空格，找不到就硬断
        if buf_len >= MAX_LEN:
            # 向后最多扫 20 字符找空格
            cut = i + 1
            for j in range(i + 1, min(i + 21, n)):
                if text[j] == " ":
                    cut = j + 1   # 断在空格之后（空格归下一句开头，strip 掉）
                    break
            seg = text[seg_start:cut]
            if seg.strip():
                res.append((base + seg_start, base + cut, seg))
            seg_start = cut
            i = cut
            continue

        i += 1

    if seg_start < n:
        seg = text[seg_start:n]
        if seg.strip():
            res.append((base + seg_start, base + n, seg))
    return res

# Azure Speech（REST，无需 SDK）。账号 0 为主、1 为备用；Key/区域由设置对话框写入，
# 主账号也可用环境变量 AZURE_SPEECH_KEY / AZURE_SPEECH_REGION。
_azure_accounts = [{"key": "", "region": ""}, {"key": "", "region": ""}]
_azure_skip_until = [0.0, 0.0]   # 账号额度用尽/认证失败后，暂时跳过的截止时间（秒级时间戳）
AZURE_FREE_QUOTA = 500_000       # F0 每月免费字符数
AZURE_SKIP_SECONDS = 600

def _norm_region(region: str) -> str:
    """接受 'eastus' 或完整端点 URL（https://eastus.api.cognitive.microsoft.com/），统一成区域名。"""
    import re
    r = (region or "").strip().lower()
    m = re.match(r"https?://([a-z0-9-]+)\.", r)
    return m.group(1) if m else r

def set_azure_config(key: str, region: str, key2: str = "", region2: str = ""):
    _azure_accounts[0] = {"key": (key or "").strip(), "region": _norm_region(region)}
    _azure_accounts[1] = {"key": (key2 or "").strip(), "region": _norm_region(region2)}
    _azure_skip_until[0] = _azure_skip_until[1] = 0.0

def _azure_usage_add(idx: int, text: str):
    """按 Azure 计费口径累计各账号本月字符数（中日韩字符每个算 2），接近免费额度时写日志告警。"""
    import json
    billed = sum(2 if ("⺀" <= c <= "鿿" or "가" <= c <= "힯" or "＀" <= c <= "￯") else 1
                 for c in text)
    path = os.path.join(APP_DIR, "azure_usage.json")
    month = time.strftime("%Y-%m")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cur = data.get(month, {})
    if not isinstance(cur, dict):          # 兼容旧格式 {"2026-10": 51}
        cur = {"0": cur}
    used = cur.get(str(idx), 0) + billed
    cur[str(idx)] = used
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({month: cur}, f)
    except Exception:
        pass
    if used >= AZURE_FREE_QUOTA * 0.8:
        _log_tts(f"[Azure] 账号{idx}本月已用约 {used} / {AZURE_FREE_QUOTA} 字符（免费额度的 {used * 100 // AZURE_FREE_QUOTA}%）")

class _AzureAccountError(Exception):
    """账号级错误（认证失败 / 额度用尽 / 持续限流），应切换到下一个账号。"""

def _azure_call(idx: int, acct: dict, voice_name: str, text: str, rate: str) -> bytes:
    import re
    import urllib.error
    from xml.sax.saxutils import escape
    key, region = acct["key"], acct["region"]
    if not re.fullmatch(r"[a-z0-9-]+", region):   # region 会拼进主机名，Key 会发往该主机，必须严格校验
        raise RuntimeError(f"Azure 区域格式不正确：{region!r}")
    lang = "-".join(voice_name.split("-")[:2])
    ssml = (f"<speak version='1.0' xml:lang='{lang}' xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice_name}'><prosody rate='{rate}'>{escape(text)}</prosody></voice></speak>")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "PyReader"}
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=ssml.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("Azure 未返回音频")
            _azure_usage_add(idx, text)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise _AzureAccountError(f"认证失败或额度用尽（HTTP {e.code}）") from e
            last_err = e
        except Exception as e:
            last_err = e
        _log_tts(f"[Azure重试] 账号{idx} 第{attempt + 1}次失败（原文前30字：{text[:30]!r}）：{last_err}")
        time.sleep(1.0 * (attempt + 1))
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 429:
        raise _AzureAccountError("持续被限流（HTTP 429）") from last_err
    raise RuntimeError(f"Azure 语音合成失败（已重试3次）：{last_err}")

def synthesize_tts_azure(text: str, voice_name: str, rate: str) -> bytes:
    """Azure Speech 云端合成，返回 mp3 字节。主账号认证失败/额度用尽/持续限流时自动切换备用账号，
    出问题的账号会被暂时跳过一段时间，避免每句都先撞一次错误。"""
    accts = [dict(a) for a in _azure_accounts]
    if not accts[0]["key"]:
        accts[0] = {"key": os.environ.get("AZURE_SPEECH_KEY", "").strip(),
                    "region": _norm_region(os.environ.get("AZURE_SPEECH_REGION", ""))}
    cand = [i for i, a in enumerate(accts) if a["key"] and a["region"]]
    if not cand:
        raise RuntimeError("未配置 Azure Speech：请在设置里填写 Key 和区域（如 eastus）")
    now = time.time()
    order = [i for i in cand if _azure_skip_until[i] <= now] or cand   # 都在冷却期则全部再试一遍
    errors = []
    for i in order:
        try:
            return _azure_call(i, accts[i], voice_name, text, rate)
        except _AzureAccountError as e:
            _azure_skip_until[i] = time.time() + AZURE_SKIP_SECONDS
            _log_tts(f"[Azure] 账号{i} 不可用，{AZURE_SKIP_SECONDS // 60} 分钟内跳过：{e}")
            errors.append(f"账号{i}：{e}")
    raise RuntimeError("Azure Speech 所有账号均不可用，请检查 Key、区域和额度（" + "；".join(errors) + "）")

def synthesize_tts(text: str, voice: str, rate: str) -> bytes:
    """把一段文本合成音频字节。voice 以 "sherpa:" 走本地 Melo 离线合成（wav，中英混读），
    以 "piper:" 开头走本地 Piper 离线合成（wav，仅英文），以 "azure:" 走 Azure Speech 云端合成（mp3）。"""
    if voice.startswith("azure:"):
        return synthesize_tts_azure(text, voice[len("azure:"):], rate)
    if voice.startswith("sherpa:") or voice == "piper:zh_CN-huayan-medium":   # 旧配置里的 Piper 中文音色迁移到 Melo
        return synthesize_tts_sherpa(text, rate)
    if voice.startswith("piper:"):
        return synthesize_tts_piper(text, voice[len("piper:"):], rate)

    raise RuntimeError(f"未知的朗读音色：{voice}")

def _log_tts(msg):
    """把朗读相关事件写入 ~/.pyreader/tts.log。"""
    try:
        with open(os.path.join(APP_DIR, "tts.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

class TtsWorker(QObject):
    ready = Signal(int, bytes)     # (句子序号, mp3 字节)
    failed = Signal(int, str)      # (句子序号, 错误信息)
    def __init__(self, seq, text, voice, rate):
        super().__init__()
        self.seq, self.text, self.voice, self.rate = seq, text, voice, rate
    def run(self):
        try:
            self.ready.emit(self.seq, synthesize_tts(self.text, self.voice, self.rate))
        except Exception as e:
            self.failed.emit(self.seq, str(e))

class TranslateWorker(QObject):
    done = Signal(int, str)        # (句子序号, 译文)
    failed = Signal(int, str)      # (句子序号, 错误信息)
    def __init__(self, seq, text, cfg, target):
        super().__init__()
        self.seq, self.text, self.cfg, self.target = seq, text, cfg, target
    def run(self):
        try:
            self.done.emit(self.seq, translate_to(self.text, self.cfg, self.target))
        except Exception as e:
            self.failed.emit(self.seq, str(e))
