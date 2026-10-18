"""Pure unit tests: no database, Redis or chain required."""

import json
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from eth_abi import encode
from eth_utils import keccak
from pydantic import SecretStr, ValidationError

from app.auth.tokens import decode_token, issue_token
from app.chain.client import PlausibilityParams
from app.chain.eip712 import ReadingData, derive_reading_id, recover_reading_signer
from app.chain.revert import RevertDecoder
from app.core.config import DEV_JWT_SECRET, ConfigError, Settings, assert_distinct_role_keys
from app.main import create_app
from app.services.meters import derive_meter_id, to_e6

REPO = Path(__file__).resolve().parents[4]
VECTORS = json.loads((REPO / "packages/shared/vectors/eip712.json").read_text())

K1 = "0x" + "01" * 32
K2 = "0x" + "02" * 32
K3 = "0x" + "03" * 32
K4 = "0x" + "04" * 32


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------------- role key separation
def test_distinct_role_keys_boot() -> None:
    s = settings(
        oracle_private_key=SecretStr(K1),
        registrar_private_key=SecretStr(K2),
        relayer_private_key=SecretStr(K3),
        pricer_private_key=SecretStr(K4),
    )
    assert len(set(s.role_key_addresses().values())) == 4


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("oracle_private_key", "registrar_private_key"),
        ("oracle_private_key", "relayer_private_key"),
        ("oracle_private_key", "pricer_private_key"),
        ("registrar_private_key", "relayer_private_key"),
        ("registrar_private_key", "pricer_private_key"),
        ("relayer_private_key", "pricer_private_key"),
    ],
)
def test_any_two_role_keys_sharing_an_address_refuses_to_boot(a: str, b: str) -> None:
    with pytest.raises(ConfigError, match="role keys must be distinct"):
        settings(**{a: SecretStr(K1), b: SecretStr(K1)})


def test_shared_key_error_names_both_roles_and_is_checked_again_at_app_start() -> None:
    s = settings(oracle_private_key=SecretStr(K1), registrar_private_key=SecretStr(K2))
    object.__setattr__(
        s, "registrar_private_key", SecretStr(K1)
    )  # bypass validation: defense in depth
    with pytest.raises(ConfigError, match="oracle and registrar"):
        assert_distinct_role_keys(s)
    with pytest.raises(ConfigError):
        create_app(s)  # create_app re-checks, so a mutated config still cannot boot


def test_invalid_role_key_rejected() -> None:
    with pytest.raises((ConfigError, ValidationError)):
        settings(oracle_private_key=SecretStr("not-a-key"))


def test_production_requires_a_strong_jwt_secret() -> None:
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        settings(api_env="production")
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        settings(api_env="production", jwt_secret=SecretStr("short"))
    assert settings(api_env="production", jwt_secret=SecretStr("x" * 40)).api_env == "production"
    assert settings().jwt_secret.get_secret_value() == DEV_JWT_SECRET  # dev default only


def test_defaults_match_the_spec() -> None:
    s = settings()
    assert s.jwt_ttl_seconds == 12 * 3600
    assert s.session_cookie_secure is True
    assert s.reading_interval_seconds == 900


# ------------------------------------------------------------------------------------ JWT
def test_token_roundtrip_and_claims() -> None:
    s = settings()
    token, claims = issue_token(s, "0xABCDEF0000000000000000000000000000000001")
    decoded = decode_token(s, token)
    assert decoded == claims
    assert claims.address == claims.address.lower()
    assert claims.expires_at - int(time.time()) in range(43_195, 43_201)


def test_token_rejections() -> None:
    s = settings()
    token, claims = issue_token(s, "0x" + "ab" * 20)
    payload = {"iss": "wattswap-api", "sub": claims.address, "jti": "j", "iat": 1, "exp": 2**31}
    secret = s.jwt_secret.get_secret_value()
    assert decode_token(s, token + "x") is None
    assert decode_token(s, "") is None
    assert decode_token(settings(jwt_secret=SecretStr("y" * 40)), token) is None
    assert decode_token(s, jwt.encode({**payload, "exp": 5}, secret, algorithm="HS256")) is None
    assert (
        decode_token(s, jwt.encode({**payload, "iss": "someone-else"}, secret, algorithm="HS256"))
        is None
    )
    assert decode_token(s, jwt.encode(payload, secret, algorithm="HS512")) is None
    no_jti = {k: v for k, v in payload.items() if k != "jti"}
    assert decode_token(s, jwt.encode(no_jti, secret, algorithm="HS256")) is None
    assert decode_token(s, jwt.encode(payload, None, algorithm="none")) is None


# ------------------------------------------------------------------------ revert decoding
ERR_ABI: list[dict[str, Any]] = [
    {
        "type": "error",
        "name": "MeterInactive",
        "inputs": [{"name": "meterId", "type": "bytes32"}],
    },
    {
        "type": "error",
        "name": "ReadingImplausible",
        "inputs": [{"name": "amountWh", "type": "uint256"}, {"name": "maxWh", "type": "uint256"}],
    },
    {"type": "error", "name": "ZeroAmount", "inputs": []},
    {"type": "error", "name": "ReadingNotComplete", "inputs": [{"name": "end", "type": "uint256"}]},
    {"type": "function", "name": "ignored", "inputs": []},
]


def selector(sig: str) -> bytes:
    return bytes(keccak(text=sig)[:4])


def test_revert_decoder() -> None:
    d = RevertDecoder([ERR_ABI])
    mid = bytes.fromhex("ab" * 32)
    info = d.decode(selector("MeterInactive(bytes32)") + encode(["bytes32"], [mid]))
    assert (
        info is not None
        and info.name == "MeterInactive"
        and info.args == {"meterId": "0x" + "ab" * 32}
    )
    assert str(info) == f"MeterInactive(meterId=0x{'ab' * 32})" and not info.transient

    imp = d.decode(
        selector("ReadingImplausible(uint256,uint256)") + encode(["uint256"] * 2, [1501, 1500])
    )
    assert str(imp) == "ReadingImplausible(amountWh=1501, maxWh=1500)"

    assert str(d.decode(selector("ZeroAmount()"))) == "ZeroAmount"
    late = d.decode(selector("ReadingNotComplete(uint256)") + encode(["uint256"], [5]))
    assert late is not None and late.transient

    std = d.decode("0x08c379a0" + encode(["string"], ["boom"]).hex())
    assert std is not None and str(std) == "Error(message=boom)"
    panic = d.decode("0x4e487b71" + encode(["uint256"], [0x11]).hex())
    assert panic is not None and panic.args == {"code": "0x11"}


def test_revert_decoder_never_raises() -> None:
    d = RevertDecoder([ERR_ABI])
    assert d.decode(None) is None
    assert d.decode("0x") is None
    assert d.decode("0x1234") is None
    unknown = d.decode("0xdeadbeef")
    assert unknown is not None and unknown.name == "UnknownError"
    truncated = d.decode(selector("ReadingImplausible(uint256,uint256)") + b"\x01")
    assert truncated is not None and truncated.name == "UndecodableRevert"


# --------------------------------------------------------------------------------- EIP-712
def test_python_recovers_the_signer_of_the_solidity_vector() -> None:
    """The shared vector was produced by Foundry from the real EnergyToken contract."""
    v = next(x for x in VECTORS["vectors"] if x["name"] == "meter_reading")
    m, dom = v["typedData"]["message"], v["typedData"]["domain"]
    data = ReadingData(
        m["meterId"], m["readingId"], m["amountWh"], m["intervalStart"], m["intervalSeconds"]
    )
    token = dom["verifyingContract"]
    assert recover_reading_signer(dom["chainId"], token, data, v["signature"]) == v["signer"]
    tampered = ReadingData(
        data.meter_id,
        data.reading_id,
        data.amount_wh + 1,
        data.interval_start,
        data.interval_seconds,
    )
    assert recover_reading_signer(dom["chainId"], token, tampered, v["signature"]) != v["signer"]
    assert recover_reading_signer(1, token, data, v["signature"]) != v["signer"]  # chain-bound


def test_malformed_signatures_recover_to_none() -> None:
    d = ReadingData("0x" + "11" * 32, "0x" + "22" * 32, 1, 2, 60)
    token = "0x" + "00" * 19 + "01"
    for sig in ("0x", "0x12", "zz", "0x" + "ff" * 3):
        assert recover_reading_signer(31337, token, d, sig) is None


def test_reading_id_derivation_is_the_documented_formula() -> None:
    meter = "0x" + "ab" * 32
    expected = (
        "0x"
        + keccak(
            encode(
                ["bytes32", "uint256", "uint256"], [bytes.fromhex("ab" * 32), 1_800_000_000, 900]
            )
        ).hex()
    )
    assert derive_reading_id(meter, 1_800_000_000, 900) == expected


# ------------------------------------------------------------------- plausibility / helpers
def test_plausibility_mirrors_solidity_examples() -> None:
    p = PlausibilityParams(safety_bps=12_000, min_interval_seconds=60, max_interval_seconds=86_400)
    assert p.max_energy_wh(5_000, 900) == 1_500  # MeterRegistry.t.sol::test_maxEnergyWh_formula
    assert p.max_energy_wh(5_000, 60) == 100
    assert p.max_energy_wh(5_000, 3_600) == 6_000
    assert p.max_energy_wh(1, 1) == 0  # integer floor, like the contract


def test_meter_id_derivation_and_coordinate_scaling() -> None:
    a = derive_meter_id("0x" + "11" * 20, "0x" + "22" * 20, b"\x00" * 32)
    assert a == derive_meter_id("0x" + "11" * 20, "0x" + "22" * 20, b"\x00" * 32)
    assert a != derive_meter_id("0x" + "11" * 20, "0x" + "22" * 20, b"\x01" * 32)
    assert len(a) == 66
    assert to_e6(13.0827) == 13_082_700 and to_e6(-33.8688) == -33_868_800
    assert to_e6(80.2707) == 80_270_700 and to_e6(0.1 + 0.2) == 300_000  # no float drift
