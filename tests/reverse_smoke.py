#!/usr/bin/env python3
"""Offline known-answer smoke checks for the retained reverse toolchain."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
from typing import Sequence


ANSWER = b"r3v-0ffline"
XOR_KEY = 0x5A
REV_OK = b"REV_OK\n"
VM_OK = b"VM_OK\n"

OP_HALT = 0x00
OP_PUSH = 0x01
OP_ADD = 0x02
OP_SUB = 0x03
OP_STORE = 0x04
OP_LOAD = 0x05
OP_EMIT = 0x06
VM_PROGRAM = bytes((
    OP_PUSH, ord("V"), OP_EMIT,
    OP_PUSH, ord("M"), OP_EMIT,
    OP_PUSH, 100, OP_PUSH, 5, OP_SUB, OP_EMIT,
    OP_PUSH, 15, OP_STORE, 0,
    OP_PUSH, 64, OP_LOAD, 0, OP_ADD, OP_EMIT,
    OP_PUSH, ord("K"), OP_EMIT,
    OP_PUSH, 10, OP_EMIT,
    OP_HALT,
))

REV_SOURCE_TEMPLATE = r"""
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

const unsigned char encoded[] = { ENCODED_BYTES };

__attribute__((noinline)) void decode_secret(unsigned char *output) {
    for (size_t index = 0; index < sizeof(encoded); ++index) {
        output[index] = encoded[index] ^ 0x5a;
    }
}

__attribute__((noinline)) int check_input(const char *input, size_t length) {
    unsigned char decoded[sizeof(encoded)];
    if (length != sizeof(encoded)) {
        return 0;
    }
    decode_secret(decoded);
    return memcmp(input, decoded, sizeof(encoded)) == 0;
}

__attribute__((noinline)) void report_success(void) {
    puts("REV_OK");
}

int main(void) {
    char input[64];
    ssize_t count = read(STDIN_FILENO, input, sizeof(input));
    if (count < 0) {
        return 2;
    }
    if (check_input(input, (size_t)count)) {
        report_success();
        return 0;
    }
    fputs("REV_BAD\n", stderr);
    return 1;
}
"""

VM_SOURCE_TEMPLATE = r"""
#include <stddef.h>
#include <stdio.h>

enum {
    OP_HALT = 0x00,
    OP_PUSH = 0x01,
    OP_ADD = 0x02,
    OP_SUB = 0x03,
    OP_STORE = 0x04,
    OP_LOAD = 0x05,
    OP_EMIT = 0x06
};

const unsigned char bytecode[] = { VM_BYTES };

typedef struct {
    unsigned char stack[32];
    unsigned char registers[4];
    size_t sp;
} VM;

__attribute__((noinline)) void vm_push(VM *vm, unsigned char value) {
    vm->stack[vm->sp++] = value;
}

__attribute__((noinline)) unsigned char vm_pop(VM *vm) {
    return vm->stack[--vm->sp];
}

__attribute__((noinline)) void vm_add(VM *vm) {
    unsigned char right = vm_pop(vm);
    unsigned char left = vm_pop(vm);
    vm_push(vm, (unsigned char)(left + right));
}

__attribute__((noinline)) void vm_sub(VM *vm) {
    unsigned char right = vm_pop(vm);
    unsigned char left = vm_pop(vm);
    vm_push(vm, (unsigned char)(left - right));
}

__attribute__((noinline)) void vm_store(VM *vm, unsigned char reg) {
    vm->registers[reg] = vm_pop(vm);
}

__attribute__((noinline)) void vm_load(VM *vm, unsigned char reg) {
    vm_push(vm, vm->registers[reg]);
}

__attribute__((noinline)) void vm_emit(VM *vm) {
    putchar(vm_pop(vm));
}

__attribute__((noinline)) int vm_run(VM *vm) {
    size_t pc = 0;
    for (;;) {
        unsigned char opcode = bytecode[pc++];
        switch (opcode) {
        case OP_PUSH:
            vm_push(vm, bytecode[pc++]);
            break;
        case OP_ADD:
            vm_add(vm);
            break;
        case OP_SUB:
            vm_sub(vm);
            break;
        case OP_STORE:
            vm_store(vm, bytecode[pc++]);
            break;
        case OP_LOAD:
            vm_load(vm, bytecode[pc++]);
            break;
        case OP_EMIT:
            vm_emit(vm);
            break;
        case OP_HALT:
            return 0;
        default:
            return 2;
        }
    }
}

int main(void) {
    VM vm = {{0}};
    return vm_run(&vm);
}
"""

SOCKET_SOURCE = r"""
#include <errno.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd >= 0) {
        close(fd);
        puts("SOCKET_OPEN_OK");
        return 0;
    }
    if (errno == EPERM) {
        puts("SOCKET_EPERM");
        return 0;
    }
    perror("socket");
    return 2;
}
"""


class SmokeSkip(Exception):
    """A skip is summarized separately and always produces a nonzero exit."""


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.update({"TERM": "xterm", "DEBUGINFOD_URLS": "", "DEBUGINFOD_PROGRESS": "0"})
    return subprocess.run(
        list(args), cwd=cwd, env=env, input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
    )


def _record(
    fixtures: dict[str, Path],
    group: str,
    name: str,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    timeout: float = 15,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = _run(args, cwd=cwd, input_data=input_data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        evidence = fixtures["evidence"] / group / f"{name}.timeout.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(
            f"command={list(args)!r}\ntimeout={timeout}\n{exc!r}\n", encoding="utf-8",
        )
        raise AssertionError(f"command timed out after {timeout}s: {list(args)!r}") from exc
    directory = fixtures["evidence"] / group
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.stdout").write_bytes(result.stdout)
    (directory / f"{name}.stderr").write_bytes(result.stderr)
    (directory / f"{name}.json").write_text(json.dumps({
        "command": list(args), "cwd": str(cwd) if cwd else None,
        "timeout_seconds": timeout, "exit_code": result.returncode,
        "expected_exit_codes": list(expected),
    }, indent=2), encoding="utf-8")
    if result.returncode not in expected:
        raise AssertionError(
            f"command {list(args)!r} exited {result.returncode}, expected {expected}\n"
            f"stdout:\n{_text(result.stdout)}\nstderr:\n{_text(result.stderr)}"
        )
    return result


def _compile(source: Path, binary: Path) -> None:
    result = _run([
        "gcc", "-O0", "-g", "-fno-builtin", "-fno-pie", "-no-pie",
        "-fno-omit-frame-pointer", "-fno-stack-protector", "-Wl,-z,noexecstack",
        "-o", str(binary), str(source),
    ], cwd=source.parent, timeout=25)
    if result.returncode != 0:
        raise AssertionError(
            f"gcc failed for {source}:\n{_text(result.stdout)}\n{_text(result.stderr)}"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_fixtures(root: Path) -> dict[str, Path]:
    evidence = root / "evidence"
    evidence.mkdir()
    rev_source = root / "rev_fixture.c"
    rev = root / "rev_fixture"
    encoded = ", ".join(f"0x{byte ^ XOR_KEY:02x}" for byte in ANSWER)
    rev_source.write_text(
        REV_SOURCE_TEMPLATE.replace("ENCODED_BYTES", encoded), encoding="utf-8",
    )
    _compile(rev_source, rev)

    vm_source = root / "vm_fixture.c"
    vm = root / "vm_fixture"
    vm_bytes = ", ".join(f"0x{byte:02x}" for byte in VM_PROGRAM)
    vm_source.write_text(VM_SOURCE_TEMPLATE.replace("VM_BYTES", vm_bytes), encoding="utf-8")
    _compile(vm_source, vm)

    socket_source = root / "socket_probe.c"
    socket_probe = root / "socket_probe"
    socket_source.write_text(SOCKET_SOURCE, encoding="utf-8")
    _compile(socket_source, socket_probe)

    fixtures = {
        "evidence": evidence, "rev": rev, "rev_source": rev_source,
        "vm": vm, "vm_source": vm_source,
        "socket_probe": socket_probe, "socket_source": socket_source,
    }
    wrong = bytes((ANSWER[0] ^ 1,)) + ANSWER[1:]
    for name, data in (
        ("correct_input", ANSWER), ("wrong_input", wrong),
        ("empty_input", b""), ("boundary_input", b"A" * 65),
    ):
        path = root / f"{name}.bin"
        path.write_bytes(data)
        fixtures[name] = path
    return fixtures


def _elf_sections(path: Path) -> list[dict[str, int | str]]:
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01":
        raise AssertionError(f"{path} is not a little-endian ELF64 file")
    section_offset = struct.unpack_from("<Q", data, 0x28)[0]
    entry_size, count, names_index = struct.unpack_from("<HHH", data, 0x3A)
    if entry_size != 64 or not count or names_index >= count:
        raise AssertionError(f"invalid ELF64 section table in {path}")

    def raw_section(index: int) -> tuple[int, ...]:
        offset = section_offset + index * entry_size
        return struct.unpack_from("<IIQQQQIIQQ", data, offset)

    names_header = raw_section(names_index)
    names_data = data[names_header[4]:names_header[4] + names_header[5]]
    sections: list[dict[str, int | str]] = []
    for index in range(count):
        header = raw_section(index)
        name_end = names_data.find(b"\0", header[0])
        if name_end < 0:
            raise AssertionError(f"unterminated section name in {path}")
        name = names_data[header[0]:name_end].decode("utf-8", errors="replace")
        sections.append({
            "name": name, "type": header[1], "address": header[3],
            "offset": header[4], "size": header[5],
        })
    return sections


def _va_to_offset(path: Path, va: int, size: int) -> tuple[int, dict[str, int | str]]:
    for section in _elf_sections(path):
        address = int(section["address"])
        section_size = int(section["size"])
        if (
            int(section["type"]) != 8 and address <= va
            and va + size <= address + section_size
        ):
            return int(section["offset"]) + va - address, section
    raise AssertionError(f"VA {va:#x} size {size} has no file-backed ELF section in {path}")


def _symbol(readelf_output: str, name: str) -> tuple[int, int]:
    matches = []
    for line in readelf_output.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].rstrip(":").isdigit() and fields[7] == name:
            matches.append((int(fields[1], 16), int(fields[2])))
    if len(matches) != 1:
        raise AssertionError(f"expected one {name} symbol, got {matches!r}")
    return matches[0]


def _mapping(path: Path, name: str, va: int, size: int) -> dict[str, int | str]:
    offset, section = _va_to_offset(path, va, size)
    return {
        "symbol": name, "va": f"{va:#x}", "file_offset": f"{offset:#x}",
        "size": size, "section": str(section["name"]),
        "section_va": f"{int(section['address']):#x}",
        "section_file_offset": f"{int(section['offset']):#x}",
    }


def _function_block(disassembly: str, name: str) -> str:
    start = re.search(rf"(?m)^[0-9a-fA-F]+ <{re.escape(name)}>:\n", disassembly)
    if not start:
        raise AssertionError(f"objdump did not contain function {name}")
    next_function = re.search(r"(?m)^[0-9a-fA-F]+ <[^>]+>:\n", disassembly[start.end():])
    end = start.end() + next_function.start() if next_function else len(disassembly)
    return disassembly[start.start():end]


def _first_instruction(block: str) -> tuple[int, bytes]:
    for line in block.splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s+)+)", line)
        if match:
            return int(match.group(1), 16), bytes.fromhex(match.group(2))
    raise AssertionError(f"no instruction bytes in objdump block:\n{block}")


def _assert_rev_output(result: subprocess.CompletedProcess[bytes], expected: int) -> None:
    combined = result.stdout + result.stderr
    if expected == 0:
        assert result.stdout == REV_OK, repr(result.stdout)
        assert result.stdout.count(REV_OK.strip()) == 1, repr(result.stdout)
        assert REV_OK.strip() not in result.stderr
    else:
        assert REV_OK.strip() not in combined, repr(combined)


def _assert_rev_result(result: subprocess.CompletedProcess[bytes], expected: int) -> None:
    assert result.returncode == expected, (result.returncode, result.stdout + result.stderr)
    _assert_rev_output(result, expected)


def check_static(fixtures: dict[str, Path]) -> None:
    rev = fixtures["rev"]
    evidence = fixtures["evidence"] / "static"
    file_output = _text(_record(fixtures, "static", "file", ["file", str(rev)]).stdout)
    assert "ELF 64-bit" in file_output and "x86-64" in file_output, file_output
    assert "not stripped" in file_output, file_output
    hash_result = _record(fixtures, "static", "sha256", ["sha256sum", str(rev)])
    assert hash_result.stdout.split()[0].decode() == _sha256(rev)
    _record(fixtures, "static", "strings", ["strings", "-a", "-t", "x", str(rev)])

    header = _text(_record(fixtures, "static", "readelf-header", ["readelf", "-hW", str(rev)]).stdout)
    assert re.search(r"Type:\s+EXEC", header), header
    _record(fixtures, "static", "readelf-sections", ["readelf", "-SW", str(rev)])
    symbols_output = _text(
        _record(fixtures, "static", "readelf-symbols", ["readelf", "-sW", str(rev)]).stdout
    )
    _record(fixtures, "static", "readelf-relocations", ["readelf", "-rW", str(rev)])
    nm_output = _text(_record(fixtures, "static", "nm", ["nm", "-an", str(rev)]).stdout)
    for name in ("encoded", "decode_secret", "check_input", "report_success", "main"):
        assert re.search(rf"\s{re.escape(name)}$", nm_output, re.MULTILINE), name

    r2 = _record(
        fixtures, "static", "radare2-xrefs",
        ["r2", "-q", "-e", "scr.color=false", "-c", "aaa; axtj @ obj.encoded", str(rev)],
        timeout=35,
    )
    try:
        xrefs = json.loads(_text(r2.stdout))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"radare2 xrefs were not JSON:\n{_text(r2.stdout)}") from exc
    assert xrefs and all("from" in xref for xref in xrefs), xrefs

    disassembly = _text(
        _record(fixtures, "static", "objdump", ["objdump", "-d", "-M", "intel", str(rev)]).stdout
    )
    blocks = {
        name: _function_block(disassembly, name)
        for name in ("decode_secret", "check_input", "report_success", "main")
    }
    branch_evidence = (
        "DECODE AND CHECK (failure path):\n" + blocks["decode_secret"] + blocks["check_input"]
        + "\nSUCCESS BRANCH AND CALLER:\n" + blocks["report_success"] + blocks["main"]
    )
    (evidence / "branch-disassembly.txt").write_text(branch_evidence, encoding="utf-8")
    assert "0x5a" in blocks["decode_secret"], blocks["decode_secret"]
    assert "memcmp" in blocks["check_input"], blocks["check_input"]
    assert "<report_success>" in blocks["main"], blocks["main"]
    assert re.search(r"\bj(?:e|ne)\b", blocks["main"]), blocks["main"]

    encoded_va, encoded_size = _symbol(symbols_output, "encoded")
    assert encoded_size == len(ANSWER), encoded_size
    encoded_offset, _ = _va_to_offset(rev, encoded_va, encoded_size)
    encoded_bytes = rev.read_bytes()[encoded_offset:encoded_offset + encoded_size]
    decoded = bytes(byte ^ XOR_KEY for byte in encoded_bytes)
    assert decoded == fixtures["correct_input"].read_bytes(), (decoded, ANSWER)
    assert encoded_va != encoded_offset, "fixture did not distinguish VA from file offset"

    mappings = [_mapping(rev, "encoded", encoded_va, encoded_size)]
    for name in ("decode_secret", "check_input", "report_success", "main"):
        va, _ = _symbol(symbols_output, name)
        mappings.append(_mapping(rev, name, va, 1))
        instruction_va, instruction_bytes = _first_instruction(blocks[name])
        assert instruction_va == va, (name, instruction_va, va)
        offset, _ = _va_to_offset(rev, instruction_va, len(instruction_bytes))
        assert rev.read_bytes()[offset:offset + len(instruction_bytes)] == instruction_bytes, name
    (evidence / "va-offset.json").write_text(json.dumps({
        "xor_key": f"{XOR_KEY:#x}", "encoded_hex": encoded_bytes.hex(),
        "decoded_utf8": decoded.decode("ascii"), "mappings": mappings,
        "radare2_xrefs": xrefs,
    }, indent=2), encoding="utf-8")
    (evidence / "unknowns.txt").write_text(
        "The fixture is not stripped; no coverage claim is made for stripped binaries.\n"
        "Indirect jump targets, PLT/library internals, and non-target functions remain unknown.\n"
        "Only the named check/decode/branch functions and extracted constant are covered.\n",
        encoding="utf-8",
    )

    correct = _record(
        fixtures, "static", "correct-exec", [str(rev)],
        input_data=fixtures["correct_input"].read_bytes(), timeout=5,
    )
    wrong = _record(
        fixtures, "static", "wrong-exec", [str(rev)],
        input_data=fixtures["wrong_input"].read_bytes(), timeout=5, expected=(1,),
    )
    _assert_rev_result(correct, 0)
    _assert_rev_result(wrong, 1)
    print(
        f"INFO static: decoded={decoded.decode()} encoded_va={encoded_va:#x} "
        f"file_offset={encoded_offset:#x} xrefs={len(xrefs)}", flush=True,
    )


def check_dynamic(fixtures: dict[str, Path]) -> None:
    rev = fixtures["rev"]
    evidence = fixtures["evidence"] / "dynamic"
    for label, expected in (("correct", 0), ("wrong", 1), ("empty", 1), ("boundary", 1)):
        result = _record(
            fixtures, "dynamic", f"{label}-exec", [str(rev)],
            input_data=fixtures[f"{label}_input"].read_bytes(), timeout=5,
            expected=(expected,),
        )
        _assert_rev_result(result, expected)

    for label, expected in (("correct", 0), ("wrong", 1), ("empty", 1)):
        trace_path = evidence / f"{label}.strace"
        result = _record(
            fixtures, "dynamic", f"{label}-strace-run",
            ["strace", "-f", "-qq", "-o", str(trace_path), "-e", "trace=read,write", str(rev)],
            input_data=fixtures[f"{label}_input"].read_bytes(), timeout=10,
            expected=(expected,),
        )
        _assert_rev_result(result, expected)
    correct_trace = (evidence / "correct.strace").read_text(encoding="utf-8", errors="replace")
    wrong_trace = (evidence / "wrong.strace").read_text(encoding="utf-8", errors="replace")
    empty_trace = (evidence / "empty.strace").read_text(encoding="utf-8", errors="replace")
    assert "REV_OK" in correct_trace and "REV_BAD" not in correct_trace, correct_trace
    assert "REV_BAD" in wrong_trace and "REV_OK" not in wrong_trace, wrong_trace
    assert re.search(r"read\(0, \"\", 64\)\s+= 0", empty_trace), empty_trace

    for label, expected in (("correct", 0), ("wrong", 1), ("empty", 1)):
        trace_path = evidence / f"{label}.ltrace"
        result = _record(
            fixtures, "dynamic", f"{label}-ltrace-run",
            ["ltrace", "-f", "-o", str(trace_path), "-e", "memcmp+puts+fputs", str(rev)],
            input_data=fixtures[f"{label}_input"].read_bytes(), timeout=10,
            expected=(0,) if expected == 0 else (0, 1),
        )
        _assert_rev_output(result, expected)
    correct_ltrace = (evidence / "correct.ltrace").read_text(encoding="utf-8", errors="replace")
    wrong_ltrace = (evidence / "wrong.ltrace").read_text(encoding="utf-8", errors="replace")
    empty_ltrace = (evidence / "empty.ltrace").read_text(encoding="utf-8", errors="replace")
    assert "exited (status 0)" in correct_ltrace, correct_ltrace
    assert "exited (status 1)" in wrong_ltrace, wrong_ltrace
    assert "exited (status 1)" in empty_ltrace, empty_ltrace
    assert "->memcmp(" in correct_ltrace and "->puts(" in correct_ltrace, correct_ltrace
    assert "->memcmp(" in wrong_ltrace and "->fputs(" in wrong_ltrace, wrong_ltrace
    assert "->puts(" not in wrong_ltrace, wrong_ltrace
    assert "->memcmp(" not in empty_ltrace and "->fputs(" in empty_ltrace, empty_ltrace

    gdb_result = _record(
        fixtures, "dynamic", "gdb-check-input",
        [
            "gdb", "-q", "-nx", "-batch",
            "-ex", "set pagination off", "-ex", "set debuginfod enabled off",
            "-ex", "break check_input",
            "-ex", f"run < {fixtures['correct_input']}",
            "-ex", "info registers rdi rsi rip", "-ex", "x/11bx $rdi",
            "-ex", f"x/{len(ANSWER)}bx &encoded", "-ex", "bt", "--args", str(rev),
        ],
        cwd=rev.parent, timeout=25,
    )
    gdb_output = _text(gdb_result.stdout + gdb_result.stderr)
    assert "Breakpoint 1" in gdb_output and "check_input" in gdb_output, gdb_output
    assert re.search(rf"rsi\s+0x{len(ANSWER):x}\s+{len(ANSWER)}", gdb_output), gdb_output
    assert re.search(r"rdi\s+0x[0-9a-f]+", gdb_output), gdb_output
    assert f"0x{ANSWER[0]:02x}" in gdb_output, gdb_output
    assert f"0x{ANSWER[0] ^ XOR_KEY:02x}" in gdb_output, gdb_output

    direct_socket = _record(
        fixtures, "dynamic", "socket-positive-control", [str(fixtures["socket_probe"])], timeout=5,
    )
    assert direct_socket.stdout == b"SOCKET_OPEN_OK\n", direct_socket.stdout
    socket_trace = evidence / "socket-wrapper.strace"
    injected_socket = _record(
        fixtures, "dynamic", "socket-wrapper",
        [
            "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "TERM=xterm",
            "DEBUGINFOD_URLS=", "DEBUGINFOD_PROGRESS=0",
            "timeout", "10s", "prlimit", "--cpu=10:10", "--as=268435456:268435456",
            "--fsize=1048576:1048576", "strace", "-f", "-qq", "-o", str(socket_trace),
            "-e", "trace=socket,socketcall", "--inject=socket:error=EPERM",
            "--inject=socketcall:error=EPERM", str(fixtures["socket_probe"]),
        ],
        cwd=rev.parent, timeout=20,
    )
    assert injected_socket.stdout == b"SOCKET_EPERM\n", injected_socket.stdout
    socket_evidence = socket_trace.read_text(encoding="utf-8", errors="replace")
    assert "socket(" in socket_evidence and "EPERM" in socket_evidence, socket_evidence
    assert "INJECTED" in socket_evidence, socket_evidence
    print("INFO dynamic: 4 behavior controls, strace/ltrace differences, gdb, socket EPERM", flush=True)


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    return -sum((count / len(data)) * math.log2(count / len(data)) for count in counts if count)


def _undefined_symbols(readelf_output: str) -> list[str]:
    names = set()
    for line in readelf_output.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].rstrip(":").isdigit() and fields[6] == "UND":
            names.add(fields[7])
    return sorted(names)


def _pack_profile(path: Path, sections_output: str, imports_output: str) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01":
        raise AssertionError(f"{path} is not a little-endian ELF64 file")
    entry_size, section_count = struct.unpack_from("<HH", data, 0x3A)
    section_table_present = not (entry_size == 0 and section_count == 0)
    elf_sections = _elf_sections(path) if section_table_present else []
    sections = []
    for section in elf_sections:
        offset, size = int(section["offset"]), int(section["size"])
        section_data = b"" if int(section["type"]) == 8 else data[offset:offset + size]
        sections.append({
            "name": section["name"], "size": size, "entropy": _entropy(section_data),
        })
    assert sections_output
    return {
        "sha256": _sha256(path), "file_size": len(data), "entropy": _entropy(data),
        "section_table": "present" if section_table_present else "absent (UPX packed layout)",
        "sections": sections, "undefined_imports": _undefined_symbols(imports_output),
    }


def _interpret_vm(code: bytes) -> tuple[bytes, list[dict[str, object]], list[int]]:
    stack: list[int] = []
    registers = [0, 0, 0, 0]
    output = bytearray()
    trace: list[dict[str, object]] = []
    pc = 0

    def immediate() -> int:
        nonlocal pc
        if pc >= len(code):
            raise ValueError(f"truncated immediate at pc {pc}")
        value = code[pc]
        pc += 1
        return value

    def pop() -> int:
        if not stack:
            raise ValueError("stack underflow")
        return stack.pop()

    while True:
        if pc >= len(code):
            raise ValueError("bytecode ended without HALT")
        start = pc
        opcode = code[pc]
        pc += 1
        stack_before, registers_before = stack.copy(), registers.copy()
        emitted = None
        if opcode == OP_PUSH:
            stack.append(immediate())
            mnemonic = "PUSH"
        elif opcode == OP_ADD:
            right, left = pop(), pop()
            stack.append((left + right) & 0xFF)
            mnemonic = "ADD"
        elif opcode == OP_SUB:
            right, left = pop(), pop()
            stack.append((left - right) & 0xFF)
            mnemonic = "SUB"
        elif opcode == OP_STORE:
            reg = immediate()
            if reg >= len(registers):
                raise ValueError(f"unknown register {reg} at pc {start}")
            registers[reg] = pop()
            mnemonic = "STORE"
        elif opcode == OP_LOAD:
            reg = immediate()
            if reg >= len(registers):
                raise ValueError(f"unknown register {reg} at pc {start}")
            stack.append(registers[reg])
            mnemonic = "LOAD"
        elif opcode == OP_EMIT:
            emitted = pop()
            output.append(emitted)
            mnemonic = "EMIT"
        elif opcode == OP_HALT:
            mnemonic = "HALT"
        else:
            raise ValueError(f"unknown opcode 0x{opcode:02x} at pc {start}")
        trace.append({
            "pc": start, "opcode": f"0x{opcode:02x}", "mnemonic": mnemonic,
            "stack_before": stack_before, "stack_after": stack.copy(),
            "registers_before": registers_before, "registers_after": registers.copy(),
            "emitted": emitted,
        })
        if opcode == OP_HALT:
            return bytes(output), trace, registers


def check_pack_vm(fixtures: dict[str, Path]) -> None:
    rev = fixtures["rev"]
    evidence = fixtures["evidence"] / "pack-vm"
    upx_dir = rev.parent / "upx-work"
    upx_dir.mkdir()
    upx_input = upx_dir / "rev-original-copy"
    packed = upx_dir / "rev.packed"
    unpacked = upx_dir / "rev.unpacked"
    shutil.copy2(rev, upx_input)
    original_hash = _sha256(rev)
    copy_hash = _sha256(upx_input)
    assert original_hash == copy_hash
    _record(fixtures, "pack-vm", "upx-pack", ["upx", "-9", "-o", str(packed), str(upx_input)], timeout=45)
    _record(fixtures, "pack-vm", "upx-unpack", ["upx", "-d", "-o", str(unpacked), str(packed)], timeout=45)
    assert _sha256(rev) == original_hash, "UPX modified the original fixture"
    assert _sha256(upx_input) == copy_hash, "UPX modified its input copy"

    profiles = {}
    for label, binary in (("original", rev), ("packed", packed), ("unpacked", unpacked)):
        sections_result = _record(
            fixtures, "pack-vm", f"{label}-sections", ["readelf", "-SW", str(binary)],
        )
        imports_result = _record(
            fixtures, "pack-vm", f"{label}-imports", ["readelf", "--dyn-syms", "-W", str(binary)],
        )
        sections_output = _text(sections_result.stdout)
        imports_output = _text(imports_result.stdout)
        profiles[label] = _pack_profile(binary, sections_output, imports_output)
    original_section_names = {section["name"] for section in profiles["original"]["sections"]}
    packed_section_names = {section["name"] for section in profiles["packed"]["sections"]}
    assert original_section_names != packed_section_names, profiles
    assert profiles["original"]["entropy"] != profiles["packed"]["entropy"], profiles
    differences = {
        "packed_vs_original": {
            "section_names_changed": original_section_names != packed_section_names,
            "imports_removed": sorted(
                set(profiles["original"]["undefined_imports"])
                - set(profiles["packed"]["undefined_imports"])
            ),
            "imports_added": sorted(
                set(profiles["packed"]["undefined_imports"])
                - set(profiles["original"]["undefined_imports"])
            ),
            "entropy_delta": profiles["packed"]["entropy"] - profiles["original"]["entropy"],
        },
    }
    (evidence / "upx-profiles.json").write_text(json.dumps({
        "original_hash_before_and_after": original_hash,
        "profiles": profiles, "differences": differences,
    }, indent=2), encoding="utf-8")
    unpacked_result = _record(
        fixtures, "pack-vm", "unpacked-correct-exec", [str(unpacked)],
        input_data=fixtures["correct_input"].read_bytes(), timeout=5,
    )
    _assert_rev_result(unpacked_result, 0)
    unpacked_wrong = _record(
        fixtures, "pack-vm", "unpacked-wrong-exec", [str(unpacked)],
        input_data=fixtures["wrong_input"].read_bytes(), timeout=5, expected=(1,),
    )
    _assert_rev_result(unpacked_wrong, 1)
    assert _sha256(rev) == original_hash

    vm = fixtures["vm"]
    vm_symbols = _text(
        _record(fixtures, "pack-vm", "vm-readelf-symbols", ["readelf", "-sW", str(vm)]).stdout
    )
    _record(fixtures, "pack-vm", "vm-nm", ["nm", "-an", str(vm)])
    vm_disassembly = _text(
        _record(fixtures, "pack-vm", "vm-objdump", ["objdump", "-d", "-M", "intel", str(vm)]).stdout
    )
    handler_names = ("vm_push", "vm_pop", "vm_add", "vm_sub", "vm_store", "vm_load", "vm_emit")
    vm_run_block = _function_block(vm_disassembly, "vm_run")
    handler_blocks = [_function_block(vm_disassembly, name) for name in handler_names]
    (evidence / "vm-run.objdump").write_text(vm_run_block, encoding="utf-8")
    (evidence / "vm-handlers.objdump").write_text("\n".join(handler_blocks), encoding="utf-8")
    for name in ("vm_push", "vm_add", "vm_sub", "vm_store", "vm_load", "vm_emit"):
        assert f"<{name}>" in vm_run_block, f"vm_run does not call {name}"
    assert "<vm_pop>" in "\n".join(handler_blocks), "handlers do not use vm_pop"
    bytecode_va, bytecode_size = _symbol(vm_symbols, "bytecode")
    bytecode_offset, _ = _va_to_offset(vm, bytecode_va, bytecode_size)
    extracted_bytecode = vm.read_bytes()[bytecode_offset:bytecode_offset + bytecode_size]
    assert extracted_bytecode == VM_PROGRAM
    assert bytecode_va != bytecode_offset
    (evidence / "vm-bytecode-mapping.json").write_text(json.dumps({
        "mapping": _mapping(vm, "bytecode", bytecode_va, bytecode_size),
        "bytecode_hex": extracted_bytecode.hex(),
        "coverage_unknowns": "Only the listed handlers are covered; indirect library control flow is unknown.",
    }, indent=2), encoding="utf-8")

    c_result = _record(fixtures, "pack-vm", "vm-c-exec", [str(vm)], timeout=5)
    interpreted, trace, registers = _interpret_vm(extracted_bytecode)
    assert c_result.stdout == interpreted == VM_OK, (c_result.stdout, interpreted)
    assert interpreted.count(VM_OK.strip()) == 1
    assert c_result.stderr == b""
    assert registers == [15, 0, 0, 0], registers
    assert {step["mnemonic"] for step in trace} == {
        "PUSH", "ADD", "SUB", "STORE", "LOAD", "EMIT", "HALT",
    }
    assert trace[-1]["stack_after"] == []
    (evidence / "vm-python-trace.json").write_text(json.dumps({
        "stdout_hex": interpreted.hex(), "final_registers": registers, "steps": trace,
    }, indent=2), encoding="utf-8")
    try:
        _interpret_vm(bytes((0xFF,)))
    except ValueError as exc:
        unknown_error = str(exc)
    else:
        raise AssertionError("unknown VM opcode did not fail")
    assert "unknown opcode 0xff" in unknown_error and "pc 0" in unknown_error, unknown_error
    (evidence / "vm-unknown-opcode.txt").write_text(unknown_error + "\n", encoding="utf-8")
    print(
        f"INFO pack-vm: original_sha256={original_hash} bytecode_va={bytecode_va:#x} "
        f"file_offset={bytecode_offset:#x} vm_steps={len(trace)} unknown_opcode=ValueError",
        flush=True,
    )


def result_exit_code(failures: int, skips: int) -> int:
    if failures:
        return 1
    if skips:
        return 2
    return 0


def main() -> int:
    failures = 0
    skips = 0
    try:
        if os.geteuid() == 0:
            raise RuntimeError("reverse smoke must run as the unprivileged agent user")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.environ.update({"TERM": "xterm", "DEBUGINFOD_URLS": "", "DEBUGINFOD_PROGRESS": "0"})
        with tempfile.TemporaryDirectory(prefix="reverse-smoke-") as tmp:
            fixtures = build_fixtures(Path(tmp))
            for group, check in (
                ("static", check_static), ("dynamic", check_dynamic), ("pack-vm", check_pack_vm),
            ):
                try:
                    check(fixtures)
                except SmokeSkip as exc:
                    skips += 1
                    print(f"SKIP {group}: {exc}", flush=True)
                except Exception:
                    failures += 1
                    print(f"FAIL {group}", flush=True)
                    traceback.print_exc()
                else:
                    print(f"PASS {group}", flush=True)
    except Exception:
        traceback.print_exc()
        return 1
    return result_exit_code(failures, skips)


if __name__ == "__main__":
    sys.exit(main())
