"""Benchmark Lithuanian STT and TTS providers on simulated 8 kHz phone audio.

STT: every audio file in --audio-dir (wav/mp3/flac/ogg) needs a reference transcript with the
same name and a .txt extension (e.g. kurjeris_01.wav + kurjeris_01.txt). Audio is converted to
8 kHz mono mu-law (what Twilio delivers), streamed in 20 ms frames at real-time pace, and scored
for WER/CER, first-partial latency (first text after audio start) and final latency (last final
result after the end of speech).

TTS: the standard phrases in app/speech/phrases_lt.py are synthesized as 8 kHz mu-law and saved
as WAV files under <out>/tts/<provider>/, with time-to-first-byte numbers.

Providers without credentials (or SDK packages) are skipped and listed at the end.

Usage:
  python scripts/benchmark_speech.py --audio-dir samples/lt
  python scripts/benchmark_speech.py --skip-stt                   # TTS only
  python scripts/benchmark_speech.py --audio-dir samples/lt --stt google,deepgram --skip-tts
  python scripts/benchmark_speech.py --skip-stt --voice google=lt-LT-Chirp3-HD-Charon
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402
from app.speech.audio import (  # noqa: E402
    iter_frames,
    load_audio_as_mulaw,
    mulaw_duration_seconds,
    mulaw_to_wav,
)
from app.speech.base import (  # noqa: E402
    TELEPHONY_FRAME_BYTES,
    TELEPHONY_SAMPLE_RATE,
    MissingCredentialsError,
    SpeechError,
    STTConfig,
    STTProvider,
    VoiceConfig,
)
from app.speech.factory import STT_PROVIDERS, TTS_PROVIDERS, create_stt, create_tts  # noqa: E402
from app.speech.metrics import (  # noqa: E402
    error_rates,
    fmt_ms,
    fmt_pct,
    format_table,
    percentile,
)
from app.speech.phrases_lt import PHRASES  # noqa: E402

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg"}
FRAME_SECONDS = TELEPHONY_FRAME_BYTES / TELEPHONY_SAMPLE_RATE
MULAW_SILENCE = b"\xff"
# Extra time allowed per file beyond its duration before the provider is considered hung.
STT_GRACE_SECONDS = 30


@dataclass
class Sample:
    name: str
    audio: bytes  # 8 kHz mu-law
    reference: str


@dataclass
class SttResult:
    provider: str
    file: str
    reference: str
    hypothesis: str
    first_partial_s: float | None
    final_latency_s: float | None
    error: str | None = None


@dataclass
class TtsResult:
    provider: str
    phrase: str
    ttfb_s: float | None
    total_s: float | None
    audio_s: float | None
    file: str | None
    error: str | None = None


@dataclass
class Skip:
    kind: str
    provider: str
    reason: str


def parse_list(value: str, allowed: dict[str, object]) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown provider(s) {unknown}; choose from {list(allowed)}"
        )
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--audio-dir", type=Path, help="folder with audio files + .txt references")
    parser.add_argument(
        "--stt",
        type=lambda v: parse_list(v, STT_PROVIDERS),
        default=list(STT_PROVIDERS),
        help=f"comma-separated STT providers (default: {','.join(STT_PROVIDERS)})",
    )
    parser.add_argument(
        "--tts",
        type=lambda v: parse_list(v, TTS_PROVIDERS),
        default=list(TTS_PROVIDERS),
        help=f"comma-separated TTS providers (default: {','.join(TTS_PROVIDERS)})",
    )
    parser.add_argument("--skip-stt", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument(
        "--languages",
        default="lt-LT",
        help="STT language codes, primary first (e.g. lt-LT,en-US to test auto-detection)",
    )
    parser.add_argument(
        "--voice",
        action="append",
        default=[],
        metavar="PROVIDER=VOICE",
        help="TTS voice override, repeatable (e.g. azure=lt-LT-LeonasNeural)",
    )
    parser.add_argument(
        "--tail-silence",
        type=float,
        default=1.5,
        help="seconds of silence streamed after each file, like a caller pausing (default 1.5)",
    )
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="send audio as fast as possible (quick WER check; latency numbers become meaningless)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("out"), help="output folder (default out/)"
    )
    return parser.parse_args()


def load_samples(audio_dir: Path) -> tuple[list[Sample], list[str]]:
    samples: list[Sample] = []
    problems: list[str] = []
    for path in sorted(audio_dir.iterdir()):
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        reference_path = path.with_suffix(".txt")
        if not reference_path.is_file():
            problems.append(f"{path.name}: no reference transcript {reference_path.name}")
            continue
        try:
            audio = load_audio_as_mulaw(path)
        except Exception as exc:  # noqa: BLE001 - report and continue with other files
            problems.append(f"{path.name}: cannot decode ({type(exc).__name__}: {exc})")
            continue
        samples.append(Sample(path.name, audio, reference_path.read_text(encoding="utf-8").strip()))
    return samples, problems


async def paced_frames(audio: bytes, realtime: bool, started: list[float]) -> AsyncIterator[bytes]:
    """Yield 20 ms frames; records the wall-clock start in started[0]."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    started.append(time.perf_counter())
    for index, frame in enumerate(iter_frames(audio)):
        if realtime:
            delay = start + index * FRAME_SECONDS - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)
        yield frame


async def run_stt_file(
    provider_name: str,
    provider: STTProvider,
    sample: Sample,
    config: STTConfig,
    tail_silence: float,
    realtime: bool,
) -> SttResult:
    speech_seconds = mulaw_duration_seconds(sample.audio)
    audio = sample.audio + MULAW_SILENCE * int(tail_silence * TELEPHONY_SAMPLE_RATE)
    started: list[float] = []
    first_partial_at: float | None = None
    last_final_at: float | None = None
    finals: list[str] = []
    error: str | None = None
    try:
        async with asyncio.timeout(speech_seconds + tail_silence + STT_GRACE_SECONDS):
            async for event in provider.stream(paced_frames(audio, realtime, started), config):
                now = time.perf_counter()
                if first_partial_at is None:
                    first_partial_at = now
                if event.is_final:
                    finals.append(event.text)
                    last_final_at = now
    except TimeoutError:
        error = "timeout"
    except SpeechError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one broken provider must not stop the benchmark
        error = f"{type(exc).__name__}: {exc}"

    start = started[0] if started else None
    first_partial = first_partial_at - start if start and first_partial_at else None
    final_latency = None
    if start and last_final_at and realtime:
        final_latency = last_final_at - (start + speech_seconds)
    return SttResult(
        provider=provider_name,
        file=sample.name,
        reference=sample.reference,
        hypothesis=" ".join(finals),
        first_partial_s=first_partial,
        final_latency_s=final_latency,
        error=error,
    )


async def benchmark_stt(
    settings: Settings, args: argparse.Namespace, skips: list[Skip]
) -> list[SttResult]:
    if args.audio_dir is None:
        skips.append(Skip("STT", "all", "no --audio-dir given"))
        return []
    if not args.audio_dir.is_dir():
        skips.append(Skip("STT", "all", f"audio dir not found: {args.audio_dir}"))
        return []
    try:
        samples, problems = load_samples(args.audio_dir)
    except ImportError as exc:
        skips.append(Skip("STT", "all", f"{exc.name} missing: pip install -e '.[bench]'"))
        return []
    for problem in problems:
        print(f"  ! {problem}")
    if not samples:
        skips.append(Skip("STT", "all", f"no usable audio files in {args.audio_dir}"))
        return []

    config = STTConfig(languages=tuple(code.strip() for code in args.languages.split(",")))
    total_audio = sum(mulaw_duration_seconds(s.audio) + args.tail_silence for s in samples)
    results: list[SttResult] = []
    for name in args.stt:
        try:
            provider = create_stt(settings, name)
        except MissingCredentialsError as exc:
            skips.append(Skip("STT", name, "missing " + ", ".join(exc.missing)))
            continue
        pace = "" if args.no_realtime else f", ~{total_audio:.0f} s real-time"
        print(f"STT {name}: {len(samples)} files{pace}")
        try:
            for sample in samples:
                result = await run_stt_file(
                    name, provider, sample, config, args.tail_silence, not args.no_realtime
                )
                status = result.error or result.hypothesis[:70]
                print(f"  {sample.name}: {status}")
                results.append(result)
        finally:
            await provider.aclose()
    return results


async def benchmark_tts(
    settings: Settings, args: argparse.Namespace, skips: list[Skip]
) -> list[TtsResult]:
    overrides = dict(item.split("=", 1) for item in args.voice)
    results: list[TtsResult] = []
    for name in args.tts:
        try:
            provider = create_tts(settings, name)
        except MissingCredentialsError as exc:
            skips.append(Skip("TTS", name, "missing " + ", ".join(exc.missing)))
            continue
        voice = VoiceConfig(language="lt-LT", voice=overrides.get(name))
        folder = args.out / "tts" / name
        folder.mkdir(parents=True, exist_ok=True)
        print(f"TTS {name}: {len(PHRASES)} phrases -> {folder}")
        try:
            for index, phrase in enumerate(PHRASES, start=1):
                started = time.perf_counter()
                first_at: float | None = None
                chunks: list[bytes] = []
                error: str | None = None
                try:
                    async with asyncio.timeout(settings.speech_timeout_seconds * 2):
                        async for chunk in provider.synthesize(phrase.text, voice):
                            if first_at is None:
                                first_at = time.perf_counter()
                            chunks.append(chunk)
                except TimeoutError:
                    error = "timeout"
                except SpeechError as exc:
                    error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                finished = time.perf_counter()
                audio = b"".join(chunks)
                path: Path | None = None
                if audio:
                    path = folder / f"{index:02d}_{phrase.key}.wav"
                    path.write_bytes(mulaw_to_wav(audio))
                results.append(
                    TtsResult(
                        provider=name,
                        phrase=phrase.key,
                        ttfb_s=first_at - started if first_at else None,
                        total_s=finished - started if not error else None,
                        audio_s=mulaw_duration_seconds(audio) if audio else None,
                        file=str(path) if path else None,
                        error=error,
                    )
                )
                print(f"  {phrase.key}: {error or fmt_ms(results[-1].ttfb_s) + ' ms TTFB'}")
        finally:
            await provider.aclose()
    if results:
        write_review_sheet(args.out / "tts" / "phrases.txt")
    return results


def write_review_sheet(path: Path) -> None:
    lines = ["Lithuanian phrases for native-speaker review (file prefix = number).", ""]
    for index, phrase in enumerate(PHRASES, start=1):
        status = "reviewed" if phrase.reviewed else "NOT REVIEWED"
        lines += [f"{index:02d}_{phrase.key} [{status}]", f"  LT: {phrase.text}"]
        lines += [f"  EN: {phrase.english}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def report_stt(results: list[SttResult]) -> None:
    if not results:
        return
    rows = []
    for r in results:
        wer = error_rates([r.reference], [r.hypothesis])[0] if not r.error else None
        rows.append(
            [
                r.provider,
                r.file,
                fmt_pct(wer),
                fmt_ms(r.first_partial_s),
                fmt_ms(r.final_latency_s),
                r.error or "",
            ]
        )
    print("\nSTT per file")
    print(format_table(["provider", "file", "WER", "first partial ms", "final ms", "error"], rows))

    summary = []
    for name in dict.fromkeys(r.provider for r in results):
        ok = [r for r in results if r.provider == name and not r.error]
        total = sum(1 for r in results if r.provider == name)
        wer, cer = error_rates([r.reference for r in ok], [r.hypothesis for r in ok])
        partials = [r.first_partial_s for r in ok if r.first_partial_s is not None]
        finals = [r.final_latency_s for r in ok if r.final_latency_s is not None]
        summary.append(
            [
                name,
                f"{len(ok)}/{total}",
                fmt_pct(wer),
                fmt_pct(cer),
                fmt_ms(percentile(partials, 50)),
                fmt_ms(percentile(finals, 50)),
                fmt_ms(percentile(finals, 90)),
            ]
        )
    print("\nSTT summary (WER/CER over successful files; latencies in ms)")
    headers = ["provider", "ok", "WER", "CER", "first partial p50", "final p50", "final p90"]
    print(format_table(headers, summary))


def report_tts(results: list[TtsResult]) -> None:
    if not results:
        return
    summary = []
    for name in dict.fromkeys(r.provider for r in results):
        mine = [r for r in results if r.provider == name]
        ok = [r for r in mine if not r.error]
        ttfbs = [r.ttfb_s for r in ok if r.ttfb_s is not None]
        totals = [r.total_s for r in ok if r.total_s is not None]
        summary.append(
            [
                name,
                f"{len(ok)}/{len(mine)}",
                fmt_ms(mine[0].ttfb_s),
                fmt_ms(percentile(ttfbs, 50)),
                fmt_ms(percentile(ttfbs, 90)),
                fmt_ms(percentile(totals, 50)),
            ]
        )
    print("\nTTS summary (ms; the first request includes connection setup)")
    headers = ["provider", "ok", "first TTFB", "TTFB p50", "TTFB p90", "total p50"]
    print(format_table(headers, summary))
    errors = [r for r in results if r.error]
    for r in errors:
        print(f"  ! {r.provider} {r.phrase}: {r.error}")


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    skips: list[Skip] = []
    stt_results = [] if args.skip_stt else await benchmark_stt(settings, args, skips)
    tts_results = [] if args.skip_tts else await benchmark_tts(settings, args, skips)

    report_stt(stt_results)
    report_tts(tts_results)

    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "speech_benchmark.json"
    results_path.write_text(
        json.dumps(
            {
                "stt": [asdict(r) for r in stt_results],
                "tts": [asdict(r) for r in tts_results],
                "skipped": [asdict(s) for s in skips],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if skips:
        print("\nSkipped")
        for skip in skips:
            print(f"  {skip.kind} {skip.provider}: {skip.reason}")
    print(f"\nRaw results: {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
