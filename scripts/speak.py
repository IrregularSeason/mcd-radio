#!/usr/bin/env python3
"""麦门电台 —— 合成音频。

把 station.py 产出的 lines.txt（一行一段）合成为一段可分享的音频。

链路::

    文稿 -> 系统 TTS -> aiff -> afconvert/ffmpeg -> m4a/mp3

平台支持：

* macOS —— 内置 `say` + `afconvert`，开箱可用
* Linux —— 回退到 `espeak-ng`
* 都没有 —— **不静默失败**，而是明确告知，并把播报稿留在原地

为什么要有时长自检
------------------

`say` 有个很隐蔽的行为：用**不带语区的短名**指定新版音色（如 `say -v Reed`）
时，它**返回成功、写出一个结构合法的音频文件，但内容是 0.016 秒的空白**。

也就是说，一个不校验输出长度的实现会「成功」地生成一期无声节目。
所以本脚本在合成后会读取音频时长，短于预期下限就自动换音色重试，
并把这个过程报告出来。

用法::

    speak.py <lines.txt> [--out out.m4a] [--voice NAME] [--rate 180] [--dry-run]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

# 段落之间的停顿时长（毫秒）。用 macOS 语音的内嵌指令 [[slnc N]] 插入，
# 因此整期节目仍然是一次合成就够了，不需要拼接音频文件。
PAUSE_MS = 900

# 每个音色都准备一个兜底：完整名失败时退回老牌音色
FALLBACK_VOICES = ["Tingting", "Sinji", "Meijia"]


def which(name):
    return shutil.which(name)


def speech_text(lines_path, pause_ms=PAUSE_MS):
    """把一行一段的播报稿拼成一段话，段间插入停顿指令。"""
    with open(lines_path, encoding="utf-8") as fh:
        segments = [ln.strip() for ln in fh if ln.strip()]
    if not segments:
        return "", 0
    joined = ("[[slnc %d]]" % pause_ms).join(segments)
    chars = sum(len(s) for s in segments)
    return joined, chars


def audio_duration(path):
    """读取音频时长（秒）。优先用 afinfo，其次用 ffprobe。"""
    if which("afinfo"):
        try:
            out = subprocess.run(["afinfo", path], capture_output=True, text=True,
                                 timeout=30).stdout
            m = re.search(r"estimated duration:\s*([0-9.]+)", out)
            if m:
                return float(m.group(1))
        except Exception:
            pass
    if which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=30).stdout
            return float(out.strip())
        except Exception:
            pass
    return None


def synth_darwin(text, voice, rate, out_aiff):
    cmd = ["say", "-o", out_aiff]
    if voice:
        cmd += ["-v", voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd.append(text)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(out_aiff)


def synth_linux(text, voice, rate, out_wav):
    exe = which("espeak-ng") or which("espeak")
    if not exe:
        return False
    cmd = [exe, "-w", out_wav]
    if rate:
        cmd += ["-s", str(rate)]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(out_wav)


def transcode(src, dst):
    """把 aiff/wav 转成便于分享的压缩格式。"""
    ext = os.path.splitext(dst)[1].lower()
    if which("afconvert"):
        fmt = "m4af" if ext == ".m4a" else ("mp4f" if ext == ".mp4" else "m4af")
        codec = "aac"
        r = subprocess.run(["afconvert", "-f", fmt, "-d", codec, src, dst],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(dst):
            return True
    if which("ffmpeg"):
        codec = "libmp3lame" if ext == ".mp3" else "aac"
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-c:a", codec, "-b:a", "96k", dst],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(dst):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="麦门电台音频合成")
    ap.add_argument("lines", help="station.py 产出的 lines.txt")
    ap.add_argument("--out", default=None, help="输出音频路径（默认与 lines 同目录的 episode.m4a）")
    ap.add_argument("--voice", default=None, help="音色名（建议用带语区的完整名）")
    ap.add_argument("--rate", type=int, default=178, help="语速，默认 178")
    ap.add_argument("--dry-run", action="store_true", help="只输出将要朗读的文本")
    args = ap.parse_args()

    text, chars = speech_text(args.lines)
    if not text:
        print("播报稿是空的，先跑 station.py script。", file=sys.stderr)
        return 1

    expected = chars / 4.7 * 0.6      # 允许 40% 的余量，低于这个值一定是坏了
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.lines)), "episode.m4a")

    if args.dry_run:
        print(text)
        print("---")
        print("%d 字，预计 %.0f 秒" % (chars, chars / 4.7), file=sys.stderr)
        return 0

    tmp_dir = os.path.dirname(os.path.abspath(out))
    os.makedirs(tmp_dir, exist_ok=True)

    # 候选音色：用户指定的优先，失败后退回内置老音色
    candidates = []
    if args.voice:
        candidates.append(args.voice)
    candidates += FALLBACK_VOICES

    raw = os.path.join(tmp_dir, ".raw.aiff")
    used = None
    warnings = []

    for idx, voice in enumerate(candidates):
        if which("say"):
            ok = synth_darwin(text, voice, args.rate, raw)
        else:
            ok = synth_linux(text, voice, args.rate, raw)
        if not ok:
            warnings.append("音色 %s 合成失败" % voice)
            continue

        dur = audio_duration(raw)
        if dur is not None and dur < expected:
            # 命中了那个静默失败的坑：合成"成功"但几乎没有声音
            warnings.append(
                "音色 %s 只产出 %.3f 秒音频（预期约 %.0f 秒），判断为静默失败，换下一个"
                % (voice, dur, chars / 4.7))
            if idx + 1 < len(candidates):
                continue
        used = voice
        break

    if not used:
        print("没有可用的 TTS 音色。播报稿保留在 %s。" % args.lines, file=sys.stderr)
        for w in warnings:
            print("  · %s" % w, file=sys.stderr)
        return 2

    final = transcode(raw, out) if os.path.splitext(out)[1].lower() in (".m4a", ".mp3", ".mp4") else False
    if not final:
        # 转码不可用时保留原始音频，不假装成功
        alt = os.path.splitext(out)[0] + ".aiff"
        if os.path.exists(raw):
            shutil.move(raw, alt)
            raw = alt
        out = alt
        warnings.append("没有找到 afconvert / ffmpeg，保留未压缩的 aiff")
    else:
        # 转码成功后删掉中间产物，否则它会跟着 episodes/ 一起被提交
        if os.path.exists(raw):
            os.remove(raw)

    dur = audio_duration(out)
    size = os.path.getsize(out) if os.path.exists(out) else 0

    print("已合成：%s" % out)
    print("时长约 %.1f 秒　|　体积 %.1f MB　|　音色 %s" % (
        dur or 0, size / 1048576.0, used))
    if used != args.voice and args.voice:
        print("提示：你指定的音色没能用上，已退回 %s。" % used)
    for w in warnings:
        print("  注意 · %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
