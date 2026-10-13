#!/usr/bin/env python3
"""Static PE coherence gate. Reads binaries; never loads libobs in the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import struct


class PE:
    def __init__(self, path: pathlib.Path):
        self.path, self.data = path, path.read_bytes()
        if self.data[:2] != b"MZ":
            raise ValueError(f"{path.name}: invalid DOS header")
        pe = self.u32(0x3C)
        if self.data[pe:pe + 4] != b"PE\0\0":
            raise ValueError(f"{path.name}: invalid PE header")
        if self.u16(pe + 4) != 0x8664 or self.u16(pe + 24) != 0x20B:
            raise ValueError(f"{path.name}: Windows AMD64 PE32+ required")
        self.directories = pe + 24 + 112
        self.sections = []
        section = pe + 24 + self.u16(pe + 20)
        for index in range(self.u16(pe + 6)):
            entry = section + index * 40
            size, address, raw_size, raw = struct.unpack_from("<IIII", self.data, entry + 8)
            self.sections.append((address, max(size, raw_size), raw, raw_size))

    def u16(self, offset: int) -> int:
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self.data, offset)[0]

    def offset(self, rva: int) -> int:
        for address, size, raw, raw_size in self.sections:
            if address <= rva < address + size:
                delta = rva - address
                if delta >= raw_size:
                    break
                return raw + delta
        raise ValueError(f"{self.path.name}: unmapped RVA {rva:x}")

    def string(self, rva: int) -> str:
        offset = self.offset(rva)
        end = self.data.index(b"\0", offset)
        return self.data[offset:end].decode("ascii")

    def exports(self) -> set[str]:
        rva = self.u32(self.directories)
        if not rva:
            return set()
        table = self.offset(rva)
        count = self.u32(table + 24)
        names = self.offset(self.u32(table + 32))
        return {self.string(self.u32(names + i * 4)) for i in range(count)}

    def imports(self, library: str) -> set[str]:
        rva = self.u32(self.directories + 8)
        if not rva:
            return set()
        descriptor = self.offset(rva)
        for index in range(4096):
            entry = descriptor + index * 20
            original, _, _, name, thunk = struct.unpack_from("<IIIII", self.data, entry)
            if not any((original, name, thunk)):
                return set()
            if self.string(name).lower() != library.lower():
                continue
            cursor = self.offset(original or thunk)
            result = set()
            for i in range(65536):
                value = struct.unpack_from("<Q", self.data, cursor + i * 8)[0]
                if value == 0:
                    return result
                if value & (1 << 63):
                    raise ValueError(f"{self.path.name}: ordinal import from {library} unsupported")
                result.add(self.string(value + 2))
            break
        raise ValueError(f"{self.path.name}: unterminated import table")


def check(bundle: pathlib.Path) -> dict:
    core = PE(bundle / "bin/64bit/obs.dll")
    exports = core.exports()
    required = {"source_profiler_request_telemetry", "source_profiler_fill_result",
                "source_profiler_enable", "source_profiler_gpu_enable",
                "obs_source_get_core_memory_usage"}
    missing = sorted(required - exports)
    if missing:
        raise ValueError("SOURCE_TELEMETRY_REQUIRES_UPDATED_LIBOBS: missing " + ", ".join(missing))
    paths = [bundle / "bin/64bit/pulsar.exe", bundle / "obs-plugins/64bit/obs-websocket.dll"]
    paths += sorted((bundle / "obs-plugins/64bit").glob("pulsar-*.dll"))
    binaries = []
    for path in paths:
        image = PE(path)
        imports = image.imports("obs.dll")
        unresolved = sorted(imports - exports)
        if unresolved:
            raise ValueError(f"{path.name}: obs.dll lacks imports: {', '.join(unresolved)}")
        binaries.append({"path": str(path.relative_to(bundle)),
                         "sha256": hashlib.sha256(image.data).hexdigest(),
                         "libobs_imports_checked": len(imports)})
    return {"schema": "pulsar.source-telemetry-bundle.v1", "coherent": True,
            "obs_sha256": hashlib.sha256(core.data).hexdigest(),
            "required_exports": sorted(required), "binaries": binaries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()
    try:
        report = check(args.bundle.resolve())
    except (ValueError, OSError, struct.error, UnicodeError) as error:
        print(json.dumps({"coherent": False, "error": str(error)}))
        return 1
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
