#!/usr/bin/env python3
"""Offline known-answer recovery checks for the retained Crypto toolchain."""

import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import traceback


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MASK32 = 0xFFFFFFFF


class SmokeSkip(Exception):
    """Reserved for an explicit platform skip; missing Crypto prerequisites fail."""


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _xor(left: bytes, right: bytes) -> bytes:
    assert len(left) == len(right)
    return bytes(a ^ b for a, b in zip(left, right))


def _require_deterministic_ecb(query, probe: bytes) -> bool:
    first = query(probe)
    second = query(probe)
    if first != second:
        raise ValueError("non-deterministic ECB oracle")
    return True


def _require_padding_distinguishable(valid_query, invalid_query) -> bool:
    valid = valid_query()
    invalid = invalid_query()
    if valid is not True or invalid is not False:
        raise ValueError("CBC padding oracle is not distinguishable")
    return True


def _require_known_curve_order(curve, candidate_order: int) -> None:
    if candidate_order != int(curve.order):
        raise ValueError("generator does not have the declared order")


def _recover_cbc_padding_block(query, padding_block: bytes, padding_iv: bytes, checkpoint):
    intermediate = bytearray(16)
    recovered = bytearray(16)
    for padding_length in range(1, 17):
        position = 16 - padding_length
        crafted = bytearray(16)
        for known_position in range(position + 1, 16):
            crafted[known_position] = intermediate[known_position] ^ padding_length
        candidates = []
        for candidate in range(256):
            crafted[position] = candidate
            if query(bytes(crafted) + padding_block):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise ValueError(
                f"CBC padding byte {position} candidates were not unique: {candidates}"
            )
        intermediate[position] = candidates[0] ^ padding_length
        recovered[position] = intermediate[position] ^ padding_iv[position]
        checkpoint(16 - position, intermediate, recovered)
    return intermediate, recovered


def check_public_key(root: Path) -> None:
    from Crypto.Cipher import PKCS1_OAEP
    from Crypto.PublicKey import RSA
    from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey
    from ecdsa.ellipticcurve import INFINITY, Point, PointJacobi
    from ecdsa.util import sigdecode_string
    from fpylll import IntegerMatrix, LLL
    from sympy import gcdex, integer_nthroot, nextprime

    root.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {}

    p = int(nextprime((3 << 510) + 123456789))
    q = int(nextprime((3 << 510) + 987654321))
    n = p * q
    phi = (p - 1) * (q - 1)
    message = 0x123456789ABCDEF
    small_e = 3
    small_ciphertext = pow(message, small_e, n)
    assert small_ciphertext == message**small_e < n
    small_root, exact = integer_nthroot(small_ciphertext, small_e)
    assert exact and int(small_root) == message
    assert pow(int(small_root), small_e, n) == small_ciphertext
    evidence["rsa_small_e"] = {
        "p_hex": hex(p), "q_hex": hex(q), "n_hex": hex(n),
        "n_bits": n.bit_length(), "e": small_e, "message_hex": hex(message),
        "ciphertext_hex": hex(small_ciphertext),
        "exact_root": exact, "independent_reencryption": True,
    }

    e1, e2 = 5, 7
    assert math.gcd(e1, e2) == 1
    c1, c2 = pow(message, e1, n), pow(message, e2, n)
    s, t, common_gcd = gcdex(e1, e2)
    assert common_gcd == 1
    common_message = (pow(c1, int(s), n) * pow(c2, int(t), n)) % n
    assert common_message == message
    assert pow(common_message, e1, n) == c1 and pow(common_message, e2, n) == c2
    evidence["rsa_common_modulus"] = {
        "n_hex": hex(n), "e1": e1, "e2": e2, "bezout": [int(s), int(t)],
        "c1_hex": hex(c1), "c2_hex": hex(c2),
        "recovered_message_hex": hex(common_message), "independent_reencryption": True,
    }

    shared_p = int(nextprime((1 << 255) + 0xC0FFEE))
    shared_q1 = int(nextprime((1 << 254) + 0xBAD5EED))
    shared_q2 = int(nextprime((1 << 254) + 0xFACADE))
    n1, n2 = shared_p * shared_q1, shared_p * shared_q2
    factor = math.gcd(n1, n2)
    assert factor == shared_p
    factored_q = n1 // factor
    assert factored_q == shared_q1 and factor * factored_q == n1
    shared_phi = (factor - 1) * (factored_q - 1)
    shared_d = pow(65537, -1, shared_phi)
    shared_ciphertext = pow(message, 65537, n1)
    shared_message = pow(shared_ciphertext, shared_d, n1)
    assert shared_message == message and pow(shared_message, 65537, n1) == shared_ciphertext
    evidence["rsa_shared_factor"] = {
        "n1_hex": hex(n1), "n2_hex": hex(n2), "shared_p_hex": hex(shared_p),
        "q1_hex": hex(shared_q1), "q2_hex": hex(shared_q2),
        "ciphertext_hex": hex(shared_ciphertext), "recovered_message_hex": hex(shared_message),
        "n1_bits": n1.bit_length(), "n2_bits": n2.bit_length(),
        "gcd_bits": factor.bit_length(), "independent_decrypt": True,
    }

    wiener_d = next(
        candidate for candidate in (5, 7, 11, 13, 17, 19, 23)
        if math.gcd(candidate, phi) == 1
    )
    assert 81 * wiener_d**4 < n, "Wiener d < n^(1/4)/3 precondition is false"
    wiener_e = pow(wiener_d, -1, phi)
    assert (wiener_e * wiener_d) % phi == 1
    tool = shutil.which("RsaCtfTool")
    assert tool, "RsaCtfTool is not installed"
    public_path = root / "wiener.pub"
    public_path.write_bytes(RSA.construct((n, wiener_e)).export_key(format="PEM"))
    wiener_args = [tool, "--publickey", str(public_path), "--private", "--attack", "wiener"]
    wiener = subprocess.run(
        wiener_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=False,
    )
    (root / "wiener.stdout").write_bytes(wiener.stdout)
    (root / "wiener.stderr").write_bytes(wiener.stderr)
    cleaned_output = ANSI_RE.sub("", _text(wiener.stdout + wiener.stderr))
    key_match = re.search(
        r"-----BEGIN RSA PRIVATE KEY-----.*?-----END RSA PRIVATE KEY-----",
        cleaned_output, re.DOTALL,
    )
    assert key_match, (
        f"Wiener output did not contain an RSA private key; exit={wiener.returncode}\n"
        f"stdout:\n{_text(wiener.stdout)}\nstderr:\n{_text(wiener.stderr)}"
    )
    recovered_pem = (key_match.group(0) + "\n").encode()
    (root / "wiener-recovered.pem").write_bytes(recovered_pem)
    recovered_key = RSA.import_key(recovered_pem)
    assert recovered_key.has_private() and recovered_key.n == n
    assert recovered_key.d == wiener_d
    assert recovered_key.p * recovered_key.q == n
    wiener_plaintext = b"wiener-known-answer"
    wiener_cipher = PKCS1_OAEP.new(RSA.construct((n, wiener_e)), label=b"smoke")
    wiener_ciphertext = wiener_cipher.encrypt(wiener_plaintext)
    independent = PKCS1_OAEP.new(recovered_key, label=b"smoke").decrypt(wiener_ciphertext)
    assert independent == wiener_plaintext
    evidence["rsa_wiener"] = {
        "command": wiener_args, "tool_exit_code_recorded_not_trusted": wiener.returncode,
        "n_hex": hex(n), "e_hex": hex(wiener_e), "ciphertext_hex": wiener_ciphertext.hex(),
        "plaintext_hex": wiener_plaintext.hex(), "independent_plaintext_hex": independent.hex(),
        "n_bits": n.bit_length(), "d": wiener_d, "d_bound": "81*d^4 < n",
        "raw_stdout": "wiener.stdout", "raw_stderr": "wiener.stderr",
        "parsed_private_key": "wiener-recovered.pem", "oaep_independent_decrypt": True,
    }

    lattice = IntegerMatrix.from_matrix([[100, 0], [99, 1]])
    expected_short = (1, -1)
    assert lattice[0][0] - lattice[1][0] == expected_short[0]
    assert lattice[0][1] - lattice[1][1] == expected_short[1]
    LLL.reduction(lattice)
    reduced = [tuple(int(lattice[row, column]) for column in range(2)) for row in range(2)]
    assert expected_short in reduced or (-1, 1) in reduced, reduced
    shortest = []
    for a in range(-3, 4):
        for b in range(-3, 4):
            if not (a or b):
                continue
            vector = (100 * a + 99 * b, b)
            shortest.append((vector[0]**2 + vector[1]**2, vector))
    minimum = min(length for length, _ in shortest)
    assert {vector for length, vector in shortest if length == minimum} == {
        expected_short, (-1, 1),
    }
    evidence["fpylll"] = {
        "basis": [[100, 0], [99, 1]], "reduced": reduced,
        "known_short_vector": expected_short, "independent_bounded_verification": True,
    }

    curve = SECP256k1
    order = int(curve.order)
    generator = curve.generator

    def validate_parameters(candidate_order: int, candidate_generator: object) -> None:
        if not isinstance(candidate_order, int) or candidate_order <= 1:
            raise ValueError("invalid group order")
        _require_known_curve_order(curve, candidate_order)
        if candidate_generator == INFINITY:
            raise ValueError("invalid infinity generator")
        if not isinstance(candidate_generator, (Point, PointJacobi)):
            raise ValueError("invalid generator type")
        if not curve.curve.contains_point(candidate_generator.x(), candidate_generator.y()):
            raise ValueError("generator is off curve")
        if candidate_order * candidate_generator != INFINITY:
            raise ValueError("generator does not have the declared order")

    validate_parameters(order, generator)
    closed_parameters = []
    for label, bad_order, bad_generator in (
        ("order-one", 1, generator),
        ("off-curve-generator", order, PointJacobi(curve.curve, 1, 1, 1)),
        ("wrong-order", order - 1, generator),
        ("multiple-order", 2 * order, generator),
    ):
        try:
            validate_parameters(bad_order, bad_generator)
        except ValueError as exc:
            closed_parameters.append({"case": label, "closed": str(exc)})
        else:
            raise AssertionError(f"invalid EC parameters accepted: {label}")
    assert len(closed_parameters) == 4

    private_scalar = 0x123456789ABCDEF123456789ABCDEF
    nonce = 0xFEDCBA987654321
    signing_key = SigningKey.from_secret_exponent(private_scalar, curve=curve)
    message1, message2 = b"nonce-reuse-message-one", b"nonce-reuse-message-two"
    signature1 = signing_key.sign(message1, hashfunc=hashlib.sha256, k=nonce)
    signature2 = signing_key.sign(message2, hashfunc=hashlib.sha256, k=nonce)
    verifying_key = signing_key.verifying_key
    assert verifying_key.verify(signature1, message1, hashfunc=hashlib.sha256)
    assert verifying_key.verify(signature2, message2, hashfunc=hashlib.sha256)
    r1, s1 = sigdecode_string(signature1, order)
    r2, s2 = sigdecode_string(signature2, order)
    assert r1 == r2 and s1 != s2
    z1 = int.from_bytes(hashlib.sha256(message1).digest(), "big")
    z2 = int.from_bytes(hashlib.sha256(message2).digest(), "big")
    assert math.gcd((s1 - s2) % order, order) == 1
    recovered_nonce = ((z1 - z2) * pow(s1 - s2, -1, order)) % order
    recovered_scalar = ((s1 * recovered_nonce - z1) * pow(r1, -1, order)) % order
    assert recovered_nonce == nonce and recovered_scalar == private_scalar
    recovered_public = VerifyingKey.from_public_point(recovered_scalar * generator, curve=curve)
    assert recovered_public.verify(signature1, message1, hashfunc=hashlib.sha256)
    assert recovered_public.verify(signature2, message2, hashfunc=hashlib.sha256)
    for signature, digest, message_bytes in (
        (signature1, z1, message1), (signature2, z2, message2),
    ):
        r, s = sigdecode_string(signature, order)
        inverse = pow(s, -1, order)
        point = (digest * inverse) * generator + (r * inverse) * (
            recovered_scalar * generator
        )
        assert point != INFINITY and point.x() % order == r
    try:
        recovered_public.verify(signature1, message2, hashfunc=hashlib.sha256)
    except BadSignatureError:
        wrong_message_rejected = True
    else:
        raise AssertionError("ECDSA wrong-message negative control was accepted")
    assert wrong_message_rejected
    evidence["ecdsa_nonce_reuse"] = {
        "curve": curve.name, "order_hex": hex(order), "order_bits": order.bit_length(),
        "generator_x_hex": hex(generator.x()), "generator_y_hex": hex(generator.y()),
        "message1_hex": message1.hex(), "message2_hex": message2.hex(),
        "signature1_hex": signature1.hex(), "signature2_hex": signature2.hex(),
        "generator_order_validated": True, "same_r": True,
        "recovered_private_scalar_hex": hex(recovered_scalar),
        "two_independent_signature_verifications": True,
        "wrong_message_rejected": wrong_message_rejected,
        "invalid_parameter_negatives": closed_parameters,
    }
    _write_json(root / "public-key-evidence.json", evidence)
    print(
        "INFO public-key: RSA small-e/common-modulus/shared-factor, "
        "single Wiener attack + OAEP decrypt, fpylll short vector, ECDSA nonce reuse",
        flush=True,
    )


def check_modes_protocol(root: Path) -> None:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad

    root.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    limits: dict[str, int] = {}
    checkpoints: dict[str, int] = {}
    positive_gate_checks = {"ecb": False, "cbc": False}

    def counted(name: str, limit: int, operation):
        limits[name] = limit
        counts[name] = counts.get(name, 0) + 1
        assert counts[name] <= limit, f"{name} query budget exceeded: {counts[name]} > {limit}"
        return operation()

    key = b"0123456789abcdef"
    ecb_secret = b"ecb-known-answer"

    def ecb_encrypt(data: bytes) -> bytes:
        return AES.new(key, AES.MODE_ECB).encrypt(pad(data + ecb_secret, 16))

    nondeterministic_calls = 0

    def nondeterministic_ecb(data: bytes) -> bytes:
        nonlocal nondeterministic_calls
        nondeterministic_calls += 1
        varying = bytes((nondeterministic_calls,))
        return AES.new(key, AES.MODE_ECB).encrypt(pad(varying + data + ecb_secret, 16))

    try:
        _require_deterministic_ecb(
            lambda probe: counted(
                "ecb-nondeterministic-negative", 2,
                lambda: nondeterministic_ecb(probe),
            ),
            b"same",
        )
    except ValueError as exc:
        nondeterministic_closed = f"{exc}; recovery closed without candidates"
    else:
        raise AssertionError("non-deterministic ECB oracle passed the recovery gate")
    assert counts["ecb-nondeterministic-negative"] == 2
    assert counts.get("ecb-recovery", 0) == 0
    assert "ecb" not in checkpoints

    positive_gate_checks["ecb"] = _require_deterministic_ecb(
        lambda probe: counted("ecb-determinism", 2, lambda: ecb_encrypt(probe)),
        b"probe",
    )
    assert positive_gate_checks["ecb"]
    baseline_length = len(counted("ecb-discovery", 64, lambda: ecb_encrypt(b"")))
    block_size = 0
    secret_length = -1
    for input_length in range(1, 33):
        length = len(counted(
            "ecb-discovery", 64, lambda input_length=input_length: ecb_encrypt(
                b"A" * input_length
            ),
        ))
        if length > baseline_length:
            block_size = length - baseline_length
            secret_length = baseline_length - input_length
            break
    assert block_size == 16 and secret_length == len(ecb_secret)
    recovery_budget = 257 * secret_length
    recovered_ecb = bytearray()
    for index in range(secret_length):
        prefix = b"A" * (block_size - 1 - index % block_size)
        block_index = index // block_size
        baseline = counted("ecb-recovery", recovery_budget, lambda: ecb_encrypt(prefix))
        target = baseline[block_index * block_size:(block_index + 1) * block_size]
        candidates = []
        for candidate in range(256):
            probe = prefix + bytes(recovered_ecb) + bytes((candidate,))
            ciphertext = counted(
                "ecb-recovery", recovery_budget, lambda probe=probe: ecb_encrypt(probe),
            )
            if ciphertext[block_index * block_size:(block_index + 1) * block_size] == target:
                candidates.append(candidate)
        assert len(candidates) == 1, f"ECB byte {index} candidates were not unique: {candidates}"
        recovered_ecb.append(candidates[0])
        checkpoints["ecb"] = index + 1
        _write_json(root / "ecb-checkpoint.json", {
            "recovered_bytes": len(recovered_ecb), "recovered_hex": recovered_ecb.hex(),
            "unique_candidate": candidates[0], "queries": counts["ecb-recovery"],
            "query_budget": recovery_budget,
        })
    assert counts["ecb-recovery"] == recovery_budget
    assert bytes(recovered_ecb) == ecb_secret
    ecb_ciphertext = AES.new(key, AES.MODE_ECB).encrypt(pad(ecb_secret, 16))
    independent_ecb = unpad(AES.new(key, AES.MODE_ECB).decrypt(ecb_ciphertext), 16)
    assert independent_ecb == ecb_secret

    bitflip_plaintext = b"A" * 16 + b"admin=0;userdata"
    bitflip_iv = b"iv-0123456789abcdef"[:16]
    assert len(bitflip_iv) == 16
    bitflip_ciphertext = AES.new(key, AES.MODE_CBC, bitflip_iv).encrypt(
        pad(bitflip_plaintext, 16)
    )
    forged = bytearray(bitflip_ciphertext)
    target_offset = 16 + len("admin=")
    forged[target_offset - 16] ^= ord("0") ^ ord("1")

    def bitflip_decrypt() -> bytes:
        return unpad(AES.new(key, AES.MODE_CBC, bitflip_iv).decrypt(bytes(forged)), 16)

    forged_plaintext = counted("cbc-bitflip-decrypt", 1, bitflip_decrypt)
    assert forged_plaintext[16:] == b"admin=1;userdata"
    assert forged_plaintext[:16] != bitflip_plaintext[:16]

    padding_iv = b"pad-iv-0123456789"[:16]
    padding_plaintext = b"padding-known-!"
    padding_padded = pad(padding_plaintext, 16)
    assert len(padding_iv) == len(padding_padded) == 16
    padding_block = AES.new(key, AES.MODE_CBC, padding_iv).encrypt(padding_padded)
    padding_ciphertext = padding_iv + padding_block

    def padding_valid(ciphertext: bytes) -> bool:
        try:
            unpad(AES.new(key, AES.MODE_CBC, ciphertext[:16]).decrypt(ciphertext[16:]), 16)
        except ValueError:
            return False
        return True

    invalid_padding = bytearray(padding_ciphertext)
    invalid_padding[-1] ^= 1

    def uniform_padding_oracle(ciphertext: bytes) -> bool:
        AES.new(key, AES.MODE_CBC, ciphertext[:16]).decrypt(ciphertext[16:])
        return False

    try:
        _require_padding_distinguishable(
            lambda: counted(
                "cbc-uniform-negative", 2,
                lambda: uniform_padding_oracle(padding_ciphertext),
            ),
            lambda: counted(
                "cbc-uniform-negative", 2,
                lambda: uniform_padding_oracle(bytes(invalid_padding)),
            ),
        )
    except ValueError as exc:
        uniform_closed = f"{exc}; recovery closed without candidates"
    else:
        raise AssertionError("uniform CBC oracle passed the recovery gate")
    assert counts["cbc-uniform-negative"] == 2
    assert counts.get("cbc-padding", 0) == 0
    assert "cbc-padding" not in checkpoints

    positive_gate_checks["cbc"] = _require_padding_distinguishable(
        lambda: counted(
            "cbc-padding", 4097, lambda: padding_valid(padding_ciphertext)
        ),
        lambda: counted(
            "cbc-invalid-padding-negative", 1,
            lambda: padding_valid(bytes(invalid_padding)),
        ),
    )
    assert positive_gate_checks["cbc"]
    assert counts["cbc-invalid-padding-negative"] == 1

    def padding_checkpoint(recovered_count, intermediate_value, recovered_value):
        checkpoints["cbc-padding"] = recovered_count
        _write_json(root / "cbc-padding-checkpoint.json", {
            "recovered_bytes": recovered_count, "intermediate_hex": intermediate_value.hex(),
            "recovered_hex": recovered_value.hex(), "queries": counts["cbc-padding"],
            "query_budget": 4097,
        })

    intermediate, recovered_padding = _recover_cbc_padding_block(
        lambda probe: counted("cbc-padding", 4097, lambda: padding_valid(probe)),
        padding_block, padding_iv, padding_checkpoint,
    )
    assert counts["cbc-padding"] == 4097
    assert counts["cbc-padding"] + counts["cbc-invalid-padding-negative"] == 4098
    assert bytes(recovered_padding) == padding_padded
    assert unpad(bytes(recovered_padding), 16) == padding_plaintext
    independent_padding = unpad(
        AES.new(key, AES.MODE_CBC, padding_iv).decrypt(padding_block), 16,
    )
    assert independent_padding == padding_plaintext

    ambiguous_plaintext = b"ambiguous-pad!"
    ambiguous_padded = pad(ambiguous_plaintext, 16)
    assert len(ambiguous_plaintext) == 14 and ambiguous_padded[-2:] == b"\x02\x02"
    ambiguous_iv = bytes(16)
    ambiguous_block = AES.new(key, AES.MODE_CBC, ambiguous_iv).encrypt(ambiguous_padded)
    ambiguous_valid_candidates = []
    ambiguous_checkpoints = []
    ambiguous_recovery = None

    def ambiguous_padding_query(probe):
        valid = counted(
            "cbc-ambiguous-padding-negative", 256, lambda: padding_valid(probe),
        )
        if valid:
            ambiguous_valid_candidates.append(probe[15])
        return valid

    try:
        ambiguous_recovery = _recover_cbc_padding_block(
            ambiguous_padding_query, ambiguous_block, ambiguous_iv,
            lambda *args: ambiguous_checkpoints.append(args),
        )
    except ValueError as exc:
        ambiguous_closed = str(exc)
        ambiguous_queries_at_closure = counts["cbc-ambiguous-padding-negative"]
    else:
        raise AssertionError("ambiguous first-byte CBC padding candidates were not closed")
    assert counts["cbc-ambiguous-padding-negative"] == 256
    assert len(ambiguous_valid_candidates) >= 2
    assert ambiguous_recovery is None and ambiguous_checkpoints == []
    assert "not unique" in ambiguous_closed
    ambiguous_queries_after_closure = (
        counts["cbc-ambiguous-padding-negative"] - ambiguous_queries_at_closure
    )
    assert ambiguous_queries_after_closure == 0

    nonce = b"nonce-01"
    ctr_known = b"known CTR plaintext 1234"
    ctr_secret = b"secret CTR target 567890"
    assert len(ctr_known) == len(ctr_secret) == 24
    ctr_ciphertext_known = AES.new(key, AES.MODE_CTR, nonce=nonce, initial_value=0).encrypt(
        ctr_known
    )
    ctr_ciphertext_secret = AES.new(key, AES.MODE_CTR, nonce=nonce, initial_value=0).encrypt(
        ctr_secret
    )
    ctr_recovered = _xor(_xor(ctr_ciphertext_known, ctr_ciphertext_secret), ctr_known)
    assert ctr_recovered == ctr_secret
    ctr_stream = AES.new(key, AES.MODE_CTR, nonce=nonce, initial_value=0).encrypt(
        bytes(len(ctr_secret))
    )
    assert _xor(ctr_ciphertext_secret, ctr_stream) == ctr_secret
    different_nonce_stream = AES.new(
        key, AES.MODE_CTR, nonce=b"nonce-02", initial_value=0,
    ).encrypt(bytes(len(ctr_secret)))
    assert _xor(ctr_ciphertext_secret, different_nonce_stream) != ctr_secret

    gcm = AES.new(key, AES.MODE_GCM, nonce=nonce)
    gcm_ciphertext, gcm_tag = gcm.encrypt_and_digest(b"integrity boundary")
    tampered_gcm = bytes((gcm_ciphertext[0] ^ 1,)) + gcm_ciphertext[1:]
    try:
        AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(tampered_gcm, gcm_tag)
    except ValueError:
        gcm_tamper_rejected = True
    else:
        raise AssertionError("GCM tamper negative control was accepted")

    p, q, generator = 23, 11, 2

    def is_prime(value: int) -> bool:
        return value >= 2 and all(value % divisor for divisor in range(2, math.isqrt(value) + 1))

    def validate_dh(modulus: int, subgroup_order: int, base: int, public: int | None = None) -> None:
        if not is_prime(modulus) or not is_prime(subgroup_order) or modulus != 2 * subgroup_order + 1:
            raise ValueError("invalid DH modulus/subgroup order")
        if not 1 < base < modulus - 1 or pow(base, subgroup_order, modulus) != 1:
            raise ValueError("invalid DH generator/subgroup")
        if public is not None and (
            not 1 < public < modulus - 1 or pow(public, subgroup_order, modulus) != 1
        ):
            raise ValueError("invalid DH public value/subgroup membership")

    dh_private_a, dh_private_b = 3, 4
    dh_public_a = pow(generator, dh_private_a, p)
    dh_public_b = pow(generator, dh_private_b, p)
    validate_dh(p, q, generator, dh_public_a)
    validate_dh(p, q, generator, dh_public_b)
    dlog_candidates = [candidate for candidate in range(q) if pow(generator, candidate, p) == dh_public_a]
    assert dlog_candidates == [dh_private_a]
    dh_shared = pow(dh_public_b, dlog_candidates[0], p)
    assert dh_shared == pow(dh_public_a, dh_private_b, p) == pow(
        generator, dh_private_a * dh_private_b, p,
    )
    dh_key = hashlib.sha256(dh_shared.to_bytes(1, "big")).digest()[:16]
    dh_roundtrip = AES.new(dh_key, AES.MODE_ECB).decrypt(
        AES.new(dh_key, AES.MODE_ECB).encrypt(pad(b"dh-kdf-roundtrip", 16))
    )
    assert unpad(dh_roundtrip, 16) == b"dh-kdf-roundtrip"
    dh_negatives = []
    for label, parameters in (
        ("non-prime-modulus", (24, 11, generator, None)),
        ("non-prime-order", (23, 10, generator, None)),
        ("trivial-generator", (23, 11, 1, None)),
        ("public-out-of-range", (23, 11, generator, 0)),
        ("public-outside-subgroup", (23, 11, generator, 22)),
    ):
        try:
            validate_dh(*parameters)
        except ValueError as exc:
            dh_negatives.append({"case": label, "closed": str(exc)})
        else:
            raise AssertionError(f"invalid DH parameters accepted: {label}")
    assert len(dh_negatives) == 5

    assert positive_gate_checks == {"ecb": True, "cbc": True}
    _write_json(root / "modes-protocol-evidence.json", {
        "oracle_query_counts": counts, "oracle_query_limits": limits,
        "checkpoints": checkpoints,
        "fixture_parameters": {
            "aes_key_hex": key.hex(), "ecb_secret_hex": ecb_secret.hex(),
            "ecb_ciphertext_hex": ecb_ciphertext.hex(),
            "bitflip_iv_hex": bitflip_iv.hex(), "bitflip_plaintext_hex": bitflip_plaintext.hex(),
            "bitflip_ciphertext_hex": bitflip_ciphertext.hex(),
            "forged_ciphertext_hex": forged.hex(), "forged_plaintext_hex": forged_plaintext.hex(),
            "padding_iv_hex": padding_iv.hex(), "padding_padded_hex": padding_padded.hex(),
            "padding_ciphertext_hex": padding_ciphertext.hex(),
            "invalid_padding_ciphertext_hex": invalid_padding.hex(),
            "ambiguous_padding_padded_hex": ambiguous_padded.hex(),
            "ambiguous_padding_ciphertext_hex": (ambiguous_iv + ambiguous_block).hex(),
            "ctr_nonce_hex": nonce.hex(), "ctr_known_hex": ctr_known.hex(),
            "ctr_secret_hex": ctr_secret.hex(),
            "ctr_ciphertext_known_hex": ctr_ciphertext_known.hex(),
            "ctr_ciphertext_secret_hex": ctr_ciphertext_secret.hex(),
            "gcm_ciphertext_hex": gcm_ciphertext.hex(), "gcm_tag_hex": gcm_tag.hex(),
            "dh_public_a": dh_public_a, "dh_public_b": dh_public_b, "dh_shared": dh_shared,
        },
        "ecb": {
            "secret_length": secret_length, "block_size": block_size,
            "recovery_formula": "257*L", "recovered_hex": recovered_ecb.hex(),
            "unique_candidate_each_round": True, "independent_decrypt": True,
            "positive_determinism_gate_invoked": positive_gate_checks["ecb"],
            "negative_gate_recovery_queries": 0,
            "nondeterministic_negative": nondeterministic_closed,
        },
        "cbc": {
            "bitflip_admin": "0 -> 1", "padding_recovered_hex": recovered_padding.hex(),
            "padding_recovery_formula": "2 + 4096B; unique candidates and immediate closure on ambiguity",
            "padding_recovery_counter_queries": counts["cbc-padding"],
            "invalid_padding_observation_queries": counts["cbc-invalid-padding-negative"],
            "aggregate_queries": counts["cbc-padding"] + counts["cbc-invalid-padding-negative"],
            "disambiguation_queries": 0,
            "future_disambiguation_formula": "2 + 4096B + D; bounded D declared separately",
            "independent_decrypt": True,
            "positive_distinguishability_gate_invoked": positive_gate_checks["cbc"],
            "negative_gate_recovery_queries": 0,
            "uniform_error_negative": uniform_closed, "invalid_padding_rejected": True,
            "ambiguous_first_byte_negative": {
                "valid_candidates": ambiguous_valid_candidates,
                "selected_candidate": None, "closed": ambiguous_closed,
                "checkpoints": ambiguous_checkpoints,
                "queries_at_closure": ambiguous_queries_at_closure,
                "recovery_queries_after_closure": ambiguous_queries_after_closure,
            },
        },
        "ctr": {
            "nonce_reused": True, "known_plaintext_recovery": True,
            "independent_keystream_replay": True, "different_nonce_negative_closed": True,
            "authentication_bypass_claimed": False,
        },
        "ghash_boundary": {
            "gcm_tamper_rejected": gcm_tamper_rejected,
            "ghash_key_or_tag_recovery_attempted": False,
            "note": "CTR ciphertext XOR is confidentiality recovery only, not authentication bypass",
        },
        "dh": {
            "p": p, "q": q, "g": generator, "bounded_dlog_candidates": q,
            "unique_private_candidate": dlog_candidates[0], "kdf_aes_roundtrip": True,
            "invalid_parameter_negatives": dh_negatives,
        },
    })
    print(
        f"INFO modes-protocol: ECB={counts['ecb-recovery']}/{recovery_budget} queries; "
        f"CBC padding={counts['cbc-padding']} recovery + 1 invalid = 4098 total; "
        f"first-byte candidates={ambiguous_valid_candidates} closed without selection; "
        "CTR confidentiality only; DH=11 bounded candidates; negatives closed",
        flush=True,
    )


def _untemper(value: int) -> int:
    def undo_right(output: int, shift: int) -> int:
        result = output
        for _ in range(32 // shift + 1):
            result = output ^ (result >> shift)
        return result & MASK32

    def undo_left(output: int, shift: int, mask: int) -> int:
        result = output
        for _ in range(32 // shift + 1):
            result = output ^ ((result << shift) & mask)
        return result & MASK32

    value = undo_right(value, 18)
    value = undo_left(value, 15, 0xEFC60000)
    value = undo_left(value, 7, 0x9D2C5680)
    return undo_right(value, 11)


def check_prng(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    mt_seed = 0x5EED1234
    mt_source = random.Random(mt_seed)
    mt_observed = [mt_source.getrandbits(32) for _ in range(632)]
    mt_encoded = b"".join(value.to_bytes(4, "big") for value in mt_observed)
    mt_decoded = [
        int.from_bytes(mt_encoded[offset:offset + 4], "big")
        for offset in range(0, len(mt_encoded), 4)
    ]
    assert mt_decoded == mt_observed

    def recover_mt(outputs: list[int]) -> random.Random:
        if len(outputs) != 624:
            raise ValueError("exactly 624 full 32-bit MT outputs required; partial state closed")
        assert all(0 <= value <= MASK32 for value in outputs)
        state = [_untemper(value) for value in outputs]
        clone = random.Random()
        clone.setstate((3, tuple(state + [624]), None))
        return clone

    mt_clone = recover_mt(mt_decoded[:624])
    mt_prediction = [mt_clone.getrandbits(32) for _ in range(8)]
    assert mt_prediction == mt_observed[624:632]
    try:
        recover_mt(mt_decoded[:623])
    except ValueError as exc:
        mt_partial_closed = str(exc)
    else:
        raise AssertionError("623 partial MT outputs were treated as a complete state")

    modulus = 2**31 - 1
    lcg_a, lcg_c, lcg_seed = 1103515245, 12345, 0x12345678

    def lcg_next(value: int) -> int:
        return (lcg_a * value + lcg_c) % modulus

    lcg_observed = [lcg_seed]
    for _ in range(7):
        lcg_observed.append(lcg_next(lcg_observed[-1]))
    x0, x1, x2 = lcg_observed[:3]
    denominator = (x1 - x0) % modulus
    assert math.gcd(denominator, modulus) == 1
    recovered_a = ((x2 - x1) * pow(denominator, -1, modulus)) % modulus
    recovered_c = (x1 - recovered_a * x0) % modulus
    assert (recovered_a, recovered_c) == (lcg_a, lcg_c)
    lcg_replay = [x0]
    for _ in range(7):
        lcg_replay.append((recovered_a * lcg_replay[-1] + recovered_c) % modulus)
    assert lcg_replay == lcg_observed
    assert lcg_replay[3:] == lcg_observed[3:]

    xorshift_seed = 0xA5A5F00D

    def xorshift32(value: int) -> int:
        value ^= (value << 13) & MASK32
        value ^= value >> 17
        value ^= (value << 5) & MASK32
        return value & MASK32

    xorshift_observed = [xorshift_seed]
    for _ in range(6):
        xorshift_observed.append(xorshift32(xorshift_observed[-1]))
    xorshift_replay = [xorshift_observed[2]]
    for _ in range(4):
        xorshift_replay.append(xorshift32(xorshift_replay[-1]))
    assert xorshift_replay == xorshift_observed[2:]
    independent_xorshift = [xorshift_seed]
    for _ in range(6):
        independent_xorshift.append(xorshift32(independent_xorshift[-1]))
    assert independent_xorshift == xorshift_observed

    timestamp_base = 1_700_000_000
    timestamp_candidates = range(timestamp_base, timestamp_base + 3)
    pid_candidates = range(100, 103)

    def weak_seed(timestamp: int, pid: int) -> int:
        return timestamp ^ (pid << 16)

    true_timestamp, true_pid = timestamp_base + 1, 102
    weak_source = random.Random(weak_seed(true_timestamp, true_pid))
    weak_observed = [weak_source.getrandbits(32) for _ in range(2)]
    weak_held_out = weak_source.getrandbits(32)
    weak_matches = []
    tested_candidates = 0
    for timestamp in timestamp_candidates:
        for pid in pid_candidates:
            tested_candidates += 1
            candidate = random.Random(weak_seed(timestamp, pid))
            if [candidate.getrandbits(32) for _ in range(2)] == weak_observed:
                weak_matches.append((timestamp, pid))
    assert weak_matches == [(true_timestamp, true_pid)]
    assert tested_candidates == len(timestamp_candidates) * len(pid_candidates) == 9
    selected_timestamp, selected_pid = weak_matches[0]
    selected_candidate = random.Random(weak_seed(selected_timestamp, selected_pid))
    selected_replay = [selected_candidate.getrandbits(32) for _ in range(2)]
    assert selected_replay == weak_observed
    weak_predicted = selected_candidate.getrandbits(32)
    assert weak_predicted == weak_held_out

    outside_source = random.Random(weak_seed(timestamp_base + 100, 999))
    outside_observed = [outside_source.getrandbits(32) for _ in range(2)]
    no_hit_matches = []
    no_hit_tested = 0
    for timestamp in timestamp_candidates:
        for pid in pid_candidates:
            no_hit_tested += 1
            candidate = random.Random(weak_seed(timestamp, pid))
            if [candidate.getrandbits(32) for _ in range(2)] == outside_observed:
                no_hit_matches.append((timestamp, pid))
    assert no_hit_matches == [] and no_hit_tested == 9
    no_hit_closed = "no time/PID candidate matched; window not expanded"

    _write_json(root / "prng-evidence.json", {
        "mt19937": {
            "known_seed_hex": hex(mt_seed), "encoded_outputs_hex": mt_encoded.hex(),
            "prediction_hex": [hex(value) for value in mt_prediction],
            "expected_prediction_hex": [hex(value) for value in mt_observed[624:632]],
            "bit_width": 32, "byte_order": "big", "observations_for_state": 624,
            "verification_outputs": 8, "untemper": True, "state_index": 624,
            "prediction_matches": True, "partial_output_negative": mt_partial_closed,
        },
        "lcg": {
            "known_seed": lcg_seed, "observed_outputs": lcg_observed,
            "replayed_outputs": lcg_replay,
            "modulus": modulus, "observations_for_parameters": 3,
            "recovered_a": recovered_a, "recovered_c": recovered_c,
            "independent_replay_outputs": len(lcg_replay), "prediction_matches": True,
        },
        "xorshift32": {
            "known_seed_hex": hex(xorshift_seed),
            "observed_outputs_hex": [hex(value) for value in xorshift_observed],
            "replayed_outputs_hex": [hex(value) for value in xorshift_replay],
            "bit_width": 32, "shifts": [13, 17, 5], "state_words": 1,
            "replay_start_observation": 3, "prediction_outputs": 4,
            "independent_replay": True,
        },
        "time_pid": {
            "observed_outputs_hex": [hex(value) for value in weak_observed],
            "no_hit_observed_outputs_hex": [hex(value) for value in outside_observed],
            "timestamp_window": [timestamp_candidates.start, timestamp_candidates.stop - 1],
            "pid_window": [pid_candidates.start, pid_candidates.stop - 1],
            "candidate_formula": "|T| * |PID|", "tested_candidates": tested_candidates,
            "unique_match": list(weak_matches[0]), "observations_for_selection": 2,
            "selection_observations_replayed": selected_replay == weak_observed,
            "held_out_expected_hex": hex(weak_held_out),
            "held_out_predicted_hex": hex(weak_predicted),
            "prediction_verified": weak_predicted == weak_held_out,
            "no_hit_candidates_tested": no_hit_tested, "no_hit_negative": no_hit_closed,
        },
        "csprng_prediction_attempted": False,
    })
    print(
        "INFO prng: MT 624x32-bit big-endian + 8 predictions; LCG 3 observations + replay; "
        "xorshift32 replay; time/PID 9 candidates + held-out prediction, no-hit closed; "
        "no CSPRNG prediction",
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
            raise RuntimeError("crypto smoke must run as the unprivileged agent user")
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with tempfile.TemporaryDirectory(prefix="crypto-smoke-") as tmp:
            base = Path(tmp)
            for group, check in (
                ("public-key", check_public_key),
                ("modes-protocol", check_modes_protocol),
                ("prng", check_prng),
            ):
                try:
                    check(base / group)
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
