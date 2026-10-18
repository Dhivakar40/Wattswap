"""The device boundary.

Everything the rest of the system needs from a meter is the `MeterDevice` protocol. The only
implementation shipped is `SimulatedMeter`, which produces SIMULATED numbers. Real hardware
would implement the same protocol (and sign with its own key); nothing downstream changes.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from eth_account import Account

from meter_sim.model import Generation, GenerationModel
from meter_sim.signing import SignedReading, sign_reading

log = logging.getLogger("meter_sim.device")


@dataclass(frozen=True)
class MeterSpec:
    """One simulated meter: identity (from the API registration), location and capacity."""

    meter_id: str
    private_key: str  # the DEVICE key; its address is what the owner registered
    lat: float
    lon: float
    capacity_w: int
    label: str = "simulated-meter"

    @property
    def device_address(self) -> str:
        return str(Account.from_key(self.private_key).address)


@dataclass(frozen=True)
class Measurement:
    reading: SignedReading
    generation: Generation


class MeterDevice(Protocol):
    meter_id: str

    async def read_interval(self, start_ts: int, seconds: int) -> Measurement | None:
        """Signed reading for a *completed* interval, or None if there is nothing to report."""
        ...


class SimulatedMeter:
    """SIMULATED solar meter. Not real hardware; the numbers come from a labeled model."""

    is_simulated = True

    def __init__(
        self, spec: MeterSpec, model: GenerationModel, chain_id: int, token_address: str
    ) -> None:
        self._spec = spec
        self._model = model
        self._chain_id = chain_id
        self._token = token_address
        self.meter_id = spec.meter_id

    async def read_interval(self, start_ts: int, seconds: int) -> Measurement | None:
        gen = await self._model.energy_wh(
            self._spec.lat, self._spec.lon, self._spec.capacity_w, start_ts, seconds
        )
        if gen.wh <= 0:
            return None  # night / no generation: nothing to tokenize (the contract rejects 0 Wh)
        reading = sign_reading(
            self._spec.private_key,
            self._chain_id,
            self._token,
            self._spec.meter_id,
            gen.wh,
            start_ts,
            seconds,
        )
        return Measurement(reading=reading, generation=gen)
