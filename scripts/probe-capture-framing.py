"""Run capture paint/framing pixels in the existing owned private native host.

The unchanged group probe owns startup, credentials, source-removal regression
and graceful shutdown. No physical capture, public Live, REC or CEF is exercised.
"""
import importlib.util
import pathlib

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
    native.EVIDENCE = ROOT / "evidence/local-20261015-capture-paint-integration/eleven"
    native.CACHE = ROOT / ".cache/fractional-framing"
    native.main()
