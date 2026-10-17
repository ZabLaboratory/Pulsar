"""Run capture paint/framing pixels in the existing owned private native host.

The unchanged group probe owns startup, credentials, source-removal regression
and graceful shutdown. No physical capture, public Live, REC or CEF is exercised.
"""
import importlib.util
import pathlib
import argparse

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    native = load("framing_native_host", "probe-group-composition.py")
    effects = load("framing_effect_pixels", "probe-capture-effects.py")
    original = native.exercise

    async def combined(config, output, runtime_id, scratch):
        paint = await effects.run(config, output)
        result = await original(config, output, runtime_id, scratch)
        result["capturePaintAndFractionalFraming"] = paint
        return result

    native.exercise = combined
    output_parser = argparse.ArgumentParser(add_help=False)
    output_parser.add_argument("--output", type=pathlib.Path, required=True)
    output_args, _ = output_parser.parse_known_args()
    native.EVIDENCE = output_args.output.resolve()
    native.CACHE = ROOT / ".cache/fractional-capture-box"
    native.main()
